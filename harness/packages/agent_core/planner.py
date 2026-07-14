"""Planner implementations."""

from __future__ import annotations

import re
import warnings
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from agent_core.guardrails import raise_if_unsupported_for_planner, validate_planned_spec
from cad_spec.models import (
    AcceptanceTestSpec,
    CadSpec,
    ComponentMetadataSpec,
    ComponentSpec,
    DocumentPolicy,
    FeatureSpec,
    JointSpec,
    OutputSpec,
    ParameterSpec,
)
from memory.schemas import MemoryRecord


class PlanningRequest(BaseModel):
    """Inputs available to the planner."""

    user_prompt: str
    project: str = "default"
    document_state: dict[str, Any] = Field(default_factory=dict)
    memory: list[MemoryRecord] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)


class RuleBasedPlanner:
    """Deterministic planner for v0 skills and benchmarks."""

    async def plan(self, request: PlanningRequest) -> CadSpec:
        """Convert known v0 prompts into a valid CadSpec."""

        prompt = request.user_prompt.lower()
        raise_if_unsupported_for_planner(request.user_prompt)
        if any(phrase in prompt for phrase in ("spacer assembly", "two plates with spacers", "standoff assembly")):
            return _checked(request.user_prompt, _spacer_plate_assembly_spec(prompt))
        if "hinge" in prompt or "revolute" in prompt or "simple hinge assembly" in prompt:
            return _checked(request.user_prompt, _hinge_assembly_spec(prompt))
        if "cnc" in prompt and any(word in prompt for word in ("build", "assembly", "machine", "router")):
            return _checked(request.user_prompt, _desktop_cnc_assembly_spec(prompt))
        if ("nema17" in prompt or "nema 17" in prompt or "stepper motor" in prompt) and any(
            phrase in prompt
            for phrase in (
                "assembly",
                "usable",
                "floating",
                "proper components",
                "actual assembly",
                "complete external",
                "real model",
                "materials",
            )
        ):
            return _checked(request.user_prompt, _nema17_external_assembly_spec(prompt))
        if ("nema17" in prompt or "nema 17" in prompt or "stepper motor" in prompt) and any(
            word in prompt for word in ("polish", "detail", "detailed", "second round", "in depth", "visual")
        ):
            return _checked(request.user_prompt, _nema17_polish_spec(prompt))
        if "nema17" in prompt or "nema 17" in prompt or "stepper motor" in prompt:
            return _checked(request.user_prompt, _nema17_stepper_spec(prompt))
        if "mgn12" in prompt or "mgn 12" in prompt or ("linear rail" in prompt and "carriage" in prompt):
            return _checked(request.user_prompt, _mgn12_linear_rail_spec(prompt))
        if (
            "2020" in prompt
            and any(word in prompt for word in ("profile", "extrusion", "aluminum", "aluminium", "t-slot", "tslot"))
        ) or "20x20 aluminum profile" in prompt or "20x20 aluminium profile" in prompt:
            return _checked(request.user_prompt, _profile2020_aluminum_spec(prompt))
        if "spacer" in prompt or "cylindrical" in prompt:
            return _checked(request.user_prompt, _spacer_spec(prompt))
        if "l bracket" in prompt or "l-bracket" in prompt:
            return _checked(request.user_prompt, _l_bracket_spec(prompt))
        if "open rectangular box" in prompt or "open box" in prompt or "box" in prompt:
            return _checked(request.user_prompt, _box_spec(prompt))
        if "plate" in prompt:
            return _checked(request.user_prompt, _plate_spec(prompt))
        if "cube" in prompt:
            return _checked(request.user_prompt, _cube_spec(prompt))
        if "parameter" in prompt or "change" in prompt or "alter" in prompt:
            return _checked(request.user_prompt, _parameter_edit_spec(prompt))
        raise ValueError(f"RuleBasedPlanner cannot plan this request yet: {request.user_prompt}")


class PromptPlanner(RuleBasedPlanner):
    """Deprecated compatibility alias for :class:`RuleBasedPlanner`.

    This class does not call a model.  Codex remains the external planner.
    """

    def __init__(self, prompt_path: Path | str | None = None) -> None:
        warnings.warn(
            "PromptPlanner is deprecated; use RuleBasedPlanner for legacy CadSpec recipes",
            DeprecationWarning,
            stacklevel=2,
        )
        self.prompt_path = Path(prompt_path) if prompt_path is not None else _default_prompt_path("planner_prompt.md")

    def load_prompt(self) -> str:
        """Return the planner prompt text."""

        return self.prompt_path.read_text(encoding="utf-8")

    async def plan(self, request: PlanningRequest) -> CadSpec:
        """Delegate to the deterministic planner until an LLM provider is configured."""

        return await super().plan(request)


def _default_prompt_path(filename: str) -> Path:
    local = Path("prompts") / filename
    if local.is_file():
        return local
    try:
        from fusion_agent_assets import asset_root

        bundled = asset_root("prompts") / filename
        if bundled.is_file():
            return bundled
    except Exception:
        pass
    return local


def _checked(prompt: str, spec: CadSpec) -> CadSpec:
    validate_planned_spec(prompt, spec)
    return spec


