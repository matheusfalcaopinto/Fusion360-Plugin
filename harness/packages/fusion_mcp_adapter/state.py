"""State models returned by the mock adapter and facade."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class BodyState(BaseModel):
    """A simplified Fusion body representation."""

    name: str
    component: str
    bbox_expr: list[str] = Field(default_factory=list)
    bounding_box_mm: list[float] = Field(default_factory=list)
    holes: int = 0
    valid: bool = True


class ComponentState(BaseModel):
    """A simplified Fusion component representation."""

    name: str
    bodies: list[str] = Field(default_factory=list)
    sketches: list[str] = Field(default_factory=list)
    features: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class FusionState(BaseModel):
    """Programmatically inspectable design state."""

    active_document: bool = True
    units: str = "mm"
    root_component: str = "root"
    active_component: str = "root"
    components: dict[str, ComponentState] = Field(default_factory=dict)
    bodies: dict[str, BodyState] = Field(default_factory=dict)
    sketches: dict[str, dict[str, Any]] = Field(default_factory=dict)
    features: dict[str, dict[str, Any]] = Field(default_factory=dict)
    parameters: dict[str, str] = Field(default_factory=dict)
    nema17_metrics: dict[str, Any] = Field(default_factory=dict)
    polish_metrics: dict[str, Any] = Field(default_factory=dict)
    assembly_metrics: dict[str, Any] = Field(default_factory=dict)
    profile2020_metrics: dict[str, Any] = Field(default_factory=dict)
    mgn12_metrics: dict[str, Any] = Field(default_factory=dict)
    cnc_metrics: dict[str, Any] = Field(default_factory=dict)
    component_metadata: dict[str, dict[str, Any]] = Field(default_factory=dict)
    joints: dict[str, dict[str, Any]] = Field(default_factory=dict)
    occurrences: dict[str, dict[str, Any]] = Field(default_factory=dict)
    physical_properties: dict[str, dict[str, Any]] = Field(default_factory=dict)
    interference: dict[str, Any] = Field(default_factory=dict)
    screenshots: dict[str, dict[str, Any]] = Field(default_factory=dict)
    exports: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @property
    def body_count(self) -> int:
        """Return the number of valid bodies."""

        return sum(1 for body in self.bodies.values() if body.valid)

    @property
    def component_count(self) -> int:
        """Return non-root component count."""

        return sum(1 for name in self.components if name != self.root_component)

    @property
    def hole_count(self) -> int:
        """Return total modeled hole count."""

        return sum(body.holes for body in self.bodies.values())
