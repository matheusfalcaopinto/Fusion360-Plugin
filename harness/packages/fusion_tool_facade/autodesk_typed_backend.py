"""Typed CadSpec v2 subset for Autodesk's local Fusion MCP endpoint.

The Autodesk endpoint currently exposes a compact CRUD/script bridge rather
than one native tool per CadSpec operation.  This adapter deliberately maps
only operations backed by fixed, repository-owned facade scripts.  Missing
feature capabilities are reported during whole-graph preflight; there is no
fallback to model-authored Python or to another MCP provider.
"""

from __future__ import annotations

import json
import re
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from agent_core.authority import (
    BoundOperation,
    CadTargetBinding,
    cad_operation_target_requirements,
    revalidate_host_path,
)
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
        self._profile_refs: dict[str, dict[str, Any]] = {}
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
        if has_crud:
            capabilities.add("parameters")
        if has_crud:
            capabilities.update(_CRUD_CAPABILITIES)
        return capabilities

    def preflight_host_io_operations(self, operations: list[OperationSpec]) -> None:
        """Reject unsupported host I/O before any binding read or mutation."""

        for operation in operations:
            if isinstance(operation, ImportOperation):
                self.facade.require_secure_host_io_platform("import")
            elif isinstance(operation, ExportOperation):
                self.facade.require_secure_host_io_platform(
                    "export", overwrite=operation.overwrite
                )
            else:
                raise ValueError(
                    f"unsupported host I/O preflight operation: {operation.kind}"
                )

    async def resolve_cad_target_binding(
        self, operation: ExportOperation
    ) -> CadTargetBinding:
        """Resolve one live export target without granting mutation authority."""

        if not isinstance(operation, ExportOperation):
            raise ValueError("Autodesk CAD target binding supports exports only")
        self.facade.require_secure_host_io_platform(
            "export", overwrite=operation.overwrite
        )
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

    async def resolve_document_binding(self) -> CadTargetBinding:
        """Resolve the active Fusion document without granting mutation authority."""

        if not _CRUD_TOOLS <= self._tool_names:
            raise ValueError(
                "Autodesk mutations require the CRUD read/execute profile for document binding"
            )
        payload = await self.facade.resolve_document_binding()
        binding = payload.get("binding")
        if not isinstance(binding, dict):
            raise ValueError("Autodesk document binding response is incomplete")
        return CadTargetBinding(
            reference_kind=str(binding.get("reference_kind") or ""),
            requested_ref=str(binding.get("requested_ref") or ""),
            document_identity=str(binding.get("document_identity") or ""),
            entity_identity=str(binding.get("entity_identity") or ""),
            fingerprint=str(binding.get("fingerprint") or ""),
        )

    async def resolve_operation_target_bindings(
        self,
        operation: OperationSpec,
        *,
        requirements: tuple[tuple[str, str], ...] | None = None,
    ) -> tuple[CadTargetBinding, ...]:
        """Resolve every entity reference used by one mutation as stable identities."""

        selected_requirements = (
            cad_operation_target_requirements(operation)
            if requirements is None
            else tuple(requirements)
        )
        if not selected_requirements:
            return ()
        if not _CRUD_TOOLS <= self._tool_names:
            raise ValueError(
                "Autodesk mutation target binding requires the CRUD read/execute profile"
            )
        prepared_payload: dict[str, Any] = {}
        prepared = self._prepared.get(operation.id)
        if prepared is not None:
            prepared_payload = json.loads(prepared.payload_json)
        if isinstance(operation, ParameterOperation):
            if selected_requirements != (("parameter_target", operation.name),):
                raise ValueError("Autodesk parameter target binding request is invalid")
            payload = await self.facade._execute_trusted_read_script_json(
                _parameter_target_binding_read_script({"name": operation.name})
            )
        else:
            descriptors = _operation_binding_descriptors(
                operation,
                requirements=selected_requirements,
                profile_refs=self._profile_refs,
                prepared_payload=prepared_payload,
            )
            payload = await self.facade.resolve_operation_target_bindings(descriptors)
        raw_bindings = payload.get("bindings")
        if not isinstance(raw_bindings, list) or len(raw_bindings) != len(
            selected_requirements
        ):
            raise ValueError("Autodesk CAD target binding response is incomplete")
        bindings: list[CadTargetBinding] = []
        for raw in raw_bindings:
            if not isinstance(raw, dict):
                raise ValueError("Autodesk CAD target binding response is invalid")
            bindings.append(
                CadTargetBinding(
                    reference_kind=str(raw.get("reference_kind") or ""),
                    requested_ref=str(raw.get("requested_ref") or ""),
                    document_identity=str(raw.get("document_identity") or ""),
                    entity_identity=str(raw.get("entity_identity") or ""),
                    fingerprint=str(raw.get("fingerprint") or ""),
                )
            )
        return tuple(bindings)

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
        self._profile_refs = {}
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
                if operation.direction != "positive":
                    raise ValueError(
                        "Autodesk typed extrude supports only positive direction"
                    )
                if operation.operation == "new_body":
                    _add_unique(
                        planned_results, operation.result_name, "feature result"
                    )
                elif operation.operation in {"cut", "intersect"}:
                    target_body_ref = operation.target_body_ref
                    if target_body_ref is None:
                        raise ValueError(
                            "Autodesk extrude modifier requires target_body_ref"
                        )
                    _validate_reference_text(target_body_ref, "extrude target_body_ref")
                    if operation.result_name != target_body_ref:
                        raise ValueError(
                            "Autodesk extrude modifier result must preserve target_body_ref"
                        )
                else:
                    raise ValueError(
                        "Autodesk typed extrude has no lossless join participant binding"
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
                if operation.operation != "new_body":
                    raise ValueError(
                        "Autodesk typed revolve modifiers lack lossless participant binding"
                    )
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
                if operation.operation != "new_body":
                    raise ValueError(
                        "Autodesk typed sweep modifiers lack lossless participant binding"
                    )
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
                if operation.operation != "new_body":
                    raise ValueError(
                        "Autodesk typed loft modifiers lack lossless participant binding"
                    )
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

        if bound_operations:
            operations_by_id = {operation.id: operation for operation in operations}
            for operation in operations:
                if operation.kind.startswith("analysis."):
                    continue
                bound = bound_operations.get(operation.id)
                if bound is None or bound.capability is None:
                    raise ValueError(
                        f"Autodesk mutation lacks a claimed authority plan: {operation.id}"
                    )
                if not isinstance(operation, ExportOperation):
                    _bound_document_binding_payload(bound)
            for operation_id, plan in list(prepared.items()):
                operation = operations_by_id[operation_id]
                bound = bound_operations[operation_id]
                payload = json.loads(plan.payload_json)
                if isinstance(operation, (ImportOperation, ExportOperation)):
                    payload["host_path_binding"] = _bound_host_path_payload(bound)
                if not isinstance(operation, ExportOperation):
                    payload["document_binding"] = _bound_document_binding_payload(bound)
                    entity_bindings = _bound_entity_bindings_payload(bound)
                    if entity_bindings:
                        payload["target_bindings"] = entity_bindings
                        payload["target_binding_descriptors"] = (
                            _operation_binding_descriptors(
                                operation,
                                requirements=cad_operation_target_requirements(
                                    operation
                                ),
                                profile_refs=profiles,
                                prepared_payload=payload,
                            )
                        )
                prepared[operation_id] = self.facade.prepare_typed_operation(
                    plan.kind, payload
                )

        self._profile_shapes = profile_shapes
        self._profile_refs = {key: dict(value) for key, value in profiles.items()}
        self._prepared = prepared
        self._preflighted_operation_ids = {operation.id for operation in operations}
        self._bound_operations = dict(bound_operations)

    def bind_bound_operation(self, bound: BoundOperation) -> None:
        """Attach one just-in-time capability to the precompiled graph."""

        operation = bound.operation
        if bound.provider != self.provider:
            raise ValueError("bound operation provider does not match Autodesk")
        if bound.capability is None:
            raise ValueError("Autodesk mutation requires a bound capability")
        if not isinstance(operation, (ImportOperation, ExportOperation)) and (
            operation.id not in self._preflighted_operation_ids
        ):
            raise ValueError("Autodesk operation was not part of whole-graph preflight")
        if isinstance(operation, (ImportOperation, ExportOperation)):
            if bound.host_path is None:
                raise ValueError("Autodesk host I/O requires a bound host path")
            revalidate_host_path(bound.host_path)

        plan = self._prepared.get(operation.id)
        if isinstance(operation, ImportOperation):
            assert bound.host_path is not None
            plan = self.facade.prepare_typed_operation(
                "import",
                {
                    "path": _validate_io_path(
                        bound.host_path.canonical_path,
                        operation.format,
                        direction="import",
                    ),
                    "format": operation.format,
                    "component_name": _validate_reference_text(
                        operation.component_name, "import component_name"
                    ),
                },
            )
        elif isinstance(operation, ExportOperation):
            assert bound.host_path is not None
            if len(bound.target_bindings) != 1:
                raise ValueError(
                    "Autodesk export requires one resolved CAD target binding"
                )
            binding = bound.target_bindings[0]
            plan = self.facade.prepare_typed_operation(
                "export",
                {
                    "target": operation.target_ref,
                    "path": _validate_io_path(
                        bound.host_path.canonical_path,
                        operation.format,
                        direction="export",
                    ),
                    "format": operation.format,
                    "binding": _cad_binding_payload(binding),
                },
            )

        if plan is not None:
            payload = json.loads(plan.payload_json)
            if isinstance(operation, (ImportOperation, ExportOperation)):
                payload["host_path_binding"] = _bound_host_path_payload(bound)
            if not isinstance(operation, ExportOperation):
                payload["document_binding"] = _bound_document_binding_payload(bound)
                entity_bindings = _bound_entity_bindings_payload(bound)
                if entity_bindings:
                    payload["target_bindings"] = entity_bindings
                    payload["target_binding_descriptors"] = (
                        _operation_binding_descriptors(
                            operation,
                            requirements=cad_operation_target_requirements(operation),
                            profile_refs=self._profile_refs,
                            prepared_payload=payload,
                        )
                    )
            plan = self.facade.prepare_typed_operation(plan.kind, payload)
            self._prepared[operation.id] = plan

        self._bound_operations[operation.id] = bound
        self._preflighted_operation_ids.add(operation.id)

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
        if bound.capability is None:
            raise RuntimeError("Autodesk mutation requires a bound capability")
        if not isinstance(operation, (ImportOperation, ExportOperation)):
            if not operation.kind.startswith("analysis."):
                _bound_document_binding_payload(bound)
            return await self.execute_operation(operation)
        if bound.host_path is None:
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
        document_binding: dict[str, str] | None = None
        target_bindings: list[dict[str, str]] = []
        target_binding_descriptors: list[dict[str, Any]] = []
        if not operation.kind.startswith("analysis."):
            bound = self._bound_operations.get(operation.id)
            if bound is None or bound.capability is None:
                raise RuntimeError(
                    "Autodesk mutation must execute through a claimed document capability"
                )
            document_binding = _bound_document_binding_payload(bound)
            target_bindings = _bound_entity_bindings_payload(bound)
            if target_bindings and not isinstance(operation, ParameterOperation):
                prepared_payload: dict[str, Any] = {}
                prepared_plan = self._prepared.get(operation.id)
                if prepared_plan is not None:
                    prepared_payload = json.loads(prepared_plan.payload_json)
                target_binding_descriptors = _operation_binding_descriptors(
                    operation,
                    requirements=cad_operation_target_requirements(operation),
                    profile_refs=self._profile_refs,
                    prepared_payload=prepared_payload,
                )
        prepared = self._prepared.get(operation.id)
        if prepared is not None:
            return await self.facade.execute_prepared_typed_operation(
                prepared,
                operation_id=operation.id,
            )
        if isinstance(operation, ParameterOperation):
            if len(target_bindings) != 1:
                raise RuntimeError(
                    "Autodesk parameter mutation lacks exact target authority"
                )
            payload = {
                "name": operation.name,
                "expression": operation.expression,
                "comment": operation.comment or "",
                "document_binding": document_binding,
                "target_bindings": target_bindings,
            }
            return await self.facade._execute_script_json(
                _parameter_set_script(payload),
                operation_id=operation.id,
            )
        if isinstance(operation, ComponentCreateOperation):
            return await self.facade.create_component(
                operation.name,
                operation_id=operation.id,
                document_binding=document_binding,
            )
        if isinstance(operation, SketchCreateOperation):
            return await self.facade.create_sketch_on_plane(
                operation.component_ref,
                operation.plane,
                operation.name,
                operation_id=operation.id,
                document_binding=document_binding,
                target_bindings=target_bindings,
                target_binding_descriptors=target_binding_descriptors,
            )
        if isinstance(operation, SketchRectangleOperation):
            result = await self.facade.draw_constrained_rectangle(
                operation.sketch_ref,
                operation.center,
                operation.width,
                operation.height,
                result_ref=operation.result_ref,
                operation_id=operation.id,
                document_binding=document_binding,
                target_bindings=target_bindings,
                target_binding_descriptors=target_binding_descriptors,
            )
            self._record_produced_profile_resolver(operation, result)
            return result
        if isinstance(operation, SketchCircleOperation):
            result = await self.facade.draw_constrained_circle(
                operation.sketch_ref,
                operation.center,
                operation.diameter,
                result_ref=operation.result_ref,
                operation_id=operation.id,
                document_binding=document_binding,
                target_bindings=target_bindings,
                target_binding_descriptors=target_binding_descriptors,
            )
            self._record_produced_profile_resolver(operation, result)
            return result
        if isinstance(operation, ExtrudeOperation):
            shape, shape_inputs = self._profile_shapes[operation.profile_ref]
            return await self.facade.extrude_profile(
                component=operation.component_ref,
                name=operation.id,
                profile_ref=operation.profile_ref,
                distance=operation.distance,
                operation=operation.operation,
                body_name=operation.result_name,
                target_body_ref=operation.target_body_ref,
                shape=shape,
                operation_id=operation.id,
                document_binding=document_binding,
                target_bindings=target_bindings,
                target_binding_descriptors=target_binding_descriptors,
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
                document_binding=document_binding,
                target_bindings=target_bindings,
                target_binding_descriptors=target_binding_descriptors,
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

    def _record_produced_profile_resolver(
        self,
        operation: SketchRectangleOperation | SketchCircleOperation,
        payload: dict[str, Any],
    ) -> None:
        """Replace the planning placeholder with the producer's live profile index.

        The returned target binding remains the authority proof.  This resolver is
        accepted only as a locator for the independent just-in-time binding read;
        that read must still equal the producer's exact identity before a consumer
        capability can be issued.
        """

        self._profile_refs.pop(operation.result_ref, None)
        resolver = payload.get("produced_profile_resolver")
        produced = payload.get("produced_target_bindings")
        if (
            payload.get("profile_ref") != operation.result_ref
            or not isinstance(resolver, dict)
            or set(resolver) != {"sketch", "index"}
            or resolver.get("sketch") != operation.sketch_ref
            or isinstance(resolver.get("index"), bool)
            or not isinstance(resolver.get("index"), int)
            or resolver["index"] < 0
            or not isinstance(produced, list)
            or len(produced) != 1
            or not isinstance(produced[0], dict)
            or produced[0].get("reference_kind") != "profile"
            or produced[0].get("requested_ref") != operation.result_ref
        ):
            return
        self._profile_refs[operation.result_ref] = {
            "sketch": operation.sketch_ref,
            "index": resolver["index"],
        }


def _bound_document_binding_payload(bound: BoundOperation) -> dict[str, str]:
    if not bound.target_bindings:
        raise ValueError("Autodesk mutation requires an active-document binding")
    binding = bound.target_bindings[0]
    if (
        binding.reference_kind != "active_document"
        or binding.requested_ref != "active_document"
    ):
        raise ValueError("Autodesk mutation carries the wrong document binding")
    payload = {
        "reference_kind": binding.reference_kind,
        "requested_ref": binding.requested_ref,
        "document_identity": binding.document_identity,
        "entity_identity": binding.entity_identity,
        "fingerprint": binding.fingerprint,
    }
    if not all(
        re.fullmatch(r"[0-9a-f]{64}", value)
        for value in payload.values()
        if value not in {"active_document"}
    ):
        raise ValueError("Autodesk document binding proof is incomplete")
    return payload


def _bound_entity_bindings_payload(bound: BoundOperation) -> list[dict[str, str]]:
    return [_cad_binding_payload(binding) for binding in bound.target_bindings[1:]]


def _cad_binding_payload(binding: CadTargetBinding) -> dict[str, str]:
    return {
        "reference_kind": binding.reference_kind,
        "requested_ref": binding.requested_ref,
        "document_identity": binding.document_identity,
        "entity_identity": binding.entity_identity,
        "fingerprint": binding.fingerprint,
    }


def _bound_host_path_payload(bound: BoundOperation) -> dict[str, object]:
    binding = bound.host_path
    if binding is None:
        raise ValueError("Autodesk host I/O requires a host path binding")
    return {
        "direction": binding.direction,
        "canonical_root": binding.canonical_root,
        "canonical_path": binding.canonical_path,
        "existed_at_issue": binding.existed_at_issue,
        "overwrite": binding.overwrite,
        "resource_fingerprint": binding.resource_fingerprint,
    }


def _operation_binding_descriptors(
    operation: OperationSpec,
    *,
    requirements: tuple[tuple[str, str], ...],
    profile_refs: dict[str, dict[str, Any]],
    prepared_payload: dict[str, Any],
) -> list[dict[str, Any]]:
    """Translate authority references to fixed, sink-replayable resolvers."""

    prepared_entities = iter(prepared_payload.get("entities") or [])
    prepared_profiles = iter(prepared_payload.get("profiles") or [])
    prepared_guides = iter(prepared_payload.get("guides") or [])
    descriptors: list[dict[str, Any]] = []
    first_geometry = str(
        next(iter(getattr(operation, "target_refs", ()) or ()), "")
        or getattr(operation, "target_ref", "")
    )

    for reference_kind, requested_ref in requirements:
        resolver: dict[str, Any]
        if reference_kind in {"component", "sketch", "body", "geometry", "occurrence"}:
            resolver = {"kind": reference_kind, "reference": requested_ref}
        elif reference_kind == "profile":
            normalized = profile_refs.get(requested_ref)
            if normalized is None:
                try:
                    normalized = next(prepared_profiles)
                except StopIteration:
                    normalized = _normalize_profile_ref(requested_ref, profile_refs)
            resolver = {"kind": "profile", "reference": dict(normalized)}
        elif reference_kind == "sketch_entity":
            try:
                normalized_entity = next(prepared_entities)
            except StopIteration as exc:
                raise ValueError(
                    "Autodesk operation target binding lacks a normalized sketch entity"
                ) from exc
            resolver = {
                "kind": "sketch_entity",
                "reference": dict(normalized_entity),
            }
        elif reference_kind == "path":
            normalized_path: Any = None
            if isinstance(operation, SweepOperation):
                normalized_path = prepared_payload.get("path")
            elif isinstance(operation, PatternOperation):
                normalized_path = prepared_payload.get("path")
            elif isinstance(operation, LoftOperation):
                try:
                    normalized_path = next(prepared_guides)
                except StopIteration:
                    normalized_path = None
            if not isinstance(normalized_path, dict):
                raise ValueError(
                    "Autodesk operation target binding lacks a normalized path"
                )
            resolver = {"kind": "path", "reference": dict(normalized_path)}
        elif reference_kind == "axis":
            axis = _normalize_axis(requested_ref)
            component_ref = getattr(operation, "component_ref", None)
            resolver = {"kind": "axis", "reference": axis}
            if component_ref:
                resolver["component"] = str(component_ref)
            elif first_geometry:
                resolver["relative_to_body"] = first_geometry
            else:
                raise ValueError(
                    "Autodesk axis binding has no exact component or body context"
                )
        elif reference_kind == "plane":
            if not first_geometry:
                raise ValueError("Autodesk plane binding has no exact body context")
            resolver = {
                "kind": "plane",
                "reference": _normalize_plane(requested_ref),
                "relative_to_body": first_geometry,
            }
        else:
            raise ValueError(
                f"Autodesk has no lossless resolver for {reference_kind!r}"
            )
        descriptors.append(
            {
                "reference_kind": reference_kind,
                "requested_ref": requested_ref,
                "resolver": resolver,
            }
        )
    return descriptors


def _parameter_binding_script(payload: dict[str, Any], body: str) -> str:
    payload_json = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return f"""\
import hashlib
import json
import adsk.core
import adsk.fusion

PAYLOAD = json.loads({payload_json!r})


def _digest(value):
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _design():
    app = adsk.core.Application.get()
    design = adsk.fusion.Design.cast(app.activeProduct)
    if not design:
        raise RuntimeError("active product is not a Fusion design")
    return design


def _document_binding(design):
    app = adsk.core.Application.get()
    document = app.activeDocument
    if not document:
        raise RuntimeError("active Fusion document is unavailable")
    data_file = getattr(document, "dataFile", None)
    data_id = str(getattr(data_file, "id", "") or "")
    version_id = str(getattr(data_file, "versionId", "") or "")
    root_token = str(getattr(design.rootComponent, "entityToken", "") or "")
    if not root_token:
        raise RuntimeError("active Fusion root component has no stable identity")
    document_identity = _digest({{
        "data_id": data_id,
        "version_id": version_id,
        "root_token": root_token,
    }})
    facts = {{
        "reference_kind": "active_document",
        "requested_ref": "active_document",
        "document_identity": document_identity,
        "entity_identity": hashlib.sha256(root_token.encode("utf-8")).hexdigest(),
    }}
    return {{**facts, "fingerprint": _digest(facts)}}


def _parameter_binding_and_entity(design, name):
    document_identity = _document_binding(design)["document_identity"]
    existing = design.userParameters.itemByName(name)
    if existing:
        token = str(getattr(existing, "entityToken", "") or "")
        if not token:
            raise RuntimeError("existing parameter has no stable entity identity")
        reference_kind = "parameter_existing"
        entity_identity = hashlib.sha256(token.encode("utf-8")).hexdigest()
        object_type = str(getattr(existing, "objectType", "") or "")
        state = "existing"
    else:
        reference_kind = "parameter_absent"
        entity_identity = _digest({{
            "document_identity": document_identity,
            "name": name,
            "state": "absent",
        }})
        object_type = ""
        state = "absent"
    facts = {{
        "reference_kind": reference_kind,
        "requested_ref": name,
        "document_identity": document_identity,
        "entity_identity": entity_identity,
        "name": name,
        "object_type": object_type,
        "state": state,
    }}
    binding = {{
        "reference_kind": reference_kind,
        "requested_ref": name,
        "document_identity": document_identity,
        "entity_identity": entity_identity,
        "fingerprint": _digest(facts),
    }}
    return binding, existing


def _parameter_binding(design, name):
    binding, _existing = _parameter_binding_and_entity(design, name)
    return binding


def run(_context):
{body}
"""


def _parameter_target_binding_read_script(payload: dict[str, Any]) -> str:
    return _parameter_binding_script(
        payload,
        """    design = _design()
    binding = _parameter_binding(design, PAYLOAD["name"])
    print(json.dumps({"success": True, "bindings": [binding]}, sort_keys=True))""",
    )


def _parameter_set_script(payload: dict[str, Any]) -> str:
    return _parameter_binding_script(
        payload,
        """    design = _design()
    expected_document = PAYLOAD.get("document_binding")
    if not isinstance(expected_document, dict) or expected_document != _document_binding(design):
        raise RuntimeError("active Fusion document binding changed")
    expected_targets = PAYLOAD.get("target_bindings")
    if not isinstance(expected_targets, list) or len(expected_targets) != 1:
        raise RuntimeError("parameter target binding is incomplete")
    actual, existing = _parameter_binding_and_entity(design, PAYLOAD["name"])
    if actual != expected_targets[0]:
        raise RuntimeError("parameter target binding changed after capability issuance")
    expression = PAYLOAD["expression"]
    if existing:
        existing.expression = expression
    else:
        unit = expression.split()[-1] if len(expression.split()) > 1 else design.unitsManager.defaultLengthUnits
        design.userParameters.add(
            PAYLOAD["name"],
            adsk.core.ValueInput.createByString(expression),
            unit,
            PAYLOAD.get("comment", ""),
        )
    print(json.dumps({"success": True, "parameter": {"name": PAYLOAD["name"], "expression": expression}}, sort_keys=True))""",
    )


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
