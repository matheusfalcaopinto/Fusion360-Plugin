"""Strict CadSpec v2 contract and legacy normalization helpers.

CadSpec v1 remains available for 0.x compatibility.  V2 is deliberately
operation-oriented: every operation has a stable id, explicit dependencies and
links to the requirements it is intended to satisfy.  Unknown fields are
rejected so capability negotiation can fail before any Fusion dispatch.
"""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    field_validator,
    model_validator,
)
from typing_extensions import TypeAliasType

from cad_spec.models import CadSpec
from cad_spec.naming_policy import validate_name
from cad_spec.unit_policy import (
    expression_to_mm,
    validate_dimension_expression,
    validate_non_negative_dimension_expression,
)


def _reference_type(name: str, kind: str) -> Any:
    """Create a named wire-schema reference while preserving runtime strings.

    Named aliases keep the compact 0.x JSON representation and therefore do
    not break either typed backend.  Their JSON Schema definitions make the
    semantic category explicit, while whole-graph/backend preflight remains
    responsible for proving that a referenced entity exists uniquely.
    """

    return TypeAliasType(
        name,
        Annotated[
            str,
            StringConstraints(
                strip_whitespace=True,
                min_length=1,
                max_length=1024,
                pattern=r"^[^\x00-\x1f\x7f]+$",
            ),
            Field(json_schema_extra={"x-cad-reference-kind": kind}),
        ],
    )


AssertionIdRef = _reference_type("AssertionIdRef", "assertion_id")
RequirementIdRef = _reference_type("RequirementIdRef", "requirement_id")
OperationIdRef = _reference_type("OperationIdRef", "operation_id")
ContractTargetRef = _reference_type("ContractTargetRef", "contract_target")
ComponentRef = _reference_type("ComponentRef", "component")
SketchRef = _reference_type("SketchRef", "sketch")
ProfileRef = _reference_type("ProfileRef", "profile")
SketchEntityRef = _reference_type("SketchEntityRef", "sketch_entity")
AxisRef = _reference_type("AxisRef", "axis")
PathRef = _reference_type("PathRef", "path")
GeometryRef = _reference_type("GeometryRef", "geometry")
BodyRef = _reference_type("BodyRef", "body")
PlaneRef = _reference_type("PlaneRef", "plane")
OccurrenceRef = _reference_type("OccurrenceRef", "occurrence")
AnalysisTargetRef = _reference_type("AnalysisTargetRef", "analysis_target")
AnalysisOutputRef = _reference_type("AnalysisOutputRef", "analysis_output")
ExportTargetRef = _reference_type("ExportTargetRef", "export_target")


class StrictModel(BaseModel):
    """Base for wire contracts that must reject misspelled fields."""

    model_config = ConfigDict(extra="forbid")


class DocumentPolicyV2(StrictModel):
    """Strict document mutation policy for the v2 wire contract."""

    modify_existing: bool = False
    create_checkpoint: bool = True


class RequirementSpec(StrictModel):
    """One user-visible requirement covered by assertions or an oracle."""

    id: str
    description: str
    required: bool = True
    assertion_ids: list[AssertionIdRef] = Field(default_factory=list)
    oracle: Literal["assertions", "independent"] = "assertions"

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        return validate_name(value, "requirement id")


class AssertionSpec(StrictModel):
    """Typed verification assertion used by CadSpec v2."""

    id: str
    kind: Literal[
        "entity_exists",
        "entity_count",
        "dimension_equals",
        "parameter_equals",
        "interference_count",
        "physical_property_range",
        "export_exists",
        "custom_oracle",
    ]
    target_ref: ContractTargetRef | None = None
    expected: Any | None = None
    tolerance: str | None = None
    required: bool = True

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        return validate_name(value, "assertion id")

    @field_validator("tolerance")
    @classmethod
    def _validate_tolerance(cls, value: str | None) -> str | None:
        return (
            validate_non_negative_dimension_expression(value, "assertion tolerance")
            if value is not None
            else value
        )

    @model_validator(mode="after")
    def _validate_assertion_contract(self) -> "AssertionSpec":
        if self.kind != "custom_oracle" and not self.target_ref:
            raise ValueError(f"{self.kind} assertion requires target_ref")
        if self.kind in {"entity_exists", "export_exists"}:
            if self.expected is not None and not isinstance(self.expected, bool):
                raise ValueError(f"{self.kind} expected must be boolean")
        elif self.kind in {"entity_count", "interference_count"}:
            expected = self.expected
            if self.kind == "entity_count" and isinstance(expected, dict):
                if not set(expected) <= {"count", "category"}:
                    raise ValueError(
                        "entity_count expected contains unsupported fields"
                    )
                category = expected.get("category")
                if category is not None and (
                    not isinstance(category, str) or not category.strip()
                ):
                    raise ValueError("entity_count category must be non-empty text")
                expected = expected.get("count")
            if (
                not isinstance(expected, int)
                or isinstance(expected, bool)
                or expected < 0
            ):
                raise ValueError(
                    f"{self.kind} expected count must be a non-negative integer"
                )
        elif self.kind == "physical_property_range":
            _validate_physical_property_range(self.expected)
        elif self.kind in {"dimension_equals", "parameter_equals"}:
            if self.expected is None or isinstance(self.expected, bool):
                raise ValueError(
                    f"{self.kind} assertion requires a typed expected value"
                )
            if isinstance(self.expected, int | float | Decimal):
                _finite_float(self.expected, f"{self.kind} expected value")
        if self.tolerance is not None and self.kind not in {
            "dimension_equals",
            "parameter_equals",
        }:
            raise ValueError(f"{self.kind} assertion does not support tolerance")
        return self


