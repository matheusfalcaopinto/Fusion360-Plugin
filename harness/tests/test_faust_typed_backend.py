from __future__ import annotations

import pytest

from agent_core.capability_executor import CapabilityExecutor
from cad_spec.v2 import CadSpecV2
from fusion_mcp_adapter.tool_result import ToolDefinition, ToolManifest, ToolResult
from fusion_tool_facade.typed_backend import FaustTypedBackend


class Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def list_tools(self):
        raise AssertionError("manifest is already supplied")

    async def call_tool(self, name, arguments, *, options=None):
        self.calls.append((name, arguments))
        return ToolResult.success(success=True, name=name)


def _manifest(*names: str) -> ToolManifest:
    return ToolManifest(
        source="faust-test",
        tools=[ToolDefinition(name=name) for name in names],
    )


def _spec() -> CadSpecV2:
    return CadSpecV2.model_validate(
        {
            "cad_spec_version": "2.0",
            "intent": "Constrain and revolve a profile",
            "requirements": [
                {"id": "shaft", "description": "shaft exists", "assertion_ids": ["shaft_exists"]}
            ],
            "operations": [
                {
                    "id": "coincident_constraint",
                    "kind": "sketch.constraint",
                    "sketch_ref": "shaft_sketch",
                    "constraint": "coincident",
                    "entity_refs": ["line#0", "line#1"],
                    "requirement_ids": ["shaft"],
                },
                {
                    "id": "shaft_revolve",
                    "kind": "feature.revolve",
                    "component_ref": "root",
                    "profile_ref": "profile#0",
                    "axis_ref": "x_axis",
                    "angle": "360 deg",
                    "result_name": "shaft_body",
                    "depends_on": ["coincident_constraint"],
                    "requirement_ids": ["shaft"],
                },
            ],
            "assertions": [
                {"id": "shaft_exists", "kind": "entity_exists", "target_ref": "shaft_body"}
            ],
        }
    )


@pytest.mark.asyncio
async def test_faust_backend_maps_only_curated_tools() -> None:
    client = Client()
    manifest = _manifest("add_constraint", "revolve", "execute_code", "delete_all")
    backend = FaustTypedBackend.from_client(client, manifest)
    result = await CapabilityExecutor(backend).execute(_spec())
    assert result.success is True
    assert [name for name, _ in client.calls] == ["add_constraint", "revolve"]
    assert "execute_code" not in backend.adapter.policy.allowed_tools
    assert "delete_all" not in backend.adapter.policy.allowed_tools


@pytest.mark.asyncio
async def test_faust_preflight_blocks_missing_later_capability_without_dispatch() -> None:
    client = Client()
    backend = FaustTypedBackend.from_client(client, _manifest("add_constraint"))
    with pytest.raises(ValueError, match="revolve"):
        await CapabilityExecutor(backend).execute(_spec())
    assert client.calls == []


def test_faust_reports_exact_manifest_capabilities() -> None:
    backend = FaustTypedBackend.from_client(
        Client(),
        _manifest("revolve", "sweep", "loft", "rectangular_pattern", "export_step"),
    )
    assert backend.capabilities == {
        "revolve", "sweep", "loft", "pattern_rectangular", "export_step", "export_stp"
    }