def _spacer_plate_assembly_spec(prompt: str) -> CadSpec:
    component_names = [
        "spacer_top_plate_component",
        "spacer_bottom_plate_component",
        "spacer_standoff_component",
    ]
    body_names = [
        "spacer_top_plate_body",
        "spacer_bottom_plate_body",
        "spacer_standoff_body",
    ]
    occurrence_names = [f"spacer_standoff_{index:02d}_occurrence" for index in range(1, 5)]
    joints = [
        JointSpec(name="spacer_plate_stack_rigid_joint", type="rigid", parent="spacer_bottom_plate_component", child="spacer_top_plate_component", axis="z"),
        *[
            JointSpec(
                name=f"spacer_standoff_{index:02d}_rigid_joint",
                type="rigid",
                parent="spacer_bottom_plate_component",
                child=f"spacer_standoff_{index:02d}_occurrence",
                axis="z",
            )
            for index in range(1, 5)
        ],
    ]
    outputs = [
        OutputSpec(
            name="spacer_plate_assembly_iso_capture",
            path="assembly_samples/spacer_plate_assembly_iso.png",
            view="isometric",
            isolate_prefix="spacer_",
        )
    ]
    return CadSpec(
        intent="create_generic_spacer_plate_assembly",
        units="mm",
        assumptions=[
            "fusion_mechanical_pro policy: final deliverable is component-first with metadata, joints, physical properties, interference check, and screenshot evidence.",
            "Spacer assembly uses custom fabricated plates and a repeated custom standoff component.",
        ],
        document_policy=DocumentPolicy(modify_existing=False, create_checkpoint=True),
        parameters=[
            ParameterSpec(name="spacer_plate_length", expression="100 mm"),
            ParameterSpec(name="spacer_plate_width", expression="60 mm"),
            ParameterSpec(name="spacer_plate_thickness", expression="6 mm"),
            ParameterSpec(name="spacer_plate_gap", expression="25 mm"),
            ParameterSpec(name="spacer_standoff_diameter", expression="8 mm"),
            ParameterSpec(name="spacer_standoff_height", expression="25 mm"),
            ParameterSpec(name="spacer_hole_diameter", expression="5 mm"),
            ParameterSpec(name="spacer_hole_pattern_x", expression="70 mm"),
            ParameterSpec(name="spacer_hole_pattern_y", expression="35 mm"),
        ],
        components=[
            ComponentSpec(
                name="spacer_plate_assembly",
                features=[
                    FeatureSpec(
                        name="spacer_plate_assembly_build",
                        type="spacer_plate_assembly",
                        inputs={
                            "assembly_component": "spacer_plate_assembly",
                            "component_names": component_names,
                            "body_names": body_names,
                            "occurrence_names": occurrence_names,
                            "plate_length": "spacer_plate_length",
                            "plate_width": "spacer_plate_width",
                            "plate_thickness": "spacer_plate_thickness",
                            "plate_gap": "spacer_plate_gap",
                            "standoff_diameter": "spacer_standoff_diameter",
                            "standoff_height": "spacer_standoff_height",
                            "hole_diameter": "spacer_hole_diameter",
                            "hole_pattern_x": "spacer_hole_pattern_x",
                            "hole_pattern_y": "spacer_hole_pattern_y",
                            "placement_offset": ["0 mm", "0 mm", "0 mm"],
                        },
                    )
                ],
            )
        ],
        component_metadata=[
            ComponentMetadataSpec(
                component="spacer_plate_assembly",
                part_number="SPA-ASM-001",
                description="Generic two plate spacer assembly",
                role="assembly",
                source_type="custom",
                physical_material="Aluminum 6061",
                appearance="clear anodized aluminum",
                revision="A",
            ),
            ComponentMetadataSpec(
                component="spacer_top_plate_component",
                part_number="SPA-PLT-TOP-001",
                description="Top spacer plate",
                role="plate",
                source_type="custom",
                physical_material="Aluminum 6061",
                appearance="clear anodized aluminum",
                revision="A",
            ),
            ComponentMetadataSpec(
                component="spacer_bottom_plate_component",
                part_number="SPA-PLT-BOT-001",
                description="Bottom spacer plate",
                role="plate",
                source_type="custom",
                physical_material="Aluminum 6061",
                appearance="clear anodized aluminum",
                revision="A",
            ),
            ComponentMetadataSpec(
                component="spacer_standoff_component",
                part_number="SPA-STO-008-025",
                description="Repeated cylindrical standoff",
                role="standoff",
                source_type="custom",
                physical_material="Aluminum 6061",
                appearance="clear anodized aluminum",
                revision="A",
            ),
        ],
        joints=joints,
        outputs=outputs,
        acceptance_tests=[
            AcceptanceTestSpec(type="component_exists", target="spacer_plate_assembly"),
            AcceptanceTestSpec(type="named_bodies", target=body_names),
            AcceptanceTestSpec(type="target_bounding_box", target="spacer_top_plate_body", target_mm=[100.0, 60.0, 6.0], tolerance_mm=0.1),
            AcceptanceTestSpec(type="target_bounding_box", target="spacer_standoff_body", target_mm=[8.0, 8.0, 25.0], tolerance_mm=0.1),
            AcceptanceTestSpec(type="component_metadata"),
            AcceptanceTestSpec(type="occurrence_contract", target={"occurrence_names": occurrence_names, "component": "spacer_standoff_component", "count": 4}),
            AcceptanceTestSpec(type="joint_contract"),
            AcceptanceTestSpec(type="interference_free"),
            AcceptanceTestSpec(type="physical_properties"),
            AcceptanceTestSpec(type="screenshots_exist"),
            AcceptanceTestSpec(type="named_objects"),
            AcceptanceTestSpec(type="feature_health"),
        ],
    )


def _hinge_assembly_spec(prompt: str) -> CadSpec:
    component_names = [
        "hinge_left_leaf_component",
        "hinge_right_leaf_component",
        "hinge_pin_component",
    ]
    body_names = [
        "hinge_left_leaf_body",
        "hinge_right_leaf_body",
        "hinge_pin_body",
        "hinge_left_knuckle_01_body",
        "hinge_left_knuckle_02_body",
        "hinge_right_knuckle_body",
    ]
    joints = [
        JointSpec(name="hinge_revolute_joint", type="revolute", parent="hinge_left_leaf_component", child="hinge_right_leaf_component", axis="x"),
        JointSpec(name="hinge_pin_rigid_joint", type="rigid", parent="hinge_left_leaf_component", child="hinge_pin_component", axis="x"),
    ]
    outputs = [
        OutputSpec(
            name="hinge_assembly_iso_capture",
            path="assembly_samples/hinge_assembly_iso.png",
            view="isometric",
            isolate_prefix="hinge_",
        )
    ]
    return CadSpec(
        intent="create_generic_hinge_assembly",
        units="mm",
        assumptions=[
            "fusion_mechanical_pro policy: hinge is a kinematic assembly and must prove a revolute joint contract.",
            "Hinge leaves are custom fabricated placeholders, not supplier CAD parts.",
        ],
        document_policy=DocumentPolicy(modify_existing=False, create_checkpoint=True),
        parameters=[
            ParameterSpec(name="hinge_leaf_length", expression="60 mm"),
            ParameterSpec(name="hinge_leaf_width", expression="30 mm"),
            ParameterSpec(name="hinge_leaf_thickness", expression="3 mm"),
            ParameterSpec(name="hinge_pin_diameter", expression="4 mm"),
            ParameterSpec(name="hinge_pin_length", expression="64 mm"),
            ParameterSpec(name="hinge_knuckle_outer_diameter", expression="8 mm"),
            ParameterSpec(name="hinge_knuckle_length", expression="18 mm"),
            ParameterSpec(name="hinge_leaf_gap", expression="2 mm"),
        ],
        components=[
            ComponentSpec(
                name="hinge_assembly",
                features=[
                    FeatureSpec(
                        name="hinge_assembly_build",
                        type="hinge_assembly",
                        inputs={
                            "assembly_component": "hinge_assembly",
                            "component_names": component_names,
                            "body_names": body_names,
                            "leaf_length": "hinge_leaf_length",
                            "leaf_width": "hinge_leaf_width",
                            "leaf_thickness": "hinge_leaf_thickness",
                            "pin_diameter": "hinge_pin_diameter",
                            "pin_length": "hinge_pin_length",
                            "knuckle_outer_diameter": "hinge_knuckle_outer_diameter",
                            "knuckle_length": "hinge_knuckle_length",
                            "leaf_gap": "hinge_leaf_gap",
                            "placement_offset": ["0 mm", "90 mm", "0 mm"],
                        },
                    )
                ],
            )
        ],
        component_metadata=[
            ComponentMetadataSpec(
                component="hinge_assembly",
                part_number="HNG-ASM-001",
                description="Generic simple revolute hinge assembly",
                role="assembly",
                source_type="custom",
                physical_material="Steel",
                appearance="brushed steel",
                revision="A",
            ),
            ComponentMetadataSpec(
                component="hinge_left_leaf_component",
                part_number="HNG-LFT-001",
                description="Left hinge leaf",
                role="leaf",
                source_type="custom",
                physical_material="Steel",
                appearance="brushed steel",
                revision="A",
            ),
            ComponentMetadataSpec(
                component="hinge_right_leaf_component",
                part_number="HNG-RGT-001",
                description="Right hinge leaf",
                role="leaf",
                source_type="custom",
                physical_material="Steel",
                appearance="brushed steel",
                revision="A",
            ),
            ComponentMetadataSpec(
                component="hinge_pin_component",
                part_number="HNG-PIN-004-064",
                description="Hinge pin",
                role="pin",
                source_type="custom",
                physical_material="Steel",
                appearance="polished steel",
                revision="A",
            ),
        ],
        joints=joints,
        outputs=outputs,
        acceptance_tests=[
            AcceptanceTestSpec(type="component_exists", target="hinge_assembly"),
            AcceptanceTestSpec(type="named_bodies", target=body_names),
            AcceptanceTestSpec(type="target_bounding_box", target="hinge_left_leaf_body", target_mm=[60.0, 30.0, 3.0], tolerance_mm=0.1),
            AcceptanceTestSpec(type="target_bounding_box", target="hinge_pin_body", target_mm=[64.0, 4.0, 4.0], tolerance_mm=0.1),
            AcceptanceTestSpec(type="component_metadata"),
            AcceptanceTestSpec(type="occurrence_contract", target={"component_names": component_names, "count": 3}),
            AcceptanceTestSpec(type="joint_contract"),
            AcceptanceTestSpec(type="interference_free", target={"allowed_contact_pairs": [["hinge_pin_body", "hinge_left_knuckle_01_body"], ["hinge_pin_body", "hinge_left_knuckle_02_body"], ["hinge_pin_body", "hinge_right_knuckle_body"]]}),
            AcceptanceTestSpec(type="physical_properties"),
            AcceptanceTestSpec(type="screenshots_exist"),
            AcceptanceTestSpec(type="named_objects"),
            AcceptanceTestSpec(type="feature_health"),
        ],
    )


