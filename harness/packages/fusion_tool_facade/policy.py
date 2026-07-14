"""Facade/native tool mapping policy."""

from __future__ import annotations


MOCK_FACADE_NATIVE_MAP = {
    "inspect_design": "inspect_design",
    "create_named_parameter": "create_parameter",
    "update_named_parameter": "update_parameter",
    "create_component": "create_component",
    "activate_component": "activate_component",
    "create_sketch_on_plane": "create_sketch",
    "draw_constrained_rectangle": "draw_rectangle",
    "draw_constrained_circle": "draw_circle",
    "extrude_profile": "extrude",
    "cut_profile": "cut_profile",
    "apply_fillet": "apply_fillet",
    "create_nema17_stepper": "create_nema17_stepper",
    "create_nema17_polish_details": "create_nema17_polish_details",
    "create_nema17_external_assembly": "create_nema17_external_assembly",
    "create_profile2020_aluminum_extrusion": "create_profile2020_aluminum_extrusion",
    "create_mgn12_linear_rail_assembly": "create_mgn12_linear_rail_assembly",
    "create_desktop_cnc_assembly": "create_desktop_cnc_assembly",
    "create_spacer_plate_assembly": "create_spacer_plate_assembly",
    "create_hinge_assembly": "create_hinge_assembly",
    "set_component_metadata": "set_component_metadata",
    "create_assembly_joints": "create_assembly_joints",
    "capture_viewport": "capture_viewport",
    "analyze_interference": "analyze_interference",
    "measure_physical_properties": "measure_physical_properties",
    "measure_bounding_box": "measure_bounding_box",
    "validate_named_objects": "validate_named_objects",
    "export_step": "export_file",
    "export_stl": "export_file",
}


class FacadeMapping:
    """Map stable facade operations to native MCP tool names."""

    def __init__(self, mapping: dict[str, str] | None = None) -> None:
        self.mapping = mapping or dict(MOCK_FACADE_NATIVE_MAP)

    def native(self, facade_operation: str) -> str:
        """Return a native tool name for one facade operation."""

        try:
            return self.mapping[facade_operation]
        except KeyError as exc:
            raise KeyError(f"facade operation is not mapped: {facade_operation}") from exc