def _validate_physical_property_range(value: Any) -> None:
    if not isinstance(value, dict) or not value:
        raise ValueError("physical_property_range expected must be a non-empty object")
    property_name = value.get("property")
    if property_name is not None and (
        not isinstance(property_name, str) or not property_name.strip()
    ):
        raise ValueError("physical_property_range property must be non-empty text")
    bound_names = [
        key
        for key in value
        if isinstance(key, str)
        and (key in {"min", "max"} or key.startswith("min_") or key.startswith("max_"))
    ]
    if not bound_names:
        raise ValueError("physical_property_range requires at least one bound")
    if property_name is not None and any("_" in key for key in bound_names):
        raise ValueError(
            "physical_property_range with property must use min/max bounds"
        )
    suffix_bound_names = [key for key in bound_names if "_" in key]
    if property_name is None:
        if len(suffix_bound_names) != len(bound_names):
            raise ValueError(
                "physical_property_range without property must use suffixed bounds"
            )
        suffixes = {key.split("_", 1)[1] for key in suffix_bound_names}
        if len(suffixes) != 1:
            raise ValueError("physical_property_range bounds must address one property")
    allowed = set(bound_names)
    if property_name is not None:
        allowed.add("property")
    if set(value) != allowed:
        raise ValueError("physical_property_range contains unsupported fields")
    normalized: dict[str, float] = {}
    for key in bound_names:
        bound = value[key]
        if not isinstance(bound, int | float | Decimal) or isinstance(bound, bool):
            raise ValueError(f"physical_property_range {key} must be finite")
        normalized[key] = _finite_float(bound, f"physical_property_range {key}")
    pairs: list[tuple[str, str]] = [("min", "max")]
    suffixes = {
        key.removeprefix("min_") for key in bound_names if key.startswith("min_")
    }
    pairs.extend((f"min_{suffix}", f"max_{suffix}") for suffix in suffixes)
    for lower, upper in pairs:
        if (
            lower in normalized
            and upper in normalized
            and normalized[lower] > normalized[upper]
        ):
            raise ValueError("physical_property_range minimum exceeds maximum")


def _finite_float(value: int | float | Decimal, path: str) -> float:
    """Convert a non-boolean number while failing closed on float overflow."""

    if isinstance(value, bool):
        raise ValueError(f"{path} must be finite")
    try:
        normalized = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{path} must be finite") from exc
    if not math.isfinite(normalized):
        raise ValueError(f"{path} must be finite")
    return normalized


class OperationBase(StrictModel):
    """Fields shared by all typed operations."""

    id: str
    depends_on: list[OperationIdRef] = Field(default_factory=list)
    requirement_ids: list[RequirementIdRef] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        return validate_name(value, "operation id")


class ParameterOperation(OperationBase):
    kind: Literal["parameter.set"]
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


class ComponentCreateOperation(OperationBase):
    kind: Literal["component.create"]
    name: str
    parent_ref: ComponentRef | None = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return validate_name(value, "component name")


class SketchCreateOperation(OperationBase):
    kind: Literal["sketch.create"]
    component_ref: ComponentRef
    plane: Literal["XY", "YZ", "XZ"] = "XY"
    name: str

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return validate_name(value, "sketch name")


class SketchRectangleOperation(OperationBase):
    kind: Literal["sketch.rectangle"]
    sketch_ref: SketchRef
    center: list[str] = Field(
        default_factory=lambda: ["0 mm", "0 mm"], min_length=2, max_length=2
    )
    width: str
    height: str
    result_ref: ProfileRef

    @field_validator("center")
    @classmethod
    def _validate_center(cls, value: list[str]) -> list[str]:
        return [
            validate_dimension_expression(item, "rectangle center") for item in value
        ]

    @field_validator("width", "height")
    @classmethod
    def _validate_dimensions(cls, value: str) -> str:
        return validate_dimension_expression(value, "rectangle dimension")


