"""Pydantic models for the CAD Spec contract."""

from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cad_spec.naming_policy import validate_name
from cad_spec.unit_policy import (
    reject_ambiguous_numeric_dimensions,
    validate_dimension_expression,
)


LegacyFeatureType = Literal[
    "extrude_rectangle",
    "extrude_cylinder",
    "hole_pattern_cut",
    "center_hole_cut",
    "l_bracket_body",
    "box_shell",
    "nema17_stepper_motor",
    "nema17_visual_polish",
    "nema17_external_assembly",
    "profile2020_aluminum_extrusion",
    "mgn12_linear_rail_assembly",
    "desktop_cnc_assembly",
    "spacer_plate_assembly",
    "hinge_assembly",
    "update_parameter",
    "apply_fillet",
    "export",
    "capture_viewport",
]

_NONEMPTY_STRING_TARGET_ASSERTIONS = frozenset(
    {"body_exists", "component_exists", "target_bounding_box"}
)
_NONEMPTY_STRING_LIST_TARGET_ASSERTIONS = frozenset(
    {"named_bodies", "named_parameters", "export_exists"}
)
_NONEMPTY_MAPPING_TARGET_ASSERTIONS = frozenset(
    {
        "nema17_dimensions",
        "nema17_polish_details",
        "nema17_external_assembly",
        "profile2020_details",
        "mgn12_linear_rail_assembly",
        "desktop_cnc_assembly",
        "occurrence_contract",
    }
)


class DocumentPolicy(BaseModel):
    """Policy for whether the session may modify an existing document."""

    modify_existing: bool = False
    create_checkpoint: bool = True


class ParameterSpec(BaseModel):
    """Named Fusion user parameter."""

    name: str
    expression: str
    comment: str | None = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return validate_name(value, "parameter name")

    @field_validator("expression")
    @classmethod
    def _validate_expression(cls, value: str) -> str:
        return validate_dimension_expression(value, "parameter expression")


class FeatureSpec(BaseModel):
    """One semantic feature operation in a component."""

    model_config = ConfigDict(extra="allow")

    name: str
    # CadSpec v1 remains available for one compatibility cycle, but its
    # dispatch registry is closed.  An unknown type must fail while parsing
    # the complete graph, before an earlier feature can reach Fusion.
    type: LegacyFeatureType
    operation: str = "new_body"
    inputs: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return validate_name(value, "feature name")

    @model_validator(mode="after")
    def _validate_inputs(self) -> "FeatureSpec":
        reject_ambiguous_numeric_dimensions(self.inputs, f"feature[{self.name}].inputs")
        extra_values = {
            key: value
            for key, value in self.__pydantic_extra__.items()
            if self.__pydantic_extra__ and key not in {"type", "operation"}
        }
        reject_ambiguous_numeric_dimensions(extra_values, f"feature[{self.name}]")
        return self

    def merged_inputs(self) -> dict[str, Any]:
        """Return feature inputs with legacy extra fields folded in."""

        merged = dict(self.inputs)
        if self.__pydantic_extra__:
            merged.update(self.__pydantic_extra__)
        return merged


class ComponentSpec(BaseModel):
    """Target component and its feature sequence."""

    name: str
    features: list[FeatureSpec]

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return validate_name(value, "component name")


class ComponentMetadataSpec(BaseModel):
    """Engineering metadata expected on a Fusion component."""

    component: str
    part_number: str
    description: str
    role: str
    source_type: Literal["custom", "purchased", "library", "placeholder"]
    physical_material: str
    appearance: str | None = None
    placeholder: bool = False
    revision: str | None = None

    @field_validator("component")
    @classmethod
    def _validate_component_name(cls, value: str) -> str:
        return validate_name(value, "metadata component name")


class JointSpec(BaseModel):
    """Expected assembly joint contract."""

    name: str
    type: Literal["rigid", "revolute", "slider", "as_built_rigid"]
    parent: str
    child: str
    axis: Literal["x", "y", "z"] | None = None
    limits: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name", "parent", "child")
    @classmethod
    def _validate_names(cls, value: str) -> str:
        return validate_name(value, "joint name")

    @model_validator(mode="after")
    def _validate_limits(self) -> "JointSpec":
        reject_ambiguous_numeric_dimensions(self.limits, f"joint[{self.name}].limits")
        return self


class OutputSpec(BaseModel):
    """Expected session output artifact."""

    name: str
    path: str
    view: Literal["isometric", "front", "top", "right"] = "isometric"
    isolate_prefix: str | None = None
    width: int = 1600
    height: int = 1100

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return validate_name(value, "output name")


