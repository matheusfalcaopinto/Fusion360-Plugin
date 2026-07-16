from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import mcp.types as types
import pytest
from mcp.shared.exceptions import McpError

from agent_core.capability_executor import CapabilityExecutionResult, CapabilityExecutor
from agent_core.fast_path import FastPathResponse
from cad_spec.v2 import CadSpecV2
from fusion_agent_mcp import mcp_surface, server
from fusion_agent_mcp.runtime import RuntimeConfiguration
from fusion_mcp_adapter.stdio_client import StdioMcpClient
from fusion_mcp_adapter.tool_result import ToolResult


SENTINEL = "CAN025_FAKE_TOKEN_NOT_A_SECRET"


class _ManifestStore:
    def latest_status(self) -> dict[str, Any]:
        return {
            "real": {
                "path": rf"C:\private\{SENTINEL}\manifest.json",
                "exists": True,
                "bytes": 17,
                "fingerprint": "a" * 64,
                "error": SENTINEL,
            }
        }


class _Runtime:
    def __init__(self, diagnostics: dict[str, Any]) -> None:
        self.manifest_store = _ManifestStore()
        self._diagnostics = diagnostics
        self.configuration = RuntimeConfiguration.from_environment()

    def diagnostics(self) -> dict[str, Any]:
        return dict(self._diagnostics)


def _analysis_spec() -> CadSpecV2:
    return CadSpecV2.model_validate(
        {
            "cad_spec_version": "2.0",
            "intent": "Measure a component without host I/O",
            "requirements": [
                {
                    "id": "mass_recorded",
                    "description": "Mass is recorded",
                    "assertion_ids": ["mass_range"],
                }
            ],
            "operations": [
                {
                    "id": "measure_mass",
                    "kind": "analysis.physical_properties",
                    "target_refs": ["part"],
                    "output_ref": "mass_report",
                    "requirement_ids": ["mass_recorded"],
                }
            ],
            "assertions": [
                {
                    "id": "mass_range",
                    "kind": "physical_property_range",
                    "target_ref": "mass_report",
                    "expected": {"min_kg": 0.0, "max_kg": 10.0},
                }
            ],
        }
    )


