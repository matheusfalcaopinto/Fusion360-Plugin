from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from agent_core.planner import PlanningRequest, RuleBasedPlanner
from cad_spec.models import CadSpec
from fusion_agent_mcp.server import execute_tool, list_tool_definitions


EXPECTED_PUBLIC_TOOLS = {
    "fusion_agent_doctor",
    "fusion_agent_capabilities",
    "fusion_agent_self_test",
    "fusion_agent_probe",
    "fusion_agent_inspect",
    "fusion_agent_extract_geometry",
    "fusion_agent_verify_active_design",
    "fusion_agent_capture_viewport",
    "fusion_agent_run_session",
    "fusion_agent_dry_run_session",
    "fusion_agent_run_sandbox_session",
    "fusion_agent_list_sessions",
    "fusion_agent_read_session_artifact",
    "fusion_agent_read_trace",
    "fusion_agent_plan_spec",
    "fusion_agent_validate_spec",
    "fusion_agent_export_spec_json",
    "fusion_agent_list_benchmarks",
    "fusion_agent_run_benchmark",
    "fusion_agent_read_benchmark_report",
    "fusion_agent_discover_tools",
    "fusion_agent_propose_mapping",
    "fusion_agent_read_manifest",
    "fusion_agent_memory_search",
    "fusion_agent_memory_write",
    "fusion_agent_memory_list_project",
    "fusion_agent_skills_list",
    "fusion_agent_skills_get",
    "fusion_agent_skills_rank",
}

RAW_TOOL_PREFIXES = ("fusion360_", "autodesk_fusion_", "fusion_mcp_")
PLATE_PROMPT = "Create a 40 mm x 20 mm x 6 mm mounting plate with four 3 mm holes, 8 mm from each edge."