class SketchCircleOperation(OperationBase):
    kind: Literal["sketch.circle"]
    sketch_ref: SketchRef
    center: list[str] = Field(
        default_factory=lambda: ["0 mm", "0 mm"], min_length=2, max_length=2
    )
    diameter: str
    result_ref: ProfileRef

    @field_validator("center")
    @classmethod
    def _validate_center(cls, value: list[str]) -> list[str]:
        return [validate_dimension_expression(item, "circle center") for item in value]

    @field_validator("diameter")
    @classmethod
    def _validate_diameter(cls, value: str) -> str:
        return validate_dimension_expression(value, "circle diameter")


def _validate_feature_target_body(operation: Any, label: str) -> Any:
    target = operation.target_body_ref
    if operation.operation != "new_body":
        if target is None:
            raise ValueError(f"{label} {operation.operation} requires target_body_ref")
        if operation.result_name != target:
            raise ValueError(
                f"{label} {operation.operation} result_name must equal target_body_ref"
            )
    elif target is not None:
        raise ValueError(f"{label} new_body cannot declare target_body_ref")
    return operation


class ExtrudeOperation(OperationBase):
    kind: Literal["feature.extrude"]
    component_ref: ComponentRef
    profile_ref: ProfileRef
    distance: str
    operation: Literal["new_body", "join", "cut", "intersect"] = "new_body"
    direction: Literal["positive", "negative", "symmetric"] = "positive"
    target_body_ref: BodyRef | None = None
    result_name: str

    @field_validator("distance")
    @classmethod
    def _validate_distance(cls, value: str) -> str:
        return validate_dimension_expression(value, "extrude distance")

    @model_validator(mode="after")
    def _validate_target_body(self) -> "ExtrudeOperation":
        return _validate_feature_target_body(self, "extrude")


class SketchConstraintOperation(OperationBase):
    kind: Literal["sketch.constraint"]
    sketch_ref: SketchRef
    constraint: Literal[
        "coincident",
        "horizontal",
        "vertical",
        "parallel",
        "perpendicular",
        "tangent",
        "equal",
        "concentric",
        "midpoint",
        "fixed",
    ]
    entity_refs: list[SketchEntityRef] = Field(min_length=1)


class SketchDimensionOperation(OperationBase):
    kind: Literal["sketch.dimension"]
    sketch_ref: SketchRef
    dimension: Literal[
        "distance", "horizontal", "vertical", "diameter", "radius", "angle"
    ]
    entity_refs: list[SketchEntityRef] = Field(min_length=1)
    expression: str

    @field_validator("expression")
    @classmethod
    def _validate_expression(cls, value: str) -> str:
        return validate_dimension_expression(value, "sketch dimension")


class RevolveOperation(OperationBase):
    kind: Literal["feature.revolve"]
    component_ref: ComponentRef
    profile_ref: ProfileRef
    axis_ref: AxisRef
    angle: str = "360 deg"
    operation: Literal["new_body", "join", "cut", "intersect"] = "new_body"
    target_body_ref: BodyRef | None = None
    result_name: str

    @field_validator("angle")
    @classmethod
    def _validate_angle(cls, value: str) -> str:
        return validate_dimension_expression(value, "revolve angle")

    @model_validator(mode="after")
    def _validate_target_body(self) -> "RevolveOperation":
        return _validate_feature_target_body(self, "revolve")


class SweepOperation(OperationBase):
    kind: Literal["feature.sweep"]
    component_ref: ComponentRef
    profile_ref: ProfileRef
    path_ref: PathRef
    orientation: Literal["perpendicular", "parallel"] = "perpendicular"
    operation: Literal["new_body", "join", "cut", "intersect"] = "new_body"
    target_body_ref: BodyRef | None = None
    result_name: str

    @model_validator(mode="after")
    def _validate_target_body(self) -> "SweepOperation":
        return _validate_feature_target_body(self, "sweep")


class LoftOperation(OperationBase):
    kind: Literal["feature.loft"]
    component_ref: ComponentRef
    profile_refs: list[ProfileRef] = Field(min_length=2)
    guide_refs: list[PathRef] = Field(default_factory=list)
    operation: Literal["new_body", "join", "cut", "intersect"] = "new_body"
    target_body_ref: BodyRef | None = None
    result_name: str

    @model_validator(mode="after")
    def _validate_target_body(self) -> "LoftOperation":
        return _validate_feature_target_body(self, "loft")


