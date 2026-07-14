"""Pydantic models for the CAD Spec contract."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cad_spec.naming_policy import validate_name
from cad_spec.unit_policy import reject_ambiguous_numeric_dimensions, validate_dimension_expression


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
    type: str
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
        return self

    def parameter_map(self) -> dict[str, str]:
        """Return parameter expressions keyed by name."""

        return {parameter.name: parameter.expression for parameter in self.parameters}

    def to_json_text(self) -> str:
        """Serialize the spec in a stable JSON representation."""

        return self.model_dump_json(indent=2)
