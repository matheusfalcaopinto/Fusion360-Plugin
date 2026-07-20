from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from agent_core.request_context import RequestContext
from fusion_agent_mcp.runtime import FusionAgentRuntime
from fusion_agent_mcp import server
from fusion_mcp_adapter.real_client import RealMcpClient
from fusion_mcp_adapter.semantics import CallSemantics, McpCallOptions


READ_SCRIPT = """import adsk.core

def run(_context: str):
    print(adsk.core.Application.get().version)
"""

ADDITIVE_SCRIPT = """import adsk.core
import adsk.fusion

def run(_context: str):
    root = target_components["root"]
    root.bRepBodies.add(None)
"""


def _security_context(
    *, fast_path: str = "read_only", execution_path: str = "auto"
) -> RequestContext:
    return RequestContext(
        request_id="test-request-parent",
        profile="all",
        mode="mock",
        backend="mock",
        capabilities=(
            f"fast_path:{fast_path}",
            f"execution_path:{execution_path}",
        ),
    )


def test_all_profile_has_35_safe_structured_tools() -> None:
    tools = server.list_tool_definitions("all")
    names = {tool.name for tool in tools}

    assert len(tools) == 35
    assert all(name.startswith("fusion_agent_") for name in names)
    assert not any(name.startswith("fusion_mcp_") for name in names)
    assert {
        "fusion_agent_native_read",
        "fusion_agent_targeted_inspect",
        "fusion_agent_fast_execute",
        "fusion_agent_recover_change",
    }.issubset(names)
    assert all(tool.outputSchema for tool in tools)
    new_specs = {
        spec.name: spec
        for spec in server.tool_specs()
        if spec.name
        in {
            "fusion_agent_native_read",
            "fusion_agent_targeted_inspect",
            "fusion_agent_fast_execute",
            "fusion_agent_recover_change",
        }
    }
    assert all(spec.output_schema is not None for spec in new_specs.values())
    assert new_specs["fusion_agent_recover_change"].annotations.destructiveHint is True


def test_server_advertises_harness_version() -> None:
    app = server.build_server()

    assert app.name == "fusion-agent-harness"
    assert app.version == server.__version__


def test_read_only_fast_execute_schema_allows_queries_without_assertions() -> None:
    schema = next(
        spec.input_schema
        for spec in server.tool_specs()
        if spec.name == "fusion_agent_fast_execute"
    )
    Draft202012Validator.check_schema(schema)
    errors = list(
        Draft202012Validator(schema).iter_errors(
            {
                "intent": "Read one bounded target",
                "change_class": "read_only",
                "script": READ_SCRIPT,
                "verification": {
                    "queries": [{"id": "document", "entity_type": "document"}]
                },
            }
        )
    )
    assert errors == []


@pytest.mark.asyncio
async def test_mock_screenshot_is_real_image_content_without_structured_base64(
    tmp_path,
) -> None:
    runtime = FusionAgentRuntime(
        manifest_root=tmp_path / "manifests", outputs_root=tmp_path / "outputs"
    )
    response = await server.execute_tool_response(
        "fusion_agent_native_read",
        {"mode": "mock", "query_type": "screenshot", "width": 32, "height": 24},
        runtime=runtime,
    )
    sentinel = "DOWNSTREAM_TEXT_AND_META_MUST_NOT_BE_PUBLIC"
    response.content.append({"type": "text", "text": sentinel})
    response.meta["native_diagnostic"] = sentinel
    projector = server._tool_spec_map()["fusion_agent_native_read"].projector
    assert callable(projector)
    result = projector("fusion_agent_native_read", response)

    assert result.structuredContent["ok"] is True
    assert "base64Data" not in json.dumps(result.structuredContent)
    images = [block for block in result.content if block.type == "image"]
    assert len(images) == 1
    assert images[0].mimeType == "image/png"
    assert result.meta is None
    assert sentinel not in result.model_dump_json(by_alias=True)
    await runtime.close()


