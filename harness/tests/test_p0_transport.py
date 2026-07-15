from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData

from fusion_mcp_adapter.adapter import FusionMcpAdapter
from fusion_mcp_adapter.errors import ErrorCode
from fusion_mcp_adapter.manifest_store import ManifestStore
from fusion_mcp_adapter.policy import ToolPolicy
from fusion_mcp_adapter.real_client import RealMcpClient
from fusion_mcp_adapter.semantics import CallSemantics, ConnectionState
from fusion_mcp_adapter.semantics import McpCallOptions
from fusion_mcp_adapter.tool_result import ToolDefinition, ToolManifest, ToolResult
from telemetry.trace import JsonlTraceLogger


@pytest.mark.asyncio
async def test_adapter_injects_executor_guard_once_and_traces_source_and_wire_hashes(tmp_path) -> None:
    class CaptureClient:
        def __init__(self) -> None:
            self.arguments: dict[str, Any] | None = None

        async def call_tool(self, name, arguments, *, options=None):
            del name, options
            self.arguments = arguments
            return ToolResult.success(message="ok")

    source = """def run(_context: str):
    return 1
"""
    client = CaptureClient()
    trace_path = tmp_path / "adapter-trace.jsonl"
    adapter = FusionMcpAdapter(
        client=client,
        policy=ToolPolicy({"fusion_mcp_execute"}),
        trace_logger=JsonlTraceLogger(trace_path),
    )

    result = await adapter.call(
        "fusion_mcp_execute",
        {"featureType": "script", "object": {"script": source}},
    )

    assert result.ok is True
    assert client.arguments is not None
    transmitted = client.arguments["object"]["script"]
    assert transmitted != source
    assert transmitted.count("import sys as _fusion_agent_runtime_sys") == 1
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert trace["executor_original_bytes"] == len(source.encode("utf-8"))
    assert trace["executor_transmitted_bytes"] == len(transmitted.encode("utf-8"))
    assert trace["executor_original_sha256"] != trace["executor_transmitted_sha256"]
    assert trace["executor_preamble_version"] == 1
    assert source not in trace_path.read_text(encoding="utf-8")

    client.arguments = None
    second = await adapter.call(
        "fusion_mcp_execute",
        {"featureType": "script", "object": {"script": transmitted}},
    )
    assert second.ok is True
    assert client.arguments["object"]["script"] == transmitted

    client.arguments = None
    malformed = await adapter.call(
        "fusion_mcp_execute",
        {"featureType": "script", "object": {"script": "print('no run')"}},
    )
    assert malformed.ok is False
    assert malformed.error_code == ErrorCode.TOOL_SCHEMA_VALIDATION_ERROR
    assert client.arguments is None


def test_native_negative_acknowledgement_is_a_functional_error() -> None:
    result = ToolResult.from_mcp(
        {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "success": False,
                            "error": "script binding failed",
                        }
                    ),
                }
            ],
            "isError": False,
        }
    )

    assert result.ok is False
    assert result.is_error is True
    assert result.error_code == ErrorCode.FUSION_OPERATION_FAILED
    assert result.error_message == "script binding failed"


def test_nested_native_negative_acknowledgement_is_a_functional_error() -> None:
    result = ToolResult.from_mcp(
        {
            "structuredContent": {
                "message": json.dumps(
                    {
                        "success": False,
                        "error": "nested script failure",
                    }
                )
            },
            "isError": False,
        }
    )

    assert result.ok is False
    assert result.is_error is True
    assert result.error_code == ErrorCode.FUSION_OPERATION_FAILED
    assert result.error_message == "nested script failure"


class _Model:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def model_dump(self, **_: Any) -> dict[str, Any]:
        return self.payload


