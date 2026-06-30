from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_core.planner import PlanningRequest, RuleBasedPlanner
from agent_core.session_controller import SessionController, SessionOptions, _extract_geometry_entities
from agent_core.self_test import run_self_test
from cli.main import _doctor
from fusion_agent_mcp.server import execute_tool, list_tool_definitions
from fusion_mcp_adapter.manifest_store import ManifestStore
from fusion_mcp_adapter.real_client import RealMcpClient
from fusion_mcp_adapter.tool_result import ToolDefinition, ToolManifest


@pytest.mark.asyncio
async def test_plate_hole_dimensions_are_parsed_contextually() -> None:
    prompt = (
        "Create a 100 x 60 x 6 mm mounting plate with four 5 mm holes, "
        "one near each corner, 12 mm from each edge."
    )

    spec = await RuleBasedPlanner().plan(PlanningRequest(user_prompt=prompt))
    parameters = {parameter.name: parameter.expression for parameter in spec.parameters}

    assert parameters["plate_thickness"] == "6 mm"
    assert parameters["hole_diameter"] == "5 mm"
    assert parameters["hole_offset"] == "12 mm"


@pytest.mark.asyncio
async def test_box_wall_thickness_does_not_capture_box_height() -> None:
    spec = await RuleBasedPlanner().plan(
        PlanningRequest(user_prompt="Create an open rectangular box 80 x 50 x 30 mm with 3 mm wall thickness.")
    )

    parameters = {parameter.name: parameter.expression for parameter in spec.parameters}

    assert parameters["box_height"] == "30 mm"
    assert parameters["wall_thickness"] == "3 mm"


@pytest.mark.asyncio
async def test_read_only_prompts_fail_closed() -> None:
    prompt = "Read the active design and list all component bounding box centers in mm. Do not create any geometry."

    with pytest.raises(ValueError, match="Read-only inspection/extraction requests"):
        await RuleBasedPlanner().plan(PlanningRequest(user_prompt=prompt))


def test_extract_geometry_entities_normalizes_positions_and_filters() -> None:
    state = {
        "bodies": {
            "bearing_lower": {
                "name": "F695ZZ lower",
                "component": "pulley_group_a",
                "min_mm": [8.0, 18.0, 2.0],
                "max_mm": [12.0, 22.0, 6.0],
                "visible": True,
            },
            "hidden_bearing": {
                "name": "F695ZZ hidden",
                "component": "pulley_group_a",
                "center_mm": [30.0, 40.0, 4.0],
                "visible": False,
            },
        },
        "occurrences": {
            "root/bearing_upper": {
                "name": "695ZZ upper",
                "component": "pulley_group_b",
                "path": "root/bearing_upper",
                "center_mm": [10.0, 20.0, 14.0],
                "bounding_box_mm": [5.0, 5.0, 4.0],
                "visible": True,
            }
        },
    }

    entities = _extract_geometry_entities(state, name_contains="695", include_hidden=False)

    assert [entity["name"] for entity in entities] == ["F695ZZ lower", "695ZZ upper"]
    assert entities[0]["center_mm"] == [10.0, 20.0, 4.0]
    assert entities[0]["xy_mm"] == [10.0, 20.0]
    assert entities[0]["z_mm"] == 4.0
    assert entities[1]["z_mm"] == 14.0


@pytest.mark.asyncio
async def test_mock_capture_writes_valid_png(tmp_path: Path) -> None:
    result = await SessionController().capture_viewport(
        project="pytest_capture",
        mode="mock",
        options=SessionOptions(
            mode="mock",
            project="pytest_capture",
            workspace_root=tmp_path / "workspace",
            output_dir=tmp_path / "outputs",
        ),
        output_dir=tmp_path / "outputs",
        name="capture",
        width=320,
        height=240,
    )

    data = Path(result.path).read_bytes()

    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert result.capture["screenshot"]["bytes"] == len(data)


@pytest.mark.asyncio
async def test_extract_geometry_tool_writes_artifact() -> None:
    result = await execute_tool(
        "fusion_agent_extract_geometry",
        {"mode": "mock", "project": "pytest_extract", "entity_type": "all"},
    )

    assert result["status"] == "success"
    assert result["counts"]["returned"] == 0
    assert Path(result["extraction_path"]).exists()
    assert Path(result["extraction_path"]).name == "extraction.json"