def _cube_spec(prompt: str) -> CadSpec:
    size = _first_length(prompt, default="10 mm")
    return CadSpec(
        intent="create_parametric_part",
        units="mm",
        assumptions=["Cube is centered at the origin on the XY plane and extruded along Z+."],
        document_policy=DocumentPolicy(modify_existing=False, create_checkpoint=True),
        parameters=[ParameterSpec(name="cube_size", expression=size)],
        components=[
            ComponentSpec(
                name="cube_part",
                features=[
                    FeatureSpec(
                        name="cube_base_extrude",
                        type="extrude_rectangle",
                        operation="new_body",
                        inputs={
                            "sketch_name": "cube_profile_sketch",
                            "plane": "XY",
                            "center": ["0 mm", "0 mm"],
                            "width": "cube_size",
                            "height": "cube_size",
                            "distance": "cube_size",
                            "body_name": "cube_body",
                        },
                    )
                ],
            )
        ],
        acceptance_tests=_base_acceptance([size, size, size], ["cube_size"]),
    )


def _plate_spec(prompt: str) -> CadSpec:
    length, width, thickness = _triple_dimensions(prompt, defaults=("100 mm", "60 mm", "6 mm"))
    hole_diameter = _length_after(prompt, "hole", default="5 mm")
    hole_offset = _length_after(prompt, "edge", default="12 mm")
    has_holes = "hole" in prompt
    features = [
        FeatureSpec(
            name="base_plate_extrude",
            type="extrude_rectangle",
            operation="new_body",
            inputs={
                "sketch_name": "base_profile_sketch",
                "plane": "XY",
                "center": ["0 mm", "0 mm"],
                "width": "plate_length",
                "height": "plate_width",
                "distance": "plate_thickness",
                "body_name": "plate_body",
            },
        )
    ]
    parameters = [
        ParameterSpec(name="plate_length", expression=length),
        ParameterSpec(name="plate_width", expression=width),
        ParameterSpec(name="plate_thickness", expression=thickness),
    ]
    acceptance = _base_acceptance([length, width, thickness], ["plate_length", "plate_width", "plate_thickness"])
    if has_holes:
        parameters.extend(
            [
                ParameterSpec(name="hole_diameter", expression=hole_diameter),
                ParameterSpec(name="hole_offset", expression=hole_offset),
            ]
        )
        features.append(
            FeatureSpec(
                name="corner_hole_cut",
                type="hole_pattern_cut",
                operation="cut",
                inputs={
                    "sketch_name": "hole_profile_sketch",
                    "plane": "XY",
                    "target_body": "plate_body",
                    "diameter": "hole_diameter",
                    "offset": "hole_offset",
                    "count": 4,
                    "distance": "plate_thickness",
                },
            )
        )
        acceptance.append(AcceptanceTestSpec(type="hole_count", target=4))
    return CadSpec(
        intent="create_parametric_part",
        units="mm",
        assumptions=["Plate origin is centered; holes are symmetric corner holes."],
        document_policy=DocumentPolicy(modify_existing=False, create_checkpoint=True),
        parameters=parameters,
        components=[ComponentSpec(name="mounting_plate", features=features)],
        acceptance_tests=acceptance,
    )


def _spacer_spec(prompt: str) -> CadSpec:
    lengths = _all_lengths(prompt)
    outer = lengths[0] if len(lengths) > 0 else "20 mm"
    inner = lengths[1] if len(lengths) > 1 else "8 mm"
    height = lengths[2] if len(lengths) > 2 else "15 mm"
    return CadSpec(
        intent="create_parametric_part",
        units="mm",
        assumptions=["Spacer is centered on XY and extruded along Z+."],
        parameters=[
            ParameterSpec(name="outer_diameter", expression=outer),
            ParameterSpec(name="inner_diameter", expression=inner),
            ParameterSpec(name="spacer_height", expression=height),
        ],
        components=[
            ComponentSpec(
                name="cylindrical_spacer",
                features=[
                    FeatureSpec(
                        name="spacer_body_extrude",
                        type="extrude_cylinder",
                        operation="new_body",
                        inputs={
                            "sketch_name": "spacer_outer_profile_sketch",
                            "plane": "XY",
                            "center": ["0 mm", "0 mm"],
                            "diameter": "outer_diameter",
                            "distance": "spacer_height",
                            "body_name": "spacer_body",
                        },
                    ),
                    FeatureSpec(
                        name="spacer_inner_hole_cut",
                        type="center_hole_cut",
                        operation="cut",
                        inputs={
                            "sketch_name": "spacer_inner_profile_sketch",
                            "plane": "XY",
                            "target_body": "spacer_body",
                            "diameter": "inner_diameter",
                            "distance": "spacer_height",
                            "count": 1,
                        },
                    ),
                ],
            )
        ],
        acceptance_tests=_base_acceptance([outer, outer, height], ["outer_diameter", "inner_diameter", "spacer_height"])
        + [AcceptanceTestSpec(type="hole_count", target=1)],
    )


def _nema17_stepper_spec(prompt: str) -> CadSpec:
    body_length = _nema17_body_length(prompt)
    return _nema17_base_spec(body_length)


def _nema17_base_spec(body_length: str) -> CadSpec:
    return CadSpec(
        intent="create_parametric_part",
        units="mm",
        assumptions=[
            "NEMA17 frame dimensions use the common 42.3 mm square face and 31 mm mounting-hole spacing.",
            "Motor body length varies by manufacturer; this model uses a typical 40 mm body unless explicitly requested.",
            "Shaft protrusion is modeled as 24 mm from the front face with a 5 mm diameter shaft.",
        ],
        document_policy=DocumentPolicy(modify_existing=False, create_checkpoint=True),
        parameters=[
            ParameterSpec(name="nema17_face_width", expression="42.3 mm"),
            ParameterSpec(name="nema17_body_length", expression=body_length),
            ParameterSpec(name="nema17_pilot_diameter", expression="22 mm"),
            ParameterSpec(name="nema17_pilot_length", expression="2 mm"),
            ParameterSpec(name="nema17_shaft_diameter", expression="5 mm"),
            ParameterSpec(name="nema17_shaft_length", expression="24 mm"),
            ParameterSpec(name="nema17_mount_hole_spacing", expression="31 mm"),
            ParameterSpec(name="nema17_mount_hole_diameter", expression="3 mm"),
            ParameterSpec(name="nema17_overall_depth", expression=f"{_to_float_mm(body_length) + 24.0:g} mm"),
        ],
        components=[
            ComponentSpec(
                name="nema17_stepper_motor",
                features=[
                    FeatureSpec(
                        name="nema17_motor_build",
                        type="nema17_stepper_motor",
                        operation="new_body",
                        inputs={
                            "body_name": "nema17_motor_body",
                            "face_width": "nema17_face_width",
                            "body_length": "nema17_body_length",
                            "pilot_diameter": "nema17_pilot_diameter",
                            "pilot_length": "nema17_pilot_length",
                            "shaft_diameter": "nema17_shaft_diameter",
                            "shaft_length": "nema17_shaft_length",
                            "mount_hole_spacing": "nema17_mount_hole_spacing",
                            "mount_hole_diameter": "nema17_mount_hole_diameter",
                            "overall_depth": "nema17_overall_depth",
                            "mount_hole_count": 4,
                        },
                    )
                ],
            )
        ],
        acceptance_tests=[
            AcceptanceTestSpec(type="named_bodies", target=["nema17_motor_body"]),
            AcceptanceTestSpec(
                type="target_bounding_box",
                target="nema17_motor_body",
                target_mm=[42.3, 42.3, _to_float_mm(body_length) + 24.0],
                tolerance_mm=0.1,
            ),
            AcceptanceTestSpec(
                type="named_parameters",
                target=[
                    "nema17_face_width",
                    "nema17_body_length",
                    "nema17_pilot_diameter",
                    "nema17_pilot_length",
                    "nema17_shaft_diameter",
                    "nema17_shaft_length",
                    "nema17_mount_hole_spacing",
                    "nema17_mount_hole_diameter",
                    "nema17_overall_depth",
                ],
            ),
            AcceptanceTestSpec(
                type="nema17_dimensions",
                target={
                    "mount_hole_count": 4,
                    "mount_hole_spacing_mm": [31.0, 31.0],
                    "mount_hole_diameter_mm": 3.0,
                    "pilot_diameter_mm": 22.0,
                    "shaft_diameter_mm": 5.0,
                },
                tolerance_mm=0.1,
            ),
            AcceptanceTestSpec(type="named_objects", target=True),
            AcceptanceTestSpec(type="feature_health", target=True),
        ],
    )


