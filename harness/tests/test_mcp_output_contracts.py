from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from fusion_agent_mcp import server


def _assert_output(name: str, result: dict[str, Any], *, ok: bool = True) -> None:
    schema = next(
        spec.output_schema for spec in server.tool_specs() if spec.name == name
    )
    assert schema is not None
    Draft202012Validator(schema).validate({"ok": ok, "result": result})


def _walk(value: Any):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def test_all_35_tools_have_non_placeholder_dedicated_result_contracts() -> None:
    specs = server.tool_specs()
    contracts = server._tool_result_contracts()

    assert len(specs) == 35
    assert set(contracts) == {spec.name for spec in specs}
    assert len({id(contract) for contract in contracts.values()}) == 35

    for spec in specs:
        assert spec.output_schema is not None
        Draft202012Validator.check_schema(spec.output_schema)
        assert not any(node == {} for node in _walk(spec.output_schema))
        assert not any(
            isinstance(node, dict) and node.get("additionalProperties") is True
            for node in _walk(spec.output_schema)
        )
        result_schema = spec.output_schema["properties"]["result"]
        assert result_schema == contracts[spec.name]
        assert (
            "type" in result_schema
            or "$ref" in result_schema
            or "oneOf" in result_schema
        )


@pytest.mark.asyncio
async def test_local_tool_payloads_validate_against_their_advertised_contracts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(server, "WORKSPACE_ROOT", tmp_path / "workspace")
    monkeypatch.setattr(server, "OUTPUTS_ROOT", tmp_path / "outputs")
    monkeypatch.setattr(server, "MANIFEST_ROOT", tmp_path / "manifests")

    actual = {
        "fusion_agent_doctor": await server.execute_tool(
            "fusion_agent_doctor", profile="all"
        ),
        "fusion_agent_list_sessions": await server.execute_tool(
            "fusion_agent_list_sessions", profile="all"
        ),
        "fusion_agent_validate_spec": await server.execute_tool(
            "fusion_agent_validate_spec", {"spec_json": "not-json"}, profile="all"
        ),
        "fusion_agent_read_manifest": await server.execute_tool(
            "fusion_agent_read_manifest", {"source": "mock"}, profile="all"
        ),
        "fusion_agent_list_benchmarks": await server.execute_tool(
            "fusion_agent_list_benchmarks", profile="all"
        ),
        "fusion_agent_skills_list": await server.execute_tool(
            "fusion_agent_skills_list", profile="all"
        ),
        "fusion_agent_memory_list_project": await server.execute_tool(
            "fusion_agent_memory_list_project",
            {"project": "schema_demo"},
            profile="all",
        ),
    }

    for name, payload in actual.items():
        _assert_output(name, payload)


@pytest.mark.asyncio
async def test_artifact_trace_and_fast_path_control_payloads_validate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    session_dir = workspace / "projects" / "demo" / "sessions" / "s1"
    session_dir.mkdir(parents=True)
    (session_dir / "verification.json").write_text('{"passed": true}', encoding="utf-8")
    (session_dir / "tool_trace.jsonl").write_text(
        '{"event": "one"}\n{"event": "two"}\n', encoding="utf-8"
    )
    monkeypatch.setattr(server, "WORKSPACE_ROOT", workspace)

    artifact = await server.execute_tool(
        "fusion_agent_read_session_artifact",
        {"project": "demo", "session_id": "s1", "artifact": "verification.json"},
        profile="all",
    )
    trace = await server.execute_tool(
        "fusion_agent_read_trace",
        {"project": "demo", "session_id": "s1", "limit": 1},
        profile="all",
    )
    monkeypatch.setenv("FUSION_AGENT_FAST_PATH_MODE", "read_only")
    blocked = await server.execute_tool(
        "fusion_agent_fast_execute",
        {
            "intent": "Create a feature",
            "change_class": "additive",
            "script": "def run(_context):\n    return None\n",
        },
        profile="all",
    )

    _assert_output("fusion_agent_read_session_artifact", artifact)
    _assert_output("fusion_agent_read_trace", trace)
    _assert_output("fusion_agent_fast_execute", blocked)


def test_output_contracts_reject_wrong_stable_types_and_semantic_values() -> None:
    doctor_schema = server._tool_output_schema("fusion_agent_doctor")
    doctor = server._doctor_tool
    assert doctor is not None  # keep this test tied to the real handler surface
    errors = list(
        Draft202012Validator(doctor_schema).iter_errors(
            {
                "ok": True,
                "result": {
                    "project_root": ".",
                    "workspace": "workspace",
                    "outputs": "outputs",
                    "manifests": "manifests",
                    "python_executable": "python",
                    "launcher_path": "launcher.py",
                    "source_plugin_root": ".",
                    "cache_plugin_version": "0.3.0",
                    "fusion_mcp_endpoint": "",
                    "fusion_mcp_endpoint_configured": "false",
                    "fusion_mcp_command_configured": False,
                    "fusion_agent_default_mode": "mock",
                    "fusion_agent_require_real": False,
                    "fusion_agent_allow_dry_run": True,
                    "dry_run_policy": "allowed",
                    "manifest_status": {},
                },
            }
        )
    )
    assert errors

    fast_schema = server._tool_output_schema("fusion_agent_fast_execute")
    assert list(
        Draft202012Validator(fast_schema).iter_errors(
            {"ok": True, "result": {"status": "invented_success_state"}}
        )
    )


def test_fast_path_input_schema_exposes_requirement_coverage_contract() -> None:
    schema = next(
        spec.input_schema
        for spec in server.tool_specs()
        if spec.name == "fusion_agent_fast_execute"
    )
    verification = schema["properties"]["verification"]
    assertion = verification["properties"]["assertions"]["items"]
    requirement = verification["properties"]["requirements"]["items"]

    assert "requirement_ids" in assertion["properties"]
    assert requirement["required"] == ["id"]
    assert requirement["properties"]["oracle"]["enum"] == [
        "contract",
        "independent_oracle",
    ]
    Draft202012Validator(schema).validate(
        {
            "intent": "Add a verified body",
            "change_class": "additive",
            "script": "def run(_context):\n    return None\n",
            "target_query_ids": ["target"],
            "verification": {
                "queries": [
                    {
                        "id": "target",
                        "entity_type": "body",
                        "selector": {"component_path": "root", "name": "CreatedBody"},
                    }
                ],
                "assertions": [
                    {
                        "id": "exists",
                        "query_id": "target",
                        "field": "exists",
                        "operator": "eq",
                        "expected": True,
                        "requirement_ids": ["body_created"],
                    }
                ],
                "requirements": [
                    {
                        "id": "body_created",
                        "description": "The requested body exists",
                        "assertion_ids": ["exists"],
                        "oracle": "contract",
                    }
                ],
            },
        }
    )