@dataclass
class _FakeMcp:
    manifests: list[list[dict[str, Any]]] = field(
        default_factory=lambda: [[{"name": "fusion_mcp_read"}, {"name": "fusion_mcp_execute"}]]
    )
    initialize_count: int = 0
    list_count: int = 0
    call_count: int = 0
    ping_count: int = 0
    session_count: int = 0
    active_calls: int = 0
    max_active_calls: int = 0
    fail_reads: int = 0
    fail_mutations: int = 0
    timeout_mcp_reads: int = 0
    timeout_mcp_mutations: int = 0
    functional_mcp_errors: int = 0
    functional_read_errors: int = 0
    block_calls: bool = False
    call_started: asyncio.Event = field(default_factory=asyncio.Event)
    release_calls: asyncio.Event = field(default_factory=asyncio.Event)

    def session_factory(self, *_: Any, **__: Any) -> "_FakeSession":
        connection_index = self.session_count
        self.session_count += 1
        return _FakeSession(self, connection_index)


class _FakeTransport:
    def __init__(self, state: _FakeMcp) -> None:
        self.state = state

    async def __aenter__(self) -> tuple[object, object, Any]:
        return object(), object(), lambda: "must-never-appear-in-traces"

    async def __aexit__(self, *_: Any) -> None:
        return None


class _FakeSession:
    def __init__(self, state: _FakeMcp, connection_index: int) -> None:
        self.state = state
        self.connection_index = connection_index

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def initialize(self) -> _Model:
        self.state.initialize_count += 1
        return _Model(
            {
                "protocolVersion": "2025-06-18",
                "serverInfo": {"name": "fake-fusion", "version": "1.0.0"},
            }
        )

    async def list_tools(self) -> _Model:
        self.state.list_count += 1
        index = min(self.connection_index, len(self.state.manifests) - 1)
        return _Model({"tools": self.state.manifests[index]})

    async def send_ping(self) -> _Model:
        self.state.ping_count += 1
        return _Model({})

    async def call_tool(self, name: str, arguments: dict[str, Any], **_: Any) -> _Model:
        del arguments
        self.state.call_count += 1
        self.state.active_calls += 1
        self.state.max_active_calls = max(self.state.max_active_calls, self.state.active_calls)
        self.state.call_started.set()
        try:
            if self.state.block_calls:
                await self.state.release_calls.wait()
            if name == "fusion_mcp_read" and self.state.fail_reads:
                self.state.fail_reads -= 1
                raise ConnectionError("injected read transport failure")
            if name == "fusion_mcp_read" and self.state.timeout_mcp_reads:
                self.state.timeout_mcp_reads -= 1
                raise McpError(ErrorData(code=408, message="Timed out while waiting for response"))
            if name == "fusion_mcp_read" and self.state.functional_mcp_errors:
                self.state.functional_mcp_errors -= 1
                raise McpError(ErrorData(code=-32602, message="invalid functional arguments"))
            if name == "fusion_mcp_read" and self.state.functional_read_errors:
                self.state.functional_read_errors -= 1
                return _Model(
                    {
                        "content": [{"type": "text", "text": "functional failure"}],
                        "isError": True,
                    }
                )
            if name == "fusion_mcp_execute" and self.state.fail_mutations:
                self.state.fail_mutations -= 1
                raise ConnectionError("injected mutation transport failure")
            if name == "fusion_mcp_execute" and self.state.timeout_mcp_mutations:
                self.state.timeout_mcp_mutations -= 1
                raise McpError(ErrorData(code=408, message="Timed out while waiting for response"))
            await asyncio.sleep(0.005)
            return _Model(
                {
                    "content": [{"type": "text", "text": '{"legacy": true}'}],
                    "structuredContent": {"name": name, "ok": True},
                    "_meta": {"source": "fake"},
                    "isError": False,
                }
            )
        finally:
            self.state.active_calls -= 1


