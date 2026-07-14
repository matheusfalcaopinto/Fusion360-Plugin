from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

import fusion_mcp_adapter.post_only_transport as post_only_module
import fusion_mcp_adapter.real_client as real_client_module
from fusion_mcp_adapter.errors import ErrorCode
from fusion_mcp_adapter.post_only_transport import post_only_streamablehttp_client
from fusion_mcp_adapter.real_client import RealMcpClient
from fusion_mcp_adapter.semantics import CallSemantics, McpCallOptions, ReplayPolicy


class _Model:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def model_dump(self, **_: Any) -> dict[str, Any]:
        return self.payload


@dataclass
class _State:
    initialize_count: int = 0
    list_count: int = 0
    call_count: int = 0
    ping_count: int = 0
    fail_next_read: bool = False
    fail_mutation: bool = False
    block_calls: bool = False
    fail_transport_enters: int = 0
    task_ids: list[int] = field(default_factory=list)

    def record_task(self) -> None:
        task = asyncio.current_task()
        assert task is not None
        self.task_ids.append(id(task))

    def session_factory(self, *_: Any, **__: Any) -> "_Session":
        return _Session(self)


class _Transport:
    def __init__(self, state: _State) -> None:
        self.state = state

    async def __aenter__(self) -> tuple[object, object, Any]:
        self.state.record_task()
        if self.state.fail_transport_enters:
            self.state.fail_transport_enters -= 1
            raise ConnectionError("injected pre-dispatch connection failure")
        return object(), object(), lambda: "redacted-session-id"

    async def __aexit__(self, *_: Any) -> None:
        self.state.record_task()


class _Session:
    def __init__(self, state: _State) -> None:
        self.state = state

    async def __aenter__(self) -> "_Session":
        self.state.record_task()
        return self

    async def __aexit__(self, *_: Any) -> None:
        self.state.record_task()

    async def initialize(self) -> _Model:
        self.state.record_task()
        self.state.initialize_count += 1
        return _Model(
            {
                "protocolVersion": "2025-06-18",
                "serverInfo": {"name": "fake-fusion", "version": "1"},
            }
        )

    async def list_tools(self) -> _Model:
        self.state.record_task()
        self.state.list_count += 1
        return _Model(
            {
                "tools": [
                    {"name": "fusion_mcp_read"},
                    {"name": "fusion_mcp_execute"},
                ]
            }
        )

    async def send_ping(self) -> _Model:
        self.state.record_task()
        self.state.ping_count += 1
        return _Model({})

    async def call_tool(self, name: str, arguments: dict[str, Any], **_: Any) -> _Model:
        del arguments
        self.state.record_task()
        self.state.call_count += 1
        if self.state.block_calls:
            await asyncio.sleep(60)
        if name == "fusion_mcp_read" and self.state.fail_next_read:
            self.state.fail_next_read = False
            raise ConnectionError("injected canary failure")
        if name == "fusion_mcp_execute" and self.state.fail_mutation:
            self.state.fail_mutation = False
            raise ConnectionError("injected mutation failure")
        return _Model(
            {
                "content": [{"type": "text", "text": '{"ok": true}'}],
                "structuredContent": {"ok": True},
                "isError": False,
            }
        )


def _client(state: _State, *, mode: str | None = None) -> RealMcpClient:
    kwargs: dict[str, Any] = {}
    if mode is not None:
        kwargs["transport_mode"] = mode
    return RealMcpClient(
        endpoint="http://127.0.0.1:27182/mcp",
        transport_factory=lambda *_args, **_kwargs: _Transport(state),
        session_factory=state.session_factory,
        **kwargs,
    )


def test_replay_policy_is_backward_compatible_and_effect_is_independent() -> None:
    positional = McpCallOptions(CallSemantics.READ_ONLY, 1.0, "operation", True)

    assert positional.trusted_internal_read is True
    assert positional.replay_policy == ReplayPolicy.BEFORE_DISPATCH_ONLY
    assert McpCallOptions.for_read().replay_policy == ReplayPolicy.TRANSPORT_RETRY
    assert McpCallOptions.for_mutation().replay_policy == ReplayPolicy.BEFORE_DISPATCH_ONLY
    assert (
        McpCallOptions.for_trusted_internal_read().replay_policy
        == ReplayPolicy.BEFORE_DISPATCH_ONLY
    )


@pytest.mark.asyncio
async def test_default_is_legacy_and_readiness_manifest_is_cached() -> None:
    state = _State()
    client = _client(state)

    await client.ensure_ready()
    first = await client.list_tools()
    second = await client.list_tools()

    assert client.diagnostics["requested_transport_mode"] == "legacy"
    assert client.diagnostics["effective_transport_mode"] == "legacy"
    assert state.initialize_count == 1
    assert state.list_count == 1
    assert first.fingerprint == second.fingerprint
    await client.aclose()