def _nema17_polish_spec(prompt: str) -> CadSpec:
    body_length = _nema17_body_length(prompt)
    spec = _nema17_base_spec(body_length)
    polish_body_names = _nema17_polish_body_names()
    spec.intent = "polish_existing_parametric_part"
    spec.document_policy.modify_existing = True
    spec.assumptions.extend(
        [
            "Second-round polish preserves the verified motor body dimensions and adds named decorative detail bodies.",
            "Wires and connector are decorative bodies excluded from the core motor body bounding-box verifier.",
        ]
    )
    spec.parameters.extend(
        [
            ParameterSpec(name="nema17_detail_projection", expression="0.18 mm"),
            ParameterSpec(name="nema17_side_panel_projection", expression="0.12 mm"),
            ParameterSpec(name="nema17_lamination_band_height", expression="0.18 mm"),
            ParameterSpec(name="nema17_hole_shadow_diameter", expression="2.2 mm"),
            ParameterSpec(name="nema17_pilot_relief_diameter", expression="25 mm"),
            ParameterSpec(name="nema17_connector_width", expression="16 mm"),
            ParameterSpec(name="nema17_connector_depth", expression="4 mm"),
            ParameterSpec(name="nema17_connector_height", expression="5 mm"),
            ParameterSpec(name="nema17_wire_length", expression="26 mm"),
            ParameterSpec(name="nema17_wire_diameter", expression="1 mm"),
        ]
    )
    spec.components[0].features.append(
        FeatureSpec(
            name="nema17_visual_polish_build",
            type="nema17_visual_polish",
            operation="modify",
            inputs={
                "target_body": "nema17_motor_body",
                "face_width": "nema17_face_width",
                "body_length": "nema17_body_length",
                "overall_depth": "nema17_overall_depth",
                "mount_hole_spacing": "nema17_mount_hole_spacing",
                "mount_hole_diameter": "nema17_mount_hole_diameter",
                "pilot_diameter": "nema17_pilot_diameter",
                "shaft_diameter": "nema17_shaft_diameter",
                "detail_projection": "nema17_detail_projection",
                "side_panel_projection": "nema17_side_panel_projection",
                "lamination_band_height": "nema17_lamination_band_height",
                "hole_shadow_diameter": "nema17_hole_shadow_diameter",
                "pilot_relief_diameter": "nema17_pilot_relief_diameter",
                "connector_width": "nema17_connector_width",
                "connector_depth": "nema17_connector_depth",
                "connector_height": "nema17_connector_height",
                "wire_length": "nema17_wire_length",
                "wire_diameter": "nema17_wire_diameter",
                "lamination_ring_count": 18,
                "wire_count": 4,
                "body_names": polish_body_names,
            },
        )
    )
    spec.acceptance_tests.append(
        AcceptanceTestSpec(
            type="nema17_polish_details",
            target={
                "min_lamination_bodies": 72,
                "wire_count": 4,
                "screw_shadow_count": 4,
                "required_bodies": polish_body_names,
            },
        )
    )
    return spec


def _nema17_polish_body_names() -> list[str]:
    names = [
        "nema17_side_panel_pos_x",
        "nema17_side_panel_neg_x",
        "nema17_side_panel_pos_y",
        "nema17_side_panel_neg_y",
        "nema17_pilot_relief_shadow",
        "nema17_rear_connector_body",
        "nema17_wire_red",
        "nema17_wire_blue",
        "nema17_wire_green",
        "nema17_wire_black",
    ]
    names.extend(f"nema17_mount_hole_shadow_{index:02d}" for index in range(1, 5))
    for ring_index in range(1, 19):
        names.extend(
            [
                f"nema17_lamination_ring_{ring_index:02d}_pos_x",
                f"nema17_lamination_ring_{ring_index:02d}_neg_x",
                f"nema17_lamination_ring_{ring_index:02d}_pos_y",
                f"nema17_lamination_ring_{ring_index:02d}_neg_y",
            ]
        )
    return names