@pytest.mark.asyncio
async def test_built_in_self_test_runs_without_benchmark(tmp_path: Path) -> None:
    result = await run_self_test(
        project="pytest_self_test",
        workspace_root=tmp_path / "workspace",
        output_dir=tmp_path / "outputs",
        manifest_dir=tmp_path / "manifests",
        run_benchmark=False,
    )

    assert result["ok"] is True
    assert result["failed"] == 0
    assert {check["name"] for check in result["checks"]} >= {
        "planner_generates_valid_spec",
        "read_only_prompt_rejected",
        "mock_run_session",
        "mock_capture_viewport",
    }


@pytest.mark.asyncio
async def test_self_test_tool_is_exposed() -> None:
    tools = {tool.name: tool for tool in list_tool_definitions()}

    assert "fusion_agent_self_test" in tools
    assert "include_real_write_sandbox" in tools["fusion_agent_self_test"].inputSchema["properties"]
    assert "include_real_capture_sandbox" in tools["fusion_agent_self_test"].inputSchema["properties"]

    result = await execute_tool(
        "fusion_agent_self_test",
        {"project": "pytest_self_test_tool", "run_benchmark": False},
    )

    assert result["ok"] is True
    assert result["failed"] == 0


@pytest.mark.asyncio
async def test_built_in_benchmark_suite_is_listed() -> None:
    result = await execute_tool("fusion_agent_list_benchmarks", {})

    assert result["suites"]
    assert result["suites"][0]["name"] == "v0_parametric_parts.md"
    assert result["suites"][0]["case_count"] == 5


def test_manifest_store_loads_latest_by_source(tmp_path: Path) -> None:
    store = ManifestStore(tmp_path)
    real = ToolManifest(source="fusion_real", tools=[ToolDefinition(name="fusion_mcp_read")])
    mock = ToolManifest(source="mock", tools=[ToolDefinition(name="inspect_design")])

    store.save(real)
    store.save(mock)

    assert store.load_latest().source == "mock"
    assert store.load_latest(source="fusion_real").source == "fusion_real"
    assert store.load_latest(source="mock").source == "mock"


def test_real_client_auto_discovers_local_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FUSION_MCP_ENDPOINT", raising=False)
    monkeypatch.delenv("FUSION_MCP_COMMAND", raising=False)
    calls: list[str] = []

    class FakeResponse:
        status = 200

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    def fake_urlopen(uri: str, timeout: float) -> FakeResponse:
        calls.append(uri)
        if "27182" not in uri:
            raise OSError("closed")
        return FakeResponse()

    monkeypatch.setattr("fusion_mcp_adapter.real_client.urllib.request.urlopen", fake_urlopen)

    client = RealMcpClient()

    assert client.endpoint == "http://127.0.0.1:27182/mcp"
    assert calls == [
        "http://127.0.0.1:17182/health",
        "http://127.0.0.1:17183/health",
        "http://127.0.0.1:27182/health",
    ]


def test_doctor_reports_effective_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FUSION_MCP_ENDPOINT", raising=False)
    monkeypatch.delenv("FUSION_MCP_COMMAND", raising=False)

    class FakeRealMcpClient:
        def __init__(self, timeout_seconds: float) -> None:
            self.endpoint = "http://127.0.0.1:27182/mcp"

    monkeypatch.setattr("cli.main.RealMcpClient", FakeRealMcpClient)

    result = _doctor()

    assert result["fusion_mcp_endpoint_configured"] is False
    assert result["fusion_mcp_command_configured"] is False
    assert result["fusion_mcp_effective_endpoint"] == "http://127.0.0.1:27182/mcp"


@pytest.mark.asyncio
async def test_mock_benchmark_plate_artifact_keeps_hole_dimensions(tmp_path: Path) -> None:
    prompt = (
        "Create a 100 x 60 x 6 mm mounting plate with four 5 mm holes, "
        "one near each corner, 12 mm from each edge."
    )
    result = await SessionController().run(
        prompt,
        project="pytest_plate",
        mode="mock",
        options=SessionOptions(
            mode="mock",
            project="pytest_plate",
            workspace_root=tmp_path / "workspace",
            output_dir=tmp_path / "outputs",
            manifest_dir=tmp_path / "manifests",
            dry_run=False,
        ),
    )

    spec = json.loads(Path(result.cad_spec_path).read_text(encoding="utf-8"))
    parameters = {parameter["name"]: parameter["expression"] for parameter in spec["parameters"]}

    assert result.status == "success"
    assert parameters["hole_diameter"] == "5 mm"
    assert parameters["hole_offset"] == "12 mm"
