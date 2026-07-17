"""CadSpec executor."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Awaitable, Callable, cast

from pydantic import BaseModel, Field

from agent_core.authority import (
    AuthorityBroker,
    AuthorityDeniedError,
    AuthorityPolicy,
    BoundOperation,
    CadTargetBinding,
    HostOutputDisabledError,
    LegacyOutputOperation,
    REAL_HOST_OUTPUT_DENIED_MESSAGE,
)
from agent_core.request_context import ExecutionMode
from cad_spec.models import CadSpec, FeatureSpec
from fusion_tool_facade.facade import FusionFacade


LEGACY_REAL_EXECUTION_DENIED_MESSAGE = (
    "CadSpec v1 cannot execute against a real provider because its operations "
    "do not carry document and entity bindings; normalize the complete graph "
    "to CadSpec v2 and execute it through CapabilityExecutor."
)


def require_legacy_execution_allowed(*, mode: str, dry_run: bool) -> None:
    """Fail closed before a deprecated v1 graph can reach a real provider.

    CadSpec v1 remains parseable and can still drive deterministic mock or
    dry-run compatibility flows during its deprecation window.  Every
    non-mock, non-dry-run route is provider-bearing in the session controller,
    so unknown/future mode strings must be rejected as well as ``real``.
    """

    if not dry_run and mode != "mock":
        raise AuthorityDeniedError(LEGACY_REAL_EXECUTION_DENIED_MESSAGE)


def preflight_legacy_execution(
    spec: CadSpec,
    *,
    mode: str,
    dry_run: bool,
    output_dir: Path,
) -> None:
    """Validate the whole local v1 graph, then deny any provider-bearing route."""

    if dry_run or mode == "mock":
        return

    requests_host_output = False
    for component in spec.components:
        for feature in component.features:
            inputs = feature.merged_inputs()
            if feature.type == "export":
                requests_host_output = True
                target = str(inputs.get("target", "design"))
                format_name = str(inputs.get("format", "step")).lower()
                _safe_export_path(
                    output_dir,
                    inputs.get("path") or f"{target}.{format_name}",
                    format_name,
                )
            elif feature.type == "capture_viewport":
                requests_host_output = True
                _safe_capture_path(output_dir, inputs["path"])
    for output in spec.outputs:
        requests_host_output = True
        _safe_capture_path(output_dir, output.path)

    if requests_host_output:
        raise HostOutputDisabledError(REAL_HOST_OUTPUT_DENIED_MESSAGE)
    require_legacy_execution_allowed(mode=mode, dry_run=dry_run)


class ExecutionContext(BaseModel):
    """Execution context passed to the executor."""

    mode: ExecutionMode = "mock"
    project: str = "default"
    output_dir: Path = Path("outputs")
    dry_run: bool = False
    session_id: str | None = None

    model_config = {"arbitrary_types_allowed": True}


class ExecutionResult(BaseModel):
    """Structured executor result."""

    success: bool
    created_objects: list[str] = Field(default_factory=list)
    modified_objects: list[str] = Field(default_factory=list)
    exports: list[str] = Field(default_factory=list)
    transactions: list[dict[str, Any]] = Field(default_factory=list)


@dataclass(slots=True)
class _PreparedLegacyAuthority:
    broker: AuthorityBroker
    by_id: dict[str, BoundOperation]
    finalized: set[str] = field(default_factory=set)

    def claim_path(
        self, operation_id: str, expected_path: Path
    ) -> tuple[BoundOperation, Path]:
        try:
            bound = self.by_id[operation_id]
        except KeyError as exc:
            raise AuthorityDeniedError("legacy output has no bound capability") from exc
        host_path = bound.host_path
        if host_path is None or host_path.canonical_path != str(expected_path):
            self.broker.revoke(bound)
            self.finalized.add(operation_id)
            raise AuthorityDeniedError("legacy output path does not match its binding")
        try:
            self.broker.claim(bound)
        except BaseException:
            self.finalized.add(operation_id)
            raise
        return bound, Path(host_path.canonical_path)

    def complete(self, operation_id: str, bound: BoundOperation) -> None:
        self.broker.complete(bound, outcome="consumed")
        self.finalized.add(operation_id)

    def fail(self, operation_id: str, bound: BoundOperation) -> None:
        self.broker.fail(bound, outcome_unknown=True)
        self.finalized.add(operation_id)

    def revoke_unused(self) -> None:
        for operation_id, bound in self.by_id.items():
            if operation_id not in self.finalized:
                self.broker.revoke(bound)
                self.finalized.add(operation_id)


class Executor:
    """Execute a validated CadSpec through the safe Fusion facade."""

    def __init__(
        self,
        facade: FusionFacade | None = None,
        *,
        authority_broker: AuthorityBroker | None = None,
        authority_provider: str = "legacy-facade",
    ) -> None:
        self.facade = facade
        self.authority_broker = authority_broker or AuthorityBroker(
            AuthorityPolicy.deny_all()
        )
        self.authority_provider = authority_provider

    async def execute(
        self, spec: CadSpec, context: ExecutionContext
    ) -> ExecutionResult:
        """Run the spec as small facade transactions."""

        preflight_legacy_execution(
            spec,
            mode=context.mode,
            dry_run=context.dry_run,
            output_dir=context.output_dir,
        )

        # The verifier registry is the authority for legacy assertion types.
        # Validate it before even a read-only provider preflight so an unknown
        # assertion can never follow earlier valid operations into Fusion.
        from verifier.geometry import require_registered_assertions

        require_registered_assertions(spec)
        result = ExecutionResult(success=True)
        if context.dry_run:
            result.transactions.append({"operation": "dry_run", "status": "ok"})
            for parameter in spec.parameters:
                result.modified_objects.append(parameter.name)
                result.transactions.append(
                    {
                        "operation": "create_named_parameter",
                        "name": parameter.name,
                        "status": "simulated",
                    }
                )
            for component in spec.components:
                result.created_objects.append(component.name)
                result.transactions.append(
                    {
                        "operation": "create_component",
                        "name": component.name,
                        "status": "simulated",
                    }
                )
                for feature in component.features:
                    await self._simulate_feature(feature, result, context)
            self._simulate_professional_contracts(spec, result, context)
            return result

        if not self.facade:
            raise RuntimeError("executor requires a facade when dry_run is false")

        authority = await self._prepare_legacy_authority(spec, context)

        try:
            await self.facade.inspect_design()
            result.transactions.append({"operation": "inspect_design", "status": "ok"})

            for parameter in spec.parameters:
                await self.facade.create_named_parameter(
                    parameter.name, parameter.expression, parameter.comment
                )
                result.modified_objects.append(parameter.name)
                result.transactions.append(
                    {
                        "operation": "create_named_parameter",
                        "name": parameter.name,
                        "status": "ok",
                    }
                )

            for component_index, component in enumerate(spec.components):
                await self.facade.create_component(component.name)
                result.created_objects.append(component.name)
                result.transactions.append(
                    {
                        "operation": "create_component",
                        "name": component.name,
                        "status": "ok",
                    }
                )
                await self._execute_component_features(
                    component.name,
                    component.features,
                    context,
                    result,
                    component_index=component_index,
                    authority=authority,
                )

            await self._execute_professional_contracts(
                spec, context, result, authority=authority
            )
            return result
        finally:
            if authority is not None:
                authority.revoke_unused()

    async def replay_features(self, spec: CadSpec, context: ExecutionContext) -> bool:
        """Replay feature execution only, without parameter/component recreation."""

        preflight_legacy_execution(
            spec,
            mode=context.mode,
            dry_run=context.dry_run,
            output_dir=context.output_dir,
        )
        if context.dry_run or not self.facade:
            return False
        if not spec.components:
            return False
        authority = await self._prepare_legacy_authority(spec, context)
        try:
            for component_index, component in enumerate(spec.components):
                await self.activate_component(component.name)
                await self._execute_component_features(
                    component.name,
                    component.features,
                    context,
                    ExecutionResult(success=True),
                    component_index=component_index,
                    authority=authority,
                )
            return True
        finally:
            if authority is not None:
                authority.revoke_unused()

    async def replay_exports(self, spec: CadSpec, context: ExecutionContext) -> bool:
        """Replay only export features."""

        preflight_legacy_execution(
            spec,
            mode=context.mode,
            dry_run=context.dry_run,
            output_dir=context.output_dir,
        )
        if context.dry_run or not self.facade:
            return False
        authority = await self._prepare_legacy_authority(
            spec, context, included_kinds={"legacy.export"}
        )
        replayed = False
        try:
            for component_index, component in enumerate(spec.components):
                await self.activate_component(component.name)
                for feature_index, feature in enumerate(component.features):
                    if feature.type != "export":
                        continue
                    await self._execute_feature(
                        component.name,
                        feature,
                        context,
                        ExecutionResult(success=True),
                        replay=True,
                        authority=authority,
                        authority_id=_feature_authority_id(
                            component_index, feature_index
                        ),
                    )
                    replayed = True
            return replayed
        finally:
            if authority is not None:
                authority.revoke_unused()

    async def activate_component(self, component_name: str) -> bool:
        """Activate a component and return true on success."""

        if not self.facade:
            return False
        await self.facade.activate_component(component_name)
        return True

    async def capture_viewport(
        self,
        *,
        context: ExecutionContext,
        name: str,
        path: str | Path,
        view: str,
        isolate_prefix: str | None,
        width: int,
        height: int,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Dispatch one direct legacy capture through the shared host authority."""

        if context.mode != "mock":
            raise HostOutputDisabledError(REAL_HOST_OUTPUT_DENIED_MESSAGE)
        if not self.facade:
            raise RuntimeError("executor requires a facade for viewport capture")
        facade = self.facade
        canonical_path = _safe_capture_path(context.output_dir, path)
        authority = await self._prepare_operations(
            (
                LegacyOutputOperation(
                    id="direct-capture",
                    kind="legacy.capture",
                    path=str(canonical_path),
                    format="png",
                    target_identity=name,
                    overwrite=overwrite,
                ),
            ),
            context,
        )

        async def dispatch(
            bound: BoundOperation | None, bound_path: Path
        ) -> dict[str, Any]:
            return await facade.capture_viewport(
                name=name,
                path=bound_path,
                view=view,
                isolate_prefix=isolate_prefix,
                width=width,
                height=height,
                operation_id="direct-capture" if bound is not None else None,
                document_binding=(
                    _bound_legacy_target_payload(bound) if bound is not None else None
                ),
                host_path_binding=(
                    _bound_legacy_host_payload(bound) if bound is not None else None
                ),
            )

        try:
            if authority is None:
                return await dispatch(None, canonical_path)
            return cast(
                dict[str, Any],
                await self._dispatch_authorized_output(
                    authority,
                    "direct-capture",
                    canonical_path,
                    dispatch,
                ),
            )
        finally:
            if authority is not None:
                authority.revoke_unused()

    async def _execute_component_features(
        self,
        component_name: str,
        features: list[FeatureSpec],
        context: ExecutionContext,
        result: ExecutionResult,
        *,
        component_index: int,
        authority: _PreparedLegacyAuthority | None,
    ) -> None:
        for feature_index, feature in enumerate(features):
            if self.facade:
                await self.facade.activate_component(component_name)
            await self._execute_feature(
                component_name,
                feature,
                context,
                result,
                authority=authority,
                authority_id=_feature_authority_id(component_index, feature_index),
            )

    async def _prepare_legacy_authority(
        self,
        spec: CadSpec,
        context: ExecutionContext,
        *,
        included_kinds: set[str] | None = None,
    ) -> _PreparedLegacyAuthority | None:
        operations: list[LegacyOutputOperation] = []
        for component_index, component in enumerate(spec.components):
            for feature_index, feature in enumerate(component.features):
                inputs = feature.merged_inputs()
                operation_id = _feature_authority_id(component_index, feature_index)
                if feature.type == "export":
                    kind = "legacy.export"
                    if included_kinds is not None and kind not in included_kinds:
                        continue
                    target = str(inputs.get("target", "design"))
                    format_name = str(inputs.get("format", "step")).lower()
                    path = _safe_export_path(
                        context.output_dir,
                        inputs.get("path") or f"{target}.{format_name}",
                        format_name,
                    )
                    operations.append(
                        LegacyOutputOperation(
                            id=operation_id,
                            kind="legacy.export",
                            path=str(path),
                            format=format_name,
                            target_identity=target,
                            overwrite=_legacy_overwrite(inputs),
                        )
                    )
                elif feature.type == "capture_viewport":
                    kind = "legacy.capture"
                    if included_kinds is not None and kind not in included_kinds:
                        continue
                    path = _safe_capture_path(context.output_dir, inputs["path"])
                    operations.append(
                        LegacyOutputOperation(
                            id=operation_id,
                            kind="legacy.capture",
                            path=str(path),
                            format="png",
                            target_identity=feature.name,
                            overwrite=_legacy_overwrite(inputs),
                        )
                    )
        if included_kinds is None or "legacy.capture" in included_kinds:
            for output_index, output in enumerate(spec.outputs):
                operations.append(
                    LegacyOutputOperation(
                        id=_output_authority_id(output_index),
                        kind="legacy.capture",
                        path=str(_safe_capture_path(context.output_dir, output.path)),
                        format="png",
                        target_identity=output.name,
                    )
                )
        return await self._prepare_operations(tuple(operations), context)

    async def _prepare_operations(
        self,
        operations: tuple[LegacyOutputOperation, ...],
        context: ExecutionContext,
    ) -> _PreparedLegacyAuthority | None:
        if not operations or context.mode == "mock":
            return None
        raise HostOutputDisabledError(REAL_HOST_OUTPUT_DENIED_MESSAGE)

    def _preflight_legacy_host_io_operations(
        self, operations: tuple[LegacyOutputOperation, ...]
    ) -> None:
        """Reject unsupported output semantics before live Fusion binding reads."""

        if self.facade is None:
            raise AuthorityDeniedError(
                "legacy host output requires a live Fusion facade"
            )
        require_secure_host_io = getattr(
            self.facade, "require_secure_host_io_platform", None
        )
        if not callable(require_secure_host_io):
            raise AuthorityDeniedError(
                "legacy host output backend cannot preflight secure host I/O"
            )
        for operation in operations:
            require_secure_host_io("export", overwrite=operation.overwrite)

    async def _resolve_legacy_output_bindings(
        self, operations: tuple[LegacyOutputOperation, ...]
    ) -> dict[str, tuple[CadTargetBinding, ...]]:
        if self.facade is None:
            raise AuthorityDeniedError(
                "legacy host output requires a live Fusion facade"
            )
        raw_document: dict[str, Any] | None = None
        if any(operation.kind == "legacy.capture" for operation in operations):
            resolve_document = getattr(self.facade, "resolve_document_binding", None)
            if not callable(resolve_document):
                raise AuthorityDeniedError(
                    "legacy host output backend cannot bind the active document"
                )
            document_payload = await resolve_document()
            value = document_payload.get("binding")
            if not isinstance(value, dict):
                raise AuthorityDeniedError("legacy document binding is incomplete")
            raw_document = value
        bindings: dict[str, tuple[CadTargetBinding, ...]] = {}
        for operation in operations:
            raw_binding = raw_document
            if operation.kind == "legacy.export":
                resolve_export = getattr(
                    self.facade, "resolve_export_target_binding", None
                )
                if not callable(resolve_export):
                    raise AuthorityDeniedError(
                        "legacy export backend cannot bind the target entity"
                    )
                export_payload = await resolve_export(
                    operation.target_identity, operation.format
                )
                value = export_payload.get("binding")
                if not isinstance(value, dict):
                    raise AuthorityDeniedError(
                        "legacy export target binding is incomplete"
                    )
                raw_binding = value
            if raw_binding is None:
                raise AuthorityDeniedError("legacy output target binding is incomplete")
            bindings[operation.id] = (
                _legacy_output_live_binding(operation, raw_binding),
            )
        return bindings

    async def _dispatch_authorized_output(
        self,
        authority: _PreparedLegacyAuthority,
        operation_id: str,
        expected_path: Path,
        dispatch: Callable[[BoundOperation, Path], Awaitable[Any]],
    ) -> Any:
        bound, canonical_path = authority.claim_path(operation_id, expected_path)
        try:
            payload = await dispatch(bound, canonical_path)
            authority.complete(operation_id, bound)
        except BaseException:
            if operation_id not in authority.finalized:
                authority.fail(operation_id, bound)
            raise
        return payload

    async def _simulate_feature(
        self,
        feature: FeatureSpec,
        result: ExecutionResult,
        context: ExecutionContext,
    ) -> None:
        inputs = feature.merged_inputs()
        result.created_objects.append(feature.name)
        if feature.type == "export":
            target = inputs.get("target", "design")
            fmt = inputs.get("format", "step")
            path = _safe_export_path(
                context.output_dir,
                inputs.get("path") or f"{target}.{fmt}",
                fmt,
            )
            result.exports.append(str(path))
        elif feature.type == "update_parameter":
            result.modified_objects.append(inputs["name"])
        elif feature.type in {
            "extrude_rectangle",
            "extrude_cylinder",
            "l_bracket_body",
            "box_shell",
        }:
            body_name = inputs.get("body_name") or f"{feature.name}_body"
            result.created_objects.append(body_name)
            result.modified_objects.append(body_name)
        elif feature.type == "nema17_stepper_motor":
            body_name = inputs["body_name"]
            result.created_objects.append(body_name)
            result.modified_objects.append(body_name)
        elif feature.type == "nema17_visual_polish":
            result.modified_objects.append(inputs["target_body"])
            result.created_objects.extend(inputs.get("body_names", []))
        elif feature.type == "nema17_external_assembly":
            result.created_objects.extend(inputs.get("component_names", []))
            result.created_objects.extend(inputs.get("body_names", []))
        elif feature.type == "profile2020_aluminum_extrusion":
            body_name = inputs.get("body_name") or "profile2020_aluminum_body"
            result.created_objects.append(body_name)
            result.modified_objects.append(body_name)
        elif feature.type == "mgn12_linear_rail_assembly":
            result.created_objects.extend(inputs.get("component_names", []))
            result.created_objects.extend(inputs.get("body_names", []))
        elif feature.type == "desktop_cnc_assembly":
            result.created_objects.extend(inputs.get("component_names", []))
            result.created_objects.extend(inputs.get("body_names", []))
        elif feature.type == "spacer_plate_assembly":
            result.created_objects.extend(inputs.get("component_names", []))
            result.created_objects.extend(inputs.get("body_names", []))
            result.created_objects.extend(inputs.get("occurrence_names", []))
        elif feature.type == "hinge_assembly":
            result.created_objects.extend(inputs.get("component_names", []))
            result.created_objects.extend(inputs.get("body_names", []))
        elif feature.type == "capture_viewport":
            path = _safe_capture_path(context.output_dir, inputs["path"])
            result.exports.append(str(path))
        elif feature.type in {"hole_pattern_cut", "center_hole_cut"}:
            if "target_body" in inputs:
                result.modified_objects.append(inputs["target_body"])
        result.transactions.append(
            {"operation": feature.type, "name": feature.name, "status": "simulated"}
        )

    async def _execute_feature(
        self,
        component_name: str,
        feature: FeatureSpec,
        context: ExecutionContext,
        result: ExecutionResult,
        *,
        replay: bool = False,
        authority: _PreparedLegacyAuthority | None = None,
        authority_id: str | None = None,
    ) -> None:
        if context.dry_run:
            await self._simulate_feature(feature, result, context)
            return

        if not self.facade:
            raise RuntimeError("executor requires a facade for non-dry-run execution")
        facade = self.facade

        inputs = feature.merged_inputs()
        if feature.type == "extrude_rectangle":
            sketch = inputs["sketch_name"]
            await self.facade.create_sketch_on_plane(
                component_name, inputs.get("plane", "XY"), sketch
            )
            profile = await self.facade.draw_constrained_rectangle(
                sketch,
                inputs.get("center", ["0 mm", "0 mm"]),
                inputs["width"],
                inputs["height"],
            )
            body_name = inputs["body_name"]
            await self.facade.extrude_profile(
                component=component_name,
                name=feature.name,
                profile_ref=profile["profile_ref"],
                distance=inputs["distance"],
                operation=feature.operation,
                body_name=body_name,
                shape="rectangle",
                width=inputs["width"],
                height=inputs["height"],
            )
            result.created_objects.extend([sketch, body_name, feature.name])
        elif feature.type == "extrude_cylinder":
            sketch = inputs["sketch_name"]
            await self.facade.create_sketch_on_plane(
                component_name, inputs.get("plane", "XY"), sketch
            )
            profile = await self.facade.draw_constrained_circle(
                sketch,
                inputs.get("center", ["0 mm", "0 mm"]),
                inputs["diameter"],
            )
            body_name = inputs["body_name"]
            await self.facade.extrude_profile(
                component=component_name,
                name=feature.name,
                profile_ref=profile["profile_ref"],
                distance=inputs["distance"],
                operation=feature.operation,
                body_name=body_name,
                shape="cylinder",
                diameter=inputs["diameter"],
            )
            result.created_objects.extend([sketch, body_name, feature.name])
        elif feature.type in {"hole_pattern_cut", "center_hole_cut"}:
            sketch = inputs["sketch_name"]
            await self.facade.create_sketch_on_plane(
                component_name, inputs.get("plane", "XY"), sketch
            )
            profile = await self.facade.draw_constrained_circle(
                sketch,
                inputs.get("center", ["0 mm", "0 mm"]),
                inputs["diameter"],
            )
            await self.facade.cut_profile(
                name=feature.name,
                target_body=inputs["target_body"],
                profile_ref=profile["profile_ref"],
                distance=inputs.get("distance"),
                count=int(inputs.get("count", 1)),
                cut_type=feature.type,
                diameter=inputs["diameter"],
                offset=inputs.get("offset"),
            )
            result.modified_objects.append(inputs["target_body"])
            result.created_objects.extend([sketch, feature.name])
        elif feature.type == "l_bracket_body":
            sketch = inputs["sketch_name"]
            await self.facade.create_sketch_on_plane(
                component_name, inputs.get("plane", "XY"), sketch
            )
            body_name = inputs["body_name"]
            await self.facade.extrude_profile(
                component=component_name,
                name=feature.name,
                profile_ref=f"{sketch}:l_bracket:0",
                distance=inputs["distance"],
                operation=feature.operation,
                body_name=body_name,
                shape="l_bracket",
                leg_length=inputs["leg_length"],
                thickness=inputs["thickness"],
            )
            result.created_objects.extend([sketch, body_name, feature.name])
        elif feature.type == "box_shell":
            sketch = inputs["sketch_name"]
            await self.facade.create_sketch_on_plane(
                component_name, inputs.get("plane", "XY"), sketch
            )
            body_name = inputs["body_name"]
            await self.facade.extrude_profile(
                component=component_name,
                name=feature.name,
                profile_ref=f"{sketch}:box_shell:0",
                distance=inputs["height"],
                operation=feature.operation,
                body_name=body_name,
                shape="box_shell",
                length=inputs["length"],
                width=inputs["width"],
                height=inputs["height"],
                wall_thickness=inputs["wall_thickness"],
            )
            result.created_objects.extend([sketch, body_name, feature.name])
        elif feature.type == "nema17_stepper_motor":
            body_name = inputs["body_name"]
            await self.facade.create_nema17_stepper(
                component=component_name,
                name=feature.name,
                body_name=body_name,
                face_width=inputs["face_width"],
                body_length=inputs["body_length"],
                pilot_diameter=inputs["pilot_diameter"],
                pilot_length=inputs["pilot_length"],
                shaft_diameter=inputs["shaft_diameter"],
                shaft_length=inputs["shaft_length"],
                mount_hole_spacing=inputs["mount_hole_spacing"],
                mount_hole_diameter=inputs["mount_hole_diameter"],
                overall_depth=inputs["overall_depth"],
                mount_hole_count=int(inputs.get("mount_hole_count", 4)),
            )
            result.created_objects.extend([body_name, feature.name])
        elif feature.type == "nema17_visual_polish":
            await self.facade.create_nema17_polish_details(
                target_body=inputs["target_body"],
                name=feature.name,
                face_width=inputs["face_width"],
                body_length=inputs["body_length"],
                overall_depth=inputs["overall_depth"],
                mount_hole_spacing=inputs["mount_hole_spacing"],
                mount_hole_diameter=inputs["mount_hole_diameter"],
                pilot_diameter=inputs["pilot_diameter"],
                shaft_diameter=inputs["shaft_diameter"],
                detail_projection=inputs["detail_projection"],
                side_panel_projection=inputs["side_panel_projection"],
                lamination_band_height=inputs["lamination_band_height"],
                hole_shadow_diameter=inputs["hole_shadow_diameter"],
                pilot_relief_diameter=inputs["pilot_relief_diameter"],
                connector_width=inputs["connector_width"],
                connector_depth=inputs["connector_depth"],
                connector_height=inputs["connector_height"],
                wire_length=inputs["wire_length"],
                wire_diameter=inputs["wire_diameter"],
                lamination_ring_count=int(inputs.get("lamination_ring_count", 18)),
                wire_count=int(inputs.get("wire_count", 4)),
                body_names=list(inputs.get("body_names", [])),
            )
            result.modified_objects.append(inputs["target_body"])
            result.created_objects.extend(
                list(inputs.get("body_names", [])) + [feature.name]
            )
        elif feature.type == "nema17_external_assembly":
            await self.facade.create_nema17_external_assembly(
                name=feature.name,
                assembly_component=inputs["assembly_component"],
                face_width=inputs["face_width"],
                body_length=inputs["body_length"],
                front_plate_thickness=inputs["front_plate_thickness"],
                rear_plate_thickness=inputs["rear_plate_thickness"],
                pilot_diameter=inputs["pilot_diameter"],
                pilot_length=inputs["pilot_length"],
                shaft_diameter=inputs["shaft_diameter"],
                shaft_length=inputs["shaft_length"],
                mount_hole_spacing=inputs["mount_hole_spacing"],
                mount_hole_diameter=inputs["mount_hole_diameter"],
                connector_width=inputs["connector_width"],
                connector_height=inputs["connector_height"],
                connector_depth=inputs["connector_depth"],
                wire_length=inputs["wire_length"],
                wire_diameter=inputs["wire_diameter"],
                lamination_count=int(inputs.get("lamination_count", 20)),
                component_names=list(inputs.get("component_names", [])),
                body_names=list(inputs.get("body_names", [])),
            )
            result.created_objects.extend(
                [feature.name]
                + list(inputs.get("component_names", []))
                + list(inputs.get("body_names", []))
            )
        elif feature.type == "profile2020_aluminum_extrusion":
            await self.facade.create_profile2020_aluminum_extrusion(
                name=feature.name,
                component=inputs["component"],
                body_name=inputs["body_name"],
                length=inputs["length"],
                size=inputs["size"],
                slot_width=inputs["slot_width"],
                slot_depth=inputs["slot_depth"],
                slot_cavity_width=inputs["slot_cavity_width"],
                center_bore_diameter=inputs["center_bore_diameter"],
                lip_thickness=inputs["lip_thickness"],
                corner_radius=inputs["corner_radius"],
                slot_count=int(inputs.get("slot_count", 4)),
                web_relief_count=int(inputs.get("web_relief_count", 4)),
                placement_offset=list(
                    inputs.get("placement_offset", ["0 mm", "0 mm", "0 mm"])
                ),
            )
            result.created_objects.extend([inputs["body_name"], feature.name])
        elif feature.type == "mgn12_linear_rail_assembly":
            await self.facade.create_mgn12_linear_rail_assembly(
                name=feature.name,
                assembly_component=inputs["assembly_component"],
                rail_length=inputs["rail_length"],
                rail_width=inputs["rail_width"],
                rail_height=inputs["rail_height"],
                rail_hole_pitch=inputs["rail_hole_pitch"],
                rail_end_hole_offset=inputs["rail_end_hole_offset"],
                rail_hole_diameter=inputs["rail_hole_diameter"],
                rail_counterbore_diameter=inputs["rail_counterbore_diameter"],
                rail_counterbore_depth=inputs["rail_counterbore_depth"],
                carriage_length=inputs["carriage_length"],
                carriage_width=inputs["carriage_width"],
                carriage_total_height=inputs["carriage_total_height"],
                carriage_top_height=inputs["carriage_top_height"],
                carriage_mount_x_spacing=inputs["carriage_mount_x_spacing"],
                carriage_mount_y_spacing=inputs["carriage_mount_y_spacing"],
                carriage_mount_thread_diameter=inputs["carriage_mount_thread_diameter"],
                component_names=list(inputs.get("component_names", [])),
                body_names=list(inputs.get("body_names", [])),
                placement_offset=list(
                    inputs.get("placement_offset", ["0 mm", "0 mm", "0 mm"])
                ),
            )
            result.created_objects.extend(
                [feature.name]
                + list(inputs.get("component_names", []))
                + list(inputs.get("body_names", []))
            )
        elif feature.type == "desktop_cnc_assembly":
            await self.facade.create_desktop_cnc_assembly(
                name=feature.name,
                assembly_component=inputs["assembly_component"],
                component_names=list(inputs.get("component_names", [])),
                body_names=list(inputs.get("body_names", [])),
                frame_width=inputs["frame_width"],
                frame_depth=inputs["frame_depth"],
                gantry_height=inputs["gantry_height"],
                profile_size=inputs["profile_size"],
                rail_length=inputs["rail_length"],
                z_rail_length=inputs["z_rail_length"],
                rail_width=inputs["rail_width"],
                rail_height=inputs["rail_height"],
                motor_face_width=inputs["motor_face_width"],
                motor_body_length=inputs["motor_body_length"],
                motor_shaft_diameter=inputs["motor_shaft_diameter"],
                motor_shaft_length=inputs["motor_shaft_length"],
                leadscrew_diameter=inputs["leadscrew_diameter"],
                coupler_diameter=inputs["coupler_diameter"],
                coupler_length=inputs["coupler_length"],
                plate_thickness=inputs["plate_thickness"],
                spoilboard_length=inputs["spoilboard_length"],
                spoilboard_width=inputs["spoilboard_width"],
                spoilboard_thickness=inputs["spoilboard_thickness"],
                spindle_diameter=inputs["spindle_diameter"],
                spindle_length=inputs["spindle_length"],
                work_area_x=inputs["work_area_x"],
                work_area_y=inputs["work_area_y"],
                work_area_z=inputs["work_area_z"],
                placement_offset=list(
                    inputs.get("placement_offset", ["0 mm", "-150 mm", "0 mm"])
                ),
            )
            result.created_objects.extend(
                [feature.name]
                + list(inputs.get("component_names", []))
                + list(inputs.get("body_names", []))
            )
        elif feature.type == "spacer_plate_assembly":
            await self.facade.create_spacer_plate_assembly(
                name=feature.name,
                assembly_component=inputs["assembly_component"],
                component_names=list(inputs.get("component_names", [])),
                body_names=list(inputs.get("body_names", [])),
                occurrence_names=list(inputs.get("occurrence_names", [])),
                plate_length=inputs["plate_length"],
                plate_width=inputs["plate_width"],
                plate_thickness=inputs["plate_thickness"],
                plate_gap=inputs["plate_gap"],
                standoff_diameter=inputs["standoff_diameter"],
                standoff_height=inputs["standoff_height"],
                hole_diameter=inputs["hole_diameter"],
                hole_pattern_x=inputs["hole_pattern_x"],
                hole_pattern_y=inputs["hole_pattern_y"],
                placement_offset=list(
                    inputs.get("placement_offset", ["0 mm", "0 mm", "0 mm"])
                ),
            )
            result.created_objects.extend(
                [feature.name]
                + list(inputs.get("component_names", []))
                + list(inputs.get("body_names", []))
                + list(inputs.get("occurrence_names", []))
            )
        elif feature.type == "hinge_assembly":
            await self.facade.create_hinge_assembly(
                name=feature.name,
                assembly_component=inputs["assembly_component"],
                component_names=list(inputs.get("component_names", [])),
                body_names=list(inputs.get("body_names", [])),
                leaf_length=inputs["leaf_length"],
                leaf_width=inputs["leaf_width"],
                leaf_thickness=inputs["leaf_thickness"],
                pin_diameter=inputs["pin_diameter"],
                pin_length=inputs["pin_length"],
                knuckle_outer_diameter=inputs["knuckle_outer_diameter"],
                knuckle_length=inputs["knuckle_length"],
                leaf_gap=inputs["leaf_gap"],
                placement_offset=list(
                    inputs.get("placement_offset", ["0 mm", "90 mm", "0 mm"])
                ),
            )
            result.created_objects.extend(
                [feature.name]
                + list(inputs.get("component_names", []))
                + list(inputs.get("body_names", []))
            )
        elif feature.type == "update_parameter":
            await self.facade.update_named_parameter(
                inputs["name"], inputs["expression"]
            )
            result.modified_objects.append(inputs["name"])
        elif feature.type == "apply_fillet":
            await self.facade.apply_fillet(
                inputs["edge_selector"], inputs["radius"], feature.name
            )
            result.created_objects.append(feature.name)
        elif feature.type == "export":
            target = inputs.get("target", "design")
            fmt = str(inputs.get("format", "step")).lower()
            path = _safe_export_path(
                context.output_dir,
                inputs.get("path") or f"{target}.{fmt}",
                fmt,
            )
            if authority is not None:
                if authority_id is None:
                    raise AuthorityDeniedError(
                        "legacy export has no operation identity"
                    )

                async def dispatch(
                    bound: BoundOperation | None, bound_path: Path
                ) -> Any:
                    operation_id = authority_id if bound is not None else None
                    target_binding = (
                        _bound_legacy_target_payload(bound)
                        if bound is not None
                        else None
                    )
                    host_path_binding = (
                        _bound_legacy_host_payload(bound) if bound is not None else None
                    )
                    if fmt == "stl":
                        return await facade.export_stl(
                            target,
                            bound_path,
                            operation_id=operation_id,
                            target_binding=target_binding,
                            host_path_binding=host_path_binding,
                        )
                    return await facade.export_step(
                        target,
                        bound_path,
                        operation_id=operation_id,
                        target_binding=target_binding,
                        host_path_binding=host_path_binding,
                    )

                await self._dispatch_authorized_output(
                    authority, authority_id, path, dispatch
                )
            elif fmt == "stl":
                await self.facade.export_stl(target, path)
            else:
                await self.facade.export_step(target, path)
            result.exports.append(str(path))
            result.created_objects.append(feature.name)
        elif feature.type == "capture_viewport":
            path = _safe_capture_path(context.output_dir, inputs["path"])

            async def dispatch(bound: BoundOperation | None, bound_path: Path) -> Any:
                return await facade.capture_viewport(
                    name=feature.name,
                    path=bound_path,
                    view=inputs.get("view", "isometric"),
                    isolate_prefix=inputs.get("isolate_prefix"),
                    width=int(inputs.get("width", 1600)),
                    height=int(inputs.get("height", 1100)),
                    operation_id=authority_id if bound is not None else None,
                    document_binding=(
                        _bound_legacy_target_payload(bound)
                        if bound is not None
                        else None
                    ),
                    host_path_binding=(
                        _bound_legacy_host_payload(bound) if bound is not None else None
                    ),
                )

            if authority is not None:
                if authority_id is None:
                    raise AuthorityDeniedError(
                        "legacy capture has no operation identity"
                    )
                await self._dispatch_authorized_output(
                    authority, authority_id, path, dispatch
                )
            else:
                await dispatch(None, path)
            result.exports.append(str(path))
            result.created_objects.append(feature.name)
        else:
            raise ValueError(f"unsupported feature type: {feature.type}")
        result.transactions.append(
            {
                "operation": feature.type,
                "name": feature.name,
                "status": "ok",
                "replayed": replay,
            }
        )

    def _simulate_professional_contracts(
        self, spec: CadSpec, result: ExecutionResult, context: ExecutionContext
    ) -> None:
        if spec.component_metadata:
            result.modified_objects.extend(
                item.component for item in spec.component_metadata
            )
            result.transactions.append(
                {
                    "operation": "set_component_metadata",
                    "status": "simulated",
                    "count": len(spec.component_metadata),
                }
            )
        if spec.joints:
            result.created_objects.extend(item.name for item in spec.joints)
            result.transactions.append(
                {
                    "operation": "create_assembly_joints",
                    "status": "simulated",
                    "count": len(spec.joints),
                }
            )
        for output in spec.outputs:
            path = _safe_capture_path(context.output_dir, output.path)
            result.exports.append(str(path))
            result.created_objects.append(output.name)
            result.transactions.append(
                {
                    "operation": "capture_viewport",
                    "name": output.name,
                    "status": "simulated",
                }
            )

    async def _execute_professional_contracts(
        self,
        spec: CadSpec,
        context: ExecutionContext,
        result: ExecutionResult,
        *,
        authority: _PreparedLegacyAuthority | None,
    ) -> None:
        if not self.facade:
            return
        facade = self.facade
        if spec.component_metadata:
            await self.facade.set_component_metadata(
                [item.model_dump(mode="json") for item in spec.component_metadata]
            )
            result.modified_objects.extend(
                item.component for item in spec.component_metadata
            )
            result.transactions.append(
                {
                    "operation": "set_component_metadata",
                    "status": "ok",
                    "count": len(spec.component_metadata),
                }
            )
        if spec.joints:
            await self.facade.create_assembly_joints(
                [item.model_dump(mode="json") for item in spec.joints]
            )
            result.created_objects.extend(item.name for item in spec.joints)
            result.transactions.append(
                {
                    "operation": "create_assembly_joints",
                    "status": "ok",
                    "count": len(spec.joints),
                }
            )
        for output_index, output in enumerate(spec.outputs):
            path = _safe_capture_path(context.output_dir, output.path)

            operation_id = _output_authority_id(output_index)

            async def dispatch(bound: BoundOperation | None, bound_path: Path) -> Any:
                return await facade.capture_viewport(
                    name=output.name,
                    path=bound_path,
                    view=output.view,
                    isolate_prefix=output.isolate_prefix,
                    width=output.width,
                    height=output.height,
                    operation_id=operation_id if bound is not None else None,
                    document_binding=(
                        _bound_legacy_target_payload(bound)
                        if bound is not None
                        else None
                    ),
                    host_path_binding=(
                        _bound_legacy_host_payload(bound) if bound is not None else None
                    ),
                )

            if authority is not None:
                await self._dispatch_authorized_output(
                    authority,
                    operation_id,
                    path,
                    dispatch,
                )
            else:
                await dispatch(None, path)
            result.exports.append(str(path))
            result.created_objects.append(output.name)
            result.transactions.append(
                {"operation": "capture_viewport", "name": output.name, "status": "ok"}
            )


