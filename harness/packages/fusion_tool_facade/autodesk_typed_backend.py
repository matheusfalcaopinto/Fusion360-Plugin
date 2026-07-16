"""Typed CadSpec v2 subset for Autodesk's local Fusion MCP endpoint.

The Autodesk endpoint currently exposes a compact CRUD/script bridge rather
than one native tool per CadSpec operation.  This adapter deliberately maps
only operations backed by fixed, repository-owned facade scripts.  Missing
feature capabilities are reported during whole-graph preflight; there is no
fallback to model-authored Python or to another MCP provider.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from agent_core.authority import CadTargetBinding, BoundOperation, revalidate_host_path
from cad_spec.v2 import (
    BooleanOperation,
    ComponentCreateOperation,
    ExtrudeOperation,
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
    SketchCircleOperation,
    SketchConstraintOperation,
    SketchCreateOperation,
    SketchDimensionOperation,
    SketchRectangleOperation,
    SweepOperation,
)
from fusion_mcp_adapter.adapter import FusionMcpAdapter
from fusion_mcp_adapter.client import McpClient
from fusion_mcp_adapter.policy import ToolPolicy
from fusion_mcp_adapter.tool_result import ToolManifest
from fusion_tool_facade.vendor_facade import PreparedFusionOperation, VendorFusionFacade


_CRUD_TOOLS = {"fusion_mcp_read", "fusion_mcp_execute"}
_DIRECT_TOOLS = {"create_parameter"}
AUTODESK_IMPLEMENTED_CAPABILITIES = frozenset(
    {
        "parameters",
        "components",
        "sketch_create",
        "sketch_rectangle",
        "sketch_circle",
        "extrude",
        "sketch_constraints",
        "sketch_dimensions",
        "revolve",
        "sweep",
        "loft",
        "pattern_rectangular",
        "pattern_circular",
        "pattern_path",
        "mirror",
        "boolean",
        "split_body",
        "joint",
        "joint_with_limits",
        "as_built_joint",
        "rigid_groups",
        "physical_properties",
        "interference",
        "import_step",
        "import_stp",
        "import_iges",
        "import_igs",
        "import_sat",
        "import_f3d",
        "export_step",
        "export_stp",
        "export_stl",
        "export_iges",
        "export_igs",
        "export_f3d",
    }
)

_CRUD_CAPABILITIES = AUTODESK_IMPLEMENTED_CAPABILITIES
_ENTITY_REF_RE = re.compile(
    r"^(?:(?P<sketch>.+?)[/:])?(?P<kind>line|circle|arc|point|curve)(?:#|:)(?P<index>\d+)$",
    re.IGNORECASE,
)
_PROFILE_REF_RE = re.compile(
    r"^(?:(?P<sketch_a>.+?)[/:]profile(?:#|:)(?P<index_a>\d+)|"
    r"profile:(?P<sketch_b>.+?):(?P<index_b>\d+))$",
    re.IGNORECASE,
)
_FORMAT_EXTENSIONS = {
    "step": {".step", ".stp"},
    "stp": {".step", ".stp"},
    "stl": {".stl"},
    "iges": {".iges", ".igs"},
    "igs": {".iges", ".igs"},
    "sat": {".sat"},
    "f3d": {".f3d"},
}


class AutodeskTypedBackend:
    """Execute the explicitly supported Autodesk capability subset."""

    provider = "autodesk_http"

    def __init__(self, facade: VendorFusionFacade, manifest: ToolManifest) -> None:
        self.facade = facade
        self.manifest = manifest
        self._tool_names = manifest.names()
        self._profile_shapes: dict[str, tuple[str, dict[str, Any]]] = {}
        self._prepared: dict[str, PreparedFusionOperation] = {}
        self._preflighted_operation_ids: set[str] = set()
        self._bound_operations: dict[str, BoundOperation] = {}

    @classmethod
    def from_client(
        cls, client: McpClient, manifest: ToolManifest
    ) -> "AutodeskTypedBackend":
        allowed = manifest.names() & (_CRUD_TOOLS | _DIRECT_TOOLS)
        adapter = FusionMcpAdapter(
            client=client,
            manifest=manifest,
            policy=ToolPolicy.from_manifest(allowed),
        )
        return cls(
            VendorFusionFacade(adapter, available_tools=manifest.names()), manifest
        )

    @property
    def capabilities(self) -> set[str]:
        capabilities: set[str] = set()
        has_crud = _CRUD_TOOLS <= self._tool_names
        if has_crud or "create_parameter" in self._tool_names:
            capabilities.add("parameters")
        if has_crud:
            capabilities.update(_CRUD_CAPABILITIES)
        return capabilities

    async def resolve_cad_target_binding(
        self, operation: ExportOperation
    ) -> CadTargetBinding:
        """Resolve one live export target without granting mutation authority."""

        if not isinstance(operation, ExportOperation):
            raise ValueError("Autodesk CAD target binding supports exports only")
        if not _CRUD_TOOLS <= self._tool_names:
            raise ValueError(
                "Autodesk export requires the CRUD read/execute profile for lossless binding"
            )
        payload = await self.facade.resolve_export_target_binding(
            str(operation.target_ref), operation.format
        )
        binding = payload.get("binding")
        if not isinstance(binding, dict):
            raise ValueError("Autodesk export target binding response is incomplete")
        return CadTargetBinding(
            reference_kind=str(binding.get("reference_kind") or ""),
            requested_ref=str(binding.get("requested_ref") or ""),
            document_identity=str(binding.get("document_identity") or ""),
            entity_identity=str(binding.get("entity_identity") or ""),
            fingerprint=str(binding.get("fingerprint") or ""),
        )

    def preflight_operations(
        self,
        operations: list[OperationSpec],
        *,
        bound_operations: dict[str, BoundOperation] | None = None,
    ) -> None:
        """Compile every fixed script and reject malformed refs before dispatch."""

        # A failed second preflight must never leave a previously executable
        # graph behind.
        self._profile_shapes = {}
        self._prepared = {}
        self._preflighted_operation_ids = set()
        self._bound_operations = {}
        bound_operations = bound_operations or {}

        supported = (
            ParameterOperation,
            ComponentCreateOperation,
            SketchCreateOperation,
            SketchRectangleOperation,
            SketchCircleOperation,
            ExtrudeOperation,
            SketchConstraintOperation,
            SketchDimensionOperation,
            RevolveOperation,
            SweepOperation,
            LoftOperation,
            PatternOperation,
            MirrorOperation,
            BooleanOperation,
            JointOperation,
            RigidGroupOperation,
            PhysicalPropertiesOperation,
            InterferenceOperation,
            ImportOperation,
            ExportOperation,
        )
        unsupported = [
            operation.kind
            for operation in operations
            if not isinstance(operation, supported)
        ]
        if unsupported:
            raise ValueError(
                "Autodesk typed backend has no fixed facade mapping for: "
                + ", ".join(sorted(set(unsupported)))
            )

        profiles: dict[str, dict[str, Any]] = {}
        profile_shapes: dict[str, tuple[str, dict[str, Any]]] = {}
        profile_sketches: set[str] = set()
        planned_sketches: set[str] = set()
        planned_components: set[str] = set()
        planned_results: set[str] = set()
        entity_counts: dict[str, dict[str, int]] = {}
        prepared: dict[str, PreparedFusionOperation] = {}

        for operation in operations:
            if isinstance(operation, ParameterOperation):
                continue
            if isinstance(operation, ComponentCreateOperation):
                if operation.parent_ref:
                    raise ValueError(
                        f"Autodesk typed component.create does not support parent_ref: {operation.id}"
                    )
                _add_unique(planned_components, operation.name, "component")
                continue
            if isinstance(operation, SketchCreateOperation):
                _validate_reference_text(operation.component_ref, "component_ref")
                _add_unique(planned_sketches, operation.name, "sketch")
                entity_counts[operation.name] = {
                    "line": 0,
                    "circle": 0,
                    "arc": 0,
                    "point": 0,
                    "curve": 0,
                }
                continue
            if isinstance(operation, SketchRectangleOperation):
                _validate_reference_text(operation.sketch_ref, "sketch_ref")
                _register_profile(
                    profiles,
                    profile_shapes,
                    profile_sketches,
                    operation.result_ref,
                    operation.sketch_ref,
                    "rectangle",
                    {"width": operation.width, "height": operation.height},
                )
                counts = entity_counts.setdefault(
                    operation.sketch_ref, _empty_entity_counts()
                )
                counts["line"] += 4
                counts["point"] += 4
                counts["curve"] += 4
                continue
            if isinstance(operation, SketchCircleOperation):
                _validate_reference_text(operation.sketch_ref, "sketch_ref")
                _register_profile(
                    profiles,
                    profile_shapes,
                    profile_sketches,
                    operation.result_ref,
                    operation.sketch_ref,
                    "cylinder",
                    {"diameter": operation.diameter},
                )
                counts = entity_counts.setdefault(
                    operation.sketch_ref, _empty_entity_counts()
                )
                counts["circle"] += 1
                counts["point"] += 1
                counts["curve"] += 1
                continue
            if isinstance(operation, ExtrudeOperation):
                _validate_reference_text(operation.component_ref, "component_ref")
                if operation.profile_ref not in profiles:
                    raise ValueError(
                        f"Autodesk extrusion {operation.id} references an unplanned profile: "
                        f"{operation.profile_ref}; existing profiles must use an explicit "
                        "<sketch>/profile#<index> reference in a supported typed feature"
                    )
                if (
                    operation.operation == "new_body"
                    or operation.result_name not in planned_results
                ):
                    _add_unique(
                        planned_results, operation.result_name, "feature result"
                    )
                continue
            if isinstance(operation, SketchConstraintOperation):
                entities = [
                    _normalize_entity_ref(
                        reference,
                        default_sketch=operation.sketch_ref,
                        planned_sketches=planned_sketches,
                        entity_counts=entity_counts,
                    )
                    for reference in operation.entity_refs
                ]
                _validate_constraint_entities(operation.constraint, entities)
                prepared[operation.id] = self.facade.prepare_typed_operation(
                    "sketch_constraint",
                    {
                        "sketch": operation.sketch_ref,
                        "constraint": operation.constraint,
                        "entities": entities,
                    },
                )
                continue
            if isinstance(operation, SketchDimensionOperation):
                entities = [
                    _normalize_entity_ref(
                        reference,
                        default_sketch=operation.sketch_ref,
                        planned_sketches=planned_sketches,
                        entity_counts=entity_counts,
                    )
                    for reference in operation.entity_refs
                ]
                _validate_dimension_entities(operation.dimension, entities)
                prepared[operation.id] = self.facade.prepare_typed_operation(
                    "sketch_dimension",
                    {
                        "sketch": operation.sketch_ref,
                        "dimension": operation.dimension,
                        "entities": entities,
                        "expression": operation.expression,
                    },
                )
                continue
            if isinstance(operation, RevolveOperation):
                profile = _normalize_profile_ref(operation.profile_ref, profiles)
                axis = _normalize_axis(operation.axis_ref)
                _add_unique(planned_results, operation.result_name, "feature result")
                prepared[operation.id] = self.facade.prepare_typed_operation(
                    "revolve",
                    {
                        "component": _validate_reference_text(
                            operation.component_ref, "component_ref"
                        ),
                        "profile": profile,
                        "axis": axis,
                        "angle": operation.angle,
                        "operation": operation.operation,
                        "feature_name": operation.id,
                        "result_name": operation.result_name,
                    },
                )
                continue
            if isinstance(operation, SweepOperation):
                profile = _normalize_profile_ref(operation.profile_ref, profiles)
                path = _normalize_path_ref(
                    operation.path_ref,
                    planned_sketches=planned_sketches,
                    entity_counts=entity_counts,
                )
                _add_unique(planned_results, operation.result_name, "feature result")
                prepared[operation.id] = self.facade.prepare_typed_operation(
                    "sweep",
                    {
                        "component": _validate_reference_text(
                            operation.component_ref, "component_ref"
                        ),
                        "profile": profile,
                        "path": path,
                        "orientation": operation.orientation,
                        "operation": operation.operation,
                        "feature_name": operation.id,
                        "result_name": operation.result_name,
                    },
                )
                continue
            if isinstance(operation, LoftOperation):
                normalized_profiles = [
                    _normalize_profile_ref(reference, profiles)
                    for reference in operation.profile_refs
                ]
                if len(
                    {(item["sketch"], item["index"]) for item in normalized_profiles}
                ) != len(normalized_profiles):
                    raise ValueError(
                        f"Autodesk typed loft {operation.id} repeats a profile reference"
                    )
                guides = [
                    _normalize_path_ref(
                        reference,
                        planned_sketches=planned_sketches,
                        entity_counts=entity_counts,
                    )
                    for reference in operation.guide_refs
                ]
                _add_unique(planned_results, operation.result_name, "feature result")
                prepared[operation.id] = self.facade.prepare_typed_operation(
                    "loft",
                    {
                        "component": _validate_reference_text(
                            operation.component_ref, "component_ref"
                        ),
                        "profiles": normalized_profiles,
                        "guides": guides,
                        "operation": operation.operation,
                        "feature_name": operation.id,
                        "result_name": operation.result_name,
                    },
                )
                continue
            if isinstance(operation, PatternOperation):
                targets = [
                    _validate_reference_text(value, "pattern target_ref")
                    for value in operation.target_refs
                ]
                if len(set(targets)) != len(targets):
                    raise ValueError(
                        f"Autodesk pattern {operation.id} repeats a target_ref"
                    )
                payload: dict[str, Any] = {
                    "pattern": operation.pattern,
                    "targets": targets,
                    "count": operation.count,
                    "feature_name": operation.id,
                }
                if operation.pattern == "rectangular":
                    payload.update(
                        {
                            "axis": _normalize_axis(operation.axis_ref or "x"),
                            "spacing": operation.spacing,
                        }
                    )
                elif operation.pattern == "circular":
                    payload.update({"axis": _normalize_axis(operation.axis_ref or "")})
                else:
                    if not operation.spacing:
                        raise ValueError(
                            f"Autodesk path pattern {operation.id} requires explicit spacing"
                        )
                    payload.update(
                        {
                            "path": _normalize_path_ref(
                                operation.path_ref or "",
                                planned_sketches=planned_sketches,
                                entity_counts=entity_counts,
                            ),
                            "spacing": operation.spacing,
                        }
                    )
                prepared[operation.id] = self.facade.prepare_typed_operation(
                    "pattern", payload
                )
                continue
            if isinstance(operation, MirrorOperation):
                targets = [
                    _validate_reference_text(value, "mirror target_ref")
                    for value in operation.target_refs
                ]
                if len(set(targets)) != len(targets):
                    raise ValueError(
                        f"Autodesk mirror {operation.id} repeats a target_ref"
                    )
                prepared[operation.id] = self.facade.prepare_typed_operation(
                    "mirror",
                    {
                        "targets": targets,
                        "plane": _normalize_plane(operation.plane_ref),
                        "feature_name": operation.id,
                        "result_prefix": operation.result_prefix,
                    },
                )
                continue
            if isinstance(operation, BooleanOperation):
                target = _validate_reference_text(
                    operation.target_ref, "boolean target_ref"
                )
                tools = [
                    _validate_reference_text(value, "boolean tool_ref")
                    for value in operation.tool_refs
                ]
                if target in tools or len(set(tools)) != len(tools):
                    raise ValueError(
                        f"Autodesk boolean {operation.id} requires distinct target/tool references"
                    )
                if operation.operation == "split" and len(tools) != 1:
                    raise ValueError(
                        f"Autodesk split {operation.id} requires exactly one tool_ref"
                    )
                prepared[operation.id] = self.facade.prepare_typed_operation(
                    "boolean",
                    {
                        "operation": operation.operation,
                        "target": target,
                        "tools": tools,
                        "keep_tools": operation.keep_tools,
                        "feature_name": operation.id,
                    },
                )
                continue
            if isinstance(operation, JointOperation):
                _validate_reference_text(operation.parent_ref, "joint parent_ref")
                _validate_reference_text(operation.child_ref, "joint child_ref")
                if operation.parent_ref == operation.child_ref:
                    raise ValueError(
                        f"Autodesk joint {operation.id} has identical parent and child"
                    )
                continue
            if isinstance(operation, RigidGroupOperation):
                occurrences = [
                    _validate_reference_text(value, "rigid group occurrence_ref")
                    for value in operation.occurrence_refs
                ]
                if len(set(occurrences)) != len(occurrences):
                    raise ValueError(
                        f"Autodesk rigid group {operation.id} repeats an occurrence_ref"
                    )
                prepared[operation.id] = self.facade.prepare_typed_operation(
                    "rigid_group",
                    {"name": operation.name, "occurrences": occurrences},
                )
                continue
            if isinstance(operation, PhysicalPropertiesOperation):
                for target in operation.target_refs:
                    _validate_reference_text(target, "physical property target_ref")
                continue
            if isinstance(operation, InterferenceOperation):
                if len(operation.target_refs) > 1:
                    raise ValueError(
                        f"Autodesk interference {operation.id} accepts at most one target_ref"
                    )
                for target in operation.target_refs:
                    _validate_reference_text(target, "interference target_ref")
                continue
            if isinstance(operation, ImportOperation):
                bound = bound_operations.get(operation.id)
                requested_path = (
                    bound.host_path.canonical_path
                    if bound is not None and bound.host_path is not None
                    else operation.path or ""
                )
                prepared[operation.id] = self.facade.prepare_typed_operation(
                    "import",
                    {
                        "path": _validate_io_path(
                            requested_path, operation.format, direction="import"
                        ),
                        "format": operation.format,
                        "component_name": _validate_reference_text(
                            operation.component_name, "import component_name"
                        ),
                    },
                )
                continue
            if isinstance(operation, ExportOperation):
                _validate_reference_text(operation.target_ref, "export target_ref")
                bound = bound_operations.get(operation.id)
                requested_path = (
                    bound.host_path.canonical_path
                    if bound is not None and bound.host_path is not None
                    else operation.path or ""
                )
                path = _validate_io_path(
                    requested_path, operation.format, direction="export"
                )
                if _CRUD_TOOLS <= self._tool_names:
                    binding_payload: dict[str, str] | None = None
                    if bound is not None:
                        if len(bound.target_bindings) != 1:
                            raise ValueError(
                                "Autodesk export requires one resolved CAD target binding"
                            )
                        binding = bound.target_bindings[0]
                        binding_payload = {
                            "reference_kind": binding.reference_kind,
                            "requested_ref": binding.requested_ref,
                            "document_identity": binding.document_identity,
                            "entity_identity": binding.entity_identity,
                            "fingerprint": binding.fingerprint,
                        }
                    payload = {
                        "target": operation.target_ref,
                        "path": path,
                        "format": operation.format,
                    }
                    if binding_payload is not None:
                        payload["binding"] = binding_payload
                    prepared[operation.id] = self.facade.prepare_typed_operation(
                        "export",
                        payload,
                    )

        self._profile_shapes = profile_shapes
        self._prepared = prepared
        self._preflighted_operation_ids = {operation.id for operation in operations}
        self._bound_operations = dict(bound_operations)

    def preflight_bound_operations(self, operations: list[BoundOperation]) -> None:
        """Compile a complete graph using only broker-canonicalized host paths."""

        by_id = {bound.operation.id: bound for bound in operations}
        if len(by_id) != len(operations):
            raise ValueError("bound operation ids must be unique")
        for bound in operations:
            if bound.provider != self.provider:
                raise ValueError("bound operation provider does not match Autodesk")
            if isinstance(bound.operation, (ImportOperation, ExportOperation)):
                if bound.host_path is None or bound.capability is None:
                    raise ValueError("Autodesk host I/O requires a bound capability")
                revalidate_host_path(bound.host_path)
        self.preflight_operations(
            [bound.operation for bound in operations], bound_operations=by_id
        )

    async def execute_bound_operation(self, bound: BoundOperation) -> dict[str, Any]:
        """Revalidate a stored binding immediately before the native sink."""

        operation = bound.operation
        stored = self._bound_operations.get(operation.id)
        if stored != bound:
            raise RuntimeError("Autodesk bound operation does not match preflight")
        if not isinstance(operation, (ImportOperation, ExportOperation)):
            return await self.execute_operation(operation)
        if bound.host_path is None or bound.capability is None:
            raise RuntimeError("Autodesk host I/O requires a bound capability")
        if isinstance(operation, ExportOperation) and len(bound.target_bindings) != 1:
            raise RuntimeError("Autodesk export CAD target binding is missing")
        revalidate_host_path(bound.host_path)
        prepared = self._prepared.get(operation.id)
        if prepared is not None:
            return await self.facade.execute_prepared_typed_operation(
                prepared,
                operation_id=operation.id,
            )
        raise RuntimeError("Autodesk bound I/O has no lossless dispatch plan")

    async def execute_operation(self, operation: OperationSpec) -> dict[str, Any]:
        if operation.id not in self._preflighted_operation_ids:
            raise RuntimeError(
                "Autodesk operation was not part of the completed graph preflight"
            )
        if isinstance(operation, (ImportOperation, ExportOperation)):
            raise RuntimeError(
                "Autodesk host I/O must execute through a claimed bound operation"
            )
        prepared = self._prepared.get(operation.id)
        if prepared is not None:
            return await self.facade.execute_prepared_typed_operation(
                prepared,
                operation_id=operation.id,
            )
        if isinstance(operation, ParameterOperation):
            return await self.facade.create_named_parameter(
                operation.name,
                operation.expression,
                operation.comment,
                operation_id=operation.id,
            )
        if isinstance(operation, ComponentCreateOperation):
            return await self.facade.create_component(
                operation.name,
                operation_id=operation.id,
            )
        if isinstance(operation, SketchCreateOperation):
            return await self.facade.create_sketch_on_plane(
                operation.component_ref,
                operation.plane,
                operation.name,
                operation_id=operation.id,
            )
        if isinstance(operation, SketchRectangleOperation):
            return await self.facade.draw_constrained_rectangle(
                operation.sketch_ref,
                operation.center,
                operation.width,
                operation.height,
                operation_id=operation.id,
            )
        if isinstance(operation, SketchCircleOperation):
            return await self.facade.draw_constrained_circle(
                operation.sketch_ref,
                operation.center,
                operation.diameter,
                operation_id=operation.id,
            )
        if isinstance(operation, ExtrudeOperation):
            shape, shape_inputs = self._profile_shapes[operation.profile_ref]
            return await self.facade.extrude_profile(
                component=operation.component_ref,
                name=operation.id,
                profile_ref=operation.profile_ref,
                distance=operation.distance,
                operation=operation.operation,
                body_name=operation.result_name,
                shape=shape,
                operation_id=operation.id,
                **shape_inputs,
            )
        if isinstance(operation, JointOperation):
            return await self.facade.create_assembly_joints(
                [
                    {
                        "name": operation.name,
                        "type": operation.joint_type,
                        "parent": operation.parent_ref,
                        "child": operation.child_ref,
                        "axis": operation.axis,
                        "limits": operation.limits,
                    }
                ],
                operation_id=operation.id,
            )
        if isinstance(operation, PhysicalPropertiesOperation):
            return await self.facade.measure_physical_properties(
                operation.target_refs,
                trusted_read=True,
                operation_id=operation.id,
            )
        if isinstance(operation, InterferenceOperation):
            # Multiple explicit targets were rejected by whole-graph preflight
            # rather than being silently widened to the complete design.
            target = operation.target_refs[0] if operation.target_refs else None
            return await self.facade.analyze_interference(
                target,
                trusted_read=True,
                operation_id=operation.id,
            )
        raise TypeError(f"unsupported Autodesk CadSpec v2 operation: {operation.kind}")


def _empty_entity_counts() -> dict[str, int]:
    return {"line": 0, "circle": 0, "arc": 0, "point": 0, "curve": 0}


def _add_unique(values: set[str], value: str, label: str) -> None:
    _validate_reference_text(value, label)
    if value in values:
        raise ValueError(
            f"duplicate Autodesk {label} reference in operation graph: {value}"
        )
    values.add(value)


def _register_profile(
    profiles: dict[str, dict[str, Any]],
    profile_shapes: dict[str, tuple[str, dict[str, Any]]],
    profile_sketches: set[str],
    result_ref: str,
    sketch_ref: str,
    shape: str,
    shape_inputs: dict[str, Any],
) -> None:
    _validate_reference_text(result_ref, "profile result_ref")
    if result_ref in profiles:
        raise ValueError(f"duplicate Autodesk profile result_ref: {result_ref}")
    if sketch_ref in profile_sketches:
        raise ValueError(
            "Autodesk typed profile binding requires one closed profile producer per sketch; "
            f"additional geometry would make profile indices ambiguous: {sketch_ref}"
        )
    profile_sketches.add(sketch_ref)
    profiles[result_ref] = {"sketch": sketch_ref, "index": 0}
    profile_shapes[result_ref] = (shape, shape_inputs)


def _validate_reference_text(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Autodesk {label} must be a string")
    if not value or value != value.strip():
        raise ValueError(
            f"Autodesk {label} must be non-empty and have no outer whitespace"
        )
    if len(value) > 512 or any(ord(character) < 32 for character in value):
        raise ValueError(f"Autodesk {label} contains invalid control data")
    return value


def _normalize_entity_ref(
    value: str,
    *,
    default_sketch: str,
    planned_sketches: set[str],
    entity_counts: dict[str, dict[str, int]],
) -> dict[str, Any]:
    _validate_reference_text(value, "sketch entity reference")
    match = _ENTITY_REF_RE.fullmatch(value)
    if not match:
        raise ValueError(
            "Autodesk sketch entity references must use line#0, circle#0, arc#0, "
            "point#0, curve#0, or <sketch>/<kind>#<index>: "
            f"{value!r}"
        )
    sketch = match.group("sketch") or default_sketch
    if not sketch:
        raise ValueError(
            f"Autodesk sketch entity reference requires an explicit sketch: {value!r}"
        )
    _validate_reference_text(sketch, "sketch entity sketch")
    if default_sketch and sketch != default_sketch:
        raise ValueError(
            "Autodesk sketch constraints and dimensions cannot bind entities from another sketch: "
            f"{value!r}"
        )
    kind = match.group("kind").lower()
    index = int(match.group("index"))
    if sketch in planned_sketches:
        count = entity_counts.get(sketch, _empty_entity_counts()).get(kind, 0)
        if index >= count:
            raise ValueError(
                f"Autodesk sketch entity reference is outside the planned graph: "
                f"{sketch}/{kind}#{index}; planned_count={count}"
            )
    return {"sketch": sketch, "kind": kind, "index": index}


def _validate_constraint_entities(kind: str, entities: list[dict[str, Any]]) -> None:
    kinds = [str(entity["kind"]) for entity in entities]
    if kind == "fixed":
        valid = len(kinds) == 1
    elif kind in {"horizontal", "vertical"}:
        valid = kinds == ["line"]
    elif kind == "coincident":
        valid = (
            len(kinds) == 2
            and kinds[0] == "point"
            and kinds[1]
            in {
                "point",
                "line",
                "circle",
                "arc",
                "curve",
            }
        )
    elif kind in {"parallel", "perpendicular"}:
        valid = kinds == ["line", "line"]
    elif kind == "tangent":
        valid = len(kinds) == 2 and all(
            item in {"line", "circle", "arc", "curve"} for item in kinds
        )
    elif kind == "equal":
        valid = kinds == ["line", "line"] or (
            len(kinds) == 2 and all(item in {"circle", "arc"} for item in kinds)
        )
    elif kind == "concentric":
        valid = len(kinds) == 2 and all(item in {"circle", "arc"} for item in kinds)
    elif kind == "midpoint":
        valid = (
            len(kinds) == 2
            and kinds[0] == "point"
            and kinds[1]
            in {
                "line",
                "arc",
                "curve",
            }
        )
    else:
        valid = False
    if not valid:
        raise ValueError(
            f"Autodesk sketch constraint {kind!r} has unsupported entity binding: {kinds}"
        )


def _validate_dimension_entities(kind: str, entities: list[dict[str, Any]]) -> None:
    kinds = [str(entity["kind"]) for entity in entities]
    if kind == "distance":
        valid = kinds == ["line"] or kinds == ["point", "point"]
    elif kind in {"horizontal", "vertical"}:
        valid = kinds == ["point", "point"]
    elif kind == "diameter":
        valid = len(kinds) == 1 and kinds[0] in {"circle", "arc"}
    elif kind == "radius":
        valid = len(kinds) == 1 and kinds[0] in {"circle", "arc"}
    elif kind == "angle":
        valid = kinds == ["line", "line"]
    else:
        valid = False
    if not valid:
        raise ValueError(
            f"Autodesk sketch dimension {kind!r} has unsupported entity binding: {kinds}"
        )


def _normalize_profile_ref(
    value: str,
    planned_profiles: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if value in planned_profiles:
        return dict(planned_profiles[value])
    _validate_reference_text(value, "profile reference")
    match = _PROFILE_REF_RE.fullmatch(value)
    if not match:
        raise ValueError(
            "Autodesk profile references must name a planned result_ref or use "
            f"<sketch>/profile#<index>: {value!r}"
        )
    sketch = match.group("sketch_a") or match.group("sketch_b") or ""
    index_text = match.group("index_a") or match.group("index_b") or ""
    return {
        "sketch": _validate_reference_text(sketch, "profile sketch"),
        "index": int(index_text),
    }


def _normalize_path_ref(
    value: str,
    *,
    planned_sketches: set[str],
    entity_counts: dict[str, dict[str, int]],
) -> dict[str, Any]:
    reference = _normalize_entity_ref(
        value,
        default_sketch="",
        planned_sketches=planned_sketches,
        entity_counts=entity_counts,
    )
    if reference["kind"] not in {"line", "arc", "curve"}:
        raise ValueError(
            f"Autodesk sweep/pattern/loft path must reference a line, arc, or curve: {value!r}"
        )
    return reference


def _normalize_axis(value: str) -> str:
    _validate_reference_text(value, "axis reference")
    normalized = value.lower()
    aliases = {
        "x": "x",
        "x_axis": "x",
        "axis_x": "x",
        "y": "y",
        "y_axis": "y",
        "axis_y": "y",
        "z": "z",
        "z_axis": "z",
        "axis_z": "z",
    }
    if normalized not in aliases:
        raise ValueError(
            f"Autodesk typed operations support only principal x/y/z axes: {value!r}"
        )
    return aliases[normalized]


def _normalize_plane(value: str) -> str:
    _validate_reference_text(value, "plane reference")
    normalized = value.lower().replace("_plane", "")
    if normalized not in {"xy", "xz", "yz"}:
        raise ValueError(
            f"Autodesk typed mirror supports only principal XY/XZ/YZ planes: {value!r}"
        )
    return normalized


def _validate_io_path(path: str, format_name: str, *, direction: str) -> str:
    _validate_reference_text(path, f"{direction} path")
    windows_path = PureWindowsPath(path)
    posix_path = PurePosixPath(path)
    if not windows_path.is_absolute() and not posix_path.is_absolute():
        raise ValueError(
            f"Autodesk {direction} path must be absolute on the Fusion host: {path!r}"
        )
    suffix = (windows_path.suffix or posix_path.suffix).lower()
    allowed = _FORMAT_EXTENSIONS.get(format_name, set())
    if suffix not in allowed:
        raise ValueError(
            f"Autodesk {direction} path extension {suffix!r} does not match "
            f"format {format_name!r}; expected one of {sorted(allowed)}"
        )
    return path
