from __future__ import annotations

import pytest

from agent_core.capability_executor import CapabilityExecutor
from cad_spec.v2 import CadSpecV2
from fusion_mcp_adapter.tool_result import ToolDefinition, ToolManifest, ToolResult
from fusion_tool_facade.typed_backend import (
    FAUST_CAPABILITY_PROOFS,
    FAUST_IMPLEMENTED_CAPABILITIES,
    FaustTypedBackend,
)


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
            "intent": "Set one literal parameter",
            "requirements": [
                {
                    "id": "shaft",
                    "description": "shaft exists",
                    "assertion_ids": ["shaft_exists"],
                }
            ],
            "operations": [
                {
                    "id": "set_shaft_diameter",
                    "kind": "parameter.set",
                    "name": "shaft_diameter",
                    "expression": "10 mm",
                    "comment": "lossless Faust literal",
                    "requirement_ids": ["shaft"],
                },
            ],
            "assertions": [
                {
                    "id": "shaft_exists",
                    "kind": "entity_exists",
                    "target_ref": "shaft_body",
                }
            ],
        }
    )


def _lossy_spec() -> CadSpecV2:
    payload = _spec().model_dump(mode="json")
    payload["operations"].append(
        {
            "id": "shaft_revolve",
            "kind": "feature.revolve",
            "component_ref": "root",
            "profile_ref": "profile#0",
            "axis_ref": "x_axis",
            "angle": "360 deg",
            "result_name": "shaft_body",
            "depends_on": ["set_shaft_diameter"],
            "requirement_ids": ["shaft"],
        }
    )
    return CadSpecV2.model_validate(payload)


@pytest.mark.asyncio
async def test_faust_backend_maps_only_curated_tools() -> None:
    client = Client()
    manifest = _manifest(
        "create_parameter", "add_constraint", "revolve", "execute_code", "delete_all"
    )
    backend = FaustTypedBackend.from_client(client, manifest)
    result = await CapabilityExecutor(backend).execute(_spec())
    assert result.success is True
    assert [name for name, _ in client.calls] == ["create_parameter"]
    assert "execute_code" not in backend.adapter.policy.allowed_tools
    assert "delete_all" not in backend.adapter.policy.allowed_tools


@pytest.mark.asyncio
async def test_faust_preflight_blocks_missing_later_capability_without_dispatch() -> (
    None
):
    client = Client()
    backend = FaustTypedBackend.from_client(client, _manifest())
    with pytest.raises(ValueError, match="parameters"):
        await CapabilityExecutor(backend).execute(_spec())
    assert client.calls == []


def test_faust_reports_exact_manifest_capabilities() -> None:
    backend = FaustTypedBackend.from_client(
        Client(),
        _manifest(
            "create_parameter",
            "add_constraint",
            "add_dimension",
            "revolve",
            "sweep",
            "loft",
            "rectangular_pattern",
            "mirror",
            "boolean_operation",
            "add_joint",
            "create_rigid_group",
            "get_physical_properties",
            "check_interference",
            "export_step",
        ),
    )
    assert backend.capabilities == {"parameters"}
    assert FAUST_IMPLEMENTED_CAPABILITIES == {"parameters"}
    assert set(FAUST_CAPABILITY_PROOFS) == {"parameters"}
    assert FAUST_CAPABILITY_PROOFS["parameters"].preserved_fields == (
        "name",
        "expression.value",
        "expression.unit",
        "comment",
    )


def test_faust_rejects_lossy_typed_references_before_dispatch() -> None:
    client = Client()
    backend = FaustTypedBackend.from_client(
        client, _manifest("revolve", "sweep", "loft")
    )
    assert not ({"revolve", "sweep", "loft"} & backend.capabilities)
    with pytest.raises(ValueError, match="lossless capability proof"):
        backend.preflight_operations(_lossy_spec().operations)
    assert client.calls == []


def test_faust_rejects_nonliteral_parameter_expressions_before_dispatch() -> None:
    payload = _spec().model_dump(mode="json")
    payload["operations"][0]["expression"] = "shaft_source"
    spec = CadSpecV2.model_validate(payload)
    client = Client()
    backend = FaustTypedBackend.from_client(client, _manifest("create_parameter"))

    with pytest.raises(ValueError, match="literal numeric unit expression"):
        backend.preflight_operations(spec.operations)

    assert client.calls == []