def _safe_output_path(output_dir: Path, raw_path: str | Path) -> Path:
    """Resolve a caller artifact name beneath its trusted session output root."""

    raw = str(raw_path)
    windows = PureWindowsPath(raw)
    posix = PurePosixPath(raw)
    if windows.is_absolute() or posix.is_absolute() or windows.drive or windows.root:
        raise ValueError("output path must be relative to output_dir")
    parts = {part for part in (*windows.parts, *posix.parts) if part not in {"", "."}}
    if ".." in parts:
        raise ValueError(f"output path must stay under output_dir: {raw_path}")
    if any(ord(character) < 32 or ord(character) == 127 for character in raw):
        raise ValueError("output path contains control characters")
    canonical_root = Path(output_dir).resolve(strict=False)
    canonical_path = (canonical_root / Path(raw)).resolve(strict=False)
    try:
        contained = os.path.commonpath(
            (str(canonical_root), str(canonical_path))
        ) == str(canonical_root)
    except ValueError:
        contained = False
    if not contained:
        raise ValueError(f"output path must stay under output_dir: {raw_path}")
    return canonical_path


def _safe_export_path(output_dir: Path, raw_path: str | Path, format_name: str) -> Path:
    formats = {
        "step": {".step", ".stp"},
        "stp": {".step", ".stp"},
        "stl": {".stl"},
    }
    if format_name not in formats:
        raise ValueError("legacy export format must be step, stp, or stl")
    path = _safe_output_path(output_dir, raw_path)
    if path.suffix.lower() not in formats[format_name]:
        raise ValueError(
            f"legacy export extension does not match format {format_name!r}"
        )
    return path


