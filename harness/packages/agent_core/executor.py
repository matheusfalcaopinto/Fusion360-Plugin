"""CadSpec executor."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from cad_spec.models import CadSpec, FeatureSpec
from fusion_tool_facade.facade import FusionFacade


class ExecutionContext(BaseModel):
    """Execution context passed to the executor."""

    mode: str = "mock"
    project: str = "default"
    output_dir: Path = Path("outputs")
    dry_run: bool = False

    model_config = {"arbitrary_types_allowed": True}


class ExecutionResult(BaseModel):
    """Structured executor result."""

    success: bool
    created_objects: list[str] = Field(default_factory=list)
    modified_objects: list[str] = Field(default_factory=list)
    exports: list[str] = Field(default_factory=list)
    transactions: list[dict[str, Any]] = Field(default_factory=list)


class Executor:
    """Execute a validated CadSpec through the safe Fusion facade."""

    def __init__(self, facade: FusionFacade | None = None) -> None:
        self.facade = facade

    async def execute(self, spec: CadSpec, context: ExecutionContext) -> ExecutionResult:
        """Run the spec as small facade transactions."""

        result = ExecutionResult(success=True)
        if context.dry_run:
            result.transactions.append({"operation": "dry_run", "status": "ok"})
            for parameter in spec.parameters:
                result.modified_objects.append(parameter.name)
                result.transactions.append({"operation": "create_named_parameter", "name": parameter.name, "status": "simulated"})
            for component in spec.components:
                result.created_objects.append(component.name)
                result.transactions.append({"operation": "create_component", "name": component.name, "status": "simulated"})
                for feature in component.features:
                    await self._simulate_feature(feature, result, context)
            self._simulate_professional_contracts(spec, result, context)
            return result

        if not self.facade:
            raise RuntimeError("executor requires a facade when dry_run is false")

        await self.facade.inspect_design()
        result.transactions.append({"operation": "inspect_design", "status": "ok"})

        for parameter in spec.parameters:
            await self.facade.create_named_parameter(parameter.name, parameter.expression, parameter.comment)
            result.modified_objects.append(parameter.name)
            result.transactions.append({"operation": "create_named_parameter", "name": parameter.name, "status": "ok"})

        for component in spec.components:
            await self.facade.create_component(component.name)
            result.created_objects.append(component.name)
            result.transactions.append({"operation": "create_component", "name": component.name, "status": "ok"})
            await self._execute_component_features(component.name, component.features, context, result)

        await self._execute_professional_contracts(spec, context, result)
        return result

    async def replay_features(self, spec: CadSpec, context: ExecutionContext) -> bool:
        """Replay feature execution only, without parameter/component recreation."""

        if context.dry_run or not self.facade:
            return False
        if not spec.components:
            return False
        for component in spec.components:
            await self.activate_component(component.name)
            await self._execute_component_features(component.name, component.features, context, ExecutionResult(success=True))
        return True

    async def replay_exports(self, spec: CadSpec, context: ExecutionContext) -> bool:
        """Replay only export features."""

        if context.dry_run or not self.facade:
            return False
        replayed = False
        for component in spec.components:
            await self.activate_component(component.name)
            for feature in component.features:
                if feature.type != "export":
                    continue
                await self._execute_feature(component.name, feature, context, ExecutionResult(success=True), replay=True)
                replayed = True
        return replayed

    async def activate_component(self, component_name: str) -> bool:
        """Activate a component and return true on success."""

        if not self.facade:
            return False
        await self.facade.activate_component(component_name)
        return True

    async def _execute_component_features(
        self,
        component_name: str,
        features: list[FeatureSpec],
        context: ExecutionContext,
        result: ExecutionResult,
    ) -> None:
        for feature in features:
            if self.facade:
                await self.facade.activate_component(component_name)
            await self._execute_feature(component_name, feature, context, result)

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
            path = Path(inputs.get("path") or context.output_dir / f"{target}.{fmt}")
            result.exports.append(str(path))
        elif feature.type == "update_parameter":
            result.modified_objects.append(inputs["name"])
        elif feature.type in {"extrude_rectangle", "extrude_cylinder", "l_bracket_body", "box_shell"}:
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
            path = _safe_output_path(context.output_dir, inputs["path"])
            result.exports.append(str(path))
        elif feature.type in {"hole_pattern_cut", "center_hole_cut"}:
            if "target_body" in inputs:
                result.modified_objects.append(inputs["target_body"])
        result.transactions.append({"operation": feature.type, "name": feature.name, "status": "simulated"})

    async def _execute_feature(
        self,
        component_name: str,
        feature: FeatureSpec,
        context: ExecutionContext,
        result: ExecutionResult,
        *,
        replay: bool = False,
    ) -> None:
        if context.dry_run:
            await self._simulate_feature(feature, result, context)
            return

        if not self.facade:
            raise RuntimeError("executor requires a facade for non-dry-run execution")

        inputs = feature.merged_inputs()
        if feature.type == "extrude_rectangle":
            sketch = inputs["sketch_name"]
            await self.facade.create_sketch_on_plane(component_name, inputs.get("plane", "XY"), sketch)
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
            await self.facade.create_sketch_on_plane(component_name, inputs.get("plane", "XY"), sketch)
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
            await self.facade.create_sketch_on_plane(component_name, inputs.get("plane", "XY"), sketch)
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
            await self.facade.create_sketch_on_plane(component_name, inputs.get("plane", "XY"), sketch)
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
            await self.facade.create_sketch_on_plane(component_name, inputs.get("plane", "XY"), sketch)
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
            result.created_objects.extend(list(inputs.get("body_names", [])) + [feature.name])
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
                [feature.name] + list(inputs.get("component_names", [])) + list(inputs.get("body_names", []))
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
                placement_offset=list(inputs.get("placement_offset", ["0 mm", "0 mm", "0 mm"])),
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
                placement_offset=list(inputs.get("placement_offset", ["0 mm", "0 mm", "0 mm"])),
            )
            result.created_objects.extend(
                [feature.name] + list(inputs.get("component_names", [])) + list(inputs.get("body_names", []))
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
                placement_offset=list(inputs.get("placement_offset", ["0 mm", "-150 mm", "0 mm"])),
            )
            result.created_objects.extend(
                [feature.name] + list(inputs.get("component_names", [])) + list(inputs.get("body_names", []))
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
                placement_offset=list(inputs.get("placement_offset", ["0 mm", "0 mm", "0 mm"])),
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
                placement_offset=list(inputs.get("placement_offset", ["0 mm", "90 mm", "0 mm"])),
            )
            result.created_objects.extend(
                [feature.name] + list(inputs.get("component_names", [])) + list(inputs.get("body_names", []))
            )
        elif feature.type == "update_parameter":
            await self.facade.update_named_parameter(inputs["name"], inputs["expression"])
            result.modified_objects.append(inputs["name"])
        elif feature.type == "apply_fillet":
            await self.facade.apply_fillet(inputs["edge_selector"], inputs["radius"], feature.name)
            result.created_objects.append(feature.name)
        elif feature.type == "export":
            target = inputs.get("target", "design")
            fmt = inputs.get("format", "step")
            path = Path(inputs.get("path") or context.output_dir / f"{target}.{fmt}")
            if fmt == "stl":
                await self.facade.export_stl(target, path)
            else:
                await self.facade.export_step(target, path)
            result.exports.append(str(path))
            result.created_objects.append(feature.name)
        elif feature.type == "capture_viewport":
            path = _safe_output_path(context.output_dir, inputs["path"])
            await self.facade.capture_viewport(
                name=feature.name,
                path=path,
                view=inputs.get("view", "isometric"),
                isolate_prefix=inputs.get("isolate_prefix"),
                width=int(inputs.get("width", 1600)),
                height=int(inputs.get("height", 1100)),
            )
            result.exports.append(str(path))
            result.created_objects.append(feature.name)
        else:
            raise ValueError(f"unsupported feature type: {feature.type}")
        result.transactions.append({"operation": feature.type, "name": feature.name, "status": "ok", "replayed": replay})

    def _simulate_professional_contracts(self, spec: CadSpec, result: ExecutionResult, context: ExecutionContext) -> None:
        if spec.component_metadata:
            result.modified_objects.extend(item.component for item in spec.component_metadata)
            result.transactions.append({"operation": "set_component_metadata", "status": "simulated", "count": len(spec.component_metadata)})
        if spec.joints:
            result.created_objects.extend(item.name for item in spec.joints)
            result.transactions.append({"operation": "create_assembly_joints", "status": "simulated", "count": len(spec.joints)})
        for output in spec.outputs:
            path = _safe_output_path(context.output_dir, output.path)
            result.exports.append(str(path))
            result.created_objects.append(output.name)
            result.transactions.append({"operation": "capture_viewport", "name": output.name, "status": "simulated"})

    async def _execute_professional_contracts(
        self,
        spec: CadSpec,
        context: ExecutionContext,
        result: ExecutionResult,
    ) -> None:
        if not self.facade:
            return
        if spec.component_metadata:
            await self.facade.set_component_metadata([item.model_dump(mode="json") for item in spec.component_metadata])
            result.modified_objects.extend(item.component for item in spec.component_metadata)
            result.transactions.append({"operation": "set_component_metadata", "status": "ok", "count": len(spec.component_metadata)})
        if spec.joints:
            await self.facade.create_assembly_joints([item.model_dump(mode="json") for item in spec.joints])
            result.created_objects.extend(item.name for item in spec.joints)
            result.transactions.append({"operation": "create_assembly_joints", "status": "ok", "count": len(spec.joints)})
        for output in spec.outputs:
            path = _safe_output_path(context.output_dir, output.path)
            await self.facade.capture_viewport(
                name=output.name,
                path=path,
                view=output.view,
                isolate_prefix=output.isolate_prefix,
                width=output.width,
                height=output.height,
            )
            result.exports.append(str(path))
            result.created_objects.append(output.name)
            result.transactions.append({"operation": "capture_viewport", "name": output.name, "status": "ok"})


def _safe_output_path(output_dir: Path, raw_path: str | Path) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path
    if ".." in path.parts:
        raise ValueError(f"output path must stay under output_dir: {raw_path}")
    return output_dir / path