class PatternOperation(OperationBase):
    kind: Literal["feature.pattern"]
    pattern: Literal["rectangular", "circular", "path"]
    target_refs: list[GeometryRef] = Field(min_length=1)
    count: int = Field(ge=2, le=1000)
    spacing: str | None = None
    axis_ref: AxisRef | None = None
    path_ref: PathRef | None = None

    @field_validator("spacing")
    @classmethod
    def _validate_spacing(cls, value: str | None) -> str | None:
        return (
            validate_dimension_expression(value, "pattern spacing") if value else value
        )

    @model_validator(mode="after")
    def _validate_pattern_refs(self) -> "PatternOperation":
        if self.pattern == "circular" and not self.axis_ref:
            raise ValueError("circular pattern requires axis_ref")
        if self.pattern == "path" and not self.path_ref:
            raise ValueError("path pattern requires path_ref")
        if self.pattern == "rectangular" and not self.spacing:
            raise ValueError("rectangular pattern requires spacing")
        return self


class MirrorOperation(OperationBase):
    kind: Literal["feature.mirror"]
    target_refs: list[GeometryRef] = Field(min_length=1)
    plane_ref: PlaneRef
    result_prefix: str | None = None


class BooleanOperation(OperationBase):
    kind: Literal["feature.boolean"]
    operation: Literal["join", "cut", "intersect", "split"]
    target_ref: BodyRef
    tool_refs: list[BodyRef] = Field(min_length=1)
    keep_tools: bool = False


class JointOperation(OperationBase):
    kind: Literal["assembly.joint"]
    name: str
    joint_type: Literal["rigid", "revolute", "slider", "as_built_rigid"]
    parent_ref: OccurrenceRef
    child_ref: OccurrenceRef
    axis: Literal["x", "y", "z"] | None = None
    limits: dict[str, str] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return validate_name(value, "joint name")

    @field_validator("limits")
    @classmethod
    def _validate_limits(cls, value: dict[str, str]) -> dict[str, str]:
        for key, expression in value.items():
            validate_dimension_expression(expression, f"joint limit {key}")
        return value


class RigidGroupOperation(OperationBase):
    kind: Literal["assembly.rigid_group"]
    name: str
    occurrence_refs: list[OccurrenceRef] = Field(min_length=2)


class PhysicalPropertiesOperation(OperationBase):
    kind: Literal["analysis.physical_properties"]
    target_refs: list[AnalysisTargetRef] = Field(min_length=1)
    output_ref: AnalysisOutputRef


class InterferenceOperation(OperationBase):
    kind: Literal["analysis.interference"]
    target_refs: list[AnalysisTargetRef] = Field(default_factory=list)
    output_ref: AnalysisOutputRef


class HostFileRef(StrictModel):
    """Reference to one path beneath an independently configured host root."""

    root_id: str
    relative_path: str

    @field_validator("root_id")
    @classmethod
    def _validate_root_id(cls, value: str) -> str:
        if not value or len(value) > 64:
            raise ValueError("host root_id must contain 1 through 64 characters")
        if not value[0].islower() or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789-"
            for character in value
        ):
            raise ValueError("host root_id must match [a-z][a-z0-9-]{0,63}")
        return value

    @field_validator("relative_path")
    @classmethod
    def _validate_relative_path(cls, value: str) -> str:
        from pathlib import PurePosixPath, PureWindowsPath

        if not value or value != value.strip():
            raise ValueError(
                "host relative_path must be non-empty without outer whitespace"
            )
        windows = PureWindowsPath(value)
        posix = PurePosixPath(value)
        if (
            windows.is_absolute()
            or posix.is_absolute()
            or windows.drive
            or windows.root
        ):
            raise ValueError("host relative_path must be relative")
        parts = {
            part for part in (*windows.parts, *posix.parts) if part not in {"", "."}
        }
        if ".." in parts:
            raise ValueError("host relative_path must not contain parent traversal")
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("host relative_path contains control characters")
        return value


class ImportOperation(OperationBase):
    kind: Literal["io.import"]
    path: str | None = None
    file_ref: HostFileRef | None = None
    format: Literal["step", "stp", "iges", "igs", "sat", "f3d"]
    component_name: str

    @model_validator(mode="after")
    def _validate_file_request(self) -> "ImportOperation":
        if (self.path is None) == (self.file_ref is None):
            raise ValueError("io.import requires exactly one of path or file_ref")
        return self


class ExportOperation(OperationBase):
    kind: Literal["io.export"]
    target_ref: ExportTargetRef
    path: str | None = None
    file_ref: HostFileRef | None = None
    format: Literal["step", "stp", "stl", "iges", "igs", "f3d"]
    overwrite: bool = False

    @model_validator(mode="after")
    def _validate_file_request(self) -> "ExportOperation":
        if (self.path is None) == (self.file_ref is None):
            raise ValueError("io.export requires exactly one of path or file_ref")
        return self