def _safe_capture_path(output_dir: Path, raw_path: str | Path) -> Path:
    path = _safe_output_path(output_dir, raw_path)
    if path.suffix.lower() != ".png":
        raise ValueError("legacy viewport capture path must use the .png extension")
    return path


def _legacy_output_live_binding(
    operation: LegacyOutputOperation, raw_binding: dict[str, Any]
) -> CadTargetBinding:
    reference_kind = str(raw_binding.get("reference_kind") or "")
    requested_ref = str(raw_binding.get("requested_ref") or "")
    document_identity = str(raw_binding.get("document_identity") or "")
    entity_identity = str(raw_binding.get("entity_identity") or "")
    source_fingerprint = str(raw_binding.get("fingerprint") or "")
    if not all(
        len(value) == 64 and all(character in "0123456789abcdef" for character in value)
        for value in (document_identity, entity_identity, source_fingerprint)
    ):
        raise AuthorityDeniedError("legacy output live identity proof is invalid")
    expected_kind = (
        "export_target" if operation.kind == "legacy.export" else "active_document"
    )
    expected_ref = (
        operation.target_identity
        if operation.kind == "legacy.export"
        else "active_document"
    )
    if reference_kind != expected_kind or requested_ref != expected_ref:
        raise AuthorityDeniedError("legacy output live identity does not match request")
    return CadTargetBinding(
        reference_kind=reference_kind,
        requested_ref=requested_ref,
        document_identity=document_identity,
        entity_identity=entity_identity,
        fingerprint=source_fingerprint,
    )


def _bound_legacy_target_payload(bound: BoundOperation) -> dict[str, str]:
    if len(bound.target_bindings) != 1:
        raise AuthorityDeniedError("legacy output target proof is incomplete")
    return cast(dict[str, str], asdict(bound.target_bindings[0]))


def _bound_legacy_host_payload(bound: BoundOperation) -> dict[str, Any]:
    binding = bound.host_path
    if binding is None:
        raise AuthorityDeniedError("legacy output host path proof is incomplete")
    return {
        "direction": binding.direction,
        "canonical_root": binding.canonical_root,
        "canonical_path": binding.canonical_path,
        "existed_at_issue": binding.existed_at_issue,
        "overwrite": binding.overwrite,
        "resource_fingerprint": binding.resource_fingerprint,
    }


def _legacy_overwrite(inputs: dict[str, Any]) -> bool:
    value = inputs.get("overwrite", False)
    if not isinstance(value, bool):
        raise ValueError("legacy output overwrite must be boolean")
    return value


def _feature_authority_id(component_index: int, feature_index: int) -> str:
    return f"legacy-feature-{component_index:04d}-{feature_index:04d}"


def _output_authority_id(output_index: int) -> str:
    return f"legacy-output-{output_index:04d}"
