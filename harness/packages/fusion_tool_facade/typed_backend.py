"""Typed CadSpec v2 adapter for the optional Faust MCP surface.

Only curated tools are mapped.  In particular, ``execute_code`` and
``delete_all`` are never placed in the adapter policy.
"""

from __future__ import annotations

import re
from typing import Any

from cad_spec.unit_policy import expression_to_mm
from cad_spec.v2 import (
    BooleanOperation,
    CamOperation,
    ExportOperation,
    ImportOperation,
    InterferenceOperation,
    JointOperation,
    LoftOperation,
    MirrorOperation,
    OperationSpec,
    ParameterOperation,
    PatternOperation,
    PhysicalPropertiesOperation,
    RevolveOperation,
    RigidGroupOperation,
    SheetMetalOperation,
    SketchConstraintOperation,
    SketchDimensionOperation,
    SweepOperation,
)
from fusion_mcp_adapter.adapter import FusionMcpAdapter
from fusion_mcp_adapter.client import McpClient
from fusion_mcp_adapter.policy import ToolPolicy
from fusion_mcp_adapter.semantics import McpCallOptions
from fusion_mcp_adapter.tool_result import ToolManifest


_TOOL_CAPABILITIES: dict[str, set[str]] = {
    "parameters": {"create_parameter"},
    "sketch_constraints": {"add_constraint"},
    "sketch_dimensions": {"add_dimension"},
    "revolve": {"revolve"},
    "sweep": {"sweep"},
    "loft": {"loft"},
    "pattern_rectangular": {"rectangular_pattern"},
    "pattern_circular": {"circular_pattern"},
    "mirror": {"mirror"},
    "boolean": {"boolean_operation"},
    "split_body": {"split_body"},
    "joint": {"add_joint"},
    "as_built_joint": {"create_as_built_joint"},
    "rigid_groups": {"create_rigid_group"},
    "physical_properties": {"get_physical_properties"},
    "interference": {"check_interference"},
    "export_step": {"export_step"},
    "export_stp": {"export_step"},
    "export_stl": {"export_stl"},
    "export_f3d": {"export_f3d"},
    "sheet_metal_create_flange": {"create_flange"},
    "sheet_metal_create_bend": {"create_bend"},
    "sheet_metal_flat_pattern": {"flat_pattern"},
    "sheet_metal_unfold": {"unfold"},
    "cam_setup": {"cam_create_setup"},
    "cam_operation": {"cam_create_operation"},
    "cam_generate_toolpath": {"cam_generate_toolpath"},
    "cam_post_process": {"cam_post_process"},
}

_READ_TOOLS = {"get_physical_properties", "check_interference"}
_BLOCKED_TOOLS = {"execute_code", "delete_all"}
FAUST_IMPLEMENTED_CAPABILITIES = frozenset(_TOOL_CAPABILITIES)


class FaustOperationDispatchError(RuntimeError):
    """Faust failure carrying authoritative stdio dispatch evidence."""

    def __init__(self, message: str, *, error_code: str | None, transport: dict[str, Any]) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.transport = transport


class FaustTypedBackend:
    """Map strict operations to Faust 0.1.0's explicit tool schemas."""

    provider = "faust_stdio"

    def __init__(self, adapter: FusionMcpAdapter, manifest: ToolManifest) -> None:
        self.adapter = adapter
        self.manifest = manifest
        self._tool_names = manifest.names()
        self._plans: dict[str, list[tuple[str, dict[str, Any]]]] = {}

    @classmethod
    def from_client(cls, client: McpClient, manifest: ToolManifest) -> "FaustTypedBackend":
        allowed = manifest.names() - _BLOCKED_TOOLS
        adapter = FusionMcpAdapter(
            client=client,
            manifest=manifest,
            policy=ToolPolicy.from_manifest(allowed),
        )
        return cls(adapter, manifest)

    @property
    def capabilities(self) -> set[str]:
        return {
            capability
            for capability, required_tools in _TOOL_CAPABILITIES.items()
            if required_tools <= self._tool_names
        }

    def preflight_operations(self, operations: list[OperationSpec]) -> None:
        """Compile every native call before the first one can be dispatched."""

        parameters: dict[str, str] = {}
        plans: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        for operation in operations:
            plans[operation.id] = _faust_calls(operation, parameters)
            if isinstance(operation, ParameterOperation):
                parameters[operation.name] = operation.expression
        self._plans = plans

    async def execute_operation(self, operation: OperationSpec) -> dict[str, Any]:
        calls = self._plans.get(operation.id)
        if calls is None:
            raise RuntimeError("Faust operation was not preflighted")
        results: list[dict[str, Any]] = []
        for tool, arguments in calls:
            options = (
                McpCallOptions.for_read()
                if tool in _READ_TOOLS
                else McpCallOptions.for_mutation()
            )
            result = await self.adapter.call(tool, arguments, options=options)
            if not result.ok:
                raise FaustOperationDispatchError(
                    f"Faust typed operation {operation.id} failed: "
                    f"{result.error_code}: {result.error_message}",
                    error_code=result.error_code,
                    transport=_transport_evidence(result),
                )
            results.append(
                {
                    "native_tool": tool,
                    "data": result.data,
                    "transport": _transport_evidence(result),
                    "manifest_fingerprint": self.manifest.fingerprint,
                }
            )
        return {"provider": self.provider, "calls": results}


