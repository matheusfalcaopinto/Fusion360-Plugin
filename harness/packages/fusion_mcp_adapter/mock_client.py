"""Deterministic mock Fusion MCP client."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from cad_spec.unit_policy import expression_to_mm
from fusion_mcp_adapter.semantics import McpCallOptions
from fusion_mcp_adapter.state import BodyState, ComponentState, FusionState
from fusion_mcp_adapter.tool_result import ToolDefinition, ToolManifest, ToolResult


MOCK_NATIVE_TOOLS = {
    "inspect_design",
    "create_parameter",
    "update_parameter",
    "create_component",
    "activate_component",
    "create_sketch",
    "draw_rectangle",
    "draw_circle",
    "extrude",
    "cut_profile",
    "apply_fillet",
    "create_nema17_stepper",
    "create_nema17_polish_details",
    "create_nema17_external_assembly",
    "create_profile2020_aluminum_extrusion",
    "create_mgn12_linear_rail_assembly",
    "create_desktop_cnc_assembly",
    "create_spacer_plate_assembly",
    "create_hinge_assembly",
    "set_component_metadata",
    "create_assembly_joints",
    "capture_viewport",
    "analyze_interference",
    "measure_physical_properties",
    "measure_bounding_box",
    "validate_named_objects",
    "export_file",
}


class MockMcpClient:
    """In-memory mock that simulates enough Fusion state for tests and benchmarks."""

    def __init__(
        self, units: str = "mm", fail_next: dict[str, str] | None = None
    ) -> None:
        self.state = FusionState(
            units=units,
            components={"root": ComponentState(name="root")},
            active_component="root",
        )
        self.fail_next = fail_next or {}

    async def list_tools(self) -> ToolManifest:
        """Return the deterministic mock manifest."""

        return ToolManifest(
            source="mock",
            tools=[
                ToolDefinition(name=name, description=f"Mock tool {name}")
                for name in sorted(MOCK_NATIVE_TOOLS)
            ],
        )

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        options: McpCallOptions | None = None,
    ) -> ToolResult:
        """Call a mock native tool."""

        del options

        if name in self.fail_next:
            return ToolResult.failure("INJECTED_FAILURE", self.fail_next.pop(name))
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            return ToolResult.failure("UNKNOWN_TOOL", f"mock tool not found: {name}")
        try:
            return ToolResult.success(**handler(arguments))
        except Exception as exc:  # pragma: no cover - defensive normalization
            return ToolResult.failure("MOCK_OPERATION_FAILED", str(exc))

    def _tool_inspect_design(self, _: dict[str, Any]) -> dict[str, Any]:
        self._refresh_bounding_boxes()
        return {
            "state": self.state.model_dump(),
            "complete": True,
            "counts_exact": True,
            "truncated": False,
            "stop_reason": "complete",
            "producer": "fusion_agent_mock",
            "document_identity": "mock:active_document",
            "provenance": {"backend": "deterministic_mock"},
        }

    def _tool_create_parameter(self, args: dict[str, Any]) -> dict[str, Any]:
        name = args["name"]
        if name in self.state.parameters:
            self.state.parameters[name] = args["expression"]
            return {
                "parameter": {
                    "name": name,
                    "expression": args["expression"],
                    "updated": True,
                }
            }
        self.state.parameters[name] = args["expression"]
        return {
            "parameter": {
                "name": name,
                "expression": args["expression"],
                "updated": False,
            }
        }

    def _tool_update_parameter(self, args: dict[str, Any]) -> dict[str, Any]:
        self.state.parameters[args["name"]] = args["expression"]
        self._refresh_bounding_boxes()
        return {
            "parameter": {
                "name": args["name"],
                "expression": args["expression"],
                "updated": True,
            }
        }

    def _tool_create_component(self, args: dict[str, Any]) -> dict[str, Any]:
        name = args["name"]
        self.state.components.setdefault(name, ComponentState(name=name))
        self.state.active_component = name
        return {"component": self.state.components[name].model_dump()}

    def _tool_activate_component(self, args: dict[str, Any]) -> dict[str, Any]:
        name = args["name"]
        if name not in self.state.components:
            raise ValueError(f"component not found: {name}")
        self.state.active_component = name
        return {"active_component": name}

    def _tool_create_sketch(self, args: dict[str, Any]) -> dict[str, Any]:
        name = args["name"]
        component = args.get("component") or self.state.active_component
        self.state.components.setdefault(component, ComponentState(name=component))
        self.state.sketches[name] = {
            "name": name,
            "component": component,
            "plane": args["plane"],
            "profiles_closed": True,
            "profiles": [],
        }
        self.state.components[component].sketches.append(name)
        return {"sketch": deepcopy(self.state.sketches[name])}

    def _tool_draw_rectangle(self, args: dict[str, Any]) -> dict[str, Any]:
        sketch = self.state.sketches[args["sketch"]]
        profile = {
            "ref": f"{args['sketch']}:rectangle:0",
            "shape": "rectangle",
            "width": args["width"],
            "height": args["height"],
        }
        sketch["profiles"].append(profile)
        return {"profile_ref": profile["ref"], "profile": profile}

    def _tool_draw_circle(self, args: dict[str, Any]) -> dict[str, Any]:
        sketch = self.state.sketches[args["sketch"]]
        profile = {
            "ref": f"{args['sketch']}:circle:{len(sketch['profiles'])}",
            "shape": "circle",
            "diameter": args["diameter"],
            "center": args.get("center", ["0 mm", "0 mm"]),
        }
        sketch["profiles"].append(profile)
        return {"profile_ref": profile["ref"], "profile": profile}

    def _tool_extrude(self, args: dict[str, Any]) -> dict[str, Any]:
        component = args["component"]
        body_name = args.get("body_name") or args["name"].replace("_extrude", "_body")
        bbox_expr = self._bbox_expr_for_extrude(args)
        bbox = [expression_to_mm(expr, self.state.parameters) for expr in bbox_expr]
        self.state.bodies[body_name] = BodyState(
            name=body_name,
            component=component,
            bbox_expr=bbox_expr,
            bounding_box_mm=bbox,
        )
        self.state.components.setdefault(
            component, ComponentState(name=component)
        ).bodies.append(body_name)
        self.state.components[component].features.append(args["name"])
        self.state.features[args["name"]] = {
            "name": args["name"],
            "type": args.get("shape", "extrude"),
            "component": component,
            "health": "ok",
            "body": body_name,
        }
        return {
            "body": self.state.bodies[body_name].model_dump(),
            "feature": self.state.features[args["name"]],
        }

    def _tool_cut_profile(self, args: dict[str, Any]) -> dict[str, Any]:
        target = args["target_body"]
        if target not in self.state.bodies:
            raise ValueError(f"target body not found: {target}")
        count = int(args.get("count", 1))
        self.state.bodies[target].holes += count
        self.state.features[args["name"]] = {
            "name": args["name"],
            "type": args.get("cut_type", "cut"),
            "component": self.state.bodies[target].component,
            "health": "ok",
            "target_body": target,
            "count": count,
        }
        self.state.components[self.state.bodies[target].component].features.append(
            args["name"]
        )
        return {
            "body": self.state.bodies[target].model_dump(),
            "feature": self.state.features[args["name"]],
        }

    def _tool_apply_fillet(self, args: dict[str, Any]) -> dict[str, Any]:
        self.state.features[args["name"]] = {
            "name": args["name"],
            "type": "fillet",
            "health": "ok",
            "radius": args["radius"],
        }
        return {"feature": self.state.features[args["name"]]}

    def _tool_create_nema17_stepper(self, args: dict[str, Any]) -> dict[str, Any]:
        component = args["component"]
        body_name = args["body_name"]
        bbox_expr = [args["face_width"], args["face_width"], args["overall_depth"]]
        bbox = [expression_to_mm(expr, self.state.parameters) for expr in bbox_expr]
        self.state.components.setdefault(component, ComponentState(name=component))
        self.state.bodies[body_name] = BodyState(
            name=body_name,
            component=component,
            bbox_expr=bbox_expr,
            bounding_box_mm=bbox,
            holes=int(args.get("mount_hole_count", 4)),
        )
        self.state.components[component].bodies.append(body_name)
        self.state.components[component].features.append(args["name"])
        self.state.features[args["name"]] = {
            "name": args["name"],
            "type": "nema17_stepper_motor",
            "component": component,
            "health": "ok",
            "body": body_name,
            "mount_hole_count": int(args.get("mount_hole_count", 4)),
        }
        self.state.nema17_metrics = {
            "mount_hole_count": int(args.get("mount_hole_count", 4)),
            "mount_hole_spacing_mm": [
                expression_to_mm(args["mount_hole_spacing"], self.state.parameters),
                expression_to_mm(args["mount_hole_spacing"], self.state.parameters),
            ],
            "mount_hole_diameter_mm": expression_to_mm(
                args["mount_hole_diameter"], self.state.parameters
            ),
            "pilot_diameter_mm": expression_to_mm(
                args["pilot_diameter"], self.state.parameters
            ),
            "shaft_diameter_mm": expression_to_mm(
                args["shaft_diameter"], self.state.parameters
            ),
        }
        return {
            "body": self.state.bodies[body_name].model_dump(),
            "feature": self.state.features[args["name"]],
        }

    def _tool_create_nema17_polish_details(
        self, args: dict[str, Any]
    ) -> dict[str, Any]:
        target = args["target_body"]
        if target not in self.state.bodies:
            raise ValueError(f"target body not found: {target}")
        body_names = list(args.get("body_names", []))
        component = self.state.bodies[target].component
        self.state.components.setdefault(component, ComponentState(name=component))
        for body_name in body_names:
            self.state.bodies[body_name] = BodyState(
                name=body_name,
                component=component,
                bounding_box_mm=[],
                valid=True,
            )
            if body_name not in self.state.components[component].bodies:
                self.state.components[component].bodies.append(body_name)
        self.state.features[args["name"]] = {
            "name": args["name"],
            "type": "nema17_visual_polish",
            "component": component,
            "health": "ok",
            "target_body": target,
        }
        self.state.components[component].features.append(args["name"])
        lamination_bodies = [
            name for name in body_names if name.startswith("nema17_lamination_ring_")
        ]
        wire_bodies = [name for name in body_names if name.startswith("nema17_wire_")]
        screw_shadow_bodies = [
            name for name in body_names if name.startswith("nema17_mount_hole_shadow_")
        ]
        self.state.polish_metrics = {
            "body_names": sorted(body_names),
            "lamination_body_count": len(lamination_bodies),
            "wire_count": len(wire_bodies),
            "screw_shadow_count": len(screw_shadow_bodies),
            "connector_present": "nema17_rear_connector_body" in body_names,
            "side_panel_count": len(
                [name for name in body_names if name.startswith("nema17_side_panel_")]
            ),
        }
        return {
            "feature": self.state.features[args["name"]],
            "polish_metrics": self.state.polish_metrics,
        }

    def _tool_create_nema17_external_assembly(
        self, args: dict[str, Any]
    ) -> dict[str, Any]:
        assembly = args["assembly_component"]
        required_components = list(args["component_names"])
        required_bodies = list(args["body_names"])
        self.state.components.setdefault(assembly, ComponentState(name=assembly))
        self.state.active_component = assembly
        for component in required_components:
            self.state.components.setdefault(component, ComponentState(name=component))

        face_width = expression_to_mm(args["face_width"], self.state.parameters)
        body_length = expression_to_mm(args["body_length"], self.state.parameters)
        front_plate_thickness = expression_to_mm(
            args["front_plate_thickness"], self.state.parameters
        )
        rear_plate_thickness = expression_to_mm(
            args["rear_plate_thickness"], self.state.parameters
        )
        pilot_diameter = expression_to_mm(args["pilot_diameter"], self.state.parameters)
        pilot_length = expression_to_mm(args["pilot_length"], self.state.parameters)
        shaft_length = expression_to_mm(args["shaft_length"], self.state.parameters)
        shaft_diameter = expression_to_mm(args["shaft_diameter"], self.state.parameters)
        connector_width = expression_to_mm(
            args["connector_width"], self.state.parameters
        )
        connector_height = expression_to_mm(
            args["connector_height"], self.state.parameters
        )
        connector_depth = expression_to_mm(
            args["connector_depth"], self.state.parameters
        )
        wire_length = expression_to_mm(args["wire_length"], self.state.parameters)
        wire_diameter = expression_to_mm(args["wire_diameter"], self.state.parameters)
        stack_length = body_length - front_plate_thickness - rear_plate_thickness
        lamination_thickness = stack_length / int(args.get("lamination_count", 20))

        body_component_map = {
            "nema17_front_endplate_body": "nema17_front_endplate_component",
            "nema17_front_pilot_boss_body": "nema17_front_endplate_component",
            "nema17_rear_endplate_body": "nema17_rear_endplate_component",
            "nema17_shaft_body": "nema17_shaft_component",
            "nema17_rear_connector_body": "nema17_rear_connector_component",
            "nema17_connector_pin_01": "nema17_rear_connector_component",
            "nema17_connector_pin_02": "nema17_rear_connector_component",
            "nema17_connector_pin_03": "nema17_rear_connector_component",
            "nema17_connector_pin_04": "nema17_rear_connector_component",
            "nema17_wire_red": "nema17_wiring_component",
            "nema17_wire_blue": "nema17_wiring_component",
            "nema17_wire_green": "nema17_wiring_component",
            "nema17_wire_black": "nema17_wiring_component",
        }
        for index in range(1, int(args.get("lamination_count", 20)) + 1):
            body_component_map[f"nema17_stator_lamination_{index:02d}_body"] = (
                "nema17_stator_stack_component"
            )

        for body_name in required_bodies:
            component = body_component_map.get(body_name, assembly)
            bbox = [face_width, face_width, body_length]
            if body_name == "nema17_front_endplate_body":
                bbox = [face_width, face_width, front_plate_thickness]
            elif body_name == "nema17_rear_endplate_body":
                bbox = [face_width, face_width, rear_plate_thickness]
            elif body_name == "nema17_front_pilot_boss_body":
                bbox = [pilot_diameter, pilot_diameter, pilot_length]
            if body_name == "nema17_shaft_body":
                bbox = [shaft_diameter, shaft_diameter, shaft_length]
            elif body_name.startswith("nema17_stator_lamination_"):
                bbox = [face_width, face_width, lamination_thickness]
            elif body_name == "nema17_rear_connector_body":
                bbox = [connector_width, connector_height, connector_depth]
            elif body_name.startswith("nema17_wire_"):
                bbox = [wire_diameter, wire_diameter, wire_length]
            elif body_name.startswith("nema17_connector_pin_"):
                bbox = [wire_diameter * 0.45, wire_diameter * 0.45, 0.35]
            self.state.bodies[body_name] = BodyState(
                name=body_name, component=component, bounding_box_mm=bbox
            )
            if body_name not in self.state.components[component].bodies:
                self.state.components[component].bodies.append(body_name)

        self.state.features[args["name"]] = {
            "name": args["name"],
            "type": "nema17_external_assembly",
            "component": assembly,
            "health": "ok",
        }
        self.state.components[assembly].features.append(args["name"])
        self.state.nema17_metrics = {
            "mount_hole_count": 4,
            "mount_hole_spacing_mm": [
                expression_to_mm(args["mount_hole_spacing"], self.state.parameters),
                expression_to_mm(args["mount_hole_spacing"], self.state.parameters),
            ],
            "mount_hole_diameter_mm": expression_to_mm(
                args["mount_hole_diameter"], self.state.parameters
            ),
            "pilot_diameter_mm": expression_to_mm(
                args["pilot_diameter"], self.state.parameters
            ),
            "shaft_diameter_mm": shaft_diameter,
        }
        self.state.assembly_metrics = {
            "assembly_component": assembly,
            "component_names": sorted(required_components),
            "body_names": sorted(required_bodies),
            "body_components": {
                name: self.state.bodies[name].component for name in required_bodies
            },
            "stator_lamination_count": int(args.get("lamination_count", 20)),
            "wire_count": 4,
            "connector_present": "nema17_rear_connector_body" in required_bodies,
            "legacy_visible_nema17_body_count": 0,
        }
        return {
            "feature": self.state.features[args["name"]],
            "assembly_metrics": self.state.assembly_metrics,
        }

    def _tool_create_profile2020_aluminum_extrusion(
        self, args: dict[str, Any]
    ) -> dict[str, Any]:
        component = args["component"]
        body_name = args["body_name"]
        self.state.components.setdefault(component, ComponentState(name=component))
        self.state.active_component = component

        size = expression_to_mm(args["size"], self.state.parameters)
        length = expression_to_mm(args["length"], self.state.parameters)
        slot_width = expression_to_mm(args["slot_width"], self.state.parameters)
        slot_depth = expression_to_mm(args["slot_depth"], self.state.parameters)
        center_bore = expression_to_mm(
            args["center_bore_diameter"], self.state.parameters
        )

        self.state.bodies[body_name] = BodyState(
            name=body_name,
            component=component,
            bbox_expr=[args["size"], args["size"], args["length"]],
            bounding_box_mm=[size, size, length],
        )
        if body_name not in self.state.components[component].bodies:
            self.state.components[component].bodies.append(body_name)
        self.state.features[args["name"]] = {
            "name": args["name"],
            "type": "profile2020_aluminum_extrusion",
            "component": component,
            "health": "ok",
            "body": body_name,
        }
        self.state.components[component].features.append(args["name"])
        self.state.profile2020_metrics = {
            "component": component,
            "body": body_name,
            "size_mm": size,
            "length_mm": length,
            "slot_count": int(args.get("slot_count", 4)),
            "slot_width_mm": slot_width,
            "slot_depth_mm": slot_depth,
            "center_bore_diameter_mm": center_bore,
            "center_bore_present": True,
            "web_relief_count": int(args.get("web_relief_count", 4)),
            "material": "Aluminum 6063-T6 clear anodized",
        }
        return {
            "feature": self.state.features[args["name"]],
            "profile2020_metrics": self.state.profile2020_metrics,
        }

    def _tool_create_mgn12_linear_rail_assembly(
        self, args: dict[str, Any]
    ) -> dict[str, Any]:
        assembly = args["assembly_component"]
        required_components = list(args["component_names"])
        required_bodies = list(args["body_names"])
        self.state.components.setdefault(assembly, ComponentState(name=assembly))
        self.state.active_component = assembly
        for component in required_components:
            self.state.components.setdefault(component, ComponentState(name=component))

        rail_length = expression_to_mm(args["rail_length"], self.state.parameters)
        rail_width = expression_to_mm(args["rail_width"], self.state.parameters)
        rail_height = expression_to_mm(args["rail_height"], self.state.parameters)
        rail_pitch = expression_to_mm(args["rail_hole_pitch"], self.state.parameters)
        rail_counterbore = expression_to_mm(
            args["rail_counterbore_diameter"], self.state.parameters
        )
        rail_hole = expression_to_mm(args["rail_hole_diameter"], self.state.parameters)
        carriage_length = expression_to_mm(
            args["carriage_length"], self.state.parameters
        )
        carriage_width = expression_to_mm(args["carriage_width"], self.state.parameters)
        carriage_total_height = expression_to_mm(
            args["carriage_total_height"], self.state.parameters
        )
        carriage_top_height = expression_to_mm(
            args["carriage_top_height"], self.state.parameters
        )
        mount_x = expression_to_mm(
            args["carriage_mount_x_spacing"], self.state.parameters
        )
        mount_y = expression_to_mm(
            args["carriage_mount_y_spacing"], self.state.parameters
        )
        mount_thread = expression_to_mm(
            args["carriage_mount_thread_diameter"], self.state.parameters
        )
        rail_hole_count = int(rail_length // rail_pitch)

        body_component_map = {
            "mgn12_rail_body": "mgn12_rail_component",
            "mgn12_carriage_top_body": "mgn12_carriage_component",
            "mgn12_carriage_left_skirt_body": "mgn12_carriage_component",
            "mgn12_carriage_right_skirt_body": "mgn12_carriage_component",
            "mgn12_carriage_front_end_cap_body": "mgn12_carriage_component",
            "mgn12_carriage_rear_end_cap_body": "mgn12_carriage_component",
            "mgn12_ball_return_left_body": "mgn12_carriage_component",
            "mgn12_ball_return_right_body": "mgn12_carriage_component",
            "mgn12_front_rail_stop_body": "mgn12_end_stop_component",
            "mgn12_rear_rail_stop_body": "mgn12_end_stop_component",
        }
        body_bbox_map = {
            "mgn12_rail_body": [rail_length, rail_width, rail_height],
            "mgn12_carriage_top_body": [
                carriage_length,
                carriage_width,
                carriage_top_height,
            ],
            "mgn12_carriage_left_skirt_body": [carriage_length, 3.0, 8.0],
            "mgn12_carriage_right_skirt_body": [carriage_length, 3.0, 8.0],
            "mgn12_carriage_front_end_cap_body": [
                3.0,
                carriage_width,
                carriage_total_height - 3.0,
            ],
            "mgn12_carriage_rear_end_cap_body": [
                3.0,
                carriage_width,
                carriage_total_height - 3.0,
            ],
            "mgn12_ball_return_left_body": [38.0, 1.2, 1.2],
            "mgn12_ball_return_right_body": [38.0, 1.2, 1.2],
            "mgn12_front_rail_stop_body": [3.0, rail_width, rail_height],
            "mgn12_rear_rail_stop_body": [3.0, rail_width, rail_height],
        }
        for body_name in required_bodies:
            component = body_component_map.get(body_name, assembly)
            bbox = body_bbox_map.get(body_name, [1.0, 1.0, 1.0])
            self.state.bodies[body_name] = BodyState(
                name=body_name, component=component, bounding_box_mm=bbox
            )
            if body_name not in self.state.components[component].bodies:
                self.state.components[component].bodies.append(body_name)

        self.state.features[args["name"]] = {
            "name": args["name"],
            "type": "mgn12_linear_rail_assembly",
            "component": assembly,
            "health": "ok",
        }
        self.state.components[assembly].features.append(args["name"])
        self.state.mgn12_metrics = {
            "assembly_component": assembly,
            "component_names": sorted(required_components),
            "body_names": sorted(required_bodies),
            "body_components": {
                name: self.state.bodies[name].component for name in required_bodies
            },
            "rail_length_mm": rail_length,
            "rail_width_mm": rail_width,
            "rail_height_mm": rail_height,
            "rail_mount_hole_count": rail_hole_count,
            "rail_counterbore_count": rail_hole_count,
            "rail_hole_pitch_mm": rail_pitch,
            "rail_hole_diameter_mm": rail_hole,
            "rail_counterbore_diameter_mm": rail_counterbore,
            "carriage_length_mm": carriage_length,
            "carriage_width_mm": carriage_width,
            "carriage_total_height_mm": carriage_total_height,
            "carriage_mount_hole_count": 4,
            "carriage_mount_spacing_mm": [mount_x, mount_y],
            "carriage_mount_thread_diameter_mm": mount_thread,
            "legacy_visible_mgn12_body_count": 0,
            "rail_material": "steel",
            "carriage_material": "steel",
        }
        return {
            "feature": self.state.features[args["name"]],
            "mgn12_metrics": self.state.mgn12_metrics,
        }

    def _tool_create_desktop_cnc_assembly(self, args: dict[str, Any]) -> dict[str, Any]:
        assembly = args["assembly_component"]
        required_components = list(args["component_names"])
        required_bodies = list(args["body_names"])
        self.state.components.setdefault(assembly, ComponentState(name=assembly))
        self.state.active_component = assembly
        for component in required_components:
            self.state.components.setdefault(component, ComponentState(name=component))

        frame_width = expression_to_mm(args["frame_width"], self.state.parameters)
        frame_depth = expression_to_mm(args["frame_depth"], self.state.parameters)
        gantry_height = expression_to_mm(args["gantry_height"], self.state.parameters)
        profile = expression_to_mm(args["profile_size"], self.state.parameters)
        rail_length = expression_to_mm(args["rail_length"], self.state.parameters)
        z_rail_length = expression_to_mm(args["z_rail_length"], self.state.parameters)
        rail_width = expression_to_mm(args["rail_width"], self.state.parameters)
        rail_height = expression_to_mm(args["rail_height"], self.state.parameters)
        motor_face = expression_to_mm(args["motor_face_width"], self.state.parameters)
        motor_length = expression_to_mm(
            args["motor_body_length"], self.state.parameters
        )
        shaft_diameter = expression_to_mm(
            args["motor_shaft_diameter"], self.state.parameters
        )
        shaft_length = expression_to_mm(
            args["motor_shaft_length"], self.state.parameters
        )
        leadscrew_diameter = expression_to_mm(
            args["leadscrew_diameter"], self.state.parameters
        )
        coupler_diameter = expression_to_mm(
            args["coupler_diameter"], self.state.parameters
        )
        coupler_length = expression_to_mm(args["coupler_length"], self.state.parameters)
        plate_thickness = expression_to_mm(
            args["plate_thickness"], self.state.parameters
        )
        spoilboard_length = expression_to_mm(
            args["spoilboard_length"], self.state.parameters
        )
        spoilboard_width = expression_to_mm(
            args["spoilboard_width"], self.state.parameters
        )
        spoilboard_thickness = expression_to_mm(
            args["spoilboard_thickness"], self.state.parameters
        )
        spindle_diameter = expression_to_mm(
            args["spindle_diameter"], self.state.parameters
        )
        spindle_length = expression_to_mm(args["spindle_length"], self.state.parameters)

        body_component_map = {
            "cnc_front_2020_profile_body": "desktop_cnc_frame_component",
            "cnc_rear_2020_profile_body": "desktop_cnc_frame_component",
            "cnc_left_2020_profile_body": "desktop_cnc_frame_component",
            "cnc_right_2020_profile_body": "desktop_cnc_frame_component",
            "cnc_center_2020_profile_body": "desktop_cnc_frame_component",
            "cnc_left_upright_2020_profile_body": "desktop_cnc_frame_component",
            "cnc_right_upright_2020_profile_body": "desktop_cnc_frame_component",
            "cnc_gantry_2020_profile_body": "desktop_cnc_x_axis_component",
            "cnc_spoilboard_body": "desktop_cnc_frame_component",
            "cnc_y_left_mgn12_rail_body": "desktop_cnc_y_axis_component",
            "cnc_y_right_mgn12_rail_body": "desktop_cnc_y_axis_component",
            "cnc_x_mgn12_rail_body": "desktop_cnc_x_axis_component",
            "cnc_z_mgn12_rail_body": "desktop_cnc_z_axis_component",
            "cnc_y_left_carriage_body": "desktop_cnc_y_axis_component",
            "cnc_y_right_carriage_body": "desktop_cnc_y_axis_component",
            "cnc_x_carriage_body": "desktop_cnc_x_axis_component",
            "cnc_z_carriage_body": "desktop_cnc_z_axis_component",
            "cnc_left_gantry_plate_body": "desktop_cnc_y_axis_component",
            "cnc_right_gantry_plate_body": "desktop_cnc_y_axis_component",
            "cnc_x_carriage_plate_body": "desktop_cnc_x_axis_component",
            "cnc_z_carriage_plate_body": "desktop_cnc_z_axis_component",
            "cnc_x_nema17_body": "desktop_cnc_motion_component",
            "cnc_y_nema17_body": "desktop_cnc_motion_component",
            "cnc_z_nema17_body": "desktop_cnc_motion_component",
            "cnc_x_motor_shaft_body": "desktop_cnc_motion_component",
            "cnc_y_motor_shaft_body": "desktop_cnc_motion_component",
            "cnc_z_motor_shaft_body": "desktop_cnc_motion_component",
            "cnc_x_t8_leadscrew_body": "desktop_cnc_motion_component",
            "cnc_y_t8_leadscrew_body": "desktop_cnc_motion_component",
            "cnc_z_t8_leadscrew_body": "desktop_cnc_motion_component",
            "cnc_x_coupler_body": "desktop_cnc_motion_component",
            "cnc_y_coupler_body": "desktop_cnc_motion_component",
            "cnc_z_coupler_body": "desktop_cnc_motion_component",
            "cnc_x_bearing_block_body": "desktop_cnc_motion_component",
            "cnc_y_bearing_block_body": "desktop_cnc_motion_component",
            "cnc_z_bearing_block_body": "desktop_cnc_motion_component",
            "cnc_spindle_clamp_body": "desktop_cnc_spindle_component",
            "cnc_spindle_body": "desktop_cnc_spindle_component",
            "cnc_er11_collet_body": "desktop_cnc_spindle_component",
            "cnc_x_drag_chain_body": "desktop_cnc_electronics_component",
            "cnc_y_drag_chain_body": "desktop_cnc_electronics_component",
            "cnc_controller_box_body": "desktop_cnc_electronics_component",
        }
        body_bbox_map = {
            "cnc_front_2020_profile_body": [frame_width, profile, profile],
            "cnc_rear_2020_profile_body": [frame_width, profile, profile],
            "cnc_left_2020_profile_body": [profile, frame_depth, profile],
            "cnc_right_2020_profile_body": [profile, frame_depth, profile],
            "cnc_center_2020_profile_body": [frame_width - 40.0, profile, profile],
            "cnc_left_upright_2020_profile_body": [profile, profile, gantry_height],
            "cnc_right_upright_2020_profile_body": [profile, profile, gantry_height],
            "cnc_gantry_2020_profile_body": [frame_width, profile, profile],
            "cnc_spoilboard_body": [
                spoilboard_length,
                spoilboard_width,
                spoilboard_thickness,
            ],
            "cnc_y_left_mgn12_rail_body": [rail_width, rail_length, rail_height],
            "cnc_y_right_mgn12_rail_body": [rail_width, rail_length, rail_height],
            "cnc_x_mgn12_rail_body": [rail_length, rail_width, rail_height],
            "cnc_z_mgn12_rail_body": [rail_width, rail_height, z_rail_length],
            "cnc_y_left_carriage_body": [27.0, 45.4, 13.0],
            "cnc_y_right_carriage_body": [27.0, 45.4, 13.0],
            "cnc_x_carriage_body": [45.4, 27.0, 13.0],
            "cnc_z_carriage_body": [27.0, 13.0, 45.4],
            "cnc_left_gantry_plate_body": [plate_thickness, 80.0, 120.0],
            "cnc_right_gantry_plate_body": [plate_thickness, 80.0, 120.0],
            "cnc_x_carriage_plate_body": [90.0, plate_thickness, 70.0],
            "cnc_z_carriage_plate_body": [80.0, plate_thickness, 110.0],
            "cnc_x_nema17_body": [motor_face, motor_face, motor_length],
            "cnc_y_nema17_body": [motor_face, motor_face, motor_length],
            "cnc_z_nema17_body": [motor_face, motor_face, motor_length],
            "cnc_x_motor_shaft_body": [shaft_diameter, shaft_diameter, shaft_length],
            "cnc_y_motor_shaft_body": [shaft_diameter, shaft_diameter, shaft_length],
            "cnc_z_motor_shaft_body": [shaft_diameter, shaft_diameter, shaft_length],
            "cnc_x_t8_leadscrew_body": [
                rail_length,
                leadscrew_diameter,
                leadscrew_diameter,
            ],
            "cnc_y_t8_leadscrew_body": [
                leadscrew_diameter,
                rail_length,
                leadscrew_diameter,
            ],
            "cnc_z_t8_leadscrew_body": [
                leadscrew_diameter,
                leadscrew_diameter,
                z_rail_length,
            ],
            "cnc_x_coupler_body": [coupler_length, coupler_diameter, coupler_diameter],
            "cnc_y_coupler_body": [coupler_diameter, coupler_length, coupler_diameter],
            "cnc_z_coupler_body": [coupler_diameter, coupler_diameter, coupler_length],
            "cnc_x_bearing_block_body": [25.0, 25.0, 12.0],
            "cnc_y_bearing_block_body": [25.0, 25.0, 12.0],
            "cnc_z_bearing_block_body": [25.0, 12.0, 25.0],
            "cnc_spindle_clamp_body": [70.0, 20.0, 70.0],
            "cnc_spindle_body": [spindle_diameter, spindle_diameter, spindle_length],
            "cnc_er11_collet_body": [16.0, 16.0, 22.0],
            "cnc_x_drag_chain_body": [150.0, 14.0, 12.0],
            "cnc_y_drag_chain_body": [14.0, 120.0, 12.0],
            "cnc_controller_box_body": [70.0, 45.0, 18.0],
        }
        for body_name in required_bodies:
            component = body_component_map.get(body_name, assembly)
            bbox = body_bbox_map.get(body_name, [1.0, 1.0, 1.0])
            self.state.bodies[body_name] = BodyState(
                name=body_name, component=component, bounding_box_mm=bbox
            )
            if body_name not in self.state.components[component].bodies:
                self.state.components[component].bodies.append(body_name)

        self.state.features[args["name"]] = {
            "name": args["name"],
            "type": "desktop_cnc_assembly",
            "component": assembly,
            "health": "ok",
        }
        self.state.components[assembly].features.append(args["name"])
        self.state.cnc_metrics = {
            "assembly_component": assembly,
            "component_names": sorted(required_components),
            "body_names": sorted(required_bodies),
            "body_components": {
                name: self.state.bodies[name].component for name in required_bodies
            },
            "profile_count": 8,
            "rail_count": 4,
            "motor_count": 3,
            "leadscrew_count": 3,
            "coupler_count": 3,
            "spindle_diameter_mm": spindle_diameter,
            "work_area_mm": [
                expression_to_mm(args["work_area_x"], self.state.parameters),
                expression_to_mm(args["work_area_y"], self.state.parameters),
                expression_to_mm(args["work_area_z"], self.state.parameters),
            ],
            "legacy_visible_cnc_body_count": 0,
            "frame_material": "aluminum",
            "rail_material": "steel",
            "plate_material": "aluminum",
        }
        return {
            "feature": self.state.features[args["name"]],
            "cnc_metrics": self.state.cnc_metrics,
        }

    def _tool_create_spacer_plate_assembly(
        self, args: dict[str, Any]
    ) -> dict[str, Any]:
        assembly = args["assembly_component"]
        required_components = list(args["component_names"])
        required_bodies = list(args["body_names"])
        occurrence_names = list(args.get("occurrence_names", []))
        self.state.components.setdefault(assembly, ComponentState(name=assembly))
        self.state.active_component = assembly
        for component in required_components:
            self.state.components.setdefault(component, ComponentState(name=component))

        plate_length = expression_to_mm(args["plate_length"], self.state.parameters)
        plate_width = expression_to_mm(args["plate_width"], self.state.parameters)
        plate_thickness = expression_to_mm(
            args["plate_thickness"], self.state.parameters
        )
        plate_gap = expression_to_mm(args["plate_gap"], self.state.parameters)
        standoff_diameter = expression_to_mm(
            args["standoff_diameter"], self.state.parameters
        )
        standoff_height = expression_to_mm(
            args["standoff_height"], self.state.parameters
        )

        body_component_map = {
            "spacer_top_plate_body": "spacer_top_plate_component",
            "spacer_bottom_plate_body": "spacer_bottom_plate_component",
            "spacer_standoff_body": "spacer_standoff_component",
        }
        body_bbox_map = {
            "spacer_top_plate_body": [plate_length, plate_width, plate_thickness],
            "spacer_bottom_plate_body": [plate_length, plate_width, plate_thickness],
            "spacer_standoff_body": [
                standoff_diameter,
                standoff_diameter,
                standoff_height,
            ],
        }
        for body_name in required_bodies:
            component = body_component_map.get(body_name, assembly)
            self.state.bodies[body_name] = BodyState(
                name=body_name,
                component=component,
                bounding_box_mm=body_bbox_map.get(body_name, [1.0, 1.0, 1.0]),
            )
            if body_name not in self.state.components[component].bodies:
                self.state.components[component].bodies.append(body_name)
        for index, occurrence_name in enumerate(occurrence_names, start=1):
            self.state.occurrences[occurrence_name] = {
                "name": occurrence_name,
                "component": "spacer_standoff_component",
                "parent": assembly,
                "index": index,
            }
        self.state.features[args["name"]] = {
            "name": args["name"],
            "type": "spacer_plate_assembly",
            "component": assembly,
            "health": "ok",
        }
        self.state.components[assembly].features.append(args["name"])
        self.state.physical_properties.update(
            {
                "spacer_top_plate_component": _physical_payload(
                    plate_length * plate_width * plate_thickness
                ),
                "spacer_bottom_plate_component": _physical_payload(
                    plate_length * plate_width * plate_thickness
                ),
                "spacer_standoff_component": _physical_payload(
                    3.14159 * (standoff_diameter / 2.0) ** 2 * plate_gap
                ),
            }
        )
        self.state.interference = {"count": 0, "pairs": []}
        return {
            "feature": self.state.features[args["name"]],
            "occurrences": self.state.occurrences,
            "physical_properties": self.state.physical_properties,
            "interference": self.state.interference,
        }

    def _tool_create_hinge_assembly(self, args: dict[str, Any]) -> dict[str, Any]:
        assembly = args["assembly_component"]
        required_components = list(args["component_names"])
        required_bodies = list(args["body_names"])
        self.state.components.setdefault(assembly, ComponentState(name=assembly))
        self.state.active_component = assembly
        for component in required_components:
            self.state.components.setdefault(component, ComponentState(name=component))
            self.state.occurrences.setdefault(
                f"{component}_occurrence",
                {
                    "name": f"{component}_occurrence",
                    "component": component,
                    "parent": assembly,
                    "index": 1,
                },
            )

        leaf_length = expression_to_mm(args["leaf_length"], self.state.parameters)
        leaf_width = expression_to_mm(args["leaf_width"], self.state.parameters)
        leaf_thickness = expression_to_mm(args["leaf_thickness"], self.state.parameters)
        pin_diameter = expression_to_mm(args["pin_diameter"], self.state.parameters)
        pin_length = expression_to_mm(args["pin_length"], self.state.parameters)
        knuckle_diameter = expression_to_mm(
            args["knuckle_outer_diameter"], self.state.parameters
        )
        knuckle_length = expression_to_mm(args["knuckle_length"], self.state.parameters)

        body_component_map = {
            "hinge_left_leaf_body": "hinge_left_leaf_component",
            "hinge_right_leaf_body": "hinge_right_leaf_component",
            "hinge_pin_body": "hinge_pin_component",
            "hinge_left_knuckle_01_body": "hinge_left_leaf_component",
            "hinge_left_knuckle_02_body": "hinge_left_leaf_component",
            "hinge_right_knuckle_body": "hinge_right_leaf_component",
        }
        body_bbox_map = {
            "hinge_left_leaf_body": [leaf_length, leaf_width, leaf_thickness],
            "hinge_right_leaf_body": [leaf_length, leaf_width, leaf_thickness],
            "hinge_pin_body": [pin_length, pin_diameter, pin_diameter],
            "hinge_left_knuckle_01_body": [
                knuckle_length,
                knuckle_diameter,
                knuckle_diameter,
            ],
            "hinge_left_knuckle_02_body": [
                knuckle_length,
                knuckle_diameter,
                knuckle_diameter,
            ],
            "hinge_right_knuckle_body": [
                knuckle_length,
                knuckle_diameter,
                knuckle_diameter,
            ],
        }
        for body_name in required_bodies:
            component = body_component_map.get(body_name, assembly)
            self.state.bodies[body_name] = BodyState(
                name=body_name,
                component=component,
                bounding_box_mm=body_bbox_map.get(body_name, [1.0, 1.0, 1.0]),
            )
            if body_name not in self.state.components[component].bodies:
                self.state.components[component].bodies.append(body_name)
        self.state.features[args["name"]] = {
            "name": args["name"],
            "type": "hinge_assembly",
            "component": assembly,
            "health": "ok",
        }
        self.state.components[assembly].features.append(args["name"])
        self.state.physical_properties.update(
            {
                "hinge_left_leaf_component": _physical_payload(
                    leaf_length * leaf_width * leaf_thickness
                ),
                "hinge_right_leaf_component": _physical_payload(
                    leaf_length * leaf_width * leaf_thickness
                ),
                "hinge_pin_component": _physical_payload(
                    3.14159 * (pin_diameter / 2.0) ** 2 * pin_length
                ),
            }
        )
        self.state.interference = {"count": 0, "pairs": []}
        return {
            "feature": self.state.features[args["name"]],
            "occurrences": self.state.occurrences,
            "physical_properties": self.state.physical_properties,
            "interference": self.state.interference,
        }

    def _tool_set_component_metadata(self, args: dict[str, Any]) -> dict[str, Any]:
        updated = {}
        for item in args.get("metadata", []):
            component_name = item["component"]
            component = self.state.components.setdefault(
                component_name, ComponentState(name=component_name)
            )
            metadata = dict(item)
            component.metadata = metadata
            self.state.component_metadata[component_name] = metadata
            updated[component_name] = metadata
        return {"component_metadata": updated}

    def _tool_create_assembly_joints(self, args: dict[str, Any]) -> dict[str, Any]:
        for joint in args.get("joints", []):
            self.state.joints[joint["name"]] = {
                "name": joint["name"],
                "type": joint["type"],
                "parent": joint["parent"],
                "child": joint["child"],
                "axis": joint.get("axis"),
                "limits": dict(joint.get("limits") or {}),
                "health": "ok",
            }
        return {"joints": deepcopy(self.state.joints)}

    def _tool_capture_viewport(self, args: dict[str, Any]) -> dict[str, Any]:
        path = Path(args["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"mock png capture\n")
        payload = {
            "name": args["name"],
            "path": str(path),
            "view": args.get("view", "isometric"),
            "bytes": path.stat().st_size,
            "ok": True,
            "evidence_quality": "verified_file",
        }
        self.state.screenshots[args["name"]] = payload
        return {"screenshot": payload}

    def _tool_analyze_interference(self, _: dict[str, Any]) -> dict[str, Any]:
        if not self.state.interference:
            self.state.interference = {"count": 0, "pairs": []}
        return {"interference": self.state.interference}

    def _tool_measure_physical_properties(self, args: dict[str, Any]) -> dict[str, Any]:
        targets = list(args.get("targets") or self.state.components.keys())
        measured = {}
        for target in targets:
            measured[target] = self.state.physical_properties.get(
                target
            ) or _physical_payload(1.0)
            self.state.physical_properties[target] = measured[target]
        return {"physical_properties": measured}

    def _tool_measure_bounding_box(self, args: dict[str, Any]) -> dict[str, Any]:
        self._refresh_bounding_boxes()
        target = args.get("target")
        if target and target in self.state.bodies:
            return {"bounding_box_mm": self.state.bodies[target].bounding_box_mm}
        if not self.state.bodies:
            return {"bounding_box_mm": [0.0, 0.0, 0.0]}
        maxes = [0.0, 0.0, 0.0]
        for body in self.state.bodies.values():
            maxes = [
                max(a, b) for a, b in zip(maxes, body.bounding_box_mm, strict=True)
            ]
        return {"bounding_box_mm": maxes}

    def _tool_validate_named_objects(self, _: dict[str, Any]) -> dict[str, Any]:
        names = (
            list(self.state.components)
            + list(self.state.bodies)
            + list(self.state.sketches)
            + list(self.state.features)
            + list(self.state.parameters)
        )
        invalid = [name for name in names if name and name[0].isupper()]
        return {"valid": not invalid, "invalid": invalid}

    def _tool_export_file(self, args: dict[str, Any]) -> dict[str, Any]:
        path = Path(args["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"mock {args['format']} export for {args.get('target', 'design')}\n",
            encoding="utf-8",
        )
        self.state.exports[str(path)] = {
            "path": str(path),
            "format": args["format"],
            "bytes": path.stat().st_size,
        }
        return {"export": self.state.exports[str(path)]}

    def _bbox_expr_for_extrude(self, args: dict[str, Any]) -> list[str]:
        shape = args.get("shape", "rectangle")
        if shape == "rectangle":
            return [args["width"], args["height"], args["distance"]]
        if shape == "cylinder":
            return [args["diameter"], args["diameter"], args["distance"]]
        if shape == "l_bracket":
            return [args["leg_length"], args["leg_length"], args["thickness"]]
        if shape == "box_shell":
            return [args["length"], args["width"], args["height"]]
        raise ValueError(f"unsupported mock extrude shape: {shape}")

    def _refresh_bounding_boxes(self) -> None:
        for body in self.state.bodies.values():
            if body.bbox_expr:
                body.bounding_box_mm = [
                    expression_to_mm(expr, self.state.parameters)
                    for expr in body.bbox_expr
                ]


def _physical_payload(volume_mm3: float) -> dict[str, float]:
    volume_cm3 = max(volume_mm3 / 1000.0, 0.001)
    return {
        "mass_kg": volume_cm3 * 0.0027,
        "volume_mm3": max(volume_mm3, 0.001),
        "density_g_cm3": 2.7,
    }