@pytest.mark.asyncio
async def test_persistent_context_and_session_are_owned_by_one_worker_task() -> None:
    state = _State()
    caller_id = id(asyncio.current_task())
    client = _client(state, mode="persistent")

    result = await client.call_tool("fusion_mcp_read", {"queryType": "document"})
    owner_name = client.diagnostics["worker_owner_task"]
    await client.aclose()

    assert result.ok is True
    assert owner_name == "fusion-mcp-transport-owner"
    assert state.task_ids
    assert len(set(state.task_ids)) == 1
    assert state.task_ids[0] != caller_id


@pytest.mark.asyncio
async def test_trusted_internal_read_timeout_is_never_replayed_and_starts_cooldown() -> None:
    state = _State(block_calls=True)
    client = _client(state, mode="persistent")

    result = await client.call_tool(
        "fusion_mcp_execute",
        {"featureType": "script", "object": {"script": "audited"}},
        options=McpCallOptions.for_trusted_internal_read(timeout_seconds=0.02),
    )

    assert result.error_code == ErrorCode.READ_TIMEOUT_MAY_STILL_BE_RUNNING
    assert result.data["dispatched"] is True
    assert result.data["may_still_be_running"] is True
    assert result.data["retry_suppressed"] is True
    assert result.data["retry_after_seconds"] > 0
    assert state.call_count == 1
    assert client.diagnostics["cooldown_remaining_seconds"] > 0
    await client.aclose()


@pytest.mark.asyncio
async def test_trusted_read_allows_two_predispatch_reconnects_then_dispatches_once() -> None:
    state = _State(fail_transport_enters=2)
    client = _client(state, mode="persistent")

    result = await client.call_tool(
        "fusion_mcp_execute",
        {"featureType": "script", "object": {"script": "audited"}},
        options=McpCallOptions.for_trusted_internal_read(timeout_seconds=1.0),
    )

    assert result.ok is True
    assert state.call_count == 1
    assert client.diagnostics["retry_count"] == 2
    assert client.diagnostics["connection_generation"] == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_auto_falls_back_once_before_user_dispatch() -> None:
    state = _State(fail_next_read=True)
    client = _client(state, mode="auto")

    result = await client.call_tool("fusion_mcp_read", {"queryType": "document"})
    diagnostics = client.diagnostics

    assert result.ok is True
    assert diagnostics["effective_transport_mode"] == "legacy"
    assert diagnostics["auto_canary_count"] == 1
    assert diagnostics["auto_canary_completed"] is True
    assert "injected canary failure" in diagnostics["fallback_reason"]
    assert state.call_count == 2  # failed canary + one user call, never a duplicate user call
    await client.aclose()


@pytest.mark.asyncio
async def test_auto_canary_timeout_falls_back_but_cooldown_blocks_user_dispatch(monkeypatch) -> None:
    monkeypatch.setattr(real_client_module, "_AUTO_CANARY_TIMEOUT_SECONDS", 0.02)
    state = _State(block_calls=True)
    client = _client(state, mode="auto")

    result = await client.call_tool("fusion_mcp_read", {"queryType": "document"})

    assert result.error_code == ErrorCode.CONNECTION_UNAVAILABLE
    assert result.data["retry_after_seconds"] > 0
    assert state.call_count == 1  # timed-out canary only; user call stayed undispatched
    assert client.diagnostics["effective_transport_mode"] == "legacy"
    await client.aclose()


@pytest.mark.asyncio
async def test_auto_never_falls_back_after_mutation_dispatch() -> None:
    state = _State(fail_mutation=True)
    client = _client(state, mode="auto")

    result = await client.call_tool(
        "fusion_mcp_execute",
        {"featureType": "script", "object": {"script": "model"}},
    )

    assert result.error_code == ErrorCode.MUTATION_OUTCOME_UNKNOWN
    assert state.call_count == 2  # successful canary + exactly one mutation
    assert client.diagnostics["mutation_dispatched"] is True
    assert client.diagnostics["effective_transport_mode"] == "persistent_post_only"
    assert client.diagnostics["fallback_reason"] is None
    await client.aclose()


@pytest.mark.asyncio
async def test_post_only_transport_suppresses_initialized_get_callback(monkeypatch) -> None:
    callback_invoked = asyncio.Event()
    get_calls = 0

    class FakeHttpClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

    class FakeTransport:
        session_id = None

        def __init__(self, url: str) -> None:
            del url

        async def handle_get_stream(self, *_: Any) -> None:
            nonlocal get_calls
            get_calls += 1

        async def post_writer(
            self,
            _client: Any,
            _write_reader: Any,
            _read_writer: Any,
            _write_stream: Any,
            start_get_stream: Any,
            _task_group: Any,
        ) -> None:
            start_get_stream()
            callback_invoked.set()
            await asyncio.Event().wait()

        async def terminate_session(self, _client: Any) -> None:
            return None

        def get_session_id(self) -> None:
            return None

    monkeypatch.setattr(post_only_module, "StreamableHTTPTransport", FakeTransport)

    def client_factory(**_: Any) -> FakeHttpClient:
        return FakeHttpClient()

    async with post_only_streamablehttp_client(
        "http://127.0.0.1:27182/mcp",
        httpx_client_factory=client_factory,
    ):
        await asyncio.wait_for(callback_invoked.wait(), timeout=0.5)

    assert get_calls == 0