def _nema17_external_assembly_spec(prompt: str) -> CadSpec:
    body_length = _nema17_body_length(prompt)
    component_names = _nema17_assembly_component_names()
    body_names = _nema17_assembly_body_names()
    parameter_names = [
        "nema17_face_width",
        "nema17_body_length",
        "nema17_front_plate_thickness",
        "nema17_rear_plate_thickness",
        "nema17_pilot_diameter",
        "nema17_pilot_length",
        "nema17_shaft_diameter",
        "nema17_shaft_length",
        "nema17_mount_hole_spacing",
        "nema17_mount_hole_diameter",
        "nema17_connector_width",
        "nema17_connector_height",
        "nema17_connector_depth",
        "nema17_wire_length",
        "nema17_wire_diameter",
    ]
    return CadSpec(
        intent="create_component_owned_external_assembly",
        units="mm",
        assumptions=[
            "Build a replacement visible NEMA17 external assembly with component-owned bodies, not root-level decorative solids.",
            "Hide earlier loose NEMA17 bodies non-destructively so the active visible model is the corrected assembly.",
            "Model the motor as a 42.3 mm square, 40 mm body-length class NEMA17 with a 5 mm shaft and 31 mm mounting pattern.",
        ],
        document_policy=DocumentPolicy(modify_existing=True, create_checkpoint=True),
        parameters=[
            ParameterSpec(name="nema17_face_width", expression="42.3 mm"),
            ParameterSpec(name="nema17_body_length", expression=body_length),
            ParameterSpec(name="nema17_front_plate_thickness", expression="3 mm"),
            ParameterSpec(name="nema17_rear_plate_thickness", expression="3 mm"),
            ParameterSpec(name="nema17_pilot_diameter", expression="22 mm"),
            ParameterSpec(name="nema17_pilot_length", expression="2 mm"),
            ParameterSpec(name="nema17_shaft_diameter", expression="5 mm"),
            ParameterSpec(name="nema17_shaft_length", expression="24 mm"),
            ParameterSpec(name="nema17_mount_hole_spacing", expression="31 mm"),
            ParameterSpec(name="nema17_mount_hole_diameter", expression="3 mm"),
            ParameterSpec(name="nema17_connector_width", expression="16 mm"),
            ParameterSpec(name="nema17_connector_height", expression="5 mm"),
            ParameterSpec(name="nema17_connector_depth", expression="4 mm"),
            ParameterSpec(name="nema17_wire_length", expression="26 mm"),
            ParameterSpec(name="nema17_wire_diameter", expression="1 mm"),
        ],
        components=[
            ComponentSpec(
                name="nema17_external_assembly",
                features=[
                    FeatureSpec(
                        name="nema17_external_assembly_build",
                        type="nema17_external_assembly",
                        operation="new_assembly",
                        inputs={
                            "assembly_component": "nema17_external_assembly",
                            "face_width": "nema17_face_width",
                            "body_length": "nema17_body_length",
                            "front_plate_thickness": "nema17_front_plate_thickness",
                            "rear_plate_thickness": "nema17_rear_plate_thickness",
                            "pilot_diameter": "nema17_pilot_diameter",
                            "pilot_length": "nema17_pilot_length",
                            "shaft_diameter": "nema17_shaft_diameter",
                            "shaft_length": "nema17_shaft_length",
                            "mount_hole_spacing": "nema17_mount_hole_spacing",
                            "mount_hole_diameter": "nema17_mount_hole_diameter",
                            "connector_width": "nema17_connector_width",
                            "connector_height": "nema17_connector_height",
                            "connector_depth": "nema17_connector_depth",
                            "wire_length": "nema17_wire_length",
                            "wire_diameter": "nema17_wire_diameter",
                            "lamination_count": 20,
                            "component_names": component_names,
                            "body_names": body_names,
                        },
                    )
                ],
            )
        ],
        acceptance_tests=[
            AcceptanceTestSpec(type="component_exists", target="nema17_external_assembly"),
            *[AcceptanceTestSpec(type="component_exists", target=name) for name in component_names],
            AcceptanceTestSpec(type="named_bodies", target=body_names),
            AcceptanceTestSpec(type="target_bounding_box", target="nema17_front_endplate_body", target_mm=[42.3, 42.3, 3.0], tolerance_mm=0.1),
            AcceptanceTestSpec(type="target_bounding_box", target="nema17_rear_endplate_body", target_mm=[42.3, 42.3, 3.0], tolerance_mm=0.1),
            AcceptanceTestSpec(type="target_bounding_box", target="nema17_front_pilot_boss_body", target_mm=[22.0, 22.0, 2.0], tolerance_mm=0.1),
            AcceptanceTestSpec(type="target_bounding_box", target="nema17_shaft_body", target_mm=[5.0, 5.0, 24.0], tolerance_mm=0.1),
            AcceptanceTestSpec(type="named_parameters", target=parameter_names),
            AcceptanceTestSpec(
                type="nema17_dimensions",
                target={
                    "mount_hole_count": 4,
                    "mount_hole_spacing_mm": [31.0, 31.0],
                    "mount_hole_diameter_mm": 3.0,
                    "pilot_diameter_mm": 22.0,
                    "shaft_diameter_mm": 5.0,
                },
                tolerance_mm=0.1,
            ),
            AcceptanceTestSpec(
                type="nema17_external_assembly",
                target={
                    "assembly_component": "nema17_external_assembly",
                    "required_components": component_names,
                    "required_bodies": body_names,
                    "min_stator_lamination_count": 20,
                    "wire_count": 4,
                    "max_legacy_visible_nema17_body_count": 0,
                },
            ),
            AcceptanceTestSpec(type="named_objects", target=True),
            AcceptanceTestSpec(type="feature_health", target=True),
        ],
    )


def _nema17_assembly_component_names() -> list[str]:
    return [
        "nema17_front_endplate_component",
        "nema17_stator_stack_component",
        "nema17_rear_endplate_component",
        "nema17_shaft_component",
        "nema17_rear_connector_component",
        "nema17_wiring_component",
    ]


def _nema17_assembly_body_names() -> list[str]:
    names = [
        "nema17_front_endplate_body",
        "nema17_front_pilot_boss_body",
        "nema17_rear_endplate_body",
        "nema17_shaft_body",
        "nema17_rear_connector_body",
        "nema17_connector_pin_01",
        "nema17_connector_pin_02",
        "nema17_connector_pin_03",
        "nema17_connector_pin_04",
        "nema17_wire_red",
        "nema17_wire_blue",
        "nema17_wire_green",
        "nema17_wire_black",
    ]
    names.extend(f"nema17_stator_lamination_{index:02d}_body" for index in range(1, 21))
    return names


def _profile2020_aluminum_spec(prompt: str) -> CadSpec:
    length = _first_length(prompt, default="200 mm")
    parameter_names = [
        "profile2020_length",
        "profile2020_size",
        "profile2020_slot_width",
        "profile2020_slot_depth",
        "profile2020_slot_cavity_width",
        "profile2020_center_bore_diameter",
        "profile2020_lip_thickness",
        "profile2020_corner_radius",
    ]
    return CadSpec(
        intent="create_metric_2020_tslot_profile",
        units="mm",
        assumptions=[
            "Model a metric 20-series 20x20 aluminum T-slot profile with four open 6 mm slots.",
            "Length is explicitly 200 mm unless another unit-qualified length is supplied.",
            "Profile is added as a separate named component in the active document, offset from existing geometry.",
        ],
        document_policy=DocumentPolicy(modify_existing=True, create_checkpoint=True),
        parameters=[
            ParameterSpec(name="profile2020_length", expression=length),
            ParameterSpec(name="profile2020_size", expression="20 mm"),
            ParameterSpec(name="profile2020_slot_width", expression="6 mm"),
            ParameterSpec(name="profile2020_slot_depth", expression="5.5 mm"),
            ParameterSpec(name="profile2020_slot_cavity_width", expression="12 mm"),
            ParameterSpec(name="profile2020_center_bore_diameter", expression="5 mm"),
            ParameterSpec(name="profile2020_lip_thickness", expression="1.5 mm"),
            ParameterSpec(name="profile2020_corner_radius", expression="1 mm"),
        ],
        components=[
            ComponentSpec(
                name="profile2020_aluminum_component",
                features=[
                    FeatureSpec(
                        name="profile2020_detailed_extrusion_build",
                        type="profile2020_aluminum_extrusion",
                        operation="new_body",
                        inputs={
                            "component": "profile2020_aluminum_component",
                            "body_name": "profile2020_aluminum_body",
                            "length": "profile2020_length",
                            "size": "profile2020_size",
                            "slot_width": "profile2020_slot_width",
                            "slot_depth": "profile2020_slot_depth",
                            "slot_cavity_width": "profile2020_slot_cavity_width",
                            "center_bore_diameter": "profile2020_center_bore_diameter",
                            "lip_thickness": "profile2020_lip_thickness",
                            "corner_radius": "profile2020_corner_radius",
                            "slot_count": 4,
                            "web_relief_count": 4,
                            "placement_offset": ["70 mm", "0 mm", "0 mm"],
                        },
                    )
                ],
            )
        ],
        acceptance_tests=[
            AcceptanceTestSpec(type="component_exists", target="profile2020_aluminum_component"),
            AcceptanceTestSpec(type="named_bodies", target=["profile2020_aluminum_body"]),
            AcceptanceTestSpec(
                type="target_bounding_box",
                target="profile2020_aluminum_body",
                target_mm=[20.0, 20.0, _to_float_mm(length)],
                tolerance_mm=0.1,
            ),
            AcceptanceTestSpec(type="named_parameters", target=parameter_names),
            AcceptanceTestSpec(
                type="profile2020_details",
                target={
                    "component": "profile2020_aluminum_component",
                    "body": "profile2020_aluminum_body",
                    "size_mm": 20.0,
                    "length_mm": _to_float_mm(length),
                    "slot_count": 4,
                    "slot_width_mm": 6.0,
                    "slot_depth_mm": 5.5,
                    "center_bore_diameter_mm": 5.0,
                    "web_relief_count": 4,
                },
                tolerance_mm=0.1,
            ),
            AcceptanceTestSpec(type="named_objects", target=True),
            AcceptanceTestSpec(type="feature_health", target=True),
        ],
    )