def _client(state: _FakeMcp, **kwargs: Any) -> RealMcpClient:
    kwargs.setdefault("transport_mode", "persistent")
    return RealMcpClient(
        endpoint="http://127.0.0.1:27182/mcp",
        transport_factory=lambda *_args, **_kwargs: _FakeTransport(state),
        session_factory=state.session_factory,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_persistent_session_initializes_and_lists_once_for_twenty_reads() -> None:
    state = _FakeMcp()
    client = _client(state)

    results = [await client.call_tool("fusion_mcp_read", {"queryType": "document"}) for _ in range(20)]

    assert all(result.ok for result in results)
    assert state.initialize_count == 1
    assert state.list_count == 1
    assert state.call_count == 20
    assert client.diagnostics["connection_generation"] == 1
    assert client.diagnostics["reconnect_count"] == 0
    await client.aclose()
    assert client.state == ConnectionState.CLOSED


@pytest.mark.asyncio
async def test_operations_are_serialized() -> None:
    state = _FakeMcp()
    client = _client(state)

    await asyncio.gather(
        *(client.call_tool("fusion_mcp_read", {"queryType": "document"}) for _ in range(8))
    )

    assert state.max_active_calls == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_read_reconnects_once_but_mutation_is_never_replayed() -> None:
    read_state = _FakeMcp(fail_reads=1)
    read_client = _client(read_state)
    read_result = await read_client.call_tool("fusion_mcp_read", {})
    assert read_result.ok
    assert read_state.call_count == 2
    assert read_state.initialize_count == 2
    assert read_client.diagnostics["retry_count"] == 1
    await read_client.aclose()

    mutation_state = _FakeMcp(fail_mutations=1)
    mutation_client = _client(mutation_state)
    mutation_result = await mutation_client.call_tool("fusion_mcp_execute", {"featureType": "script"})
    assert not mutation_result.ok
    assert mutation_result.error_code == ErrorCode.MUTATION_OUTCOME_UNKNOWN
    assert mutation_state.call_count == 1
    assert mutation_state.ping_count == 1
    outcome = mutation_client.diagnostics["last_call_outcome"]
    assert outcome["dispatched"] is True
    assert outcome["may_have_applied"] is True
    assert outcome["post_dispatch_replay_suppressed"] is True
    assert outcome["mutation_outcome"] == "unknown"
    assert mutation_result.meta["fusion_agent_transport"] == outcome
    await mutation_client.aclose()


@pytest.mark.asyncio
async def test_sdk_mcp_timeout_retries_read_but_never_replays_mutation() -> None:
    read_state = _FakeMcp(timeout_mcp_reads=1)
    read_client = _client(read_state)
    read_result = await read_client.call_tool("fusion_mcp_read", {})

    assert read_result.ok is True
    assert read_state.call_count == 2
    assert read_state.initialize_count == 2
    assert read_client.diagnostics["retry_count"] == 1
    await read_client.aclose()

    mutation_state = _FakeMcp(timeout_mcp_mutations=1)
    mutation_client = _client(mutation_state)
    mutation_result = await mutation_client.call_tool("fusion_mcp_execute", {"featureType": "script"})

    assert mutation_result.error_code == ErrorCode.MUTATION_OUTCOME_UNKNOWN
    assert mutation_state.call_count == 1
    assert mutation_client.state == ConnectionState.BROKEN
    await mutation_client.aclose()


@pytest.mark.asyncio
async def test_functional_mcp_error_is_not_retried_or_treated_as_transport_loss() -> None:
    state = _FakeMcp(functional_mcp_errors=1)
    client = _client(state)

    result = await client.call_tool("fusion_mcp_read", {})

    assert result.error_code == ErrorCode.MCP_PROTOCOL_ERROR
    assert state.call_count == 1
    assert client.state == ConnectionState.READY
    assert client.diagnostics["retry_count"] == 0
    await client.aclose()


@pytest.mark.asyncio
async def test_close_cancels_initial_connection_and_client_cannot_resurrect() -> None:
    state = _FakeMcp()
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingTransport(_FakeTransport):
        async def __aenter__(self) -> tuple[object, object, Any]:
            entered.set()
            await release.wait()
            return await super().__aenter__()

    client = RealMcpClient(
        endpoint="http://127.0.0.1:27182/mcp",
        transport_factory=lambda *_args, **_kwargs: BlockingTransport(state),
        session_factory=state.session_factory,
    )
    connecting = asyncio.create_task(client.start())
    await asyncio.wait_for(entered.wait(), timeout=0.5)

    await client.aclose(timeout_seconds=0.5)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await connecting
    await asyncio.sleep(0)

    assert client.state == ConnectionState.CLOSED


def test_mutating_default_tools_cannot_be_downgraded_to_retryable_reads() -> None:
    client = RealMcpClient(endpoint="http://127.0.0.1:27182/mcp")

    for tool_name in ("fusion_mcp_execute", "fusion_mcp_update", "unknown_native_tool"):
        resolved = client._resolve_options(
            tool_name,
            McpCallOptions.for_read(timeout_seconds=1, operation_id=tool_name),
        )
        assert resolved.semantics == CallSemantics.MUTATING


@pytest.mark.asyncio
async def test_model_execute_cannot_downgrade_transport_semantics_or_timeout() -> None:
    state = _FakeMcp(fail_mutations=1)
    client = _client(state)

    result = await client.call_tool(
        "fusion_mcp_execute",
        {"featureType": "script", "object": {"script": "model supplied"}},
        options=McpCallOptions(CallSemantics.READ_ONLY, timeout_seconds=None),
    )

    assert result.error_code == ErrorCode.MUTATION_OUTCOME_UNKNOWN
    assert state.call_count == 1
    assert state.ping_count == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_cancelled_mutation_returns_unknown_without_replay() -> None:
    state = _FakeMcp(block_calls=True)
    client = _client(state)
    task = asyncio.create_task(client.call_tool("fusion_mcp_execute", {"featureType": "script"}))
    await asyncio.wait_for(state.call_started.wait(), timeout=0.5)

    task.cancel()
    result = await asyncio.wait_for(task, timeout=1.0)

    assert result.error_code == ErrorCode.MUTATION_OUTCOME_UNKNOWN
    assert state.call_count == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_cancelled_while_queued_is_not_dispatched_and_is_traced(tmp_path: Path) -> None:
    state = _FakeMcp(block_calls=True)
    trace_path = tmp_path / "queued-cancel.jsonl"
    client = _client(state, trace_logger=JsonlTraceLogger(trace_path))
    active = asyncio.create_task(client.call_tool("fusion_mcp_read", {}))
    await asyncio.wait_for(state.call_started.wait(), timeout=0.5)
    queued = asyncio.create_task(client.call_tool("fusion_mcp_execute", {"featureType": "script"}))
    await asyncio.sleep(0.01)

    queued.cancel()
    cancelled = await asyncio.wait_for(queued, timeout=1.0)
    state.release_calls.set()
    await active

    assert cancelled.error_code == ErrorCode.CALL_CANCELLED
    assert state.call_count == 1
    events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    cancellation = next(event for event in events if event.get("error_code") == ErrorCode.CALL_CANCELLED)
    assert cancellation["attempt_count"] == 0
    assert cancellation["outcome"] == ErrorCode.CALL_CANCELLED
    assert cancellation["dispatched"] is False
    assert cancellation["mutation_outcome"] == "known"
    await client.aclose()


@pytest.mark.asyncio
async def test_functional_read_error_is_not_retried() -> None:
    state = _FakeMcp(functional_read_errors=1)
    client = _client(state)

    result = await client.call_tool("fusion_mcp_read", {})

    assert result.is_error is True
    assert state.call_count == 1
    assert client.diagnostics["retry_count"] == 0
    await client.aclose()


@pytest.mark.asyncio
async def test_timeout_mutation_returns_quickly_and_next_call_reconnects_without_replay() -> None:
    state = _FakeMcp(block_calls=True)
    client = _client(state)
    started = time.perf_counter()

    unknown = await client.call_tool(
        "fusion_mcp_execute",
        {"featureType": "script"},
        options=McpCallOptions.for_mutation(timeout_seconds=0.05),
    )
    elapsed = time.perf_counter() - started
    state.block_calls = False
    state.release_calls.set()
    cooling_down = await client.call_tool("fusion_mcp_read", {})
    client._cooldown_until = 0.0
    recovered = await client.call_tool("fusion_mcp_read", {})

    assert unknown.error_code == ErrorCode.MUTATION_OUTCOME_UNKNOWN
    assert elapsed < 2.0
    assert cooling_down.error_code == ErrorCode.CONNECTION_UNAVAILABLE
    assert cooling_down.data["retry_after_seconds"] > 0
    assert recovered.ok is True
    assert state.call_count == 2
    assert state.initialize_count == 2
    await client.aclose()


@pytest.mark.asyncio
async def test_legacy_mode_never_retries_mutation() -> None:
    state = _FakeMcp(fail_mutations=1)
    client = _client(state, transport_mode="legacy")

    result = await client.call_tool("fusion_mcp_execute", {"featureType": "script"})

    assert result.error_code == ErrorCode.MUTATION_OUTCOME_UNKNOWN
    assert state.call_count == 1
    assert state.initialize_count == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_command_mode_remains_one_shot_and_never_retries_mutation() -> None:
    client = RealMcpClient(command="fake-fusion-mcp")
    calls = 0

    async def fake_command(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"error": "injected command transport failure"}

    client._command_jsonrpc_async = fake_command  # type: ignore[method-assign]
    result = await client.call_tool("fusion_mcp_execute", {"featureType": "script"})

    assert result.error_code == ErrorCode.MUTATION_OUTCOME_UNKNOWN
    assert calls == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_command_timeout_terminates_process_and_returns_within_two_seconds() -> None:
    import sys

    command = f'"{sys.executable}" -c "import time;time.sleep(30)"'
    client = RealMcpClient(command=command)
    started = time.perf_counter()

    result = await client.call_tool(
        "fusion_mcp_execute",
        {"featureType": "script"},
        options=McpCallOptions.for_mutation(timeout_seconds=0.05),
    )

    assert result.error_code == ErrorCode.MUTATION_OUTCOME_UNKNOWN
    assert time.perf_counter() - started < 2.0
    await client.aclose()


@pytest.mark.asyncio
async def test_manifest_drift_blocks_retry_until_explicit_rediscovery() -> None:
    state = _FakeMcp(
        manifests=[
            [{"name": "fusion_mcp_read", "description": "v1"}],
            [{"name": "fusion_mcp_read", "description": "v2"}],
        ],
        fail_reads=1,
    )
    client = _client(state)

    blocked = await client.call_tool("fusion_mcp_read", {})
    assert blocked.error_code == ErrorCode.MANIFEST_DRIFT
    assert state.call_count == 1
    assert client.diagnostics["manifest_drift"] is True

    manifest = await client.list_tools()
    assert manifest.tools[0].description == "v2"
    assert client.diagnostics["manifest_drift"] is False
    verified = await client.call_tool("fusion_mcp_read", {})
    assert verified.ok
    await client.aclose()


def test_manifest_v2_migrates_legacy_and_saves_only_on_fingerprint_change(tmp_path: Path) -> None:
    legacy = {
        "source": "fusion_real",
        "tools": [{"name": "fusion_mcp_read", "input_schema": {"type": "object"}}],
    }
    (tmp_path / "fusion_mcp_tools_latest.json").write_text(json.dumps(legacy), encoding="utf-8")
    store = ManifestStore(tmp_path)

    loaded = store.load_latest("real")
    assert loaded is not None
    assert loaded.schema_version == 2
    assert len(loaded.fingerprint) == 64
    assert (tmp_path / "fusion_mcp_tools_latest_real.json").exists()
    assert store.save_if_changed(loaded) is None

    loaded.tools[0].description = "changed"
    created = store.save_if_changed(loaded)
    assert created is not None and created.exists()
    assert store.save_if_changed(loaded) is None
    assert len(list(tmp_path.glob("fusion_mcp_tools_real_*.json"))) == 1


def test_manifest_fingerprint_is_order_independent_and_schema_sensitive() -> None:
    first = ToolManifest(
        source="real",
        tools=[
            ToolDefinition(name="b", description="B", input_schema={"type": "object"}),
            ToolDefinition(name="a", description="A", output_schema={"type": "string"}),
        ],
    )
    reordered = ToolManifest(
        source="real",
        tools=[
            ToolDefinition(name="a", description="A", output_schema={"type": "string"}),
            ToolDefinition(name="b", description="B", input_schema={"type": "object"}),
        ],
    )
    changed = reordered.model_copy(deep=True)
    changed.tools[0].description = "changed"
    changed.refresh_fingerprint()

    assert first.fingerprint == reordered.fingerprint
    assert changed.fingerprint != first.fingerprint


@pytest.mark.asyncio
async def test_manifest_persistence_failure_does_not_drop_live_connection(tmp_path: Path) -> None:
    class FailingStore(ManifestStore):
        def save_if_changed(self, manifest):
            del manifest
            raise PermissionError("OneDrive lock")

    state = _FakeMcp()
    client = _client(state, manifest_store=FailingStore(tmp_path))

    result = await client.call_tool("fusion_mcp_read", {})

    assert result.ok is True
    assert "OneDrive lock" in client.diagnostics["manifest_persistence_error"]
    assert client.state == ConnectionState.READY
    await client.aclose()


def test_tool_result_channels_and_recursive_trace_redaction(tmp_path: Path) -> None:
    result = ToolResult.from_mcp(
        {
            "content": [{"type": "image", "data": "raw-image"}],
            "structuredContent": {"value": 42},
            "_meta": {"kind": "native"},
        }
    )
    assert result.data == {"value": 42}
    assert result.structured_content == {"value": 42}
    assert result.content[0]["type"] == "image"
    assert result.meta == {"kind": "native"}

    path = tmp_path / "trace.jsonl"
    logger = JsonlTraceLogger(path)
    logger.log_tool_call(
        session_id="safe-local-session",
        facade_tool="fusion_agent_fast_execute",
        native_tool="fusion_mcp_execute",
        arguments={
            "script": "TOP SECRET PYTHON",
            "nested": {"api_token": "TOKEN VALUE", "safe": 7},
            "content": [{"text": "PRIVATE CONTENT"}],
        },
        result_status="ok",
        duration_ms=1,
        connection_generation=2,
        fingerprint="fingerprint",
        semantics="read_only",
        attempts=1,
        reconnect=False,
        queue_ms=3,
        connection_ms=4,
        timeout_seconds=120,
        operation_id="operation",
        outcome="ok",
        transport_mode="persistent",
    )
    raw = path.read_text(encoding="utf-8")
    assert "TOP SECRET PYTHON" not in raw
    assert "TOKEN VALUE" not in raw
    assert "PRIVATE CONTENT" not in raw
    payload = json.loads(raw)
    assert payload["arguments_redacted"]["script"]["redacted"] is True
    assert payload["arguments_redacted"]["nested"]["api_token"]["type"] == "str"
    assert payload["transport_mode"] == "persistent"
    assert payload["connection_generation"] == 2
    assert payload["manifest_fingerprint"] == "fingerprint"
    assert payload["call_semantics"] == "read_only"
    assert payload["attempt_count"] == 1
    assert payload["reconnected"] is False
    assert payload["queue_wait_ms"] == 3
    assert payload["connection_ms"] == 4
    assert payload["call_ms"] == 1