@pytest.mark.asyncio
async def test_read_only_fast_execute_has_baseline_single_dispatch_and_readback(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(server, "FAST_PATH_OUTPUT_ROOT", tmp_path / "fast_path")
    runtime = FusionAgentRuntime(
        manifest_root=tmp_path / "manifests", outputs_root=tmp_path / "outputs"
    )
    request = {
        "mode": "mock",
        "intent": "Read the active document version without changing it",
        "change_class": "read_only",
        "script": READ_SCRIPT,
        "verification": {
            "queries": [
                {
                    "id": "document",
                    "entity_type": "document",
                    "fields": ["name"],
                }
            ],
            "assertions": [
                {
                    "id": "document_name_unchanged",
                    "query_id": "document",
                    "field": "name",
                    "operator": "unchanged",
                }
            ],
            "requirements": [
                {
                    "id": "read_only_document_unchanged",
                    "assertion_ids": ["document_name_unchanged"],
                    "required": True,
                }
            ],
        },
    }

    response = await server.execute_tool_response(
        "fusion_agent_fast_execute",
        request,
        runtime=runtime,
        profile="advanced",
        request_context=_security_context(),
    )

    assert response.payload["status"] == "applied_verified"
    assert response.payload["native_call_count"] == 4
    assert response.payload["mutating_call_count"] == 0
    assert response.payload["declared_mutation_count"] == 0
    assert response.payload["transport_mutating_dispatch_count"] == 0
    artifact_root = tmp_path / "fast_path" / response.payload["operation_id"]
    assert not (artifact_root / "script.py").exists()
    audit = json.loads((artifact_root / "audit.json").read_text(encoding="utf-8"))
    assert audit["script"]["redacted"] is True
    assert audit["script"]["type"] == "str"
    assert audit["script"]["size"] > 0
    assert READ_SCRIPT not in json.dumps(audit)
    await runtime.close()


@pytest.mark.asyncio
async def test_protected_payload_limit_is_public_and_preserved_in_sanitized_audit(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("FUSION_AGENT_MAX_PROTECTED_SCRIPT_BYTES", "1")
    monkeypatch.setattr(server, "FAST_PATH_OUTPUT_ROOT", tmp_path / "fast_path")
    runtime = FusionAgentRuntime(
        manifest_root=tmp_path / "manifests", outputs_root=tmp_path / "outputs"
    )

    response = await server.execute_tool_response(
        "fusion_agent_fast_execute",
        {
            "mode": "mock",
            "intent": "Create PayloadGateBody",
            "change_class": "additive",
            "script": ADDITIVE_SCRIPT,
            "target_query_ids": ["body"],
            "verification": {
                "queries": [
                    {
                        "id": "body",
                        "entity_type": "body",
                        "selector": {
                            "component_path": "root",
                            "name": "PayloadGateBody",
                        },
                    }
                ],
                "assertions": [
                    {
                        "id": "body_exists",
                        "query_id": "body",
                        "field": "exists",
                        "operator": "eq",
                        "expected": True,
                    }
                ],
                "requirements": [
                    {
                        "id": "payload_gate_body_created",
                        "assertion_ids": ["body_exists"],
                        "required": True,
                    }
                ],
            },
        },
        runtime=runtime,
        profile="advanced",
        request_context=_security_context(fast_path="enabled"),
    )

    assert response.payload["status"] == "blocked_before_apply"
    assert response.payload["error_code"] == "SCRIPT_SIZE_LIMIT_EXCEEDED"
    assert response.payload["transport_mutating_dispatch_count"] == 0
    guard = response.payload["executor_guard"]
    assert guard["protected_payload_bytes"] > guard["limit_bytes"] == 1
    assert len(guard["protected_payload_sha256"]) == 64

    audit_path = (
        tmp_path / "fast_path" / response.payload["operation_id"] / "audit.json"
    )
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    audited_guard = audit["response"]["executor_guard"]
    assert audited_guard["protected_payload_bytes"] == guard["protected_payload_bytes"]
    assert (
        audited_guard["protected_payload_sha256"] == guard["protected_payload_sha256"]
    )
    assert audit["response"]["transport_mutating_dispatch_count"] == 0
    await runtime.close()


@pytest.mark.asyncio
async def test_flags_and_route_lock_fail_closed(monkeypatch, tmp_path) -> None:
    runtime = FusionAgentRuntime(
        manifest_root=tmp_path / "manifests", outputs_root=tmp_path / "outputs"
    )
    blocked = await server.execute_tool_response(
        "fusion_agent_fast_execute",
        {"mode": "mock", "change_class": "additive"},
        runtime=runtime,
        profile="advanced",
        request_context=_security_context(),
    )
    assert blocked.payload["reason"] == "fast_path_read_only"

    blocked_read = await server.execute_tool_response(
        "fusion_agent_native_read",
        {"mode": "mock", "query_type": "active_command"},
        runtime=runtime,
        request_context=_security_context(execution_path="safe_harness"),
    )
    assert blocked_read.payload["reason"] == "route_lock_safe_harness"
    await runtime.close()


@pytest.mark.asyncio
async def test_recovery_is_explicit_latest_operation_and_state_verified(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(server, "FAST_PATH_OUTPUT_ROOT", tmp_path / "fast_path")
    runtime = FusionAgentRuntime(
        manifest_root=tmp_path / "manifests", outputs_root=tmp_path / "outputs"
    )
    request_context = _security_context(fast_path="enabled")
    query = {
        "id": "body",
        "entity_type": "body",
        "selector": {"component_path": "root", "name": "RecoveryBody"},
    }
    applied = await server.execute_tool_response(
        "fusion_agent_fast_execute",
        {
            "mode": "mock",
            "intent": "Create RecoveryBody",
            "change_class": "additive",
            "script": ADDITIVE_SCRIPT,
            "target_query_ids": ["body"],
            "verification": {
                "queries": [query],
                "assertions": [
                    {
                        "id": "body_exists",
                        "query_id": "body",
                        "field": "exists",
                        "operator": "eq",
                        "expected": True,
                    }
                ],
                "requirements": [
                    {
                        "id": "recovery_body_created",
                        "assertion_ids": ["body_exists"],
                        "required": True,
                    }
                ],
            },
        },
        runtime=runtime,
        profile="advanced",
        request_context=request_context,
    )
    assert applied.payload["status"] == "applied_verified"

    recovered = await server.execute_tool_response(
        "fusion_agent_recover_change",
        {
            "mode": "mock",
            "action": "undo",
            "operation_id": applied.payload["operation_id"],
            "confirm": True,
            "verification": {
                "queries": [query],
                "assertions": [
                    {
                        "query_id": "body",
                        "field": "exists",
                        "operator": "eq",
                        "expected": False,
                    }
                ],
            },
        },
        runtime=runtime,
        profile="advanced",
        request_context=request_context,
    )

    assert recovered.payload["status"] == "recovered_verified"
    redone = await server.execute_tool_response(
        "fusion_agent_recover_change",
        {
            "mode": "mock",
            "action": "redo",
            "operation_id": applied.payload["operation_id"],
            "confirm": True,
            "verification": {
                "queries": [query],
                "assertions": [
                    {
                        "query_id": "body",
                        "field": "exists",
                        "operator": "eq",
                        "expected": True,
                    }
                ],
            },
        },
        runtime=runtime,
        profile="advanced",
        request_context=request_context,
    )
    assert redone.payload["status"] == "recovered_verified"
    second_redo = await server.execute_tool_response(
        "fusion_agent_recover_change",
        {
            "mode": "mock",
            "action": "redo",
            "operation_id": applied.payload["operation_id"],
            "confirm": True,
            "verification": {"queries": [query], "assertions": []},
        },
        runtime=runtime,
        profile="advanced",
        request_context=request_context,
    )
    assert second_redo.payload["reason"] == "recovery_action_not_available"
    assert second_redo.payload["expected_action"] == "undo"
    await runtime.close()


@pytest.mark.asyncio
async def test_recovery_blocks_same_count_drift_outside_target_queries(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(server, "FAST_PATH_OUTPUT_ROOT", tmp_path / "fast_path")
    runtime = FusionAgentRuntime(
        manifest_root=tmp_path / "manifests", outputs_root=tmp_path / "outputs"
    )
    request_context = _security_context(fast_path="enabled")
    runtime._mock_backend.entities[("parameter", "Unrelated")] = {
        "entity_type": "parameter",
        "name": "Unrelated",
        "entity_token": "mock:parameter:Unrelated",
        "exists": True,
        "visible": True,
        "expression": "10 mm",
        "value": 1.0,
    }
    query = {
        "id": "body",
        "entity_type": "body",
        "selector": {"component_path": "root", "name": "RecoveryDriftBody"},
    }
    applied = await server.execute_tool_response(
        "fusion_agent_fast_execute",
        {
            "mode": "mock",
            "intent": "Create RecoveryDriftBody",
            "change_class": "additive",
            "script": ADDITIVE_SCRIPT,
            "target_query_ids": ["body"],
            "verification": {
                "queries": [query],
                "assertions": [
                    {
                        "id": "body_exists",
                        "query_id": "body",
                        "field": "exists",
                        "operator": "eq",
                        "expected": True,
                    }
                ],
                "requirements": [
                    {
                        "id": "drift_body_created",
                        "assertion_ids": ["body_exists"],
                        "required": True,
                    }
                ],
            },
        },
        runtime=runtime,
        profile="advanced",
        request_context=request_context,
    )
    assert applied.payload["status"] == "applied_verified"
    runtime._mock_backend.entities[("parameter", "Unrelated")]["expression"] = "11 mm"

    recovered = await server.execute_tool_response(
        "fusion_agent_recover_change",
        {
            "mode": "mock",
            "action": "undo",
            "operation_id": applied.payload["operation_id"],
            "confirm": True,
            "verification": {
                "queries": [query],
                "assertions": [
                    {
                        "query_id": "body",
                        "field": "exists",
                        "operator": "eq",
                        "expected": False,
                    }
                ],
            },
        },
        runtime=runtime,
        request_context=request_context,
    )

    assert recovered.payload["status"] == "blocked_before_apply"
    assert recovered.payload["reason"] == "document_or_state_drift"
    await runtime.close()


def test_execute_read_downgrade_requires_internal_marker() -> None:
    client = RealMcpClient(endpoint="http://127.0.0.1:1/mcp")
    external = client._resolve_options(
        "fusion_mcp_execute",
        McpCallOptions.for_read(operation_id="external"),
    )
    internal = client._resolve_options(
        "fusion_mcp_execute",
        McpCallOptions.for_trusted_internal_read(operation_id="internal"),
    )

    assert external.semantics == CallSemantics.MUTATING
    assert internal.semantics == CallSemantics.READ_ONLY
    assert internal.trusted_internal_read is True


@pytest.mark.asyncio
async def test_legacy_planner_routes_unknown_and_destructive_requests() -> None:
    unknown = await server.execute_tool(
        "fusion_agent_plan_spec",
        {
            "prompt": "Design an ergonomic turbine blade from measured geometry",
            "project": "routing",
        },
    )
    destructive = await server.execute_tool(
        "fusion_agent_plan_spec",
        {"prompt": "Delete all hidden shared imported bodies", "project": "routing"},
    )

    assert unknown["recommended_path"] == "api_documentation_then_native_fast"
    assert destructive["recommended_path"] == "safe_harness"


@pytest.mark.asyncio
async def test_read_only_planner_guard_matches_public_output_contract() -> None:
    payload = await server._plan_spec_tool(
        {
            "prompt": "inspect the active design read-only",
            "project": "routing",
        }
    )

    response = server._as_call_tool_result(
        "fusion_agent_plan_spec",
        server.FastPathResponse(payload),
    )

    assert response.isError is False
    assert response.structuredContent is not None
    assert response.structuredContent["result"]["supported"] is False
    assert response.structuredContent["result"]["code"] == "unsupported_for_planner"
    assert (
        response.structuredContent["result"]["recommended_path"]
        == "native_read_then_targeted_inspect"
    )


@pytest.mark.asyncio
async def test_server_runs_and_pages_strict_mock_benchmark(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(server, "OUTPUTS_ROOT", tmp_path / "outputs")
    runtime = FusionAgentRuntime(
        manifest_root=tmp_path / "manifests", outputs_root=tmp_path / "outputs"
    )
    result = await server.execute_tool(
        "fusion_agent_run_benchmark",
        {
            "driver": "internal",
            "mode": "mock",
            "execution_paths": ["safe_harness", "native_fast"],
            "repetitions": 1,
            "warmups": 0,
            "seed": 7,
        },
        runtime=runtime,
        profile="benchmark",
    )
    page = await server.execute_tool(
        "fusion_agent_read_benchmark_report",
        {"run_id": result["run_id"], "view": "trials", "offset": 1, "limit": 2},
        runtime=runtime,
        profile="benchmark",
    )

    assert result["schema_version"] == "benchmark_report.v2"
    assert result["trial_count"] == 26
    assert result["summary"]["gates"]["all_required"] is True
    assert page["total"] == 26
    assert len(page["items"]) == 2
    await runtime.close()


@pytest.mark.parametrize(
    "value",
    [
        r"C:escape.json",
        r"D:escape.json",
        r"\escape.json",
        r"\\server\share\escape.json",
        r"\\?\C:\escape.json",
        "/absolute/escape.json",
    ],
)
def test_output_helper_rejects_every_absolute_or_drive_relative_form(
    value: str,
) -> None:
    with pytest.raises(ValueError, match="relative"):
        server._safe_relative_path(Path("outputs"), value)


def test_output_helper_keeps_legitimate_nested_relative_path() -> None:
    assert server._safe_relative_path(Path("outputs"), "sessions/result.json") == Path(
        "outputs/sessions/result.json"
    )