class SheetMetalOperation(OperationBase):
    kind: Literal["experimental.sheet_metal"]
    operation: Literal["create_flange", "create_bend", "flat_pattern", "unfold"]
    target_ref: GeometryRef
    parameters: dict[str, str] = Field(default_factory=dict)


class CamOperation(OperationBase):
    kind: Literal["experimental.cam"]
    operation: Literal["setup", "operation", "generate_toolpath", "post_process"]
    target_ref: GeometryRef
    parameters: dict[str, str] = Field(default_factory=dict)


OperationSpec = Annotated[
    ParameterOperation
    | ComponentCreateOperation
    | SketchCreateOperation
    | SketchRectangleOperation
    | SketchCircleOperation
    | ExtrudeOperation
    | SketchConstraintOperation
    | SketchDimensionOperation
    | RevolveOperation
    | SweepOperation
    | LoftOperation
    | PatternOperation
    | MirrorOperation
    | BooleanOperation
    | JointOperation
    | RigidGroupOperation
    | PhysicalPropertiesOperation
    | InterferenceOperation
    | ImportOperation
    | ExportOperation
    | SheetMetalOperation
    | CamOperation,
    Field(discriminator="kind"),
]

OPERATION_ADAPTER = TypeAdapter(OperationSpec)


CAPABILITY_BY_KIND: dict[str, str] = {
    "parameter.set": "parameters",
    "component.create": "components",
    "sketch.create": "sketch_create",
    "sketch.rectangle": "sketch_rectangle",
    "sketch.circle": "sketch_circle",
    "feature.extrude": "extrude",
    "sketch.constraint": "sketch_constraints",
    "sketch.dimension": "sketch_dimensions",
    "feature.revolve": "revolve",
    "feature.sweep": "sweep",
    "feature.loft": "loft",
    "feature.pattern": "patterns",
    "feature.mirror": "mirror",
    "feature.boolean": "boolean",
    "assembly.joint": "joints",
    "assembly.rigid_group": "rigid_groups",
    "analysis.physical_properties": "physical_properties",
    "analysis.interference": "interference",
    "io.import": "import",
    "io.export": "export",
    "experimental.sheet_metal": "sheet_metal_experimental",
    "experimental.cam": "cam_experimental",
}

EXPERIMENTAL_CAPABILITIES = {"sheet_metal_experimental", "cam_experimental"}