class AcceptanceTestSpec(BaseModel):
    """Programmatic acceptance test requested by the CAD Spec."""

    model_config = ConfigDict(extra="allow")

    type: str
    target: Any | None = None
    target_mm: list[float] | None = None
    tolerance_mm: float | None = None

    @field_validator("target_mm", mode="before")
    @classmethod
    def _validate_target_mm(cls, value: Any) -> Any:
        if value is None:
            return value
        if not isinstance(value, list):
            raise ValueError("target_mm must be a list of finite numbers")
        for item in value:
            if (
                isinstance(item, bool)
                or not isinstance(item, int | float)
                or not math.isfinite(float(item))
            ):
                raise ValueError(
                    "target_mm must contain only finite non-boolean numbers"
                )
        return value

    @field_validator("tolerance_mm", mode="before")
    @classmethod
    def _validate_tolerance_mm(cls, value: Any) -> Any:
        if value is None:
            return value
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError("tolerance_mm must be a finite non-boolean number")
        number = float(value)
        if not math.isfinite(number) or number < 0:
            raise ValueError("tolerance_mm must be finite and non-negative")
        return value

    @model_validator(mode="after")
    def _validate_numeric_contract(self) -> "AcceptanceTestSpec":
        if self.type in {"body_count", "component_count", "hole_count"}:
            if (
                isinstance(self.target, bool)
                or not isinstance(self.target, int)
                or self.target < 0
            ):
                raise ValueError(f"{self.type} target must be a non-negative integer")
        _validate_named_numeric_targets(self.target, path=f"{self.type}.target")
        if _contains_non_finite_number(self.target):
            raise ValueError("acceptance target contains a non-finite number")
        if self.type in _NONEMPTY_STRING_TARGET_ASSERTIONS and (
            not isinstance(self.target, str) or not self.target.strip()
        ):
            raise ValueError(f"{self.type} requires a non-empty string target")
        if self.type in _NONEMPTY_STRING_LIST_TARGET_ASSERTIONS and (
            not isinstance(self.target, list)
            or not self.target
            or any(
                not isinstance(item, str) or not item.strip() for item in self.target
            )
        ):
            raise ValueError(
                f"{self.type} requires a non-empty list of non-empty string targets"
            )
        if self.type in _NONEMPTY_MAPPING_TARGET_ASSERTIONS and (
            not isinstance(self.target, dict) or not self.target
        ):
            raise ValueError(f"{self.type} requires a non-empty mapping target")
        if self.type in {"bounding_box", "target_bounding_box"} and (
            self.target_mm is None or len(self.target_mm) != 3
        ):
            raise ValueError(f"{self.type} requires exactly three target_mm values")
        return self


def _contains_non_finite_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int | float):
        return not math.isfinite(float(value))
    if isinstance(value, dict):
        return any(_contains_non_finite_number(item) for item in value.values())
    if isinstance(value, list | tuple):
        return any(_contains_non_finite_number(item) for item in value)
    return False


def _validate_named_numeric_targets(value: Any, *, path: str) -> None:
    if not isinstance(value, dict):
        return
    for key, item in value.items():
        item_path = f"{path}.{key}"
        if (
            key == "count"
            or key.endswith("_count")
            or key
            in {
                "min_lamination_bodies",
            }
        ):
            if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                raise ValueError(f"{item_path} must be a non-negative integer")
        elif key.endswith("_mm"):
            items = item if isinstance(item, list | tuple) else (item,)
            if not items or any(
                isinstance(child, bool)
                or not isinstance(child, int | float)
                or not math.isfinite(float(child))
                for child in items
            ):
                raise ValueError(f"{item_path} must contain finite non-boolean numbers")
        elif isinstance(item, dict):
            _validate_named_numeric_targets(item, path=item_path)


class CadSpec(BaseModel):
    """Complete structured CAD plan for executor and verifier."""

    intent: str
    units: Literal["mm", "cm", "in"] = "mm"
    assumptions: list[str] = Field(default_factory=list)
    document_policy: DocumentPolicy = Field(default_factory=DocumentPolicy)
    parameters: list[ParameterSpec]
    components: list[ComponentSpec]
    component_metadata: list[ComponentMetadataSpec] = Field(default_factory=list)
    joints: list[JointSpec] = Field(default_factory=list)
    outputs: list[OutputSpec] = Field(default_factory=list)
    acceptance_tests: list[AcceptanceTestSpec]

    @model_validator(mode="after")
    def _validate_cad_spec(self) -> "CadSpec":
        if not self.acceptance_tests:
            raise ValueError("CadSpec requires at least one acceptance test")
        if not self.components:
            raise ValueError("CadSpec requires at least one component")
        assertions = {item.type for item in self.acceptance_tests}
        if assertions & {"component_metadata", "physical_properties"} and not (
            self.component_metadata
        ):
            raise ValueError(
                "component_metadata and physical_properties assertions require "
                "a non-empty component_metadata contract"
            )
        if "joint_contract" in assertions and not self.joints:
            raise ValueError("joint_contract requires a non-empty joints contract")
        if "screenshots_exist" in assertions and not self.outputs:
            raise ValueError("screenshots_exist requires a non-empty outputs contract")
        return self

    def parameter_map(self) -> dict[str, str]:
        """Return parameter expressions keyed by name."""

        return {parameter.name: parameter.expression for parameter in self.parameters}

    def to_json_text(self) -> str:
        """Serialize the spec in a stable JSON representation."""

        return self.model_dump_json(indent=2)