def _faust_calls(
    operation: OperationSpec,
    parameters: dict[str, str],
) -> list[tuple[str, dict[str, Any]]]:
    if isinstance(operation, ParameterOperation):
        value, unit = _numeric_unit(operation.expression, parameters)
        return [("create_parameter", {"name": operation.name, "value": value, "unit": unit, "comment": operation.comment or ""})]
    if isinstance(operation, SketchConstraintOperation):
        refs = [_entity_index(value) for value in operation.entity_refs]
        payload: dict[str, Any] = {
            "constraint_type": "fix" if operation.constraint == "fixed" else operation.constraint,
            "entity_one": refs[0],
            "sketch_name": operation.sketch_ref,
        }
        if len(refs) > 1:
            payload["entity_two"] = refs[1]
        return [("add_constraint", payload)]
    if isinstance(operation, SketchDimensionOperation):
        refs = [_entity_index(value) for value in operation.entity_refs]
        dimension_type = {"angle": "angular", "radius": "radial"}.get(
            operation.dimension, operation.dimension
        )
        value = (
            _angle_degrees(operation.expression, parameters)
            if operation.dimension == "angle"
            else expression_to_mm(operation.expression, parameters) / 10.0
        )
        payload = {
            "dimension_type": dimension_type,
            "value": value,
            "entity_one": refs[0],
            "sketch_name": operation.sketch_ref,
        }
        if len(refs) > 1:
            payload["entity_two"] = refs[1]
        return [("add_dimension", payload)]
    if isinstance(operation, RevolveOperation):
        axis = _axis(operation.axis_ref)
        direction = {
            "axis_direction_x": 1 if axis == "x" else 0,
            "axis_direction_y": 1 if axis == "y" else 0,
            "axis_direction_z": 1 if axis == "z" else 0,
        }
        return [("revolve", {
            "angle": _angle_degrees(operation.angle, parameters),
            "profile_index": _entity_index(operation.profile_ref, default=0),
            "operation": operation.operation,
            **direction,
        })]
    if isinstance(operation, SweepOperation):
        path_name, path_index = _name_and_index(operation.path_ref)
        return [("sweep", {
            "profile_index": _entity_index(operation.profile_ref, default=0),
            "path_sketch_name": path_name,
            "path_curve_index": path_index,
            "operation": operation.operation,
        })]
    if isinstance(operation, LoftOperation):
        return [("loft", {"profile_sketch_names": operation.profile_refs, "operation": operation.operation})]
    if isinstance(operation, PatternOperation):
        if len(operation.target_refs) != 1:
            raise ValueError("Faust patterns require exactly one target_ref")
        if operation.pattern == "rectangular":
            return [("rectangular_pattern", {
                "body_name": operation.target_refs[0],
                "x_count": operation.count,
                "x_spacing": expression_to_mm(operation.spacing or "0 mm", parameters) / 10.0,
                "y_count": 1,
                "y_spacing": 0.0,
            })]
        if operation.pattern == "circular":
            return [("circular_pattern", {
                "body_name": operation.target_refs[0],
                "count": operation.count,
                "axis": _axis(operation.axis_ref or "z"),
                "total_angle": 360.0,
            })]
        raise ValueError("Faust 0.1.0 does not support path patterns")
    if isinstance(operation, MirrorOperation):
        if len(operation.target_refs) != 1:
            raise ValueError("Faust mirror requires exactly one target_ref")
        return [("mirror", {"body_name": operation.target_refs[0], "mirror_plane": _plane(operation.plane_ref)})]
    if isinstance(operation, BooleanOperation):
        if len(operation.tool_refs) != 1:
            raise ValueError("Faust boolean/split requires exactly one tool_ref")
        if operation.operation == "split":
            return [("split_body", {
                "body_name": operation.target_ref,
                "splitting_body": operation.tool_refs[0],
                "extend_tool": True,
            })]
        return [("boolean_operation", {
            "target_body": operation.target_ref,
            "tool_body": operation.tool_refs[0],
            "operation": operation.operation,
        })]
    if isinstance(operation, JointOperation):
        if operation.limits:
            raise ValueError("Faust 0.1.0 joint mapping does not support limits")
        tool = "create_as_built_joint" if operation.joint_type == "as_built_rigid" else "add_joint"
        joint_type = "rigid" if operation.joint_type == "as_built_rigid" else operation.joint_type
        return [(tool, {"component_one": operation.parent_ref, "component_two": operation.child_ref, "joint_type": joint_type})]
    if isinstance(operation, RigidGroupOperation):
        return [("create_rigid_group", {"component_names": operation.occurrence_refs, "include_children": True})]
    if isinstance(operation, PhysicalPropertiesOperation):
        return [("get_physical_properties", {"body_name": target, "accuracy": "high"}) for target in operation.target_refs]
    if isinstance(operation, InterferenceOperation):
        if len(operation.target_refs) < 2:
            raise ValueError("Faust interference requires at least two target_refs")
        return [("check_interference", {"component_names": operation.target_refs, "include_coincident_faces": False})]
    if isinstance(operation, ImportOperation):
        raise ValueError("Faust 0.1.0 does not expose import tools")
    if isinstance(operation, ExportOperation):
        tool = {"step": "export_step", "stp": "export_step", "stl": "export_stl", "f3d": "export_f3d"}.get(operation.format)
        if not tool:
            raise ValueError(f"Faust 0.1.0 does not export {operation.format}")
        payload = {"file_path": operation.path}
        if tool != "export_f3d":
            payload["body_name"] = operation.target_ref
        return [(tool, payload)]
    if isinstance(operation, SheetMetalOperation):
        tool = {
            "create_flange": "create_flange",
            "create_bend": "create_bend",
            "flat_pattern": "flat_pattern",
            "unfold": "unfold",
        }[operation.operation]
        return [(tool, _coerce_experimental_payload(operation.target_ref, operation.parameters))]
    if isinstance(operation, CamOperation):
        tool = {
            "setup": "cam_create_setup",
            "operation": "cam_create_operation",
            "generate_toolpath": "cam_generate_toolpath",
            "post_process": "cam_post_process",
        }[operation.operation]
        return [(tool, _coerce_experimental_payload(operation.target_ref, operation.parameters))]
    raise TypeError(f"unsupported Faust typed operation: {type(operation).__name__}")