class CadSpecV2(StrictModel):
    """Complete strict operation graph for version 2 of the CAD contract."""

    cad_spec_version: Literal["2.0"] = "2.0"
    intent: str
    units: Literal["mm", "cm", "in"] = "mm"
    assumptions: list[str] = Field(default_factory=list)
    document_policy: DocumentPolicyV2 = Field(default_factory=DocumentPolicyV2)
    requirements: list[RequirementSpec] = Field(min_length=1)
    operations: list[OperationSpec] = Field(min_length=1)
    assertions: list[AssertionSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_graph(self) -> "CadSpecV2":
        requirement_ids = _unique_ids(self.requirements, "requirement")
        operation_ids = _unique_ids(self.operations, "operation")
        assertion_ids = _unique_ids(self.assertions, "assertion")
        seen_operations: set[str] = set()
        for operation in self.operations:
            unknown_dependencies = set(operation.depends_on) - operation_ids
            if unknown_dependencies:
                raise ValueError(
                    f"operation {operation.id} has unknown dependencies: {sorted(unknown_dependencies)}"
                )
            forward_dependencies = set(operation.depends_on) - seen_operations
            if forward_dependencies:
                raise ValueError(
                    f"operation {operation.id} depends on operations not yet ordered: {sorted(forward_dependencies)}"
                )
            unknown_requirements = set(operation.requirement_ids) - requirement_ids
            if unknown_requirements:
                raise ValueError(
                    f"operation {operation.id} references unknown requirements: {sorted(unknown_requirements)}"
                )
            seen_operations.add(operation.id)
        for requirement in self.requirements:
            unknown_assertions = set(requirement.assertion_ids) - assertion_ids
            if unknown_assertions:
                raise ValueError(
                    f"requirement {requirement.id} references unknown assertions: {sorted(unknown_assertions)}"
                )
            if requirement.required and not requirement.assertion_ids:
                raise ValueError(
                    f"required requirement {requirement.id} has no assertions"
                )
            if requirement.oracle == "independent" and not any(
                assertion.id in requirement.assertion_ids
                and assertion.kind == "custom_oracle"
                for assertion in self.assertions
            ):
                raise ValueError(
                    f"independent requirement {requirement.id} requires a custom_oracle assertion"
                )
        return self

    @property
    def capabilities(self) -> set[str]:
        """Return the backend capabilities required before dispatch."""

        return {_operation_capability(operation) for operation in self.operations}

    def ensure_supported(
        self,
        available: set[str],
        *,
        experimental_enabled: bool = False,
    ) -> None:
        """Fail closed when a backend cannot execute the complete operation graph."""

        required = self.capabilities
        experimental = {
            capability
            for capability in required
            if capability in EXPERIMENTAL_CAPABILITIES
            or capability.startswith("sheet_metal_")
            or capability.startswith("cam_")
        }
        if experimental and not experimental_enabled:
            raise ValueError(
                "experimental capabilities require FUSION_AGENT_EXPERIMENTAL_MANUFACTURING=1: "
                + ", ".join(sorted(experimental))
            )
        missing = required - available
        if missing:
            raise ValueError(
                "backend lacks required capabilities: " + ", ".join(sorted(missing))
            )

    def to_json_text(self) -> str:
        return self.model_dump_json(indent=2)


class NormalizedCadSpec(StrictModel):
    """Result of accepting v2 directly or translating a legacy CadSpec."""

    spec: CadSpecV2 | None = None
    legacy_spec: CadSpec | None = None
    source_version: Literal["1", "2.0"]
    warnings: list[str] = Field(default_factory=list)
    contract_eligible: bool


def parse_cad_spec(payload: str | dict[str, Any]) -> NormalizedCadSpec:
    """Parse a v2 contract or accept v1 with an explicit compatibility warning."""

    import json

    raw = json.loads(payload) if isinstance(payload, str) else dict(payload)
    if raw.get("cad_spec_version") == "2.0":
        return NormalizedCadSpec(
            spec=CadSpecV2.model_validate(raw),
            source_version="2.0",
            contract_eligible=True,
        )
    legacy = CadSpec.model_validate(raw)
    return NormalizedCadSpec(
        legacy_spec=legacy,
        source_version="1",
        contract_eligible=False,
        warnings=[
            "CadSpec v1 is deprecated and cannot claim complete requirement coverage; "
            "upgrade to cad_spec_version 2.0."
        ],
    )


PROMPT_V2_NORMALIZABLE_FEATURE_TYPES = frozenset(
    {
        "extrude_rectangle",
        "extrude_cylinder",
        "center_hole_cut",
        "hole_pattern_cut",
    }
)


def legacy_plan_v2_coverage(legacy: CadSpec) -> dict[str, Any]:
    """Describe deterministic prompt-plan normalization without dispatching.

    This intentionally reports semantic legacy feature types, not the much
    broader set accepted by caller-supplied CadSpec v2.  A recipe is complete
    only when every feature has an audited typed expansion.
    """

    feature_types = sorted(
        {
            feature.type
            for component in legacy.components
            for feature in component.features
        }
    )
    unsupported = sorted(set(feature_types) - set(PROMPT_V2_NORMALIZABLE_FEATURE_TYPES))
    return {
        "complete": not unsupported,
        "feature_types": feature_types,
        "normalizable_feature_types": sorted(
            set(feature_types) & set(PROMPT_V2_NORMALIZABLE_FEATURE_TYPES)
        ),
        "unsupported_feature_types": unsupported,
    }


def upgrade_legacy_plan_to_v2(legacy: CadSpec) -> CadSpecV2:
    """Normalize the deterministic planner's basic recipes into strict v2.

    Unsupported legacy recipe features fail during planning.  They are never
    smuggled through a generic dictionary or dispatched via arbitrary Python.
    """

    coverage = legacy_plan_v2_coverage(legacy)
    if not coverage["complete"]:
        unsupported = ", ".join(coverage["unsupported_feature_types"])
        raise ValueError(
            "CadSpec v2 prompt planner has no strict operation mapping for legacy "
            f"feature types: {unsupported}; supply an explicit cad_spec_version 2.0 "
            "document"
        )

    operations: list[dict[str, Any]] = []
    parameters = legacy.parameter_map()
    for parameter in legacy.parameters:
        operations.append(
            {
                "id": f"set_{parameter.name}",
                "kind": "parameter.set",
                "name": parameter.name,
                "expression": parameter.expression,
                "comment": parameter.comment,
                "requirement_ids": ["planned_contract"],
            }
        )

    for component in legacy.components:
        component_operation_id = f"create_{component.name}"
        operations.append(
            {
                "id": component_operation_id,
                "kind": "component.create",
                "name": component.name,
                "requirement_ids": ["planned_contract"],
            }
        )
        previous_id = component_operation_id
        known_bodies: dict[str, dict[str, Any]] = {}
        for feature in component.features:
            inputs = feature.merged_inputs()
            if feature.type in {"center_hole_cut", "hole_pattern_cut"}:
                previous_id = _append_typed_hole_cut_operations(
                    operations,
                    component_name=component.name,
                    feature_name=feature.name,
                    feature_type=feature.type,
                    inputs=inputs,
                    parameters=parameters,
                    known_bodies=known_bodies,
                    depends_on=previous_id,
                )
                continue
            if feature.operation != "new_body":
                raise ValueError(
                    "prompt-to-v2 base extrusion must be new_body before bounded typed cuts: "
                    f"{feature.name} uses {feature.operation!r}"
                )
            sketch_name = str(inputs.get("sketch_name") or f"{feature.name}_sketch")
            sketch_id = f"create_{sketch_name}"
            body_name = str(inputs.get("body_name") or f"{feature.name}_body")
            operations.append(
                {
                    "id": sketch_id,
                    "kind": "sketch.create",
                    "component_ref": component.name,
                    "plane": str(inputs.get("plane") or "XY").upper(),
                    "name": sketch_name,
                    "depends_on": [previous_id],
                    "requirement_ids": ["planned_contract"],
                }
            )
            profile_ref: str
            if feature.type == "extrude_rectangle":
                profile_ref = f"{sketch_name}:rectangle:0"
                geometry_id = f"draw_{feature.name}_rectangle"
                operations.append(
                    {
                        "id": geometry_id,
                        "kind": "sketch.rectangle",
                        "sketch_ref": sketch_name,
                        "center": inputs.get("center") or ["0 mm", "0 mm"],
                        "width": inputs["width"],
                        "height": inputs["height"],
                        "result_ref": profile_ref,
                        "depends_on": [sketch_id],
                        "requirement_ids": ["planned_contract"],
                    }
                )
            else:
                profile_ref = f"{sketch_name}:circle:0"
                geometry_id = f"draw_{feature.name}_circle"
                operations.append(
                    {
                        "id": geometry_id,
                        "kind": "sketch.circle",
                        "sketch_ref": sketch_name,
                        "center": inputs.get("center") or ["0 mm", "0 mm"],
                        "diameter": inputs.get("diameter") or inputs["outer_diameter"],
                        "result_ref": profile_ref,
                        "depends_on": [sketch_id],
                        "requirement_ids": ["planned_contract"],
                    }
                )
            operations.append(
                {
                    "id": f"extrude_{feature.name}",
                    "kind": "feature.extrude",
                    "component_ref": component.name,
                    "profile_ref": profile_ref,
                    "distance": inputs["distance"],
                    "operation": feature.operation,
                    "result_name": body_name,
                    "depends_on": [geometry_id],
                    "requirement_ids": ["planned_contract"],
                }
            )
            previous_id = f"extrude_{feature.name}"
            known_bodies[body_name] = {
                "shape": "rectangle"
                if feature.type == "extrude_rectangle"
                else "cylinder",
                "width": inputs.get("width"),
                "height": inputs.get("height"),
                "diameter": inputs.get("diameter") or inputs.get("outer_diameter"),
            }

    return CadSpecV2.model_validate(
        {
            "cad_spec_version": "2.0",
            "intent": legacy.intent,
            "units": legacy.units,
            "assumptions": legacy.assumptions,
            "document_policy": legacy.document_policy.model_dump(mode="json"),
            "requirements": [
                {
                    "id": "planned_contract",
                    "description": "All explicit acceptance checks from the deterministic plan pass",
                    "oracle": "independent",
                    "assertion_ids": ["planned_acceptance_checks"],
                }
            ],
            "operations": operations,
            "assertions": [
                {
                    "id": "planned_acceptance_checks",
                    "kind": "custom_oracle",
                    "expected": [
                        acceptance.model_dump(mode="json")
                        for acceptance in legacy.acceptance_tests
                    ],
                }
            ],
        }
    )


def _append_typed_hole_cut_operations(
    operations: list[dict[str, Any]],
    *,
    component_name: str,
    feature_name: str,
    feature_type: str,
    inputs: dict[str, Any],
    parameters: dict[str, str],
    known_bodies: dict[str, dict[str, Any]],
    depends_on: str,
) -> str:
    """Expand only geometrically bounded legacy hole recipes to typed ops."""

    target_body = str(inputs.get("target_body") or "")
    body = known_bodies.get(target_body)
    if body is None:
        raise ValueError(
            f"typed hole cut {feature_name} requires one previously planned target body: "
            f"{target_body!r}"
        )
    diameter = str(inputs.get("diameter") or "")
    distance = str(inputs.get("distance") or "")
    diameter_mm = expression_to_mm(diameter, parameters)
    distance_mm = expression_to_mm(distance, parameters)
    if diameter_mm <= 0 or distance_mm <= 0:
        raise ValueError(f"typed hole cut {feature_name} requires positive dimensions")

    if feature_type == "center_hole_cut":
        _validate_center_hole_fit(feature_name, diameter_mm, body, parameters)
        centers = [(0.0, 0.0)]
    else:
        if body.get("shape") != "rectangle":
            raise ValueError(
                f"typed four-hole pattern {feature_name} requires a rectangular target body"
            )
        count = inputs.get("count")
        if count != 4:
            raise ValueError(
                f"typed hole pattern {feature_name} supports exactly four symmetric holes"
            )
        width_mm = expression_to_mm(str(body.get("width") or ""), parameters)
        height_mm = expression_to_mm(str(body.get("height") or ""), parameters)
        offset_mm = expression_to_mm(str(inputs.get("offset") or ""), parameters)
        x = width_mm / 2.0 - offset_mm
        y = height_mm / 2.0 - offset_mm
        if x <= diameter_mm / 2.0 or y <= diameter_mm / 2.0:
            raise ValueError(
                f"typed hole pattern {feature_name} does not fit inside the target body"
            )
        centers = [(-x, -y), (-x, y), (x, -y), (x, y)]

    previous_id = depends_on
    base_sketch_name = str(inputs.get("sketch_name") or f"{feature_name}_sketch")
    for index, (x_mm, y_mm) in enumerate(centers, start=1):
        suffix = "" if len(centers) == 1 else f"_{index:02d}"
        sketch_name = f"{base_sketch_name}{suffix}"
        sketch_id = f"create_{sketch_name}"
        profile_ref = f"{sketch_name}:circle:0"
        circle_id = f"draw_{feature_name}_circle{suffix}"
        cut_id = f"cut_{feature_name}{suffix}"
        operations.extend(
            [
                {
                    "id": sketch_id,
                    "kind": "sketch.create",
                    "component_ref": component_name,
                    "plane": str(inputs.get("plane") or "XY").upper(),
                    "name": sketch_name,
                    "depends_on": [previous_id],
                    "requirement_ids": ["planned_contract"],
                },
                {
                    "id": circle_id,
                    "kind": "sketch.circle",
                    "sketch_ref": sketch_name,
                    "center": [_mm_expression(x_mm), _mm_expression(y_mm)],
                    "diameter": diameter,
                    "result_ref": profile_ref,
                    "depends_on": [sketch_id],
                    "requirement_ids": ["planned_contract"],
                },
                {
                    "id": cut_id,
                    "kind": "feature.extrude",
                    "component_ref": component_name,
                    "profile_ref": profile_ref,
                    "distance": distance,
                    "operation": "cut",
                    "target_body_ref": target_body,
                    # Autodesk returns the modified target in CutFeature.bodies;
                    # preserving its exact name avoids reference drift.
                    "result_name": target_body,
                    "depends_on": [circle_id],
                    "requirement_ids": ["planned_contract"],
                },
            ]
        )
        previous_id = cut_id
    return previous_id


def _validate_center_hole_fit(
    feature_name: str,
    diameter_mm: float,
    body: dict[str, Any],
    parameters: dict[str, str],
) -> None:
    if body.get("shape") == "cylinder":
        outer_mm = expression_to_mm(str(body.get("diameter") or ""), parameters)
        fits = diameter_mm < outer_mm
    else:
        width_mm = expression_to_mm(str(body.get("width") or ""), parameters)
        height_mm = expression_to_mm(str(body.get("height") or ""), parameters)
        fits = diameter_mm < min(width_mm, height_mm)
    if not fits:
        raise ValueError(
            f"typed center hole {feature_name} does not fit inside target body"
        )


def _mm_expression(value: float) -> str:
    normalized = 0.0 if abs(value) < 1e-12 else value
    return f"{normalized:.9g} mm"


def _unique_ids(items: list[Any], label: str) -> set[str]:
    values = [str(item.id) for item in items]
    if len(values) != len(set(values)):
        raise ValueError(f"{label} ids must be unique")
    return set(values)


def _operation_capability(operation: OperationSpec) -> str:
    """Return the most specific capability required by one operation."""

    if isinstance(operation, PatternOperation):
        return f"pattern_{operation.pattern}"
    if isinstance(operation, BooleanOperation):
        return "split_body" if operation.operation == "split" else "boolean"
    if isinstance(operation, JointOperation):
        if operation.limits:
            return "joint_with_limits"
        return "as_built_joint" if operation.joint_type == "as_built_rigid" else "joint"
    if isinstance(operation, ImportOperation):
        return f"import_{operation.format}"
    if isinstance(operation, ExportOperation):
        return f"export_{operation.format}"
    if isinstance(operation, SheetMetalOperation):
        return f"sheet_metal_{operation.operation}"
    if isinstance(operation, CamOperation):
        return f"cam_{operation.operation}"
    return CAPABILITY_BY_KIND[operation.kind]