def _serialized(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", by_alias=True)
    return json.dumps(value, sort_keys=True, default=str)


def _keys(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            found.add(str(key))
            found.update(_keys(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_keys(child))
    return found


def test_surface_registry_declares_every_public_kind_and_profile() -> None:
    specs = server.surface_specs()

    assert {spec.kind for spec in specs} == {
        "tool",
        "resource",
        "resource_template",
        "prompt",
    }
    assert len({(spec.kind, spec.name) for spec in specs}) == len(specs)
    assert all(spec.profiles for spec in specs)
    assert all(spec.risk in {"read", "write", "destructive"} for spec in specs)
    assert all(spec.data_class for spec in specs)
    tool_specs = [spec for spec in specs if spec.kind == "tool"]
    assert all(spec.input_schema for spec in tool_specs)
    assert all(spec.output_schema for spec in tool_specs)
    assert all(callable(spec.handler) for spec in tool_specs)
    assert all(callable(spec.projector) for spec in tool_specs)


@pytest.mark.asyncio
async def test_restricted_resource_families_fail_before_lookup_in_normal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def unexpected_memory(_project: str):
        calls.append("memory")
        return [], {}

    async def unexpected_benchmark(_arguments: dict[str, Any]):
        calls.append("benchmark")
        return {"text": SENTINEL}

    monkeypatch.setattr(server, "_gated_project_memory_records", unexpected_memory)
    monkeypatch.setattr(server, "_read_benchmark_report_tool", unexpected_benchmark)

    for uri in (
        "fusion-agent://memory/demo",
        "fusion-agent://benchmarks/run-1/summary",
    ):
        with pytest.raises(mcp_surface.SurfaceProfileError) as caught:
            await server._read_mcp_resource(uri, runtime=object(), profile="normal")
        assert caught.value.code == "SURFACE_NOT_AVAILABLE_IN_PROFILE"

    assert calls == []


@pytest.mark.asyncio
async def test_undeclared_route_in_authorized_family_has_zero_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    remaining = tuple(
        spec
        for spec in mcp_surface._RESOURCE_TEMPLATE_SPECS
        if spec.name != "fusion-agent-session-artifact"
    )
    monkeypatch.setattr(mcp_surface, "_RESOURCE_TEMPLATE_SPECS", remaining)

    async def unexpected_artifact(arguments: dict[str, Any]) -> dict[str, Any]:
        calls.append(arguments)
        return {"path": "safe", "content": "safe"}

    monkeypatch.setattr(server, "_read_session_artifact_tool", unexpected_artifact)

    with pytest.raises(FileNotFoundError):
        await server._read_mcp_resource(
            "fusion-agent://sessions/demo/session-1/artifact/execution.json",
            runtime=object(),
            profile="normal",
        )

    assert calls == []


@pytest.mark.asyncio
async def test_authorized_resource_profiles_preserve_legitimate_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def benchmark(arguments: dict[str, Any]):
        return {
            "run_id": arguments["run_id"],
            "view": arguments["view"],
            "text": "safe",
        }

    monkeypatch.setattr(server, "_read_benchmark_report_tool", benchmark)
    payload = await server._read_mcp_resource(
        "fusion-agent://benchmarks/run-1/summary",
        runtime=object(),
        profile="benchmark",
    )

    assert payload["text"] == "safe"
    normal_templates = {
        item.uriTemplate for item in mcp_surface.resource_templates("normal")
    }
    benchmark_templates = {
        item.uriTemplate for item in mcp_surface.resource_templates("benchmark")
    }
    advanced_templates = {
        item.uriTemplate for item in mcp_surface.resource_templates("advanced")
    }
    assert not any("/memory/" in item for item in normal_templates)
    assert not any("/benchmarks/" in item for item in normal_templates)
    assert any("/benchmarks/" in item for item in benchmark_templates)
    assert any("/memory/" in item for item in advanced_templates)


def test_prompt_advertisement_and_use_share_the_same_profile_policy() -> None:
    normal = {prompt.name for prompt in mcp_surface.prompts("normal")}
    diagnostic = {prompt.name for prompt in mcp_surface.prompts("diagnostic")}
    benchmark = {prompt.name for prompt in mcp_surface.prompts("benchmark")}

    assert normal == {
        "fusion-inspect-plan-verify",
        "fusion-safe-change",
        "fusion-recover-unknown-outcome",
    }
    assert diagnostic == {"fusion-recover-unknown-outcome"}
    assert benchmark == {"fusion-benchmark-case"}
    with pytest.raises(mcp_surface.SurfaceProfileError):
        mcp_surface.render_prompt(
            "fusion-benchmark-case",
            {"case_id": "b01"},
            profile="normal",
        )
    rendered = mcp_surface.render_prompt(
        "fusion-benchmark-case",
        {"case_id": "b01"},
        profile="benchmark",
    )
    assert rendered.messages[0].content.type == "text"


@pytest.mark.asyncio
async def test_server_advertisement_handlers_apply_the_fixed_profile() -> None:
    app = server.build_server(runtime=_Runtime({}), profile="normal")
    template_response = await app.request_handlers[types.ListResourceTemplatesRequest](
        types.ListResourceTemplatesRequest(method="resources/templates/list")
    )
    prompt_response = await app.request_handlers[types.ListPromptsRequest](
        types.ListPromptsRequest(method="prompts/list")
    )

    templates = {item.uriTemplate for item in template_response.root.resourceTemplates}
    prompts = {item.name for item in prompt_response.root.prompts}
    assert not any("/memory/" in item for item in templates)
    assert not any("/benchmarks/" in item for item in templates)
    assert prompts == {
        "fusion-inspect-plan-verify",
        "fusion-safe-change",
        "fusion-recover-unknown-outcome",
    }


@pytest.mark.asyncio
async def test_faust_argv_and_raw_diagnostics_never_reach_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = StdioMcpClient("faust-server", ["--token", SENTINEL])
    client._last_error = f"backend failed with {SENTINEL}"
    direct = client.diagnostics

    assert SENTINEL not in _serialized(direct)
    assert "command" not in direct
    assert "args" not in direct
    assert direct["command_configured"] is True
    assert direct["argument_count"] == 2

    runtime = _Runtime(
        {
            **direct,
            "endpoint": rf"http://127.0.0.1/{SENTINEL}",
            "last_error": SENTINEL,
            "manifest_status": _ManifestStore().latest_status(),
        }
    )
    monkeypatch.setattr(
        server,
        "_doctor",
        lambda *_args, **_kwargs: {
            "project_root": rf"C:\private\{SENTINEL}",
            "fusion_mcp_endpoint": f"http://127.0.0.1/{SENTINEL}",
            "fusion_mcp_endpoint_configured": True,
            "fusion_mcp_command_configured": True,
            "cache_plugin_version": "0.4.1",
            "fusion_agent_default_mode": "real",
            "fusion_agent_require_real": True,
            "fusion_agent_allow_dry_run": False,
            "dry_run_policy": "disabled",
        },
    )
    readiness = await server.execute_tool(
        "fusion_agent_readiness_report",
        runtime=runtime,
        profile="normal",
    )

    assert SENTINEL not in _serialized(readiness)
    assert not (
        _keys(readiness)
        & {"command", "args", "argv", "last_error", "error", "path", "endpoint"}
    )
    assert readiness["tool_profile"] == "normal"
    assert readiness["safe_facade_tool_count"] == 12
    assert readiness["mcp_version"]


def test_session_health_projection_removes_paths_and_free_form_errors() -> None:
    projected = server._public_session_health(
        {
            "mode": "real",
            "launcher_ok": True,
            "python_executable": rf"C:\private\{SENTINEL}\python.exe",
            "fusion_mcp_endpoint": f"http://127.0.0.1/{SENTINEL}",
            "manifest_ok": False,
            "manifest_error": SENTINEL,
            "manifest_status": _ManifestStore().latest_status(),
            "mcp_server_ok": False,
            "native_error": SENTINEL,
            "native_tools_attached": False,
            "healthy": False,
            "connection": {"last_error": SENTINEL, "state": "DISCONNECTED"},
        }
    )

    assert SENTINEL not in _serialized(projected)
    assert not (
        _keys(projected)
        & {
            "python_executable",
            "fusion_mcp_endpoint",
            "manifest_error",
            "native_error",
            "last_error",
            "path",
        }
    )
    assert projected["mcp_server_ok"] is False
    assert server._output_envelope_valid(
        "fusion_agent_session_health", {"ok": True, "result": projected}
    )


@pytest.mark.parametrize(
    "envelope",
    [
        {"content": [{"type": "text", "text": SENTINEL}], "isError": True},
        {"structuredContent": {"success": False, "error": SENTINEL}},
        {"structuredContent": {"error": SENTINEL}},
        {"_meta": {"diagnostic": SENTINEL}, "isError": True},
        {"error": SENTINEL, "isError": True},
        {"error": SENTINEL},
    ],
)
def test_downstream_error_normalization_discards_every_raw_channel(
    envelope: dict[str, Any],
) -> None:
    result = ToolResult.from_mcp(envelope)
    serialized = _serialized(result)

    assert result.ok is False
    assert result.error_code == "FUSION_OPERATION_FAILED"
    assert result.error_message == "The downstream Fusion operation failed."
    assert result.public_error is not None
    assert result.public_error.code == "FUSION_OPERATION_FAILED"
    assert result.public_error.correlation_id.startswith("diag-")
    assert SENTINEL not in serialized
    assert result.content == []
    assert result.structured_content is None
    assert result.meta == {}


def test_successful_downstream_result_preserves_supported_channels() -> None:
    result = ToolResult.from_mcp(
        {
            "content": [{"type": "text", "text": "safe"}],
            "structuredContent": {"value": 42},
            "_meta": {"kind": "native"},
        }
    )

    assert result.ok is True
    assert result.data == {"value": 42}
    assert result.content[0]["text"] == "safe"
    assert result.meta == {"kind": "native"}


@pytest.mark.asyncio
async def test_mcp_exception_boundary_returns_public_error_without_raw_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail(_arguments: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError(f"authorization=Bearer {SENTINEL}")

    original = server._readiness_report_tool
    monkeypatch.setattr(server, "_readiness_report_tool", fail)

    # tool_specs captures module globals when called, so temporarily replace the
    # existing handler through a small registry wrapper.
    original_specs = server.tool_specs

    def specs():
        return [
            server.replace(spec, handler=fail)
            if spec.name == "fusion_agent_readiness_report"
            else spec
            for spec in original_specs()
        ]

    monkeypatch.setattr(server, "tool_specs", specs)
    app = server.build_server(runtime=_Runtime({}), profile="normal")
    handler = app.request_handlers[types.CallToolRequest]
    request = types.CallToolRequest(
        method="tools/call",
        params=types.CallToolRequestParams(
            name="fusion_agent_readiness_report",
            arguments={},
        ),
    )
    response = await handler(request)
    payload = response.root.structuredContent

    assert response.root.isError is True
    assert payload["error_code"] == "INTERNAL_ERROR"
    assert (
        payload["error"]["generic_message"] == "The operation could not be completed."
    )
    assert SENTINEL not in _serialized(response.root)
    assert original is not None


def test_runtime_output_schema_validation_fails_closed_without_payload_echo() -> None:
    response = server._as_call_tool_result(
        "fusion_agent_readiness_report",
        FastPathResponse({"unexpected": SENTINEL}),
    )

    assert response.isError is True
    assert response.structuredContent["error_code"] == "OUTPUT_SCHEMA_VIOLATION"
    assert SENTINEL not in _serialized(response)


def test_semantic_safe_change_failure_is_projected_as_public_error() -> None:
    response = server._as_call_tool_result(
        "fusion_agent_safe_change_apply",
        FastPathResponse(
            {
                "status": "execution_failed",
                "error_code": "FUSION_OPERATION_FAILED",
                "error": f"authorization=Bearer {SENTINEL}",
                "dispatched": False,
                "mutation_outcome": "known",
            }
        ),
    )

    assert response.isError is True
    assert response.structuredContent["error_code"] == "FUSION_OPERATION_FAILED"
    assert response.structuredContent["error"]["correlation_id"].startswith("diag-")
    assert SENTINEL not in _serialized(response)


@pytest.mark.asyncio
async def test_probe_downstream_failure_never_exports_raw_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def probe(_endpoint: str | None, **_kwargs: Any) -> dict[str, Any]:
        return {
            "probes": [
                {
                    "endpoint": "http://127.0.0.1:27182/mcp",
                    "tools_list": {
                        "ok": False,
                        "error": f"argv=--token {SENTINEL}",
                    },
                }
            ]
        }

    monkeypatch.setattr(server, "_tools_probe", probe)
    response = await server.execute_tool_response(
        "fusion_agent_probe", {}, profile="diagnostic"
    )
    projected = server._as_call_tool_result("fusion_agent_probe", response)

    assert SENTINEL not in _serialized(projected)
    assert "argv" not in _serialized(projected).lower()


@pytest.mark.asyncio
async def test_capability_failure_discards_exception_and_transport_canaries() -> None:
    class Backend:
        provider = "canary-backend"
        capabilities = {"physical_properties"}

        def preflight_operations(self, _operations: list[Any]) -> None:
            return None

        async def execute_operation(self, _operation: Any) -> dict[str, Any]:
            error = RuntimeError(f"authorization=Bearer {SENTINEL}")
            error.transport = {
                "dispatched": False,
                "path": rf"C:\\private\\{SENTINEL}",
                "token": SENTINEL,
            }
            raise error

    result = await CapabilityExecutor(Backend()).execute(_analysis_spec())

    assert result.success is False
    assert result.error_message == "The downstream Fusion operation failed."
    assert result.public_error is not None
    assert SENTINEL not in _serialized(result)
    assert set(result.transactions[0]["transport"]) <= {
        "dispatched",
        "may_have_applied",
        "post_dispatch_replay_suppressed",
        "mutation_outcome",
        "operation_id",
        "operation_ids",
        "semantics",
    }


@pytest.mark.asyncio
async def test_run_session_artifacts_and_resource_reads_exclude_raw_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(server, "WORKSPACE_ROOT", tmp_path)
    execution = CapabilityExecutionResult(
        success=False,
        provider="canary-backend",
        error_code=f"PRIVATE_{SENTINEL}",
        error_message=f"authorization=Bearer {SENTINEL}",
        transactions=[
            {
                "operation_id": "measure_mass",
                "kind": "analysis.physical_properties",
                "status": "failed",
                "error_message": SENTINEL,
                "transport": {
                    "dispatched": False,
                    "path": rf"C:\\private\\{SENTINEL}",
                    "token": SENTINEL,
                },
            }
        ],
    )
    result = server._record_v2_session(
        _analysis_spec(),
        execution=execution,
        project="canary-project",
        mode="real",
        dry_run=False,
        warnings=[],
        readback_error=f"readback argv {SENTINEL}",
    )
    for artifact in tmp_path.rglob("*"):
        if artifact.is_file():
            assert SENTINEL not in artifact.read_text(encoding="utf-8")
    Path(result["trace_path"]).write_text(
        json.dumps(
            {
                "event": "downstream_failure",
                "error": SENTINEL,
                "path": rf"C:\\private\\{SENTINEL}",
            }
        )
        + "\n"
        + SENTINEL
        + "\n",
        encoding="utf-8",
    )
    resource = await server._read_mcp_resource(
        "fusion-agent://sessions/"
        f"canary-project/{result['session_id']}/artifact/execution.json",
        runtime=object(),
        profile="normal",
    )
    trace_resource = await server._read_mcp_resource(
        f"fusion-agent://traces/canary-project/{result['session_id']}",
        runtime=object(),
        profile="normal",
    )
    trace_artifact = await server._read_mcp_resource(
        "fusion-agent://sessions/"
        f"canary-project/{result['session_id']}/artifact/tool_trace.jsonl",
        runtime=object(),
        profile="normal",
    )

    assert SENTINEL not in _serialized(result)
    assert SENTINEL not in _serialized(resource)
    assert SENTINEL not in _serialized(trace_resource)
    assert SENTINEL not in _serialized(trace_artifact)


@pytest.mark.asyncio
async def test_resource_handler_returns_typed_generic_error_without_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = server.build_server(runtime=_Runtime({}), profile="normal")
    handler = app.request_handlers[types.ReadResourceRequest]
    request = types.ReadResourceRequest(
        method="resources/read",
        params=types.ReadResourceRequestParams(
            uri=f"fusion-agent://traces/{SENTINEL}/missing-session"
        ),
    )

    with pytest.raises(McpError) as caught:
        await handler(request)

    error = caught.value.error
    assert error.data["correlation_id"].startswith("diag-")
    assert error.data["code"] == "RESOURCE_NOT_FOUND"
    assert SENTINEL not in _serialized(error)
    assert "workspace" not in _serialized(error).lower()


def test_normal_surface_remains_exact_and_has_no_script_input() -> None:
    tools = server.list_tool_definitions("normal")

    assert len(tools) == 12
    assert all("script" not in tool.inputSchema.get("properties", {}) for tool in tools)
    assert all(tool.outputSchema for tool in tools)