def _numeric_unit(expression: str, parameters: dict[str, str]) -> tuple[float, str]:
    current = parameters.get(expression, expression)
    match = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*(mm|cm|in|deg|rad)\s*", current, re.IGNORECASE)
    if not match:
        raise ValueError(f"Faust requires a resolvable numeric unit expression: {expression!r}")
    return float(match.group(1)), match.group(2).lower()


def _angle_degrees(expression: str, parameters: dict[str, str]) -> float:
    value, unit = _numeric_unit(expression, parameters)
    if unit == "deg":
        return value
    if unit == "rad":
        return value * 180.0 / 3.141592653589793
    raise ValueError(f"angle must use deg or rad: {expression!r}")


def _entity_index(reference: str, *, default: int | None = None) -> int:
    match = re.search(r"(?:^|[#:\[])(\d+)\]?$", reference)
    if match:
        return int(match.group(1))
    if default is not None:
        return default
    raise ValueError(f"entity reference must end in a numeric index: {reference!r}")


def _name_and_index(reference: str) -> tuple[str, int]:
    match = re.fullmatch(r"(.+?)(?:[#:]([0-9]+))?", reference)
    if not match:
        raise ValueError(f"invalid indexed reference: {reference!r}")
    return match.group(1), int(match.group(2) or 0)


def _axis(reference: str) -> str:
    lowered = reference.lower()
    for axis in ("x", "y", "z"):
        if lowered in {axis, f"{axis}_axis", f"axis_{axis}"}:
            return axis
    raise ValueError(f"Faust axis reference must be x, y, or z: {reference!r}")


def _plane(reference: str) -> str:
    lowered = reference.lower().replace("_plane", "")
    if lowered in {"xy", "yz", "xz"}:
        return lowered
    raise ValueError(f"Faust plane reference must be xy, yz, or xz: {reference!r}")


def _coerce_experimental_payload(target_ref: str, parameters: dict[str, str]) -> dict[str, Any]:
    payload: dict[str, Any] = {"body_name": target_ref}
    for key, value in parameters.items():
        match = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*(mm|cm|in|deg|rad)\s*", value, re.IGNORECASE)
        payload[key] = float(match.group(1)) if match else value
    return payload


def _transport_evidence(result: Any) -> dict[str, Any]:
    transport = getattr(result, "meta", {}).get("fusion_agent_transport")
    if isinstance(transport, dict):
        return dict(transport)
    data = getattr(result, "data", {})
    if isinstance(data, dict):
        return {
            key: data[key]
            for key in (
                "dispatched",
                "may_have_applied",
                "post_dispatch_replay_suppressed",
                "mutation_outcome",
            )
            if key in data
        }
    return {}
