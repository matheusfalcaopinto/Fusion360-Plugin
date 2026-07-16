"""Allowlisted CAD operation facade."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fusion_mcp_adapter.adapter import FusionMcpAdapter
from fusion_mcp_adapter.tool_result import ToolResult
from fusion_tool_facade.policy import FacadeMapping


class FusionFacade:
    """Semantic facade used by executor, verifier, and CLI."""

    def __init__(
        self, adapter: FusionMcpAdapter, mapping: FacadeMapping | None = None
    ) -> None:
        self.adapter = adapter
        self.mapping = mapping or FacadeMapping()

    async def inspect_design(self) -> dict[str, Any]:
        """Inspect active document, units, objects, and parameters."""

        return await self._call("inspect_design", {})

    async def create_named_parameter(
        self, name: str, expression: str, comment: str | None = None
    ) -> dict[str, Any]:
        """Create or update a named user parameter."""

        return await self._call(
            "create_named_parameter",
            {"name": name, "expression": expression, "comment": comment},
        )

    async def update_named_parameter(
        self, name: str, expression: str
    ) -> dict[str, Any]:
        """Update a named user parameter."""

        return await self._call(
            "update_named_parameter", {"name": name, "expression": expression}
        )

    async def create_component(self, name: str) -> dict[str, Any]:
        """Create and activate a component."""

        return await self._call("create_component", {"name": name})

    async def activate_component(self, name: str) -> dict[str, Any]:
        """Activate an existing component."""

        return await self._call("activate_component", {"name": name})

    async def create_sketch_on_plane(
        self, component: str, plane: str, name: str
    ) -> dict[str, Any]:
        """Create a named sketch on a known origin plane."""

        return await self._call(
            "create_sketch_on_plane",
            {"component": component, "plane": plane, "name": name},
        )

    async def draw_constrained_rectangle(
        self, sketch: str, center: list[str], width: str, height: str
    ) -> dict[str, Any]:
        """Draw a constrained rectangle profile."""

        return await self._call(
            "draw_constrained_rectangle",
            {"sketch": sketch, "center": center, "width": width, "height": height},
        )

    async def draw_constrained_circle(
        self, sketch: str, center: list[str], diameter: str
    ) -> dict[str, Any]:
        """Draw a constrained circle profile."""

        return await self._call(
            "draw_constrained_circle",
            {"sketch": sketch, "center": center, "diameter": diameter},
        )

    async def extrude_profile(
        self,
        *,
        component: str,
        name: str,
        profile_ref: str,
        distance: str,
        operation: str,
        body_name: str,
        shape: str = "rectangle",
        **shape_inputs: Any,
    ) -> dict[str, Any]:
        """Extrude a closed profile into a body or cut."""

        payload = {
            "component": component,
            "name": name,
            "profile_ref": profile_ref,
            "distance": distance,
            "operation": operation,
            "body_name": body_name,
            "shape": shape,
            **shape_inputs,
        }
        return await self._call("extrude_profile", payload)

    async def cut_profile(
        self,
        *,
        name: str,
        target_body: str,
        profile_ref: str | None = None,
        distance: str | None = None,
        count: int = 1,
        cut_type: str = "cut",
        **inputs: Any,
    ) -> dict[str, Any]:
        """Cut one or more profiles from a target body."""

        return await self._call(
            "cut_profile",
            {
                "name": name,
                "target_body": target_body,
                "profile_ref": profile_ref,
                "distance": distance,
                "count": count,
                "cut_type": cut_type,
                **inputs,
            },
        )

    async def apply_fillet(
        self, edge_selector: str, radius: str, name: str
    ) -> dict[str, Any]:
        """Apply a named fillet."""

        return await self._call(
            "apply_fillet",
            {"edge_selector": edge_selector, "radius": radius, "name": name},
        )

    async def create_nema17_stepper(
        self,
        *,
        component: str,
        name: str,
        body_name: str,
        face_width: str,
        body_length: str,
        pilot_diameter: str,
        pilot_length: str,
        shaft_diameter: str,
        shaft_length: str,
        mount_hole_spacing: str,
        mount_hole_diameter: str,
        overall_depth: str,
        mount_hole_count: int = 4,
    ) -> dict[str, Any]:
        """Create a dimensioned NEMA17 stepper motor body."""

        return await self._call(
            "create_nema17_stepper",
            {
                "component": component,
                "name": name,
                "body_name": body_name,
                "face_width": face_width,
                "body_length": body_length,
                "pilot_diameter": pilot_diameter,
                "pilot_length": pilot_length,
                "shaft_diameter": shaft_diameter,
                "shaft_length": shaft_length,
                "mount_hole_spacing": mount_hole_spacing,
                "mount_hole_diameter": mount_hole_diameter,
                "overall_depth": overall_depth,
                "mount_hole_count": mount_hole_count,
            },
        )

    async def create_nema17_polish_details(
        self,
        *,
        target_body: str,
        name: str,
        face_width: str,
        body_length: str,
        overall_depth: str,
        mount_hole_spacing: str,
        mount_hole_diameter: str,
        pilot_diameter: str,
        shaft_diameter: str,
        detail_projection: str,
        side_panel_projection: str,
        lamination_band_height: str,
        hole_shadow_diameter: str,
        pilot_relief_diameter: str,
        connector_width: str,
        connector_depth: str,
        connector_height: str,
        wire_length: str,
        wire_diameter: str,
        lamination_ring_count: int,
        wire_count: int,
        body_names: list[str],
    ) -> dict[str, Any]:
        """Add detailed visual polish bodies to an existing NEMA17 motor."""

        return await self._call(
            "create_nema17_polish_details",
            {
                "target_body": target_body,
                "name": name,
                "face_width": face_width,
                "body_length": body_length,
                "overall_depth": overall_depth,
                "mount_hole_spacing": mount_hole_spacing,
                "mount_hole_diameter": mount_hole_diameter,
                "pilot_diameter": pilot_diameter,
                "shaft_diameter": shaft_diameter,
                "detail_projection": detail_projection,
                "side_panel_projection": side_panel_projection,
                "lamination_band_height": lamination_band_height,
                "hole_shadow_diameter": hole_shadow_diameter,
                "pilot_relief_diameter": pilot_relief_diameter,
                "connector_width": connector_width,
                "connector_depth": connector_depth,
                "connector_height": connector_height,
                "wire_length": wire_length,
                "wire_diameter": wire_diameter,
                "lamination_ring_count": lamination_ring_count,
                "wire_count": wire_count,
                "body_names": body_names,
            },
        )

    async def create_nema17_external_assembly(
        self,
        *,
        name: str,
        assembly_component: str,
        face_width: str,
        body_length: str,
        front_plate_thickness: str,
        rear_plate_thickness: str,
        pilot_diameter: str,
        pilot_length: str,
        shaft_diameter: str,
        shaft_length: str,
        mount_hole_spacing: str,
        mount_hole_diameter: str,
        connector_width: str,
        connector_height: str,
        connector_depth: str,
        wire_length: str,
        wire_diameter: str,
        lamination_count: int,
        component_names: list[str],
        body_names: list[str],
    ) -> dict[str, Any]:
        """Create a component-owned NEMA17 external assembly."""

        return await self._call(
            "create_nema17_external_assembly",
            {
                "name": name,
                "assembly_component": assembly_component,
                "face_width": face_width,
                "body_length": body_length,
                "front_plate_thickness": front_plate_thickness,
                "rear_plate_thickness": rear_plate_thickness,
                "pilot_diameter": pilot_diameter,
                "pilot_length": pilot_length,
                "shaft_diameter": shaft_diameter,
                "shaft_length": shaft_length,
                "mount_hole_spacing": mount_hole_spacing,
                "mount_hole_diameter": mount_hole_diameter,
                "connector_width": connector_width,
                "connector_height": connector_height,
                "connector_depth": connector_depth,
                "wire_length": wire_length,
                "wire_diameter": wire_diameter,
                "lamination_count": lamination_count,
                "component_names": component_names,
                "body_names": body_names,
            },
        )

    async def create_profile2020_aluminum_extrusion(
        self,
        *,
        name: str,
        component: str,
        body_name: str,
        length: str,
        size: str,
        slot_width: str,
        slot_depth: str,
        slot_cavity_width: str,
        center_bore_diameter: str,
        lip_thickness: str,
        corner_radius: str,
        slot_count: int,
        web_relief_count: int,
        placement_offset: list[str],
    ) -> dict[str, Any]:
        """Create a detailed 20x20 metric aluminum T-slot extrusion."""

        return await self._call(
            "create_profile2020_aluminum_extrusion",
            {
                "name": name,
                "component": component,
                "body_name": body_name,
                "length": length,
                "size": size,
                "slot_width": slot_width,
                "slot_depth": slot_depth,
                "slot_cavity_width": slot_cavity_width,
                "center_bore_diameter": center_bore_diameter,
                "lip_thickness": lip_thickness,
                "corner_radius": corner_radius,
                "slot_count": slot_count,
                "web_relief_count": web_relief_count,
                "placement_offset": placement_offset,
            },
        )

    async def create_mgn12_linear_rail_assembly(
        self,
        *,
        name: str,
        assembly_component: str,
        rail_length: str,
        rail_width: str,
        rail_height: str,
        rail_hole_pitch: str,
        rail_end_hole_offset: str,
        rail_hole_diameter: str,
        rail_counterbore_diameter: str,
        rail_counterbore_depth: str,
        carriage_length: str,
        carriage_width: str,
        carriage_total_height: str,
        carriage_top_height: str,
        carriage_mount_x_spacing: str,
        carriage_mount_y_spacing: str,
        carriage_mount_thread_diameter: str,
        component_names: list[str],
        body_names: list[str],
        placement_offset: list[str],
    ) -> dict[str, Any]:
        """Create a component-owned MGN12 linear rail and carriage assembly."""

        return await self._call(
            "create_mgn12_linear_rail_assembly",
            {
                "name": name,
                "assembly_component": assembly_component,
                "rail_length": rail_length,
                "rail_width": rail_width,
                "rail_height": rail_height,
                "rail_hole_pitch": rail_hole_pitch,
                "rail_end_hole_offset": rail_end_hole_offset,
                "rail_hole_diameter": rail_hole_diameter,
                "rail_counterbore_diameter": rail_counterbore_diameter,
                "rail_counterbore_depth": rail_counterbore_depth,
                "carriage_length": carriage_length,
                "carriage_width": carriage_width,
                "carriage_total_height": carriage_total_height,
                "carriage_top_height": carriage_top_height,
                "carriage_mount_x_spacing": carriage_mount_x_spacing,
                "carriage_mount_y_spacing": carriage_mount_y_spacing,
                "carriage_mount_thread_diameter": carriage_mount_thread_diameter,
                "component_names": component_names,
                "body_names": body_names,
                "placement_offset": placement_offset,
            },
        )

    async def create_desktop_cnc_assembly(
        self,
        *,
        name: str,
        assembly_component: str,
        component_names: list[str],
        body_names: list[str],
        frame_width: str,
        frame_depth: str,
        gantry_height: str,
        profile_size: str,
        rail_length: str,
        z_rail_length: str,
        rail_width: str,
        rail_height: str,
        motor_face_width: str,
        motor_body_length: str,
        motor_shaft_diameter: str,
        motor_shaft_length: str,
        leadscrew_diameter: str,
        coupler_diameter: str,
        coupler_length: str,
        plate_thickness: str,
        spoilboard_length: str,
        spoilboard_width: str,
        spoilboard_thickness: str,
        spindle_diameter: str,
        spindle_length: str,
        work_area_x: str,
        work_area_y: str,
        work_area_z: str,
        placement_offset: list[str],
    ) -> dict[str, Any]:
        """Create a component-owned compact desktop CNC router assembly."""

        return await self._call(
            "create_desktop_cnc_assembly",
            {
                "name": name,
                "assembly_component": assembly_component,
                "component_names": component_names,
                "body_names": body_names,
                "frame_width": frame_width,
                "frame_depth": frame_depth,
                "gantry_height": gantry_height,
                "profile_size": profile_size,
                "rail_length": rail_length,
                "z_rail_length": z_rail_length,
                "rail_width": rail_width,
                "rail_height": rail_height,
                "motor_face_width": motor_face_width,
                "motor_body_length": motor_body_length,
                "motor_shaft_diameter": motor_shaft_diameter,
                "motor_shaft_length": motor_shaft_length,
                "leadscrew_diameter": leadscrew_diameter,
                "coupler_diameter": coupler_diameter,
                "coupler_length": coupler_length,
                "plate_thickness": plate_thickness,
                "spoilboard_length": spoilboard_length,
                "spoilboard_width": spoilboard_width,
                "spoilboard_thickness": spoilboard_thickness,
                "spindle_diameter": spindle_diameter,
                "spindle_length": spindle_length,
                "work_area_x": work_area_x,
                "work_area_y": work_area_y,
                "work_area_z": work_area_z,
                "placement_offset": placement_offset,
            },
        )

    async def create_spacer_plate_assembly(
        self,
        *,
        name: str,
        assembly_component: str,
        component_names: list[str],
        body_names: list[str],
        occurrence_names: list[str],
        plate_length: str,
        plate_width: str,
        plate_thickness: str,
        plate_gap: str,
        standoff_diameter: str,
        standoff_height: str,
        hole_diameter: str,
        hole_pattern_x: str,
        hole_pattern_y: str,
        placement_offset: list[str],
    ) -> dict[str, Any]:
        """Create a generic two-plate spacer assembly."""

        return await self._call(
            "create_spacer_plate_assembly",
            {
                "name": name,
                "assembly_component": assembly_component,
                "component_names": component_names,
                "body_names": body_names,
                "occurrence_names": occurrence_names,
                "plate_length": plate_length,
                "plate_width": plate_width,
                "plate_thickness": plate_thickness,
                "plate_gap": plate_gap,
                "standoff_diameter": standoff_diameter,
                "standoff_height": standoff_height,
                "hole_diameter": hole_diameter,
                "hole_pattern_x": hole_pattern_x,
                "hole_pattern_y": hole_pattern_y,
                "placement_offset": placement_offset,
            },
        )

    async def create_hinge_assembly(
        self,
        *,
        name: str,
        assembly_component: str,
        component_names: list[str],
        body_names: list[str],
        leaf_length: str,
        leaf_width: str,
        leaf_thickness: str,
        pin_diameter: str,
        pin_length: str,
        knuckle_outer_diameter: str,
        knuckle_length: str,
        leaf_gap: str,
        placement_offset: list[str],
    ) -> dict[str, Any]:
        """Create a generic simple hinge assembly."""

        return await self._call(
            "create_hinge_assembly",
            {
                "name": name,
                "assembly_component": assembly_component,
                "component_names": component_names,
                "body_names": body_names,
                "leaf_length": leaf_length,
                "leaf_width": leaf_width,
                "leaf_thickness": leaf_thickness,
                "pin_diameter": pin_diameter,
                "pin_length": pin_length,
                "knuckle_outer_diameter": knuckle_outer_diameter,
                "knuckle_length": knuckle_length,
                "leaf_gap": leaf_gap,
                "placement_offset": placement_offset,
            },
        )

    async def set_component_metadata(
        self, metadata: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Set engineering metadata on components."""

        return await self._call("set_component_metadata", {"metadata": metadata})

    async def create_assembly_joints(
        self, joints: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Create or update assembly joints."""

        return await self._call("create_assembly_joints", {"joints": joints})

    async def capture_viewport(
        self,
        *,
        name: str,
        path: Path | str,
        view: str,
        isolate_prefix: str | None = None,
        width: int = 1600,
        height: int = 1100,
    ) -> dict[str, Any]:
        """Capture a Fusion viewport image."""

        return await self._call(
            "capture_viewport",
            {
                "name": name,
                "path": str(path),
                "view": view,
                "isolate_prefix": isolate_prefix,
                "width": width,
                "height": height,
            },
        )

    async def analyze_interference(self, target: str | None = None) -> dict[str, Any]:
        """Analyze assembly interference."""

        return await self._call("analyze_interference", {"target": target})

    async def measure_physical_properties(
        self, targets: list[str] | None = None
    ) -> dict[str, Any]:
        """Measure component physical properties."""

        return await self._call(
            "measure_physical_properties", {"targets": targets or []}
        )

    async def measure_bounding_box(self, target: str | None = None) -> list[float]:
        """Measure a body or design bounding box in millimeters."""

        data = await self._call("measure_bounding_box", {"target": target})
        return list(data["bounding_box_mm"])

    async def validate_named_objects(self) -> dict[str, Any]:
        """Check that objects do not have default/generated names."""

        return await self._call("validate_named_objects", {})

    async def export_step(self, target: str, path: Path | str) -> dict[str, Any]:
        """Export a STEP file after verification."""

        return await self._call(
            "export_step", {"target": target, "path": str(path), "format": "step"}
        )

    async def export_stl(self, target: str, path: Path | str) -> dict[str, Any]:
        """Export an STL file after verification."""

        return await self._call(
            "export_stl", {"target": target, "path": str(path), "format": "stl"}
        )

    async def _call(
        self, facade_operation: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        native = self.mapping.native(facade_operation)
        result: ToolResult = await self.adapter.call(
            native, {"_facade_tool": facade_operation, **args}
        )
        if not result.ok:
            raise RuntimeError(
                f"{facade_operation} failed: {result.error_code}: {result.error_message}"
            )
        return result.data
