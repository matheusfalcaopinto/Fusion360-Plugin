from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fusion_mcp_adapter.mock_client import MOCK_NATIVE_TOOLS, MockMcpClient


async def _success(
    client: MockMcpClient, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    result = await client.call_tool(name, arguments)
    assert result.ok, result.error_message
    return result.data


@pytest.mark.asyncio
async def test_mock_manifest_and_fail_closed_dispatch() -> None:
    client = MockMcpClient(fail_next={"inspect_design": "injected once"})
    manifest = await client.list_tools()
    assert manifest.names() == MOCK_NATIVE_TOOLS
    assert manifest.source == "mock"

    injected = await client.call_tool("inspect_design", {})
    assert not injected.ok
    assert injected.error_code == "INJECTED_FAILURE"
    assert injected.error_message == "injected once"

    recovered = await client.call_tool("inspect_design", {})
    assert recovered.ok
    unknown = await client.call_tool("arbitrary_script", {"script": "forbidden"})
    assert not unknown.ok
    assert unknown.error_code == "UNKNOWN_TOOL"


@pytest.mark.asyncio
async def test_mock_primitive_modeling_and_measurement_contract(tmp_path: Path) -> None:
    client = MockMcpClient()
    empty_bbox = await _success(client, "measure_bounding_box", {})
    assert empty_bbox["bounding_box_mm"] == [0.0, 0.0, 0.0]

    created = await _success(
        client,
        "create_parameter",
        {"name": "width", "expression": "10 mm"},
    )
    assert created["parameter"]["updated"] is False
    updated_by_create = await _success(
        client,
        "create_parameter",
        {"name": "width", "expression": "12 mm"},
    )
    assert updated_by_create["parameter"]["updated"] is True
    await _success(
        client,
        "update_parameter",
        {"name": "width", "expression": "15 mm"},
    )

    missing_component = await client.call_tool(
        "activate_component", {"name": "missing_component"}
    )
    assert not missing_component.ok
    assert missing_component.error_code == "MOCK_OPERATION_FAILED"

    await _success(client, "create_component", {"name": "fixture_component"})
    await _success(client, "create_component", {"name": "fixture_component"})
    await _success(client, "activate_component", {"name": "fixture_component"})
    await _success(
        client,
        "create_sketch",
        {"name": "base_sketch", "plane": "xy"},
    )
    await _success(
        client,
        "create_sketch",
        {
            "name": "explicit_sketch",
            "plane": "xz",
            "component": "fixture_component",
        },
    )
    rectangle = await _success(
        client,
        "draw_rectangle",
        {"sketch": "base_sketch", "width": "width", "height": "5 mm"},
    )
    first_circle = await _success(
        client,
        "draw_circle",
        {"sketch": "base_sketch", "diameter": "2 mm"},
    )
    second_circle = await _success(
        client,
        "draw_circle",
        {
            "sketch": "base_sketch",
            "diameter": "3 mm",
            "center": ["1 mm", "1 mm"],
        },
    )
    assert rectangle["profile"]["shape"] == "rectangle"
    assert first_circle["profile"]["center"] == ["0 mm", "0 mm"]
    assert second_circle["profile"]["center"] == ["1 mm", "1 mm"]

    shapes = [
        (
            "rectangle",
            {
                "width": "width",
                "height": "5 mm",
                "distance": "2 mm",
            },
        ),
        (
            "cylinder",
            {"diameter": "6 mm", "distance": "3 mm"},
        ),
        (
            "l_bracket",
            {"leg_length": "8 mm", "thickness": "2 mm", "distance": "4 mm"},
        ),
        (
            "box_shell",
            {
                "length": "20 mm",
                "width": "10 mm",
                "height": "6 mm",
                "distance": "1 mm",
            },
        ),
    ]
    for index, (shape, shape_inputs) in enumerate(shapes):
        await _success(
            client,
            "extrude",
            {
                "component": "fixture_component",
                "name": f"feature_{index}",
                "body_name": f"body_{index}",
                "shape": shape,
                **shape_inputs,
            },
        )

    unsupported = await client.call_tool(
        "extrude",
        {
            "component": "fixture_component",
            "name": "bad_feature",
            "body_name": "bad_body",
            "shape": "revolve",
            "distance": "1 mm",
        },
    )
    assert not unsupported.ok

    missing_cut = await client.call_tool(
        "cut_profile", {"name": "missing_cut", "target_body": "missing"}
    )
    assert not missing_cut.ok
    cut = await _success(
        client,
        "cut_profile",
        {
            "name": "hole_pattern",
            "target_body": "body_0",
            "count": 3,
            "cut_type": "through_all",
        },
    )
    assert cut["body"]["holes"] == 3
    await _success(
        client,
        "apply_fillet",
        {"name": "edge_fillet", "radius": "1 mm"},
    )

    target_bbox = await _success(client, "measure_bounding_box", {"target": "body_0"})
    aggregate_bbox = await _success(client, "measure_bounding_box", {})
    assert target_bbox["bounding_box_mm"] == [15.0, 5.0, 2.0]
    assert aggregate_bbox["bounding_box_mm"] == [20.0, 10.0, 6.0]

    client.state.interference = {}
    assert (await _success(client, "analyze_interference", {}))["interference"] == {
        "count": 0,
        "pairs": [],
    }
    client.state.interference = {"count": 1, "pairs": [["a", "b"]]}
    assert (await _success(client, "analyze_interference", {}))["interference"][
        "count"
    ] == 1

    client.state.physical_properties["fixture_component"] = {
        "mass_kg": 1.0,
        "volume_mm3": 2.0,
        "density_g_cm3": 2.7,
    }
    explicit_properties = await _success(
        client,
        "measure_physical_properties",
        {"targets": ["fixture_component", "unmeasured"]},
    )
    assert (
        explicit_properties["physical_properties"]["fixture_component"]["mass_kg"]
        == 1.0
    )
    default_properties = await _success(
        client, "measure_physical_properties", {"targets": []}
    )
    assert "root" in default_properties["physical_properties"]

    metadata = await _success(
        client,
        "set_component_metadata",
        {
            "metadata": [
                {
                    "component": "fixture_component",
                    "part_number": "PN-1",
                    "role": "fixture",
                }
            ]
        },
    )
    assert metadata["component_metadata"]["fixture_component"]["part_number"] == "PN-1"
    joints = await _success(
        client,
        "create_assembly_joints",
        {
            "joints": [
                {
                    "name": "fixture_joint",
                    "type": "revolute",
                    "parent": "root",
                    "child": "fixture_component",
                    "axis": "z",
                }
            ]
        },
    )
    assert joints["joints"]["fixture_joint"]["limits"] == {}

    capture_path = tmp_path / "captures" / "view.png"
    capture = await _success(
        client,
        "capture_viewport",
        {"name": "fixture_view", "path": str(capture_path)},
    )
    assert capture["screenshot"]["view"] == "isometric"
    assert capture_path.is_file()

    export_path = tmp_path / "exports" / "fixture.step"
    export = await _success(
        client,
        "export_file",
        {"path": str(export_path), "format": "step"},
    )
    assert export["export"]["bytes"] > 0
    assert export_path.is_file()

    await _success(client, "create_component", {"name": "InvalidComponent"})
    invalid_names = await _success(client, "validate_named_objects", {})
    assert invalid_names["valid"] is False
    assert "InvalidComponent" in invalid_names["invalid"]
    inspection = await _success(client, "inspect_design", {})
    assert inspection["complete"] is True
    assert inspection["state"]["parameters"]["width"] == "15 mm"

    fresh = MockMcpClient()
    assert (await _success(fresh, "validate_named_objects", {}))["valid"] is True


@pytest.mark.asyncio
async def test_mock_nema17_semantic_and_external_assembly_contract() -> None:
    client = MockMcpClient()
    stepper = await _success(
        client,
        "create_nema17_stepper",
        {
            "name": "nema17_feature",
            "component": "nema17_component",
            "body_name": "nema17_body",
            "face_width": "42 mm",
            "overall_depth": "40 mm",
            "mount_hole_count": 4,
            "mount_hole_spacing": "31 mm",
            "mount_hole_diameter": "3 mm",
            "pilot_diameter": "22 mm",
            "shaft_diameter": "5 mm",
        },
    )
    assert stepper["body"]["holes"] == 4

    polish_body_names = [
        "nema17_lamination_ring_01",
        "nema17_lamination_ring_02",
        "nema17_wire_red",
        "nema17_mount_hole_shadow_01",
        "nema17_rear_connector_body",
        "nema17_side_panel_01",
    ]
    polish = await _success(
        client,
        "create_nema17_polish_details",
        {
            "name": "nema17_polish",
            "target_body": "nema17_body",
            "body_names": polish_body_names,
        },
    )
    assert polish["polish_metrics"]["lamination_body_count"] == 2
    assert polish["polish_metrics"]["connector_present"] is True
    missing_target = await client.call_tool(
        "create_nema17_polish_details",
        {"name": "bad_polish", "target_body": "missing"},
    )
    assert not missing_target.ok

    components = [
        "nema17_external_assembly",
        "nema17_front_endplate_component",
        "nema17_rear_endplate_component",
        "nema17_shaft_component",
        "nema17_rear_connector_component",
        "nema17_wiring_component",
        "nema17_stator_stack_component",
    ]
    bodies = [
        "nema17_front_endplate_body",
        "nema17_rear_endplate_body",
        "nema17_front_pilot_boss_body",
        "nema17_shaft_body",
        "nema17_rear_connector_body",
        "nema17_connector_pin_01",
        "nema17_connector_pin_02",
        "nema17_connector_pin_03",
        "nema17_connector_pin_04",
        "nema17_wire_red",
        "nema17_wire_blue",
        "nema17_wire_green",
        "nema17_wire_black",
        "nema17_stator_lamination_01_body",
        "nema17_stator_lamination_02_body",
        "nema17_unmapped_cover_body",
    ]
    assembly = await _success(
        client,
        "create_nema17_external_assembly",
        {
            "name": "nema17_external_feature",
            "assembly_component": "nema17_external_assembly",
            "component_names": components,
            "body_names": bodies,
            "face_width": "42 mm",
            "body_length": "40 mm",
            "front_plate_thickness": "3 mm",
            "rear_plate_thickness": "3 mm",
            "pilot_diameter": "22 mm",
            "pilot_length": "2 mm",
            "shaft_length": "24 mm",
            "shaft_diameter": "5 mm",
            "connector_width": "12 mm",
            "connector_height": "8 mm",
            "connector_depth": "5 mm",
            "wire_length": "80 mm",
            "wire_diameter": "1 mm",
            "lamination_count": 2,
            "mount_hole_spacing": "31 mm",
            "mount_hole_diameter": "3 mm",
        },
    )
    metrics = assembly["assembly_metrics"]
    assert metrics["stator_lamination_count"] == 2
    assert metrics["connector_present"] is True
    assert metrics["body_components"]["nema17_unmapped_cover_body"] == (
        "nema17_external_assembly"
    )


@pytest.mark.asyncio
async def test_mock_profile_and_mgn12_assembly_contract() -> None:
    client = MockMcpClient()
    profile = await _success(
        client,
        "create_profile2020_aluminum_extrusion",
        {
            "name": "profile_feature",
            "component": "profile_component",
            "body_name": "profile_body",
            "size": "20 mm",
            "length": "500 mm",
            "slot_width": "6 mm",
            "slot_depth": "6 mm",
            "center_bore_diameter": "4.2 mm",
            "slot_count": 4,
            "web_relief_count": 4,
        },
    )
    assert profile["profile2020_metrics"]["material"].startswith("Aluminum")

    components = [
        "mgn12_assembly",
        "mgn12_rail_component",
        "mgn12_carriage_component",
        "mgn12_end_stop_component",
    ]
    bodies = [
        "mgn12_rail_body",
        "mgn12_carriage_top_body",
        "mgn12_carriage_left_skirt_body",
        "mgn12_carriage_right_skirt_body",
        "mgn12_carriage_front_end_cap_body",
        "mgn12_carriage_rear_end_cap_body",
        "mgn12_ball_return_left_body",
        "mgn12_ball_return_right_body",
        "mgn12_front_rail_stop_body",
        "mgn12_rear_rail_stop_body",
        "mgn12_unmapped_body",
    ]
    rail = await _success(
        client,
        "create_mgn12_linear_rail_assembly",
        {
            "name": "mgn12_feature",
            "assembly_component": "mgn12_assembly",
            "component_names": components,
            "body_names": bodies,
            "rail_length": "300 mm",
            "rail_width": "12 mm",
            "rail_height": "8 mm",
            "rail_hole_pitch": "25 mm",
            "rail_counterbore_diameter": "6 mm",
            "rail_hole_diameter": "3.5 mm",
            "carriage_length": "45.4 mm",
            "carriage_width": "27 mm",
            "carriage_total_height": "13 mm",
            "carriage_top_height": "5 mm",
            "carriage_mount_x_spacing": "20 mm",
            "carriage_mount_y_spacing": "15 mm",
            "carriage_mount_thread_diameter": "3 mm",
        },
    )
    metrics = rail["mgn12_metrics"]
    assert metrics["rail_mount_hole_count"] == 12
    assert metrics["body_components"]["mgn12_unmapped_body"] == "mgn12_assembly"


CNC_COMPONENTS = [
    "desktop_cnc_assembly",
    "desktop_cnc_frame_component",
    "desktop_cnc_x_axis_component",
    "desktop_cnc_y_axis_component",
    "desktop_cnc_z_axis_component",
    "desktop_cnc_motion_component",
    "desktop_cnc_spindle_component",
    "desktop_cnc_electronics_component",
]

CNC_BODIES = [
    "cnc_front_2020_profile_body",
    "cnc_rear_2020_profile_body",
    "cnc_left_2020_profile_body",
    "cnc_right_2020_profile_body",
    "cnc_center_2020_profile_body",
    "cnc_left_upright_2020_profile_body",
    "cnc_right_upright_2020_profile_body",
    "cnc_gantry_2020_profile_body",
    "cnc_spoilboard_body",
    "cnc_y_left_mgn12_rail_body",
    "cnc_y_right_mgn12_rail_body",
    "cnc_x_mgn12_rail_body",
    "cnc_z_mgn12_rail_body",
    "cnc_y_left_carriage_body",
    "cnc_y_right_carriage_body",
    "cnc_x_carriage_body",
    "cnc_z_carriage_body",
    "cnc_left_gantry_plate_body",
    "cnc_right_gantry_plate_body",
    "cnc_x_carriage_plate_body",
    "cnc_z_carriage_plate_body",
    "cnc_x_nema17_body",
    "cnc_y_nema17_body",
    "cnc_z_nema17_body",
    "cnc_x_motor_shaft_body",
    "cnc_y_motor_shaft_body",
    "cnc_z_motor_shaft_body",
    "cnc_x_t8_leadscrew_body",
    "cnc_y_t8_leadscrew_body",
    "cnc_z_t8_leadscrew_body",
    "cnc_x_coupler_body",
    "cnc_y_coupler_body",
    "cnc_z_coupler_body",
    "cnc_x_bearing_block_body",
    "cnc_y_bearing_block_body",
    "cnc_z_bearing_block_body",
    "cnc_spindle_clamp_body",
    "cnc_spindle_body",
    "cnc_er11_collet_body",
    "cnc_x_drag_chain_body",
    "cnc_y_drag_chain_body",
    "cnc_controller_box_body",
    "cnc_unmapped_fixture_body",
]


@pytest.mark.asyncio
async def test_mock_desktop_cnc_assembly_contract() -> None:
    client = MockMcpClient()
    assembly = await _success(
        client,
        "create_desktop_cnc_assembly",
        {
            "name": "desktop_cnc_feature",
            "assembly_component": "desktop_cnc_assembly",
            "component_names": CNC_COMPONENTS,
            "body_names": CNC_BODIES,
            "frame_width": "500 mm",
            "frame_depth": "400 mm",
            "gantry_height": "250 mm",
            "profile_size": "20 mm",
            "rail_length": "300 mm",
            "z_rail_length": "180 mm",
            "rail_width": "12 mm",
            "rail_height": "8 mm",
            "motor_face_width": "42 mm",
            "motor_body_length": "40 mm",
            "motor_shaft_diameter": "5 mm",
            "motor_shaft_length": "24 mm",
            "leadscrew_diameter": "8 mm",
            "coupler_diameter": "20 mm",
            "coupler_length": "25 mm",
            "plate_thickness": "6 mm",
            "spoilboard_length": "420 mm",
            "spoilboard_width": "320 mm",
            "spoilboard_thickness": "12 mm",
            "spindle_diameter": "65 mm",
            "spindle_length": "200 mm",
            "work_area_x": "300 mm",
            "work_area_y": "250 mm",
            "work_area_z": "80 mm",
        },
    )
    metrics = assembly["cnc_metrics"]
    assert metrics["motor_count"] == 3
    assert metrics["work_area_mm"] == [300.0, 250.0, 80.0]
    assert metrics["body_components"]["cnc_unmapped_fixture_body"] == (
        "desktop_cnc_assembly"
    )


@pytest.mark.asyncio
async def test_mock_spacer_and_hinge_assemblies_contract() -> None:
    client = MockMcpClient()
    spacer = await _success(
        client,
        "create_spacer_plate_assembly",
        {
            "name": "spacer_feature",
            "assembly_component": "spacer_assembly",
            "component_names": [
                "spacer_assembly",
                "spacer_top_plate_component",
                "spacer_bottom_plate_component",
                "spacer_standoff_component",
            ],
            "body_names": [
                "spacer_top_plate_body",
                "spacer_bottom_plate_body",
                "spacer_standoff_body",
                "spacer_unmapped_body",
            ],
            "occurrence_names": ["standoff_01", "standoff_02"],
            "plate_length": "100 mm",
            "plate_width": "80 mm",
            "plate_thickness": "3 mm",
            "plate_gap": "20 mm",
            "standoff_diameter": "8 mm",
            "standoff_height": "20 mm",
        },
    )
    assert len(spacer["occurrences"]) == 2
    assert spacer["physical_properties"]["spacer_top_plate_component"]["mass_kg"] > 0

    hinge = await _success(
        client,
        "create_hinge_assembly",
        {
            "name": "hinge_feature",
            "assembly_component": "hinge_assembly",
            "component_names": [
                "hinge_assembly",
                "hinge_left_leaf_component",
                "hinge_right_leaf_component",
                "hinge_pin_component",
            ],
            "body_names": [
                "hinge_left_leaf_body",
                "hinge_right_leaf_body",
                "hinge_pin_body",
                "hinge_left_knuckle_01_body",
                "hinge_left_knuckle_02_body",
                "hinge_right_knuckle_body",
                "hinge_unmapped_body",
            ],
            "leaf_length": "60 mm",
            "leaf_width": "25 mm",
            "leaf_thickness": "2 mm",
            "pin_diameter": "4 mm",
            "pin_length": "60 mm",
            "knuckle_outer_diameter": "8 mm",
            "knuckle_length": "15 mm",
        },
    )
    assert hinge["feature"]["type"] == "hinge_assembly"
    hinge_occurrences = [
        item
        for item in hinge["occurrences"].values()
        if item["parent"] == "hinge_assembly"
    ]
    assert len(hinge_occurrences) == 4
    assert hinge["interference"] == {"count": 0, "pairs": []}
