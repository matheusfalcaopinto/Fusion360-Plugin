from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_core.planner import PlanningRequest, RuleBasedPlanner
from cad_spec.v2 import (
    CadSpecV2,
    ExtrudeOperation,
    OPERATION_ADAPTER,
    SketchCircleOperation,
    legacy_plan_v2_coverage,
    upgrade_legacy_plan_to_v2,
)
from fusion_mcp_adapter.tool_result import ToolDefinition, ToolManifest, ToolResult
from fusion_tool_facade.autodesk_typed_backend import AutodeskTypedBackend


class _NoDispatchClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def list_tools(self):
        raise AssertionError("the test supplies a fixed manifest")

    async def call_tool(self, name, arguments, *, options=None):
        self.calls.append((name, arguments))
        return ToolResult.success(message="unexpected dispatch")


def _autodesk_backend(client: _NoDispatchClient) -> AutodeskTypedBackend:
    return AutodeskTypedBackend.from_client(
        client,
        ToolManifest(
            source="autodesk-prompt-planning-test",
            tools=[
                ToolDefinition(name="fusion_mcp_read"),
                ToolDefinition(name="fusion_mcp_execute"),
            ],
        ),
    )


def test_v2_schema_exposes_reference_categories_without_changing_wire_values() -> None:
    schema = CadSpecV2.model_json_schema()
    definitions = schema["$defs"]

    assert definitions["ComponentRef"]["x-cad-reference-kind"] == "component"
    assert definitions["ProfileRef"]["x-cad-reference-kind"] == "profile"
    assert definitions["AxisRef"]["x-cad-reference-kind"] == "axis"
    assert definitions["OperationIdRef"]["x-cad-reference-kind"] == "operation_id"
    assert definitions["RequirementIdRef"]["x-cad-reference-kind"] == "requirement_id"
    assert definitions["AssertionIdRef"]["x-cad-reference-kind"] == "assertion_id"

    revolve = definitions["RevolveOperation"]["properties"]
    assert revolve["component_ref"] == {"$ref": "#/$defs/ComponentRef"}
    assert revolve["profile_ref"] == {"$ref": "#/$defs/ProfileRef"}
    assert revolve["axis_ref"] == {"$ref": "#/$defs/AxisRef"}
    assert revolve["depends_on"]["items"] == {"$ref": "#/$defs/OperationIdRef"}
    assert revolve["requirement_ids"]["items"] == {"$ref": "#/$defs/RequirementIdRef"}

    operation = OPERATION_ADAPTER.validate_python(
        {
            "id": "shaft_revolve",
            "kind": "feature.revolve",
            "component_ref": "  root  ",
            "profile_ref": " shaft_profile ",
            "axis_ref": " x_axis ",
            "result_name": "shaft_body",
        }
    )
    assert operation.component_ref == "root"
    assert operation.profile_ref == "shaft_profile"
    assert operation.axis_ref == "x_axis"
    assert isinstance(operation.component_ref, str)


@pytest.mark.parametrize("invalid_ref", [{"name": "root"}, "root\nmalicious"])
def test_v2_typed_references_reject_non_wire_or_control_character_values(
    invalid_ref: object,
) -> None:
    with pytest.raises(ValidationError):
        OPERATION_ADAPTER.validate_python(
            {
                "id": "shaft_revolve",
                "kind": "feature.revolve",
                "component_ref": invalid_ref,
                "profile_ref": "shaft_profile",
                "axis_ref": "x_axis",
                "result_name": "shaft_body",
            }
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prompt",
    [
        "build a plate 100 mm 60 mm 6 mm with 5 mm holes 12 mm from edge",
        "build a plate 100 mm 60 mm 6 mm with holes diameter 5 mm at 12 mm edge",
    ],
)
async def test_plate_hole_prompts_expand_to_four_bounded_typed_cuts_without_dispatch(
    prompt: str,
) -> None:
    legacy = await RuleBasedPlanner().plan(PlanningRequest(user_prompt=prompt))
    assert legacy_plan_v2_coverage(legacy) == {
        "complete": True,
        "feature_types": ["extrude_rectangle", "hole_pattern_cut"],
        "normalizable_feature_types": ["extrude_rectangle", "hole_pattern_cut"],
        "unsupported_feature_types": [],
    }

    spec = upgrade_legacy_plan_to_v2(legacy)
    circles = [
        operation
        for operation in spec.operations
        if isinstance(operation, SketchCircleOperation)
    ]
    cuts = [
        operation
        for operation in spec.operations
        if isinstance(operation, ExtrudeOperation) and operation.operation == "cut"
    ]
    assert {tuple(circle.center) for circle in circles} == {
        ("-38 mm", "-18 mm"),
        ("-38 mm", "18 mm"),
        ("38 mm", "-18 mm"),
        ("38 mm", "18 mm"),
    }
    assert len(cuts) == 4
    assert {cut.result_name for cut in cuts} == {"plate_body"}
    assert spec.requirements[0].oracle == "independent"

    client = _NoDispatchClient()
    _autodesk_backend(client).preflight_operations(spec.operations)
    assert client.calls == []


@pytest.mark.asyncio
async def test_spacer_prompt_expands_center_bore_and_preflights_without_dispatch() -> (
    None
):
    legacy = await RuleBasedPlanner().plan(
        PlanningRequest(user_prompt="create cylindrical spacer 20 mm 8 mm 15 mm")
    )
    spec = upgrade_legacy_plan_to_v2(legacy)

    cuts = [
        operation
        for operation in spec.operations
        if isinstance(operation, ExtrudeOperation) and operation.operation == "cut"
    ]
    circles = [
        operation
        for operation in spec.operations
        if isinstance(operation, SketchCircleOperation)
    ]
    assert len(cuts) == 1
    assert cuts[0].result_name == "spacer_body"
    assert tuple(circles[-1].center) == ("0 mm", "0 mm")
    assert circles[-1].diameter == "inner_diameter"

    client = _NoDispatchClient()
    _autodesk_backend(client).preflight_operations(spec.operations)
    assert client.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("prompt", "unsupported"),
    [
        ("create an open box 100 mm 60 mm 40 mm", "box_shell"),
        ("change parameter thickness to 8 mm", "update_parameter"),
    ],
)
async def test_prompt_normalizer_reports_unsupported_recipes_before_building_partial_v2(
    prompt: str,
    unsupported: str,
) -> None:
    legacy = await RuleBasedPlanner().plan(PlanningRequest(user_prompt=prompt))
    coverage = legacy_plan_v2_coverage(legacy)

    assert coverage["complete"] is False
    assert coverage["unsupported_feature_types"] == [unsupported]
    with pytest.raises(ValueError, match=unsupported):
        upgrade_legacy_plan_to_v2(legacy)