def test_plugin_manifest_registers_only_safe_fusion_agent_server(plugin_root: Path) -> None:
    plugin = json.loads((plugin_root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    mcp = json.loads((plugin_root / ".mcp.json").read_text(encoding="utf-8"))

    assert plugin["name"] == "fusion-agent-codex"
    assert plugin["skills"] == "./skills/"
    assert plugin["mcpServers"] == "./.mcp.json"
    assert plugin["interface"]["displayName"] == "Fusion Agent Codex"
    assert plugin["interface"]["capabilities"] == ["MCP", "CAD automation", "Local harness"]

    servers = mcp["mcpServers"]
    assert set(servers) == {"fusion_agent"}
    assert all(not name.startswith(RAW_TOOL_PREFIXES) for name in servers)

    server = servers["fusion_agent"]
    assert Path(server["command"]).name.lower() in {"python.exe", "python"}
    assert server["args"] == ["scripts/fusion_agent_codex_mcp_launcher.py"]
    assert server["env"]["FUSION_AGENT_CODEX"] == "1"
    assert "FUSION_MCP_ENDPOINT" not in server["env"]


def test_launcher_and_cli_are_easy_for_codex_to_diagnose(plugin_root: Path, unpacked_wheel: Path) -> None:
    env = os.environ.copy()
    pythonpath_entries = []
    if unpacked_wheel.is_dir():
        pythonpath_entries.append(str(unpacked_wheel))
    if env.get("PYTHONPATH"):
        pythonpath_entries.append(env["PYTHONPATH"])
    if pythonpath_entries:
        env["PYTHONPATH"] = os.pathsep.join(pythonpath_entries)
    launcher = plugin_root / "scripts" / "fusion_agent_codex_mcp_launcher.py"
    completed = subprocess.run(
        [sys.executable, str(launcher), "--check"],
        cwd=plugin_root,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    assert f"plugin_root={plugin_root}" in completed.stdout
    assert "installed_server_available=True" in completed.stdout
    assert "fusion_agent_codex=1" in completed.stdout

    help_result = subprocess.run(
        [sys.executable, "-m", "cli.main", "--help"],
        cwd=plugin_root,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    assert "Fusion CAD automation harness" in help_result.stdout
    for command in (
        "inspect",
        "self-test",
        "extract",
        "run",
        "run-sandbox",
        "verify",
        "capture",
        "benchmark",
        "tools",
        "memory",
        "doctor",
        "capabilities",
    ):
        assert command in help_result.stdout


@pytest.mark.asyncio
async def test_mcp_tool_surface_is_complete_safe_schemaed_and_self_describing() -> None:
    tools = {tool.name: tool for tool in list_tool_definitions()}

    assert set(tools) == EXPECTED_PUBLIC_TOOLS
    assert all(name.startswith("fusion_agent_") for name in tools)
    assert all(not name.startswith(RAW_TOOL_PREFIXES) for name in tools)

    for name, tool in tools.items():
        schema = tool.inputSchema
        assert tool.description
        assert schema["type"] == "object", name
        assert schema.get("additionalProperties") is False, name
        assert isinstance(schema.get("properties"), dict), name
        assert isinstance(schema.get("required"), list), name

    assert tools["fusion_agent_plan_spec"].inputSchema["required"] == ["prompt"]
    assert tools["fusion_agent_validate_spec"].inputSchema["required"] == ["spec_json"]
    assert "dry_run_session_id" in tools["fusion_agent_run_session"].inputSchema["properties"]
    assert "allow_existing_document_write" in tools["fusion_agent_run_session"].inputSchema["properties"]
    assert tools["fusion_agent_run_sandbox_session"].inputSchema["required"] == ["prompt"]

    capabilities = await execute_tool("fusion_agent_capabilities", {})
    assert capabilities["ok"] is True
    assert capabilities["schema_version"] == "1.1"
    assert capabilities["server"] == "fusion_agent"
    assert set(capabilities["tools"]) == EXPECTED_PUBLIC_TOOLS
    assert capabilities["raw_tool_prefixes_not_exposed"] == ["fusion360_", "autodesk_fusion_", "fusion_mcp_"]
    assert capabilities["real_write_policy"]["sandbox_closes_without_saving"] is True


@pytest.mark.asyncio
async def test_mock_planning_validation_and_session_artifact_cycle(project_name: str, harness_paths: Any) -> None:
    inspection = await execute_tool("fusion_agent_inspect", {"mode": "mock"})
    assert inspection["schema_version"] == "1.1"
    assert inspection["tool"] == "fusion_agent_inspect"
    assert inspection["state"]["units"] == "mm"
    assert inspection["state"]["active_document"] is True

    planned = await execute_tool("fusion_agent_plan_spec", {"project": project_name, "prompt": PLATE_PROMPT})
    spec = CadSpec.model_validate(planned["cad_spec"])
    assert spec.acceptance_tests
    assert all(_has_explicit_unit_or_parameter(parameter.expression) for parameter in spec.parameters)

    valid = await execute_tool("fusion_agent_validate_spec", {"spec_json": planned["cad_spec_json"]})
    assert valid["valid"] is True

    exported = await execute_tool(
        "fusion_agent_export_spec_json",
        {"project": project_name, "prompt": PLATE_PROMPT, "output_path": "specs/plate.json"},
    )
    assert Path(exported["path"]).is_file()
    assert Path(exported["path"]).is_relative_to(harness_paths.outputs)
    assert any(artifact["path"] == exported["path"] for artifact in exported["artifacts"])

    dry_run = await execute_tool(
        "fusion_agent_dry_run_session",
        {"project": project_name, "mode": "mock", "prompt": PLATE_PROMPT, "max_repairs": 2},
    )
    assert dry_run["status"] == "simulated"
    assert dry_run["dry_run"] is True
    assert dry_run["verification"]["passed"] is True
    assert dry_run["execution"]["transactions"][0]["operation"] == "dry_run"
    assert {artifact["field"] for artifact in dry_run["artifacts"]} >= {"cad_spec_path", "journal_path", "trace_path"}

    trace = await execute_tool(
        "fusion_agent_read_trace",
        {"project": project_name, "session_id": dry_run["session_id"], "limit": 10},
    )
    assert trace["event_count"] >= 1
    assert any(event["event"] == "dry_run_skipped_execution" for event in trace["events"])

    journal = await execute_tool(
        "fusion_agent_read_session_artifact",
        {"project": project_name, "session_id": dry_run["session_id"], "artifact": "session_journal.json"},
    )
    assert journal["json"]["final_status"] == "simulated"
    assert journal["json"]["dry_run"] is True

    run = await execute_tool(
        "fusion_agent_run_session",
        {"project": project_name, "mode": "mock", "prompt": PLATE_PROMPT, "max_repairs": 2},
    )
    assert run["status"] == "success"
    assert run["verification"]["passed"] is True
    assert run["verification"]["metrics"]["body_count"] == 1

    sessions = await execute_tool("fusion_agent_list_sessions", {"project": project_name, "limit": 10})
    session_ids = {session["session_id"] for session in sessions["sessions"]}
    assert {dry_run["session_id"], run["session_id"]} <= session_ids


@pytest.mark.asyncio
async def test_mock_readonly_extract_verify_and_capture_tools(project_name: str, harness_paths: Any) -> None:
    extraction = await execute_tool(
        "fusion_agent_extract_geometry",
        {"project": project_name, "mode": "mock", "entity_type": "all", "include_hidden": False, "limit": 20},
    )
    assert extraction["status"] == "success"
    assert extraction["units"] == "mm"
    assert extraction["counts"] == {"bodies_in_state": 0, "occurrences_in_state": 0, "returned": 0}

    extraction_artifact = await execute_tool(
        "fusion_agent_read_session_artifact",
        {"project": project_name, "session_id": extraction["session_id"], "artifact": "extraction.json"},
    )
    assert extraction_artifact["json"]["filters"]["entity_type"] == "all"

    verification = await execute_tool(
        "fusion_agent_verify_active_design",
        {"project": project_name, "mode": "mock", "prompt": PLATE_PROMPT},
    )
    assert verification["status"] == "failed"
    assert verification["verification"]["passed"] is False
    assert {issue["code"] for issue in verification["verification"]["issues"]} >= {
        "WRONG_ACTIVE_COMPONENT",
        "FEATURE_CREATION_FAILED",
    }

    capture = await execute_tool(
        "fusion_agent_capture_viewport",
        {
            "project": project_name,
            "mode": "mock",
            "name": "mock_capture.png",
            "view": "isometric",
            "width": 320,
            "height": 240,
        },
    )
    assert capture["status"] == "success"
    assert Path(capture["path"]).is_relative_to(harness_paths.outputs)
    assert Path(capture["path"]).read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


@pytest.mark.asyncio
async def test_real_run_session_requires_dry_run_proof(project_name: str) -> None:
    with pytest.raises(ValueError, match="dry_run_session_id"):
        await execute_tool("fusion_agent_run_session", {"project": project_name, "mode": "real", "prompt": PLATE_PROMPT})

    dry_run = await execute_tool(
        "fusion_agent_dry_run_session",
        {"project": project_name, "mode": "mock", "prompt": PLATE_PROMPT},
    )
    with pytest.raises(ValueError, match="allow_existing_document_write"):
        await execute_tool(
            "fusion_agent_run_session",
            {
                "project": project_name,
                "mode": "real",
                "prompt": PLATE_PROMPT,
                "dry_run_session_id": dry_run["session_id"],
            },
        )


@pytest.mark.asyncio
async def test_planner_and_validation_reject_unsafe_or_ambiguous_specs() -> None:
    with pytest.raises(ValueError, match="Read-only inspection/extraction requests"):
        await RuleBasedPlanner().plan(
            PlanningRequest(
                user_prompt="Read the active design and list all component bounding boxes. Do not create geometry."
            )
        )

    invalid_spec = {
        "intent": "bad_units",
        "units": "mm",
        "parameters": [{"name": "plate_length", "expression": "40"}],
        "components": [
            {
                "name": "plate",
                "features": [
                    {
                        "name": "base",
                        "type": "extrude_rectangle",
                        "inputs": {"width": 40, "height": "20 mm", "distance": "6 mm"},
                    }
                ],
            }
        ],
        "acceptance_tests": [{"type": "body_count", "target": 1}],
    }
    result = await execute_tool("fusion_agent_validate_spec", {"spec_json": json.dumps(invalid_spec)})

    assert result["valid"] is False
    assert "must include units" in result["error"]
    assert "ambiguous numeric dimension" in result["error"]


@pytest.mark.asyncio
async def test_mcp_contract_errors_are_explicit_and_safe(project_name: str) -> None:
    bad_calls = [
        ("fusion_mcp_read", {}),
        ("fusion_agent_plan_spec", {}),
        ("fusion_agent_inspect", {"mode": "bogus"}),
        ("fusion_agent_list_sessions", {"project": "../escape"}),
        ("fusion_agent_export_spec_json", {"prompt": PLATE_PROMPT, "output_path": "../escape.json"}),
        (
            "fusion_agent_read_session_artifact",
            {"project": project_name, "session_id": "missing", "artifact": "../secret.txt"},
        ),
        (
            "fusion_agent_capture_viewport",
            {"project": project_name, "mode": "mock", "name": "../capture.png"},
        ),
        (
            "fusion_agent_memory_write",
            {"project": project_name, "path": "../memory.md", "content": "# unsafe"},
        ),
    ]

    for tool_name, arguments in bad_calls:
        with pytest.raises((KeyError, ValueError, FileNotFoundError), match=r".+"):
            await execute_tool(tool_name, arguments)


@pytest.mark.asyncio
async def test_skills_memory_and_benchmark_tools_are_usable(project_name: str) -> None:
    skills = await execute_tool("fusion_agent_skills_list", {})
    skill_names = {skill["name"] for skill in skills["skills"]}
    assert {"create_parametric_plate", "create_hole_pattern", "validate_export"} <= skill_names

    skill = await execute_tool("fusion_agent_skills_get", {"name": "create_parametric_plate"})
    assert skill["skill"]["name"] == "create_parametric_plate"
    assert "content" in skill["skill"]

    ranked = await execute_tool("fusion_agent_skills_rank", {"query": "create a parametric plate with holes", "limit": 3})
    ranked_names = {item["name"] for item in ranked["skills"]}
    assert ranked_names & {"create_parametric_plate", "create_hole_pattern", "fusion_mechanical_pro"}

    write = await execute_tool(
        "fusion_agent_memory_write",
        {
            "project": project_name,
            "path": "decisions/validation.md",
            "content": "# Validation Decision\n\nUse explicit unit strings in every CAD spec.",
        },
    )
    assert Path(write["path"]).is_file()

    listed_memory = await execute_tool("fusion_agent_memory_list_project", {"project": project_name})
    assert any(record["summary"] == "Validation Decision" for record in listed_memory["records"])

    search = await execute_tool(
        "fusion_agent_memory_search",
        {"project": project_name, "query": "explicit unit strings validation"},
    )
    assert search["records"]

    suites = await execute_tool("fusion_agent_list_benchmarks", {})
    assert suites["suites"] == [{"name": "v0_parametric_parts.md", "path": "built-in:v0_parametric_parts.md", "case_count": 5}]

    benchmark = await execute_tool(
        "fusion_agent_run_benchmark",
        {"suite": "v0_parametric_parts.md", "mode": "mock", "dry_run": True, "project": project_name},
    )
    assert len(benchmark["results"]) == 5
    assert all(result["final_success"] is True for result in benchmark["results"])
    assert all(result["status"] == "simulated" for result in benchmark["results"])

    report = await execute_tool("fusion_agent_read_benchmark_report", {"path": "benchmark_report.json"})
    assert len(report["report"]) == 5


@pytest.mark.asyncio
async def test_real_fusion_endpoint_discovery_and_sandbox_are_mandatory(project_name: str) -> None:
    endpoint = os.getenv("FUSION_MCP_ENDPOINT", "http://127.0.0.1:27182/mcp")

    probe = await execute_tool("fusion_agent_probe", {"endpoint": endpoint})
    assert probe["probes"], "real Fusion probe returned no endpoints"
    assert probe["probes"][0]["health"]["ok"] is True
    assert probe["probes"][0]["tools_list"]["ok"] is True
    assert probe["probes"][0]["tools_list"]["tool_count"] >= 1

    manifest = await execute_tool("fusion_agent_discover_tools", {"mode": "real"})
    manifest_names = {tool["name"] for tool in manifest["tools"]}
    assert manifest["source"] == "fusion_real"
    assert {"fusion_mcp_read", "fusion_mcp_execute"}.issubset(manifest_names)

    loaded_manifest = await execute_tool("fusion_agent_read_manifest", {})
    assert loaded_manifest["loaded"] is True
    assert {tool["name"] for tool in loaded_manifest["manifest"]["tools"]} == manifest_names

    mapping = await execute_tool("fusion_agent_propose_mapping", {})
    assert mapping["manifest_loaded"] is True
    assert mapping["profile"] == "fusion_mcp_crud"
    assert {"inspect_design", "create_named_parameter", "capture_viewport"} <= {
        proposal["facade_operation"] for proposal in mapping["proposals"] if proposal["available"]
    }

    real_inspection = await execute_tool("fusion_agent_inspect", {"mode": "real"})
    assert real_inspection["state"]["active_document"] is True
    assert real_inspection["state"]["units"] == "mm"

    real_extraction = await execute_tool(
        "fusion_agent_extract_geometry",
        {"project": project_name, "mode": "real", "entity_type": "all", "include_hidden": False, "limit": 50},
    )
    assert real_extraction["status"] == "success"
    assert real_extraction["units"] == "mm"
    assert Path(real_extraction["extraction_path"]).is_file()

    self_test = await execute_tool(
        "fusion_agent_self_test",
        {
            "project": f"{project_name}_real_sandbox",
            "run_benchmark": False,
            "include_real_readonly": True,
            "include_real_write_sandbox": True,
            "include_real_capture_sandbox": True,
        },
    )
    assert self_test["ok"] is True
    assert self_test["failed"] == 0

    checks = {check["name"]: check for check in self_test["checks"]}
    sandbox = checks["real_write_sandbox_session"]["details"]
    assert sandbox["status"] == "success"
    assert sandbox["scratch_closed"] is True
    assert sandbox["body_count"] == 1
    assert sandbox["bounding_box_mm"] == pytest.approx([30.0, 20.0, 4.0], abs=0.2)
    assert sandbox["capture"]["screenshot"]["ok"] is True

    scratch = json.loads(Path(sandbox["scratch_path"]).read_text(encoding="utf-8"))
    assert scratch["scratch"]["closed"]["scratch_document"]["closed_without_saving"] is True
    assert scratch["verification"]["passed"] is True

    sandbox_tool = await execute_tool(
        "fusion_agent_run_sandbox_session",
        {
            "project": f"{project_name}_tool_sandbox",
            "prompt": "Create a 20 mm x 10 mm x 3 mm plate.",
            "include_capture": True,
        },
    )
    assert sandbox_tool["status"] == "success"
    assert sandbox_tool["scratch_closed"] is True


def _has_explicit_unit_or_parameter(expression: str) -> bool:
    if re.search(r"\b(mm|cm|in|deg)\b", expression):
        return True
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(\s*/\s*\d+(\.\d+)?)?", expression))