def _mgn12_linear_rail_spec(prompt: str) -> CadSpec:
    length = _first_length(prompt, default="200 mm")
    component_names = [
        "mgn12_rail_component",
        "mgn12_carriage_component",
        "mgn12_end_stop_component",
    ]
    body_names = [
        "mgn12_rail_body",
        "mgn12_carriage_top_body",
        "mgn12_carriage_left_skirt_body",
        "mgn12_carriage_right_skirt_body",
        "mgn12_carriage_front_end_cap_body",
        "mgn12_carriage_rear_end_cap_body",
        "mgn12_ball_return_left_body",
        "mgn12_ball_return_right_body",
        "mgn12_front_rail_stop_body",
        "mgn12_rear_rail_stop_body",
    ]
    parameter_names = [
        "mgn12_rail_length",
        "mgn12_rail_width",
        "mgn12_rail_height",
        "mgn12_rail_hole_pitch",
        "mgn12_rail_end_hole_offset",
        "mgn12_rail_hole_diameter",
        "mgn12_rail_counterbore_diameter",
        "mgn12_rail_counterbore_depth",
        "mgn12_carriage_length",
        "mgn12_carriage_width",
        "mgn12_carriage_total_height",
        "mgn12_carriage_top_height",
        "mgn12_carriage_mount_x_spacing",
        "mgn12_carriage_mount_y_spacing",
        "mgn12_carriage_mount_thread_diameter",
    ]
    return CadSpec(
        intent="create_mgn12_linear_rail_with_carriage",
        units="mm",
        assumptions=[
            "Model a 200 mm MGN12 guide rail with one MGN12H-style carriage block.",
            "MGN12 rail uses 12 mm width, 8 mm rail height, and 25 mm rail mounting-hole pitch.",
            "MGN12H carriage uses 27 mm width, 45.4 mm length, 13 mm assembly height, and 20 x 32.4 mm M3 top-hole spacing.",
            "Assembly is added as separate named components in the active document without altering existing NEMA17 or 2020 profile components.",
        ],
        document_policy=DocumentPolicy(modify_existing=True, create_checkpoint=True),
        parameters=[
            ParameterSpec(name="mgn12_rail_length", expression=length),
            ParameterSpec(name="mgn12_rail_width", expression="12 mm"),
            ParameterSpec(name="mgn12_rail_height", expression="8 mm"),
            ParameterSpec(name="mgn12_rail_hole_pitch", expression="25 mm"),
            ParameterSpec(name="mgn12_rail_end_hole_offset", expression="12.5 mm"),
            ParameterSpec(name="mgn12_rail_hole_diameter", expression="3.5 mm"),
            ParameterSpec(name="mgn12_rail_counterbore_diameter", expression="6 mm"),
            ParameterSpec(name="mgn12_rail_counterbore_depth", expression="3.5 mm"),
            ParameterSpec(name="mgn12_carriage_length", expression="45.4 mm"),
            ParameterSpec(name="mgn12_carriage_width", expression="27 mm"),
            ParameterSpec(name="mgn12_carriage_total_height", expression="13 mm"),
            ParameterSpec(name="mgn12_carriage_top_height", expression="5 mm"),
            ParameterSpec(name="mgn12_carriage_mount_x_spacing", expression="32.4 mm"),
            ParameterSpec(name="mgn12_carriage_mount_y_spacing", expression="20 mm"),
            ParameterSpec(name="mgn12_carriage_mount_thread_diameter", expression="3 mm"),
        ],
        components=[
            ComponentSpec(
                name="mgn12_linear_rail_assembly",
                features=[
                    FeatureSpec(
                        name="mgn12_linear_rail_assembly_build",
                        type="mgn12_linear_rail_assembly",
                        operation="new_assembly",
                        inputs={
                            "assembly_component": "mgn12_linear_rail_assembly",
                            "rail_length": "mgn12_rail_length",
                            "rail_width": "mgn12_rail_width",
                            "rail_height": "mgn12_rail_height",
                            "rail_hole_pitch": "mgn12_rail_hole_pitch",
                            "rail_end_hole_offset": "mgn12_rail_end_hole_offset",
                            "rail_hole_diameter": "mgn12_rail_hole_diameter",
                            "rail_counterbore_diameter": "mgn12_rail_counterbore_diameter",
                            "rail_counterbore_depth": "mgn12_rail_counterbore_depth",
                            "carriage_length": "mgn12_carriage_length",
                            "carriage_width": "mgn12_carriage_width",
                            "carriage_total_height": "mgn12_carriage_total_height",
                            "carriage_top_height": "mgn12_carriage_top_height",
                            "carriage_mount_x_spacing": "mgn12_carriage_mount_x_spacing",
                            "carriage_mount_y_spacing": "mgn12_carriage_mount_y_spacing",
                            "carriage_mount_thread_diameter": "mgn12_carriage_mount_thread_diameter",
                            "component_names": component_names,
                            "body_names": body_names,
                            "placement_offset": ["0 mm", "110 mm", "0 mm"],
                        },
                    )
                ],
            )
        ],
        acceptance_tests=[
            AcceptanceTestSpec(type="component_exists", target="mgn12_linear_rail_assembly"),
            *[AcceptanceTestSpec(type="component_exists", target=name) for name in component_names],
            AcceptanceTestSpec(type="named_bodies", target=body_names),
            AcceptanceTestSpec(
                type="target_bounding_box",
                target="mgn12_rail_body",
                target_mm=[_to_float_mm(length), 12.0, 8.0],
                tolerance_mm=0.15,
            ),
            AcceptanceTestSpec(
                type="target_bounding_box",
                target="mgn12_carriage_top_body",
                target_mm=[45.4, 27.0, 5.0],
                tolerance_mm=0.15,
            ),
            AcceptanceTestSpec(type="named_parameters", target=parameter_names),
            AcceptanceTestSpec(
                type="mgn12_linear_rail_assembly",
                target={
                    "assembly_component": "mgn12_linear_rail_assembly",
                    "required_components": component_names,
                    "required_bodies": body_names,
                    "rail_length_mm": _to_float_mm(length),
                    "rail_width_mm": 12.0,
                    "rail_height_mm": 8.0,
                    "rail_hole_pitch_mm": 25.0,
                    "rail_mount_hole_count": int(_to_float_mm(length) // 25),
                    "rail_counterbore_count": int(_to_float_mm(length) // 25),
                    "carriage_length_mm": 45.4,
                    "carriage_width_mm": 27.0,
                    "carriage_total_height_mm": 13.0,
                    "carriage_mount_hole_count": 4,
                    "carriage_mount_spacing_mm": [32.4, 20.0],
                    "max_legacy_visible_mgn12_body_count": 0,
                },
                tolerance_mm=0.15,
            ),
            AcceptanceTestSpec(type="named_objects", target=True),
            AcceptanceTestSpec(type="feature_health", target=True),
        ],
    )


def _desktop_cnc_assembly_spec(prompt: str) -> CadSpec:
    component_names = [
        "desktop_cnc_frame_component",
        "desktop_cnc_y_axis_component",
        "desktop_cnc_x_axis_component",
        "desktop_cnc_z_axis_component",
        "desktop_cnc_motion_component",
        "desktop_cnc_spindle_component",
        "desktop_cnc_electronics_component",
    ]
    body_names = _desktop_cnc_body_names()
    parameter_names = [
        "cnc_frame_width",
        "cnc_frame_depth",
        "cnc_gantry_height",
        "cnc_profile_size",
        "cnc_rail_length",
        "cnc_z_rail_length",
        "cnc_rail_width",
        "cnc_rail_height",
        "cnc_motor_face_width",
        "cnc_motor_body_length",
        "cnc_motor_shaft_diameter",
        "cnc_motor_shaft_length",
        "cnc_leadscrew_diameter",
        "cnc_coupler_diameter",
        "cnc_coupler_length",
        "cnc_plate_thickness",
        "cnc_spoilboard_length",
        "cnc_spoilboard_width",
        "cnc_spoilboard_thickness",
        "cnc_spindle_diameter",
        "cnc_spindle_length",
        "cnc_work_area_x",
        "cnc_work_area_y",
        "cnc_work_area_z",
    ]
    return CadSpec(
        intent="create_component_owned_desktop_cnc_assembly",
        units="mm",
        assumptions=[
            "Build a compact desktop CNC router around metric 20-series 20x20 aluminum profiles.",
            "Use MGN12-style rails for X/Y/Z guidance, NEMA17 motors for three axes, and T8-class 8 mm lead screws with 5-to-8 mm couplers.",
            "Model added machine parts as component-owned assembly bodies; earlier standalone NEMA17, 2020, and MGN12 reference parts are preserved.",
            "Default work envelope is 180 x 140 x 70 mm inside a 260 x 220 mm frame.",
        ],
        document_policy=DocumentPolicy(modify_existing=True, create_checkpoint=True),
        parameters=[
            ParameterSpec(name="cnc_frame_width", expression="260 mm"),
            ParameterSpec(name="cnc_frame_depth", expression="220 mm"),
            ParameterSpec(name="cnc_gantry_height", expression="170 mm"),
            ParameterSpec(name="cnc_profile_size", expression="20 mm"),
            ParameterSpec(name="cnc_rail_length", expression="200 mm"),
            ParameterSpec(name="cnc_z_rail_length", expression="120 mm"),
            ParameterSpec(name="cnc_rail_width", expression="12 mm"),
            ParameterSpec(name="cnc_rail_height", expression="8 mm"),
            ParameterSpec(name="cnc_motor_face_width", expression="42.3 mm"),
            ParameterSpec(name="cnc_motor_body_length", expression="40 mm"),
            ParameterSpec(name="cnc_motor_shaft_diameter", expression="5 mm"),
            ParameterSpec(name="cnc_motor_shaft_length", expression="24 mm"),
            ParameterSpec(name="cnc_leadscrew_diameter", expression="8 mm"),
            ParameterSpec(name="cnc_coupler_diameter", expression="19 mm"),
            ParameterSpec(name="cnc_coupler_length", expression="25 mm"),
            ParameterSpec(name="cnc_plate_thickness", expression="6 mm"),
            ParameterSpec(name="cnc_spoilboard_length", expression="220 mm"),
            ParameterSpec(name="cnc_spoilboard_width", expression="160 mm"),
            ParameterSpec(name="cnc_spoilboard_thickness", expression="12 mm"),
            ParameterSpec(name="cnc_spindle_diameter", expression="52 mm"),
            ParameterSpec(name="cnc_spindle_length", expression="120 mm"),
            ParameterSpec(name="cnc_work_area_x", expression="180 mm"),
            ParameterSpec(name="cnc_work_area_y", expression="140 mm"),
            ParameterSpec(name="cnc_work_area_z", expression="70 mm"),
        ],
        components=[
            ComponentSpec(
                name="desktop_cnc_assembly",
                features=[
                    FeatureSpec(
                        name="desktop_cnc_assembly_build",
                        type="desktop_cnc_assembly",
                        operation="new_assembly",
                        inputs={
                            "assembly_component": "desktop_cnc_assembly",
                            "component_names": component_names,
                            "body_names": body_names,
                            "frame_width": "cnc_frame_width",
                            "frame_depth": "cnc_frame_depth",
                            "gantry_height": "cnc_gantry_height",
                            "profile_size": "cnc_profile_size",
                            "rail_length": "cnc_rail_length",
                            "z_rail_length": "cnc_z_rail_length",
                            "rail_width": "cnc_rail_width",
                            "rail_height": "cnc_rail_height",
                            "motor_face_width": "cnc_motor_face_width",
                            "motor_body_length": "cnc_motor_body_length",
                            "motor_shaft_diameter": "cnc_motor_shaft_diameter",
                            "motor_shaft_length": "cnc_motor_shaft_length",
                            "leadscrew_diameter": "cnc_leadscrew_diameter",
                            "coupler_diameter": "cnc_coupler_diameter",
                            "coupler_length": "cnc_coupler_length",
                            "plate_thickness": "cnc_plate_thickness",
                            "spoilboard_length": "cnc_spoilboard_length",
                            "spoilboard_width": "cnc_spoilboard_width",
                            "spoilboard_thickness": "cnc_spoilboard_thickness",
                            "spindle_diameter": "cnc_spindle_diameter",
                            "spindle_length": "cnc_spindle_length",
                            "work_area_x": "cnc_work_area_x",
                            "work_area_y": "cnc_work_area_y",
                            "work_area_z": "cnc_work_area_z",
                            "placement_offset": ["0 mm", "-170 mm", "0 mm"],
                        },
                    )
                ],
            )
        ],
        acceptance_tests=[
            AcceptanceTestSpec(type="component_exists", target="desktop_cnc_assembly"),
            *[AcceptanceTestSpec(type="component_exists", target=name) for name in component_names],
            AcceptanceTestSpec(type="named_bodies", target=body_names),
            AcceptanceTestSpec(type="named_parameters", target=parameter_names),
            AcceptanceTestSpec(
                type="target_bounding_box",
                target="cnc_front_2020_profile_body",
                target_mm=[260.0, 20.0, 20.0],
                tolerance_mm=0.2,
            ),
            AcceptanceTestSpec(
                type="target_bounding_box",
                target="cnc_y_left_mgn12_rail_body",
                target_mm=[12.0, 200.0, 8.0],
                tolerance_mm=0.2,
            ),
            AcceptanceTestSpec(
                type="target_bounding_box",
                target="cnc_x_mgn12_rail_body",
                target_mm=[200.0, 12.0, 8.0],
                tolerance_mm=0.2,
            ),
            AcceptanceTestSpec(
                type="target_bounding_box",
                target="cnc_spindle_body",
                target_mm=[52.0, 52.0, 120.0],
                tolerance_mm=0.2,
            ),
            AcceptanceTestSpec(
                type="desktop_cnc_assembly",
                target={
                    "assembly_component": "desktop_cnc_assembly",
                    "required_components": component_names,
                    "required_bodies": body_names,
                    "profile_count": 8,
                    "rail_count": 4,
                    "motor_count": 3,
                    "leadscrew_count": 3,
                    "coupler_count": 3,
                    "spindle_diameter_mm": 52.0,
                    "work_area_mm": [180.0, 140.0, 70.0],
                    "max_legacy_visible_cnc_body_count": 0,
                },
                tolerance_mm=0.2,
            ),
            AcceptanceTestSpec(type="named_objects", target=True),
            AcceptanceTestSpec(type="feature_health", target=True),
        ],
    )


def _desktop_cnc_body_names() -> list[str]:
    return [
        "cnc_front_2020_profile_body",
        "cnc_rear_2020_profile_body",
        "cnc_left_2020_profile_body",
        "cnc_right_2020_profile_body",
        "cnc_center_2020_profile_body",
        "cnc_left_upright_2020_profile_body",
        "cnc_right_upright_2020_profile_body",
        "cnc_gantry_2020_profile_body",
        "cnc_spoilboard_body",
        "cnc_y_left_mgn12_rail_body",
        "cnc_y_right_mgn12_rail_body",
        "cnc_x_mgn12_rail_body",
        "cnc_z_mgn12_rail_body",
        "cnc_y_left_carriage_body",
        "cnc_y_right_carriage_body",
        "cnc_x_carriage_body",
        "cnc_z_carriage_body",
        "cnc_left_gantry_plate_body",
        "cnc_right_gantry_plate_body",
        "cnc_x_carriage_plate_body",
        "cnc_z_carriage_plate_body",
        "cnc_x_nema17_body",
        "cnc_y_nema17_body",
        "cnc_z_nema17_body",
        "cnc_x_motor_shaft_body",
        "cnc_y_motor_shaft_body",
        "cnc_z_motor_shaft_body",
        "cnc_x_t8_leadscrew_body",
        "cnc_y_t8_leadscrew_body",
        "cnc_z_t8_leadscrew_body",
        "cnc_x_coupler_body",
        "cnc_y_coupler_body",
        "cnc_z_coupler_body",
        "cnc_x_bearing_block_body",
        "cnc_y_bearing_block_body",
        "cnc_z_bearing_block_body",
        "cnc_spindle_clamp_body",
        "cnc_spindle_body",
        "cnc_er11_collet_body",
        "cnc_x_drag_chain_body",
        "cnc_y_drag_chain_body",
        "cnc_controller_box_body",
    ]


def _nema17_body_length(prompt: str) -> str:
    before = re.search(r"(\d+(?:\.\d+)?)\s*(mm|cm|in)\s+body\s+length", prompt)
    if before:
        return f"{before.group(1)} {before.group(2)}"
    after = re.search(r"body\s+length[^0-9]*(\d+(?:\.\d+)?)\s*(mm|cm|in)", prompt)
    if after:
        return f"{after.group(1)} {after.group(2)}"
    return "40 mm"


def _l_bracket_spec(prompt: str) -> CadSpec:
    lengths = _all_lengths(prompt)
    leg = lengths[0] if len(lengths) > 0 else "50 mm"
    thickness = lengths[1] if len(lengths) > 1 else "5 mm"
    hole = lengths[2] if len(lengths) > 2 else "5 mm"
    return CadSpec(
        intent="create_parametric_part",
        units="mm",
        assumptions=["L bracket is represented as a single valid solid body in the mock harness."],
        parameters=[
            ParameterSpec(name="leg_length", expression=leg),
            ParameterSpec(name="bracket_thickness", expression=thickness),
            ParameterSpec(name="hole_diameter", expression=hole),
        ],
        components=[
            ComponentSpec(
                name="l_bracket",
                features=[
                    FeatureSpec(
                        name="l_bracket_body_extrude",
                        type="l_bracket_body",
                        operation="new_body",
                        inputs={
                            "sketch_name": "l_bracket_profile_sketch",
                            "plane": "XY",
                            "leg_length": "leg_length",
                            "thickness": "bracket_thickness",
                            "distance": "bracket_thickness",
                            "body_name": "l_bracket_body",
                        },
                    ),
                    FeatureSpec(
                        name="l_bracket_mounting_hole_cut",
                        type="hole_pattern_cut",
                        operation="cut",
                        inputs={
                            "sketch_name": "l_bracket_hole_profile_sketch",
                            "plane": "XY",
                            "target_body": "l_bracket_body",
                            "diameter": "hole_diameter",
                            "count": 2,
                            "distance": "bracket_thickness",
                        },
                    ),
                ],
            )
        ],
        acceptance_tests=[
            AcceptanceTestSpec(type="component_count", target=1),
            AcceptanceTestSpec(type="body_count", target=1),
            AcceptanceTestSpec(type="named_parameters", target=["leg_length", "bracket_thickness", "hole_diameter"]),
            AcceptanceTestSpec(type="hole_count", target=2),
            AcceptanceTestSpec(type="named_objects", target=True),
            AcceptanceTestSpec(type="feature_health", target=True),
        ],
    )


def _box_spec(prompt: str) -> CadSpec:
    length, width, height = _triple_dimensions(prompt, defaults=("80 mm", "50 mm", "30 mm"))
    wall = _length_after(prompt, "wall", default="3 mm")
    return CadSpec(
        intent="create_parametric_part",
        units="mm",
        assumptions=["Open box is modeled as one shell body in the mock harness."],
        parameters=[
            ParameterSpec(name="box_length", expression=length),
            ParameterSpec(name="box_width", expression=width),
            ParameterSpec(name="box_height", expression=height),
            ParameterSpec(name="wall_thickness", expression=wall),
        ],
        components=[
            ComponentSpec(
                name="open_box",
                features=[
                    FeatureSpec(
                        name="open_box_shell_extrude",
                        type="box_shell",
                        operation="new_body",
                        inputs={
                            "sketch_name": "box_base_profile_sketch",
                            "plane": "XY",
                            "length": "box_length",
                            "width": "box_width",
                            "height": "box_height",
                            "wall_thickness": "wall_thickness",
                            "body_name": "open_box_body",
                        },
                    )
                ],
            )
        ],
        acceptance_tests=_base_acceptance(
            [length, width, height], ["box_length", "box_width", "box_height", "wall_thickness"]
        ),
    )


def _parameter_edit_spec(prompt: str) -> CadSpec:
    expression = _first_length(prompt, default="10 mm")
    return CadSpec(
        intent="edit_named_parameter",
        units="mm",
        assumptions=["Parameter edit targets plate_thickness unless a richer parser identifies another parameter."],
        document_policy=DocumentPolicy(modify_existing=True, create_checkpoint=True),
        parameters=[ParameterSpec(name="plate_thickness", expression=expression)],
        components=[
            ComponentSpec(
                name="parameter_edit",
                features=[
                    FeatureSpec(
                        name="plate_thickness_update",
                        type="update_parameter",
                        operation="modify",
                        inputs={"name": "plate_thickness", "expression": expression},
                    )
                ],
            )
        ],
        acceptance_tests=[AcceptanceTestSpec(type="named_parameters", target=["plate_thickness"])],
    )


def _base_acceptance(bbox_exprs: list[str], parameter_names: list[str]) -> list[AcceptanceTestSpec]:
    return [
        AcceptanceTestSpec(type="component_count", target=1),
        AcceptanceTestSpec(type="body_count", target=1),
        AcceptanceTestSpec(type="bounding_box", target_mm=[_to_float_mm(expr) for expr in bbox_exprs], tolerance_mm=0.05),
        AcceptanceTestSpec(type="named_parameters", target=parameter_names),
        AcceptanceTestSpec(type="named_objects", target=True),
        AcceptanceTestSpec(type="feature_health", target=True),
    ]


def _first_length(prompt: str, default: str) -> str:
    lengths = _all_lengths(prompt)
    return lengths[0] if lengths else default


def _length_after(prompt: str, word: str, default: str) -> str:
    match = re.search(rf"(\d+(?:\.\d+)?)\s*(mm|cm|in)\s+[^.]*{re.escape(word)}", prompt)
    if match:
        return f"{match.group(1)} {match.group(2)}"
    match = re.search(rf"{re.escape(word)}[^.]*?(\d+(?:\.\d+)?)\s*(mm|cm|in)", prompt)
    if match:
        return f"{match.group(1)} {match.group(2)}"
    return default


def _all_lengths(prompt: str) -> list[str]:
    return [f"{match.group(1)} {match.group(2)}" for match in re.finditer(r"(\d+(?:\.\d+)?)\s*(mm|cm|in)", prompt)]


def _triple_dimensions(prompt: str, defaults: tuple[str, str, str]) -> tuple[str, str, str]:
    match = re.search(
        r"(\d+(?:\.\d+)?)\s*x\s*(\d+(?:\.\d+)?)\s*x\s*(\d+(?:\.\d+)?)\s*(mm|cm|in)",
        prompt,
    )
    if match:
        unit = match.group(4)
        return (f"{match.group(1)} {unit}", f"{match.group(2)} {unit}", f"{match.group(3)} {unit}")
    lengths = _all_lengths(prompt)
    if len(lengths) >= 3:
        return (lengths[0], lengths[1], lengths[2])
    return defaults


def _to_float_mm(expression: str) -> float:
    number, unit = expression.split()
    multiplier = {"mm": 1.0, "cm": 10.0, "in": 25.4}[unit]
    return float(number) * multiplier
