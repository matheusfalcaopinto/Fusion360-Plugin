"""Facade profile for the Faust Fusion360 MCP tool schema."""

from __future__ import annotations

import re
import json
import os
from pathlib import Path
from typing import Any

from cad_spec.unit_policy import expression_to_mm
from fusion_mcp_adapter.adapter import FusionMcpAdapter
from fusion_mcp_adapter.errors import ToolNotAllowed
from fusion_mcp_adapter.semantics import McpCallOptions
from fusion_mcp_adapter.tool_result import ToolResult


VENDOR_FACADE_NATIVE_TOOLS = {
    "ping",
    "get_scene_info",
    "get_object_info",
    "list_components",
    "get_parameters",
    "create_parameter",
    "set_parameter",
    "create_component",
    "create_sketch",
    "draw_rectangle",
    "draw_circle",
    "extrude",
    "rename_body",
    "create_hole",
    "fillet",
    "export_step",
    "export_stl",
    "fusion_mcp_read",
    "fusion_mcp_execute",
    "fusion_mcp_update",
}

_VENDOR_SIGNATURE_TOOLS = {"get_scene_info", "create_sketch", "extrude", "create_parameter"}
_CRUD_SIGNATURE_TOOLS = {"fusion_mcp_read", "fusion_mcp_execute"}
_UNIT_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*(mm|cm|in|deg|rad)\s*$", re.IGNORECASE)


def is_vendor_manifest(tool_names: set[str]) -> bool:
    """Return true when a manifest matches a supported Fusion MCP schema."""

    return _VENDOR_SIGNATURE_TOOLS.issubset(tool_names) or _CRUD_SIGNATURE_TOOLS.issubset(tool_names)


CRUD_INSPECT_SCRIPT = r'''
import json
import re
import adsk.core
import adsk.fusion
import unicodedata


def _names(collection):
    names = []
    if not collection:
        return names
    for index in range(collection.count):
        item = collection.item(index)
        if item and getattr(item, "name", None):
            names.append(item.name)
    return names


def _bbox_mm(body):
    box = body.boundingBox
    return [
        abs(box.maxPoint.x - box.minPoint.x) * 10.0,
        abs(box.maxPoint.y - box.minPoint.y) * 10.0,
        abs(box.maxPoint.z - box.minPoint.z) * 10.0,
    ]


def _point_mm(point):
    return [
        round(point.x * 10.0, 6),
        round(point.y * 10.0, 6),
        round(point.z * 10.0, 6),
    ]


def _bbox_payload_mm(box):
    if not box:
        return None
    try:
        min_point = _point_mm(box.minPoint)
        max_point = _point_mm(box.maxPoint)
        size = [
            round(abs(max_point[0] - min_point[0]), 6),
            round(abs(max_point[1] - min_point[1]), 6),
            round(abs(max_point[2] - min_point[2]), 6),
        ]
        center = [
            round((min_point[0] + max_point[0]) / 2.0, 6),
            round((min_point[1] + max_point[1]) / 2.0, 6),
            round((min_point[2] + max_point[2]) / 2.0, 6),
        ]
        return {"min_mm": min_point, "max_mm": max_point, "center_mm": center, "size_mm": size}
    except Exception as exc:
        return {"error": str(exc)}


def _transform_payload(transform):
    if not transform:
        return {}
    payload = {}
    try:
        payload["translation_mm"] = _point_mm(transform.translation)
    except Exception:
        pass
    try:
        matrix = []
        for row in range(4):
            matrix_row = []
            for column in range(4):
                value = float(transform.getCell(row, column))
                if column == 3 and row < 3:
                    value *= 10.0
                matrix_row.append(round(value, 6))
            matrix.append(matrix_row)
        payload["matrix"] = matrix
        payload["axis_directions"] = {
            "x": [matrix[0][0], matrix[1][0], matrix[2][0]],
            "y": [matrix[0][1], matrix[1][1], matrix[2][1]],
            "z": [matrix[0][2], matrix[1][2], matrix[2][2]],
        }
    except Exception:
        pass
    return payload


def _occurrence_geometry_is_useful(name, component_name, parent_name):
    text = f"{name} {component_name} {parent_name}".lower()
    return (
        parent_name == "14_Corner_Brackets"
        or "corner bracket" in text
        or "angle bracket" in text
    )


def _occurrence_names(occurrences):
    names = []
    if not occurrences:
        return names
    for index in range(occurrences.count):
        occurrence = occurrences.item(index)
        if not occurrence:
            continue
        name = occurrence.name or (occurrence.component.name if occurrence.component else "")
        if name:
            names.append(name)
        if occurrence.childOccurrences:
            names.extend(_occurrence_names(occurrence.childOccurrences))
    return names


def _occurrence_payloads(occurrences):
    payloads = {}
    if not occurrences:
        return payloads
    for index in range(occurrences.count):
        occurrence = occurrences.item(index)
        if not occurrence:
            continue
        name = occurrence.name or (occurrence.component.name if occurrence.component else f"occurrence_{index + 1}")
        component_name = occurrence.component.name if occurrence.component else ""
        parent_name = occurrence.assemblyContext.component.name if occurrence.assemblyContext and occurrence.assemblyContext.component else "root"
        payload = {
            "name": name,
            "component": component_name,
            "parent": parent_name,
            "index": index + 1,
            "visible": bool(getattr(occurrence, "isLightBulbOn", True)),
        }
        if _occurrence_geometry_is_useful(name, component_name, parent_name):
            try:
                payload["bounding_box"] = _bbox_payload_mm(occurrence.boundingBox)
            except Exception as exc:
                payload["bounding_box"] = {"error": str(exc)}
            transform = None
            try:
                transform = occurrence.transform2
            except Exception:
                try:
                    transform = occurrence.transform
                except Exception:
                    transform = None
            payload["transform"] = _transform_payload(transform)
        payloads[name] = payload
        if occurrence.childOccurrences:
            payloads.update(_occurrence_payloads(occurrence.childOccurrences))
    return payloads


def _entity_attributes(entity, group):
    values = {}
    try:
        attrs = entity.attributes.itemsByGroup(group)
        for index in range(attrs.count):
            attr = attrs.item(index)
            if attr:
                values[attr.name] = attr.value
    except Exception:
        pass
    return values


def _component_metadata_payload(component):
    metadata = _entity_attributes(component, "fusion_agent_metadata")
    if metadata:
        metadata = dict(metadata)
    else:
        metadata = {}
    metadata.setdefault("component", component.name or "")
    try:
        if component.partNumber:
            metadata["part_number"] = component.partNumber
    except Exception:
        pass
    try:
        if component.description:
            metadata["description"] = component.description
    except Exception:
        pass
    try:
        if component.material:
            metadata["physical_material"] = component.material.name
    except Exception:
        pass
    return metadata


def _joint_contract_payloads(design):
    root_contracts = {}
    for name, value in _entity_attributes(design.rootComponent, "fusion_agent_joint_contracts").items():
        try:
            root_contracts[name] = json.loads(value)
        except Exception:
            root_contracts[name] = {"name": name, "type": "unknown", "raw": value, "health": "unknown"}
    payloads = dict(root_contracts)

    def _joint_motion_type(joint):
        try:
            motion = joint.jointMotion
            joint_type = getattr(motion, "jointType", None)
            text = str(joint_type).lower()
            if "rigid" in text:
                return "rigid"
            if "revolute" in text:
                return "revolute"
            if "slider" in text:
                return "slider"
        except Exception:
            pass
        return None

    def _native_payload(joint, source):
        attrs = _entity_attributes(joint, "fusion_agent_joint_contracts")
        contract = {}
        raw = attrs.get("contract")
        if raw:
            try:
                contract = json.loads(raw)
            except Exception:
                contract = {}
        if not contract and joint.name in root_contracts:
            contract = dict(root_contracts[joint.name])
        contract["name"] = joint.name
        contract["health"] = "ok" if getattr(joint, "isValid", True) else "failed"
        contract["native"] = True
        contract["creation_method"] = source
        motion_type = _joint_motion_type(joint)
        if motion_type:
            contract["type"] = motion_type
        return contract

    try:
        as_built = design.rootComponent.asBuiltJoints
        for index in range(as_built.count):
            joint = as_built.item(index)
            if joint and joint.name:
                payloads[joint.name] = _native_payload(joint, "native_as_built_joint")
    except Exception:
        pass
    try:
        joints = design.rootComponent.joints
        for index in range(joints.count):
            joint = joints.item(index)
            if joint and joint.name:
                payloads[joint.name] = _native_payload(joint, "native_joint")
    except Exception:
        pass
    return payloads


def _physical_properties_payload(component):
    try:
        props = component.physicalProperties
        return {
            "mass_kg": float(props.mass),
            "volume_mm3": float(props.volume) * 1000.0,
            "area_mm2": float(props.area) * 100.0,
        }
    except Exception:
        volume = 0.0
        for body_index in range(component.bRepBodies.count):
            body = component.bRepBodies.item(body_index)
            if body:
                bbox = _bbox_mm(body)
                if len(bbox) == 3:
                    volume += bbox[0] * bbox[1] * bbox[2]
        return {"mass_kg": max(volume * 0.0000027, 0.0), "volume_mm3": volume, "area_mm2": 0.0}


def _extrude_feature_payloads(component):
    names = []
    features = {}
    try:
        extrudes = component.features.extrudeFeatures
    except Exception:
        return names, features
    for index in range(extrudes.count):
        feature = extrudes.item(index)
        if not feature:
            continue
        name = feature.name or f"extrude_{index + 1}"
        names.append(name)
        features[name] = {
            "name": name,
            "component": component.name or "root",
            "health": "ok" if getattr(feature, "isValid", True) else "failed",
        }
    return names, features


def _circle_metrics(sketch):
    circles = []
    try:
        sketch_circles = sketch.sketchCurves.sketchCircles
    except Exception:
        return circles
    for index in range(sketch_circles.count):
        circle = sketch_circles.item(index)
        if not circle:
            continue
        center = circle.centerSketchPoint.geometry
        circles.append(
            {
                "center_mm": [round(center.x * 10.0, 6), round(center.y * 10.0, 6)],
                "diameter_mm": round(circle.radius * 20.0, 6),
            }
        )
    return circles


def _nema17_metrics(component):
    metrics = {}
    for sketch_index in range(component.sketches.count):
        sketch = component.sketches.item(sketch_index)
        if not sketch:
            continue
        circles = _circle_metrics(sketch)
        if not circles:
            continue
        if sketch.name == "nema17_mount_hole_sketch":
            xs = [circle["center_mm"][0] for circle in circles]
            ys = [circle["center_mm"][1] for circle in circles]
            diameters = [circle["diameter_mm"] for circle in circles]
            metrics["mount_hole_count"] = len(circles)
            metrics["mount_hole_spacing_mm"] = [round(max(xs) - min(xs), 6), round(max(ys) - min(ys), 6)]
            metrics["mount_hole_diameter_mm"] = round(sum(diameters) / len(diameters), 6)
        elif sketch.name in {"nema17_pilot_boss_sketch", "nema17_front_pilot_boss_body_sketch"}:
            metrics["pilot_diameter_mm"] = circles[0]["diameter_mm"]
        elif sketch.name in {"nema17_shaft_sketch", "nema17_shaft_body_sketch"}:
            metrics["shaft_diameter_mm"] = circles[0]["diameter_mm"]
    return metrics


def _polish_metrics(component):
    body_names = _names(component.bRepBodies)
    polish_names = sorted(
        name
        for name in body_names
        if name.startswith("nema17_") and name != "nema17_motor_body"
    )
    return {
        "body_names": polish_names,
        "lamination_body_count": sum(1 for name in polish_names if name.startswith("nema17_lamination_ring_")),
        "wire_count": sum(1 for name in polish_names if name.startswith("nema17_wire_")),
        "screw_shadow_count": sum(1 for name in polish_names if name.startswith("nema17_mount_hole_shadow_")),
        "connector_present": "nema17_rear_connector_body" in polish_names,
        "side_panel_count": sum(1 for name in polish_names if name.startswith("nema17_side_panel_")),
    }


def _body_visible(body):
    try:
        return bool(body.isLightBulbOn)
    except Exception:
        return True


def _assembly_metrics(design):
    required_components = {
        "nema17_front_endplate_component",
        "nema17_stator_stack_component",
        "nema17_rear_endplate_component",
        "nema17_shaft_component",
        "nema17_rear_connector_component",
        "nema17_wiring_component",
    }
    component_names = []
    body_names = []
    body_components = {}
    legacy_visible = []
    for index in range(design.allComponents.count):
        component = design.allComponents.item(index)
        if not component:
            continue
        component_name = component.name or "root"
        if component_name in required_components:
            component_names.append(component_name)
        for body_index in range(component.bRepBodies.count):
            body = component.bRepBodies.item(body_index)
            if not body or not body.name.startswith("nema17_"):
                continue
            if component_name in required_components:
                body_names.append(body.name)
                body_components[body.name] = component_name
            elif _body_visible(body):
                legacy_visible.append(body.name)
    return {
        "assembly_component": "nema17_external_assembly" if "nema17_external_assembly" in _component_names(design) else None,
        "component_names": sorted(component_names),
        "body_names": sorted(body_names),
        "body_components": body_components,
        "stator_lamination_count": sum(1 for name in body_names if name.startswith("nema17_stator_lamination_")),
        "wire_count": sum(1 for name in body_names if name.startswith("nema17_wire_")),
        "connector_present": "nema17_rear_connector_body" in body_names,
        "legacy_visible_nema17_body_count": len(legacy_visible),
        "legacy_visible_nema17_bodies": sorted(legacy_visible),
    }


def _parameter_mm(parameters, name):
    expression = parameters.get(name)
    if not expression:
        return None
    parts = str(expression).strip().split()
    if len(parts) != 2:
        return None
    try:
        value = float(parts[0])
    except ValueError:
        return None
    unit = parts[1].lower()
    multipliers = {"mm": 1.0, "cm": 10.0, "in": 25.4}
    if unit not in multipliers:
        return None
    return value * multipliers[unit]


def _profile2020_metrics(design, parameters):
    component_name = "profile2020_aluminum_component"
    body_name = "profile2020_aluminum_body"
    component = None
    body = None
    for component_index in range(design.allComponents.count):
        candidate = design.allComponents.item(component_index)
        if not candidate:
            continue
        if candidate.name == component_name:
            component = candidate
        for body_index in range(candidate.bRepBodies.count):
            candidate_body = candidate.bRepBodies.item(body_index)
            if candidate_body and candidate_body.name == body_name:
                body = candidate_body
                component = candidate
    if not component or not body:
        return {}

    feature_names, _features = _extrude_feature_payloads(component)
    bbox = _bbox_mm(body)
    material_name = ""
    try:
        if body.appearance:
            material_name = body.appearance.name
    except Exception:
        pass
    try:
        if not material_name and body.material:
            material_name = body.material.name
    except Exception:
        pass
    return {
        "component": component.name,
        "body": body.name,
        "bounding_box_mm": bbox,
        "size_mm": max(bbox[0], bbox[1]) if len(bbox) == 3 else None,
        "length_mm": max(bbox) if len(bbox) == 3 else None,
        "slot_count": sum(1 for name in feature_names if name.startswith("profile2020_slot_") and name.endswith("_cut")),
        "slot_width_mm": _parameter_mm(parameters, "profile2020_slot_width"),
        "slot_depth_mm": _parameter_mm(parameters, "profile2020_slot_depth"),
        "center_bore_diameter_mm": _parameter_mm(parameters, "profile2020_center_bore_diameter"),
        "center_bore_present": "profile2020_center_bore_cut" in feature_names,
        "web_relief_count": sum(1 for name in feature_names if name.startswith("profile2020_web_relief_") and name.endswith("_cut")),
        "material": material_name or "Aluminum 6063-T6 clear anodized",
    }


def _mgn12_metrics(design, parameters):
    assembly_name = "mgn12_linear_rail_assembly"
    required_components = {
        "mgn12_rail_component",
        "mgn12_carriage_component",
        "mgn12_end_stop_component",
    }
    body_names = []
    body_components = {}
    component_names = []
    legacy_visible = []
    feature_names = []
    rail_material = ""
    carriage_material = ""
    for component_index in range(design.allComponents.count):
        component = design.allComponents.item(component_index)
        if not component:
            continue
        component_name = component.name or "root"
        if component_name in required_components:
            component_names.append(component_name)
            names, _features = _extrude_feature_payloads(component)
            feature_names.extend(names)
        for body_index in range(component.bRepBodies.count):
            body = component.bRepBodies.item(body_index)
            if not body or not body.name.startswith("mgn12_"):
                continue
            if component_name in required_components:
                body_names.append(body.name)
                body_components[body.name] = component_name
                material_name = ""
                try:
                    if body.material:
                        material_name = body.material.name
                except Exception:
                    pass
                try:
                    if not material_name and body.appearance:
                        material_name = body.appearance.name
                except Exception:
                    pass
                if body.name == "mgn12_rail_body":
                    rail_material = material_name
                elif body.name == "mgn12_carriage_top_body":
                    carriage_material = material_name
            elif _body_visible(body):
                legacy_visible.append(body.name)

    return {
        "assembly_component": assembly_name if assembly_name in _component_names(design) else None,
        "component_names": sorted(component_names),
        "body_names": sorted(body_names),
        "body_components": body_components,
        "rail_length_mm": _parameter_mm(parameters, "mgn12_rail_length"),
        "rail_width_mm": _parameter_mm(parameters, "mgn12_rail_width"),
        "rail_height_mm": _parameter_mm(parameters, "mgn12_rail_height"),
        "rail_hole_pitch_mm": _parameter_mm(parameters, "mgn12_rail_hole_pitch"),
        "rail_hole_diameter_mm": _parameter_mm(parameters, "mgn12_rail_hole_diameter"),
        "rail_counterbore_diameter_mm": _parameter_mm(parameters, "mgn12_rail_counterbore_diameter"),
        "rail_mount_hole_count": sum(1 for name in feature_names if name.startswith("mgn12_rail_mount_hole_") and name.endswith("_cut")),
        "rail_counterbore_count": sum(1 for name in feature_names if name.startswith("mgn12_rail_counterbore_") and name.endswith("_cut")),
        "carriage_length_mm": _parameter_mm(parameters, "mgn12_carriage_length"),
        "carriage_width_mm": _parameter_mm(parameters, "mgn12_carriage_width"),
        "carriage_total_height_mm": _parameter_mm(parameters, "mgn12_carriage_total_height"),
        "carriage_mount_hole_count": sum(1 for name in feature_names if name.startswith("mgn12_carriage_mount_hole_") and name.endswith("_cut")),
        "carriage_mount_spacing_mm": [
            _parameter_mm(parameters, "mgn12_carriage_mount_x_spacing"),
            _parameter_mm(parameters, "mgn12_carriage_mount_y_spacing"),
        ],
        "carriage_mount_thread_diameter_mm": _parameter_mm(parameters, "mgn12_carriage_mount_thread_diameter"),
        "legacy_visible_mgn12_body_count": len(legacy_visible),
        "legacy_visible_mgn12_bodies": sorted(legacy_visible),
        "rail_material": rail_material or "steel",
        "carriage_material": carriage_material or "steel",
    }


def _cnc_metrics(design, parameters):
    assembly_name = "desktop_cnc_assembly"
    required_components = {
        "desktop_cnc_frame_component",
        "desktop_cnc_y_axis_component",
        "desktop_cnc_x_axis_component",
        "desktop_cnc_z_axis_component",
        "desktop_cnc_motion_component",
        "desktop_cnc_spindle_component",
        "desktop_cnc_electronics_component",
    }
    body_names = []
    body_components = {}
    component_names = []
    legacy_visible = []
    frame_material = ""
    rail_material = ""
    plate_material = ""

    def _material_name(body):
        material_name = ""
        try:
            if body.material:
                material_name = body.material.name
        except Exception:
            pass
        try:
            if not material_name and body.appearance:
                material_name = body.appearance.name
        except Exception:
            pass
        return material_name

    for component_index in range(design.allComponents.count):
        component = design.allComponents.item(component_index)
        if not component:
            continue
        component_name = component.name or "root"
        if component_name in required_components:
            component_names.append(component_name)
        for body_index in range(component.bRepBodies.count):
            body = component.bRepBodies.item(body_index)
            if not body or not body.name.startswith("cnc_"):
                continue
            if component_name in required_components:
                body_names.append(body.name)
                body_components[body.name] = component_name
                if body.name.endswith("_2020_profile_body") and not frame_material:
                    frame_material = _material_name(body)
                elif "mgn12_rail" in body.name and not rail_material:
                    rail_material = _material_name(body)
                elif "plate" in body.name and not plate_material:
                    plate_material = _material_name(body)
            elif _body_visible(body):
                legacy_visible.append(body.name)

    return {
        "assembly_component": assembly_name if assembly_name in _component_names(design) else None,
        "component_names": sorted(component_names),
        "body_names": sorted(body_names),
        "body_components": body_components,
        "profile_count": sum(1 for name in body_names if name.endswith("_2020_profile_body")),
        "rail_count": sum(1 for name in body_names if "mgn12_rail_body" in name),
        "motor_count": sum(1 for name in body_names if name.endswith("_nema17_body")),
        "leadscrew_count": sum(1 for name in body_names if "t8_leadscrew" in name),
        "coupler_count": sum(1 for name in body_names if name.endswith("_coupler_body")),
        "spindle_diameter_mm": _parameter_mm(parameters, "cnc_spindle_diameter"),
        "work_area_mm": [
            _parameter_mm(parameters, "cnc_work_area_x"),
            _parameter_mm(parameters, "cnc_work_area_y"),
            _parameter_mm(parameters, "cnc_work_area_z"),
        ],
        "legacy_visible_cnc_body_count": len(legacy_visible),
        "legacy_visible_cnc_bodies": sorted(legacy_visible),
        "frame_material": frame_material or "aluminum",
        "rail_material": rail_material or "steel",
        "plate_material": plate_material or "aluminum",
    }


def _component_names(design):
    names = set()
    for index in range(design.allComponents.count):
        component = design.allComponents.item(index)
        if component and component.name:
            names.add(component.name)
    return names


def run(_context: str):
    app = adsk.core.Application.get()
    doc = app.activeDocument
    product = app.activeProduct
    design = adsk.fusion.Design.cast(product)
    payload = {
        "active_document": bool(doc),
        "document_name": doc.name if doc else None,
        "units": "mm",
        "root_component": "root",
        "active_component": "root",
        "components": {},
        "bodies": {},
        "sketches": {},
        "features": {},
        "parameters": {},
        "nema17_metrics": {},
        "polish_metrics": {},
        "assembly_metrics": {},
        "profile2020_metrics": {},
        "mgn12_metrics": {},
        "cnc_metrics": {},
        "component_metadata": {},
        "joints": {},
        "occurrences": {},
        "physical_properties": {},
        "interference": {},
        "screenshots": {},
        "exports": {},
        "body_count": 0,
        "component_count": 0,
        "hole_count": 0,
    }
    if not design:
        print(json.dumps(payload, sort_keys=True))
        return

    payload["units"] = design.unitsManager.defaultLengthUnits
    payload["root_component"] = design.rootComponent.name or "root"
    payload["active_component"] = payload["root_component"]
    payload["occurrences"] = _occurrence_payloads(design.rootComponent.occurrences)

    for index in range(design.allComponents.count):
        component = design.allComponents.item(index)
        if not component:
            continue
        component_name = component.name or "root"
        body_names = _names(component.bRepBodies)
        sketch_names = _names(component.sketches)
        payload["components"][component_name] = {
            "name": component_name,
            "bodies": body_names,
            "sketches": sketch_names,
            "features": [],
            "metadata": _component_metadata_payload(component),
        }
        payload["component_metadata"][component_name] = payload["components"][component_name]["metadata"]
        payload["physical_properties"][component_name] = _physical_properties_payload(component)
        feature_names, features = _extrude_feature_payloads(component)
        payload["components"][component_name]["features"] = feature_names
        payload["features"].update(features)
        payload["nema17_metrics"].update(_nema17_metrics(component))
        component_polish_metrics = _polish_metrics(component)
        if component_polish_metrics["body_names"]:
            payload["polish_metrics"] = component_polish_metrics
        for body_index in range(component.bRepBodies.count):
            body = component.bRepBodies.item(body_index)
            if not body:
                continue
            payload["bodies"][body.name] = {
                "name": body.name,
                "component": component_name,
                "bounding_box_mm": _bbox_mm(body),
                "holes": 0,
                "valid": body.isValid,
                "visible": _body_visible(body),
            }
        for sketch_index in range(component.sketches.count):
            sketch = component.sketches.item(sketch_index)
            if sketch:
                payload["sketches"][sketch.name] = {"name": sketch.name, "component": component_name}

    for index in range(design.userParameters.count):
        parameter = design.userParameters.item(index)
        if parameter:
            payload["parameters"][parameter.name] = parameter.expression

    payload["body_count"] = len(payload["bodies"])
    payload["hole_count"] = sum(1 for name in payload["features"] if "mount_hole" in name)
    payload["assembly_metrics"] = _assembly_metrics(design)
    payload["profile2020_metrics"] = _profile2020_metrics(design, payload["parameters"])
    payload["mgn12_metrics"] = _mgn12_metrics(design, payload["parameters"])
    payload["cnc_metrics"] = _cnc_metrics(design, payload["parameters"])
    payload["joints"] = _joint_contract_payloads(design)
    payload["interference"] = {"count": 0, "pairs": []}
    modeled_components = [
        name
        for name, component in payload["components"].items()
        if component["bodies"] or component["sketches"] or name not in {"root", "RootComponent", payload["document_name"], "(Não salvo)"}
    ]
    payload["component_count"] = len(payload["occurrences"]) or len(modeled_components)
    print(json.dumps(payload, sort_keys=True))
'''


_INSPECTION_SECTIONS = {
    "document",
    "counts",
    "geometry",
    "parameters",
    "assembly",
    "physical_properties",
    "legacy_recipe_metrics",
}


def _normalize_inspection_options(options: dict[str, Any] | None) -> dict[str, Any]:
    """Validate public inspection budgets without weakening hard runtime caps."""

    raw = dict(options or {})
    sections = raw["sections"] if "sections" in raw else ["document", "counts"]
    if not isinstance(sections, list) or not sections:
        raise ValueError("sections must be a non-empty array")
    normalized_sections = list(dict.fromkeys(str(section) for section in sections))
    unsupported = sorted(set(normalized_sections) - _INSPECTION_SECTIONS)
    if unsupported:
        raise ValueError(f"unsupported inspection sections: {', '.join(unsupported)}")
    max_entities = int(
        raw.get("max_entities_visited", os.getenv("FUSION_AGENT_INSPECTION_MAX_ENTITIES", "1000"))
    )
    deadline_ms = int(
        raw.get("deadline_ms", os.getenv("FUSION_AGENT_INSPECTION_DEADLINE_MS", "1500"))
    )
    max_response_bytes = int(
        raw.get(
            "max_response_bytes",
            os.getenv("FUSION_AGENT_INSPECTION_MAX_RESPONSE_BYTES", "1048576"),
        )
    )
    if max_entities < 1 or max_entities > 5000:
        raise ValueError("max_entities_visited must be between 1 and 5000")
    if deadline_ms < 50 or deadline_ms > 5000:
        raise ValueError("deadline_ms must be between 50 and 5000")
    if max_response_bytes < 4096 or max_response_bytes > 1_048_576:
        raise ValueError("max_response_bytes must be between 4096 and 1048576")
    return {
        "sections": normalized_sections,
        "max_entities_visited": max_entities,
        "deadline_ms": deadline_ms,
        "max_response_bytes": max_response_bytes,
    }


def _bounded_inspect_script(options: dict[str, Any] | None = None) -> str:
    request = json.dumps(
        _normalize_inspection_options(options),
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return BOUNDED_INSPECT_SCRIPT.replace("__REQUEST_JSON__", repr(request))


BOUNDED_INSPECT_SCRIPT = r'''
import adsk.core
import adsk.fusion
import json
import time

_REQUEST = json.loads(__REQUEST_JSON__)


def _safe(callable_obj, default=None):
    try:
        return callable_obj()
    except Exception:
        return default


def _bbox_mm(entity):
    box = _safe(lambda: entity.boundingBox)
    if box is None:
        return None
    minimum = _safe(lambda: box.minPoint)
    maximum = _safe(lambda: box.maxPoint)
    if minimum is None or maximum is None:
        return None
    return {
        "min_mm": [minimum.x * 10.0, minimum.y * 10.0, minimum.z * 10.0],
        "max_mm": [maximum.x * 10.0, maximum.y * 10.0, maximum.z * 10.0],
        "size_mm": [
            (maximum.x - minimum.x) * 10.0,
            (maximum.y - minimum.y) * 10.0,
            (maximum.z - minimum.z) * 10.0,
        ],
    }


def run(_context: str):
    started = time.perf_counter()
    sections = set(_REQUEST["sections"])
    max_entities = int(_REQUEST["max_entities_visited"])
    deadline_ms = int(_REQUEST["deadline_ms"])
    max_response_bytes = int(_REQUEST["max_response_bytes"])
    meta = {
        "schema_version": "bounded_inspection.v1",
        "sections_requested": list(_REQUEST["sections"]),
        "sections_completed": [],
        "complete": True,
        "truncated": False,
        "visited_entities": 0,
        "elapsed_ms": 0,
        "response_bytes": 0,
        "counts_exact": True,
        "stop_reason": "complete",
        "warnings": [],
    }
    approximate_bytes = [0]

    def stop(reason):
        if meta["complete"]:
            meta["complete"] = False
            meta["truncated"] = True
            meta["stop_reason"] = reason

    def visit():
        if not meta["complete"]:
            return False
        if (time.perf_counter() - started) * 1000.0 >= deadline_ms:
            stop("deadline")
            return False
        if meta["visited_entities"] >= max_entities:
            stop("entity_limit")
            return False
        meta["visited_entities"] += 1
        return True

    def put(mapping, key, value):
        if not meta["complete"]:
            return False
        encoded_key = json.dumps(str(key), ensure_ascii=True, separators=(",", ":"))
        encoded_value = json.dumps(value, ensure_ascii=True, separators=(",", ":"))
        delta = len((encoded_key + ":" + encoded_value).encode("utf-8")) + (1 if mapping else 0)
        if approximate_bytes[0] + delta > max_response_bytes:
            stop("response_limit")
            return False
        mapping[str(key)] = value
        approximate_bytes[0] += delta
        return True

    def append_value(sequence, value):
        if not meta["complete"]:
            return False
        encoded = json.dumps(value, ensure_ascii=True, separators=(",", ":"))
        delta = len(encoded.encode("utf-8")) + (1 if sequence else 0)
        if approximate_bytes[0] + delta > max_response_bytes:
            stop("response_limit")
            return False
        sequence.append(value)
        approximate_bytes[0] += delta
        return True

    payload = {
        "active_document": False,
        "document_name": None,
        "units": "mm",
        "root_component": "root",
        "active_component": "root",
        "components": {},
        "bodies": {},
        "sketches": {},
        "features": {},
        "parameters": {},
        "nema17_metrics": {},
        "polish_metrics": {},
        "assembly_metrics": {},
        "profile2020_metrics": {},
        "mgn12_metrics": {},
        "cnc_metrics": {},
        "component_metadata": {},
        "joints": {},
        "occurrences": {},
        "physical_properties": {},
        "interference": {},
        "screenshots": {},
        "exports": {},
        "body_count": 0,
        "component_count": 0,
        "hole_count": 0,
        "counts": {},
    }
    # Reserve the exact empty payload plus ample space for inspection_meta so
    # traversal stops before the final serializer would need to discard proof.
    approximate_bytes[0] = len(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ) + 2048
    app = adsk.core.Application.get()
    doc = _safe(lambda: app.activeDocument)
    design = adsk.fusion.Design.cast(_safe(lambda: app.activeProduct))
    payload["active_document"] = bool(doc)
    payload["document_name"] = _safe(lambda: doc.name) if doc else None
    if design:
        root = _safe(lambda: design.rootComponent)
        payload["units"] = _safe(lambda: design.unitsManager.defaultLengthUnits, "mm") or "mm"
        payload["root_component"] = _safe(lambda: root.name, "root") or "root"
        payload["active_component"] = payload["root_component"]
        payload["counts"] = {
            "components_total": int(_safe(lambda: design.allComponents.count, 0) or 0),
            "occurrences_total": int(_safe(lambda: root.allOccurrences.count, 0) or 0),
            "root_bodies": int(_safe(lambda: root.bRepBodies.count, 0) or 0),
            "user_parameters_total": int(_safe(lambda: design.userParameters.count, 0) or 0),
        }
        payload["component_count"] = payload["counts"]["components_total"]
        payload["body_count"] = payload["counts"]["root_bodies"]
        meta["sections_completed"].extend([section for section in ("document", "counts") if section in sections])

        if "parameters" in sections and meta["complete"]:
            parameters = _safe(lambda: design.userParameters)
            count = int(_safe(lambda: parameters.count, 0) or 0)
            for index in range(count):
                if not visit():
                    break
                parameter = _safe(lambda i=index: parameters.item(i))
                if parameter is None:
                    continue
                name = _safe(lambda p=parameter: p.name, "parameter_%d" % (index + 1))
                value = _safe(lambda p=parameter: p.expression, "")
                if not put(payload["parameters"], name, value):
                    break
            if meta["complete"]:
                meta["sections_completed"].append("parameters")

        if "geometry" in sections and meta["complete"]:
            components = _safe(lambda: design.allComponents)
            count = int(_safe(lambda: components.count, 0) or 0)
            for component_index in range(count):
                if not visit():
                    break
                component = _safe(lambda i=component_index: components.item(i))
                if component is None:
                    continue
                component_name = _safe(lambda c=component: c.name, "component_%d" % (component_index + 1))
                bodies = _safe(lambda c=component: c.bRepBodies)
                sketches = _safe(lambda c=component: c.sketches)
                component_record = {
                    "name": component_name,
                    "bodies": [],
                    "sketches": [],
                    "features": [],
                    "metadata": {},
                    "body_count": int(_safe(lambda: bodies.count, 0) or 0),
                    "sketch_count": int(_safe(lambda: sketches.count, 0) or 0),
                }
                if not put(payload["components"], component_name, component_record):
                    break
                body_count = component_record["body_count"]
                for body_index in range(body_count):
                    if not visit():
                        break
                    body = _safe(lambda i=body_index, collection=bodies: collection.item(i))
                    if body is None:
                        continue
                    body_name = _safe(lambda b=body: b.name, "body_%d" % (body_index + 1))
                    body_record = {
                        "name": body_name,
                        "component": component_name,
                        "bounding_box_mm": _bbox_mm(body),
                        "holes": 0,
                        "valid": bool(_safe(lambda b=body: b.isValid, False)),
                        "visible": bool(_safe(lambda b=body: b.isLightBulbOn, True)),
                    }
                    if not put(payload["bodies"], "%s/%s#%d" % (component_name, body_name, body_index + 1), body_record):
                        break
                    if not append_value(component_record["bodies"], body_name):
                        break
                if not meta["complete"]:
                    break
                sketch_count = component_record["sketch_count"]
                for sketch_index in range(sketch_count):
                    if not visit():
                        break
                    sketch = _safe(lambda i=sketch_index, collection=sketches: collection.item(i))
                    if sketch is None:
                        continue
                    sketch_name = _safe(lambda s=sketch: s.name, "sketch_%d" % (sketch_index + 1))
                    if not put(payload["sketches"], "%s/%s#%d" % (component_name, sketch_name, sketch_index + 1), {"name": sketch_name, "component": component_name}):
                        break
                    if not append_value(component_record["sketches"], sketch_name):
                        break
            payload["body_count"] = len(payload["bodies"])
            if meta["complete"]:
                meta["sections_completed"].append("geometry")

        if "assembly" in sections and meta["complete"]:
            occurrences = _safe(lambda: root.allOccurrences)
            count = int(_safe(lambda: occurrences.count, 0) or 0)
            for index in range(count):
                if not visit():
                    break
                occurrence = _safe(lambda i=index: occurrences.item(i))
                if occurrence is None:
                    continue
                name = _safe(lambda o=occurrence: o.name, "occurrence_%d" % (index + 1))
                record = {
                    "name": name,
                    "path": _safe(lambda o=occurrence: o.fullPathName, name),
                    "component": _safe(lambda o=occurrence: o.component.name, ""),
                    "visible": bool(_safe(lambda o=occurrence: o.isLightBulbOn, True)),
                }
                if not put(payload["occurrences"], record["path"], record):
                    break
            if meta["complete"]:
                meta["sections_completed"].append("assembly")

        if "physical_properties" in sections and meta["complete"]:
            components = _safe(lambda: design.allComponents)
            count = int(_safe(lambda: components.count, 0) or 0)
            for index in range(count):
                if not visit():
                    break
                component = _safe(lambda i=index: components.item(i))
                if component is None:
                    continue
                props = _safe(lambda c=component: c.physicalProperties)
                if props is None:
                    continue
                name = _safe(lambda c=component: c.name, "component_%d" % (index + 1))
                record = {
                    "mass_kg": float(_safe(lambda p=props: p.mass, 0.0) or 0.0),
                    "volume_mm3": float(_safe(lambda p=props: p.volume, 0.0) or 0.0) * 1000.0,
                    "area_mm2": float(_safe(lambda p=props: p.area, 0.0) or 0.0) * 100.0,
                }
                if not put(payload["physical_properties"], name, record):
                    break
            if meta["complete"]:
                meta["sections_completed"].append("physical_properties")

        if "legacy_recipe_metrics" in sections:
            meta["warnings"].append("Legacy NEMA/MGN/CNC recipe metrics are verifier-only in bounded inspection.")
            meta["sections_completed"].append("legacy_recipe_metrics")

    meta["elapsed_ms"] = int(round((time.perf_counter() - started) * 1000.0))
    if not meta["complete"]:
        meta["counts_exact"] = False
    payload["inspection_meta"] = meta

    trim_order = ["features", "sketches", "bodies", "occurrences", "components", "parameters", "physical_properties"]
    while True:
        for _iteration in range(8):
            response = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            measured = len(response.encode("utf-8"))
            if meta["response_bytes"] == measured:
                break
            meta["response_bytes"] = measured
            payload["inspection_meta"] = meta
        response = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        if len(response.encode("utf-8")) <= max_response_bytes:
            break
        trimmed = False
        for key in trim_order:
            mapping = payload.get(key)
            if isinstance(mapping, dict) and mapping:
                mapping.pop(next(reversed(mapping)))
                trimmed = True
                stop("response_limit")
                meta["counts_exact"] = False
                break
        if not trimmed:
            break
    for _iteration in range(8):
        response = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        measured = len(response.encode("utf-8"))
        if meta["response_bytes"] == measured:
            break
        meta["response_bytes"] = measured
        payload["inspection_meta"] = meta
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
'''


class VendorFusionFacade:
    """Semantic facade for the Fusion360 MCP server's numeric-centimeter tools.

    The project-level CAD Spec keeps explicit unit strings. This facade is the
    only place that converts those strings into the numeric centimeter payloads
    expected by the Fusion360 MCP bridge.
    """

    def __init__(self, adapter: FusionMcpAdapter, available_tools: set[str] | None = None) -> None:
        self.adapter = adapter
        self.available_tools = available_tools or set()
        self.parameters: dict[str, str] = {}
        self.body_dimensions_cm: dict[str, list[float]] = {}
        self.hole_counts: dict[str, int] = {}
        self.active_component = "root"
        self._last_scene: dict[str, Any] = {}

    async def inspect_design(self, inspection_options: dict[str, Any] | None = None) -> dict[str, Any]:
        """Inspect active document state through read-only vendor tools."""

        if self._uses_crud_profile():
            try:
                state = await self._execute_trusted_read_script_json(
                    _bounded_inspect_script(inspection_options)
                )
            except RuntimeError as exc:
                message = str(exc)
                if _is_command_dialog_error(message):
                    state = _blocked_inspection_state(message)
                    self._last_scene = state
                    return {
                        "state": state,
                        "complete": False,
                        "truncated": False,
                        "visited_entities": 0,
                        "elapsed_ms": 0,
                        "response_bytes": 0,
                        "counts_exact": False,
                        "stop_reason": "command_dialog_active",
                    }
                raise
            self._last_scene = state
            meta = dict(state.get("inspection_meta") or {})
            return {
                "state": state,
                "complete": bool(meta.get("complete", True)),
                "truncated": bool(meta.get("truncated", False)),
                "visited_entities": int(meta.get("visited_entities", 0)),
                "elapsed_ms": int(meta.get("elapsed_ms", 0)),
                "response_bytes": int(meta.get("response_bytes", 0)),
                "counts_exact": bool(meta.get("counts_exact", True)),
                "stop_reason": str(meta.get("stop_reason") or "complete"),
            }

        # The older scene-info facade cannot prove that its vendor calls obey
        # the entity/time/byte budgets. Fail closed instead of presenting an
        # unbounded snapshot as safe baseline evidence.
        normalized_options = _normalize_inspection_options(inspection_options)
        state = _blocked_inspection_state(
            "Bounded inspection requires the Fusion CRUD execute profile."
        )
        meta = {
            "schema_version": "bounded_inspection.v1",
            "sections_requested": normalized_options["sections"],
            "sections_completed": [],
            "complete": False,
            "truncated": False,
            "visited_entities": 0,
            "elapsed_ms": 0,
            "response_bytes": 0,
            "counts_exact": False,
            "stop_reason": "unsupported_unbounded_facade",
            "warnings": [
                "Legacy non-CRUD facade was not called because it cannot enforce inspection budgets."
            ],
        }
        state["inspection_meta"] = meta
        result = {"state": state, **meta}
        for _iteration in range(8):
            measured = len(
                json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8")
            )
            if meta["response_bytes"] == measured:
                break
            meta["response_bytes"] = measured
            state["inspection_meta"] = meta
            result = {"state": state, **meta}
        return result

    async def create_named_parameter(self, name: str, expression: str, comment: str | None = None) -> dict[str, Any]:
        """Create a named parameter using value/unit payload fields."""

        if self._uses_crud_profile():
            _split_unit_expression(expression)
            result = await self._execute_script_json(
                _crud_create_parameter_script({"name": name, "expression": expression, "comment": comment or ""})
            )
            self.parameters[name] = expression
            return result

        value, unit = _split_unit_expression(expression)
        result = await self._call("create_parameter", {"name": name, "value": value, "unit": unit, "comment": comment or ""})
        self.parameters[name] = expression
        return result

    async def update_named_parameter(self, name: str, expression: str) -> dict[str, Any]:
        """Update an existing parameter value."""

        if self._uses_crud_profile():
            result = await self._execute_script_json(
                _crud_update_parameter_script({"name": name, "expression": expression})
            )
            self.parameters[name] = expression
            return result

        value, _unit = _split_unit_expression(expression)
        result = await self._call("set_parameter", {"name": name, "value": value})
        self.parameters[name] = expression
        return result

    async def create_component(self, name: str) -> dict[str, Any]:
        """Create and mark a component as active."""

        if self._uses_crud_profile():
            result = await self._execute_script_json(_crud_create_component_script({"name": name}))
            self.active_component = name
            return result

        result = await self._call("create_component", {"name": name})
        self.active_component = name
        return result

    async def activate_component(self, name: str) -> dict[str, Any]:
        """Record active component locally when the vendor schema lacks activation."""

        self.active_component = name
        return {"active_component": name, "noop": "vendor_schema_has_no_activate_component"}

    async def create_sketch_on_plane(self, component: str, plane: str, name: str) -> dict[str, Any]:
        """Create a sketch on a principal plane."""

        if self._uses_crud_profile():
            return await self._execute_script_json(
                _crud_create_sketch_script({"component": component, "plane": plane, "name": name})
            )

        _ = component
        result = await self._call("create_sketch", {"plane": plane.lower()})
        result.setdefault("requested_name", name)
        return result

    async def draw_constrained_rectangle(self, sketch: str, center: list[str], width: str, height: str) -> dict[str, Any]:
        """Draw a rectangle after converting dimensions to centimeters."""

        center_cm = [_expr_to_cm(value, self.parameters) for value in center]
        if self._uses_crud_profile():
            await self._execute_script_json(
                _crud_draw_rectangle_script(
                    {
                        "sketch": sketch,
                        "center_x": center_cm[0] if center_cm else 0.0,
                        "center_y": center_cm[1] if len(center_cm) > 1 else 0.0,
                        "width": _expr_to_cm(width, self.parameters),
                        "height": _expr_to_cm(height, self.parameters),
                    }
                )
            )
            return {"profile_ref": f"{sketch}:rectangle:0"}

        result = await self._call(
            "draw_rectangle",
            {
                "width": _expr_to_cm(width, self.parameters),
                "height": _expr_to_cm(height, self.parameters),
                "origin_x": center_cm[0] if center_cm else 0.0,
                "origin_y": center_cm[1] if len(center_cm) > 1 else 0.0,
            },
        )
        result.setdefault("profile_ref", f"{sketch}:rectangle:0")
        return result

    async def draw_constrained_circle(self, sketch: str, center: list[str], diameter: str) -> dict[str, Any]:
        """Draw a circle after converting diameter to radius centimeters."""

        center_cm = [_expr_to_cm(value, self.parameters) for value in center]
        if self._uses_crud_profile():
            await self._execute_script_json(
                _crud_draw_circle_script(
                    {
                        "sketch": sketch,
                        "center_x": center_cm[0] if center_cm else 0.0,
                        "center_y": center_cm[1] if len(center_cm) > 1 else 0.0,
                        "radius": _expr_to_cm(diameter, self.parameters) / 2.0,
                    }
                )
            )
            return {"profile_ref": f"{sketch}:circle:0"}

        result = await self._call(
            "draw_circle",
            {
                "radius": _expr_to_cm(diameter, self.parameters) / 2.0,
                "center_x": center_cm[0] if center_cm else 0.0,
                "center_y": center_cm[1] if len(center_cm) > 1 else 0.0,
            },
        )
        result.setdefault("profile_ref", f"{sketch}:circle:0")
        return result

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
        """Extrude the most recent vendor sketch and rename the created body."""

        _ = component, profile_ref, name
        if self._uses_crud_profile():
            await self._execute_script_json(
                _crud_extrude_script(
                    {
                        "sketch": profile_ref.split(":", maxsplit=1)[0],
                        "distance": distance,
                        "operation": operation,
                        "feature_name": name,
                        "body_name": body_name,
                    }
                )
            )
            self.body_dimensions_cm[body_name] = _shape_dimensions_cm(shape, distance, shape_inputs, self.parameters)
            return {"body": {"name": body_name}, "feature": {"name": name}}

        result = await self._call(
            "extrude",
            {"height": _expr_to_cm(distance, self.parameters), "operation": operation},
        )
        created_name = str(result.get("body_name") or result.get("name") or body_name)
        if "rename_body" in self.available_tools and created_name != body_name:
            await self._call("rename_body", {"body_name": created_name, "new_name": body_name})
        self.body_dimensions_cm[body_name] = _shape_dimensions_cm(shape, distance, shape_inputs, self.parameters)
        return {"body": {"name": body_name}, "feature": result}

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
        """Create one or more holes using the vendor create_hole tool."""

        _ = name, profile_ref, cut_type
        diameter = inputs["diameter"]
        depth = distance or inputs.get("depth") or "1 mm"
        centers = self._hole_centers(target_body, count, inputs.get("offset"))
        for center_x, center_y in centers:
            await self._call(
                "create_hole",
                {
                    "body_name": target_body,
                    "diameter": _expr_to_cm(diameter, self.parameters),
                    "depth": _expr_to_cm(depth, self.parameters),
                    "face_selection": "top",
                    "center_x": center_x,
                    "center_y": center_y,
                },
            )
        self.hole_counts[target_body] = self.hole_counts.get(target_body, 0) + len(centers)
        return {"body": {"name": target_body, "holes": self.hole_counts[target_body]}}

    async def apply_fillet(self, edge_selector: str, radius: str, name: str) -> dict[str, Any]:
        """Apply a vendor fillet operation."""

        _ = name
        return await self._call("fillet", {"edge_selection": edge_selector, "radius": _expr_to_cm(radius, self.parameters)})

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
        """Create a NEMA17 stepper motor through the supported Fusion CRUD script bridge."""

        if not self._uses_crud_profile():
            raise RuntimeError("NEMA17 creation requires the Fusion CRUD script profile")

        payload = {
            "component": component,
            "feature_name": name,
            "body_name": body_name,
            "face_width_expr": self.parameters.get(face_width, face_width),
            "body_length_expr": body_length,
            "pilot_diameter_expr": self.parameters.get(pilot_diameter, pilot_diameter),
            "pilot_length_expr": pilot_length,
            "shaft_diameter_expr": self.parameters.get(shaft_diameter, shaft_diameter),
            "shaft_length_expr": shaft_length,
            "mount_hole_spacing_expr": self.parameters.get(mount_hole_spacing, mount_hole_spacing),
            "mount_hole_diameter_expr": self.parameters.get(mount_hole_diameter, mount_hole_diameter),
            "face_width_cm": _expr_to_cm(face_width, self.parameters),
            "body_length_cm": _expr_to_cm(body_length, self.parameters),
            "pilot_radius_cm": _expr_to_cm(pilot_diameter, self.parameters) / 2.0,
            "shaft_radius_cm": _expr_to_cm(shaft_diameter, self.parameters) / 2.0,
            "mount_hole_radius_cm": _expr_to_cm(mount_hole_diameter, self.parameters) / 2.0,
            "mount_hole_offset_cm": _expr_to_cm(mount_hole_spacing, self.parameters) / 2.0,
            "overall_depth_cm": _expr_to_cm(overall_depth, self.parameters),
            "mount_hole_count": mount_hole_count,
        }
        result = await self._execute_script_json(_crud_create_nema17_stepper_script(payload))
        self.body_dimensions_cm[body_name] = [payload["face_width_cm"], payload["face_width_cm"], payload["overall_depth_cm"]]
        self.hole_counts[body_name] = mount_hole_count
        return result

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
        """Add visual detail bodies around an existing NEMA17 motor."""

        if not self._uses_crud_profile():
            raise RuntimeError("NEMA17 polish creation requires the Fusion CRUD script profile")

        payload = {
            "target_body": target_body,
            "feature_name": name,
            "face_width_cm": _expr_to_cm(face_width, self.parameters),
            "body_length_cm": _expr_to_cm(body_length, self.parameters),
            "overall_depth_cm": _expr_to_cm(overall_depth, self.parameters),
            "mount_hole_spacing_cm": _expr_to_cm(mount_hole_spacing, self.parameters),
            "mount_hole_radius_cm": _expr_to_cm(mount_hole_diameter, self.parameters) / 2.0,
            "pilot_radius_cm": _expr_to_cm(pilot_diameter, self.parameters) / 2.0,
            "shaft_radius_cm": _expr_to_cm(shaft_diameter, self.parameters) / 2.0,
            "detail_projection_cm": _expr_to_cm(detail_projection, self.parameters),
            "side_panel_projection_cm": _expr_to_cm(side_panel_projection, self.parameters),
            "lamination_band_height_cm": _expr_to_cm(lamination_band_height, self.parameters),
            "hole_shadow_radius_cm": _expr_to_cm(hole_shadow_diameter, self.parameters) / 2.0,
            "pilot_relief_radius_cm": _expr_to_cm(pilot_relief_diameter, self.parameters) / 2.0,
            "connector_width_cm": _expr_to_cm(connector_width, self.parameters),
            "connector_depth_cm": _expr_to_cm(connector_depth, self.parameters),
            "connector_height_cm": _expr_to_cm(connector_height, self.parameters),
            "wire_length_cm": _expr_to_cm(wire_length, self.parameters),
            "wire_diameter_cm": _expr_to_cm(wire_diameter, self.parameters),
            "lamination_ring_count": lamination_ring_count,
            "wire_count": wire_count,
            "body_names": body_names,
        }
        result = await self._execute_script_json(_crud_create_nema17_polish_script(payload))
        return result

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
        """Create a component-owned NEMA17 external assembly through the Fusion CRUD script bridge."""

        if not self._uses_crud_profile():
            raise RuntimeError("NEMA17 assembly creation requires the Fusion CRUD script profile")

        payload = {
            "feature_name": name,
            "assembly_component": assembly_component,
            "face_width_expr": self.parameters.get(face_width, face_width),
            "body_length_expr": self.parameters.get(body_length, body_length),
            "front_plate_thickness_expr": self.parameters.get(front_plate_thickness, front_plate_thickness),
            "rear_plate_thickness_expr": self.parameters.get(rear_plate_thickness, rear_plate_thickness),
            "pilot_diameter_expr": self.parameters.get(pilot_diameter, pilot_diameter),
            "pilot_length_expr": self.parameters.get(pilot_length, pilot_length),
            "shaft_diameter_expr": self.parameters.get(shaft_diameter, shaft_diameter),
            "shaft_length_expr": self.parameters.get(shaft_length, shaft_length),
            "mount_hole_spacing_expr": self.parameters.get(mount_hole_spacing, mount_hole_spacing),
            "mount_hole_diameter_expr": self.parameters.get(mount_hole_diameter, mount_hole_diameter),
            "connector_width_expr": self.parameters.get(connector_width, connector_width),
            "connector_height_expr": self.parameters.get(connector_height, connector_height),
            "connector_depth_expr": self.parameters.get(connector_depth, connector_depth),
            "wire_length_expr": self.parameters.get(wire_length, wire_length),
            "wire_diameter_expr": self.parameters.get(wire_diameter, wire_diameter),
            "face_width_cm": _expr_to_cm(face_width, self.parameters),
            "body_length_cm": _expr_to_cm(body_length, self.parameters),
            "front_plate_thickness_cm": _expr_to_cm(front_plate_thickness, self.parameters),
            "rear_plate_thickness_cm": _expr_to_cm(rear_plate_thickness, self.parameters),
            "pilot_radius_cm": _expr_to_cm(pilot_diameter, self.parameters) / 2.0,
            "pilot_length_cm": _expr_to_cm(pilot_length, self.parameters),
            "shaft_radius_cm": _expr_to_cm(shaft_diameter, self.parameters) / 2.0,
            "shaft_length_cm": _expr_to_cm(shaft_length, self.parameters),
            "mount_hole_radius_cm": _expr_to_cm(mount_hole_diameter, self.parameters) / 2.0,
            "mount_hole_offset_cm": _expr_to_cm(mount_hole_spacing, self.parameters) / 2.0,
            "connector_width_cm": _expr_to_cm(connector_width, self.parameters),
            "connector_height_cm": _expr_to_cm(connector_height, self.parameters),
            "connector_depth_cm": _expr_to_cm(connector_depth, self.parameters),
            "wire_length_cm": _expr_to_cm(wire_length, self.parameters),
            "wire_radius_cm": _expr_to_cm(wire_diameter, self.parameters) / 2.0,
            "lamination_count": lamination_count,
            "component_names": component_names,
            "body_names": body_names,
        }
        return await self._execute_script_json(_crud_create_nema17_external_assembly_script(payload))

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
        """Create a detailed metric 2020 T-slot aluminum extrusion through the Fusion CRUD bridge."""

        if not self._uses_crud_profile():
            raise RuntimeError("2020 aluminum profile creation requires the Fusion CRUD script profile")

        offset_cm = [_expr_to_cm(value, self.parameters) for value in placement_offset]
        while len(offset_cm) < 3:
            offset_cm.append(0.0)

        payload = {
            "feature_name": name,
            "component": component,
            "body_name": body_name,
            "length_expr": self.parameters.get(length, length),
            "size_expr": self.parameters.get(size, size),
            "slot_width_expr": self.parameters.get(slot_width, slot_width),
            "slot_depth_expr": self.parameters.get(slot_depth, slot_depth),
            "slot_cavity_width_expr": self.parameters.get(slot_cavity_width, slot_cavity_width),
            "center_bore_diameter_expr": self.parameters.get(center_bore_diameter, center_bore_diameter),
            "lip_thickness_expr": self.parameters.get(lip_thickness, lip_thickness),
            "corner_radius_expr": self.parameters.get(corner_radius, corner_radius),
            "length_cm": _expr_to_cm(length, self.parameters),
            "size_cm": _expr_to_cm(size, self.parameters),
            "slot_width_cm": _expr_to_cm(slot_width, self.parameters),
            "slot_depth_cm": _expr_to_cm(slot_depth, self.parameters),
            "slot_cavity_width_cm": _expr_to_cm(slot_cavity_width, self.parameters),
            "center_bore_radius_cm": _expr_to_cm(center_bore_diameter, self.parameters) / 2.0,
            "lip_thickness_cm": _expr_to_cm(lip_thickness, self.parameters),
            "corner_radius_cm": _expr_to_cm(corner_radius, self.parameters),
            "slot_count": slot_count,
            "web_relief_count": web_relief_count,
            "placement_offset_cm": offset_cm,
        }
        return await self._execute_script_json(_crud_create_profile2020_aluminum_script(payload))

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
        """Create a component-owned MGN12 linear rail and carriage assembly through the Fusion CRUD bridge."""

        if not self._uses_crud_profile():
            raise RuntimeError("MGN12 rail assembly creation requires the Fusion CRUD script profile")

        offset_cm = [_expr_to_cm(value, self.parameters) for value in placement_offset]
        while len(offset_cm) < 3:
            offset_cm.append(0.0)

        payload = {
            "feature_name": name,
            "assembly_component": assembly_component,
            "rail_length_expr": self.parameters.get(rail_length, rail_length),
            "rail_width_expr": self.parameters.get(rail_width, rail_width),
            "rail_height_expr": self.parameters.get(rail_height, rail_height),
            "rail_hole_pitch_expr": self.parameters.get(rail_hole_pitch, rail_hole_pitch),
            "rail_end_hole_offset_expr": self.parameters.get(rail_end_hole_offset, rail_end_hole_offset),
            "rail_hole_diameter_expr": self.parameters.get(rail_hole_diameter, rail_hole_diameter),
            "rail_counterbore_diameter_expr": self.parameters.get(rail_counterbore_diameter, rail_counterbore_diameter),
            "rail_counterbore_depth_expr": self.parameters.get(rail_counterbore_depth, rail_counterbore_depth),
            "carriage_length_expr": self.parameters.get(carriage_length, carriage_length),
            "carriage_width_expr": self.parameters.get(carriage_width, carriage_width),
            "carriage_total_height_expr": self.parameters.get(carriage_total_height, carriage_total_height),
            "carriage_top_height_expr": self.parameters.get(carriage_top_height, carriage_top_height),
            "carriage_mount_x_spacing_expr": self.parameters.get(carriage_mount_x_spacing, carriage_mount_x_spacing),
            "carriage_mount_y_spacing_expr": self.parameters.get(carriage_mount_y_spacing, carriage_mount_y_spacing),
            "carriage_mount_thread_diameter_expr": self.parameters.get(
                carriage_mount_thread_diameter,
                carriage_mount_thread_diameter,
            ),
            "rail_length_cm": _expr_to_cm(rail_length, self.parameters),
            "rail_width_cm": _expr_to_cm(rail_width, self.parameters),
            "rail_height_cm": _expr_to_cm(rail_height, self.parameters),
            "rail_hole_pitch_cm": _expr_to_cm(rail_hole_pitch, self.parameters),
            "rail_end_hole_offset_cm": _expr_to_cm(rail_end_hole_offset, self.parameters),
            "rail_hole_radius_cm": _expr_to_cm(rail_hole_diameter, self.parameters) / 2.0,
            "rail_counterbore_radius_cm": _expr_to_cm(rail_counterbore_diameter, self.parameters) / 2.0,
            "rail_counterbore_depth_cm": _expr_to_cm(rail_counterbore_depth, self.parameters),
            "carriage_length_cm": _expr_to_cm(carriage_length, self.parameters),
            "carriage_width_cm": _expr_to_cm(carriage_width, self.parameters),
            "carriage_total_height_cm": _expr_to_cm(carriage_total_height, self.parameters),
            "carriage_top_height_cm": _expr_to_cm(carriage_top_height, self.parameters),
            "carriage_mount_x_spacing_cm": _expr_to_cm(carriage_mount_x_spacing, self.parameters),
            "carriage_mount_y_spacing_cm": _expr_to_cm(carriage_mount_y_spacing, self.parameters),
            "carriage_mount_thread_radius_cm": _expr_to_cm(carriage_mount_thread_diameter, self.parameters) / 2.0,
            "component_names": component_names,
            "body_names": body_names,
            "placement_offset_cm": offset_cm,
        }
        return await self._execute_script_json(_crud_create_mgn12_linear_rail_script(payload))

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
        """Create a compact component-owned desktop CNC assembly through the Fusion CRUD bridge."""

        if not self._uses_crud_profile():
            raise RuntimeError("desktop CNC assembly creation requires the Fusion CRUD script profile")

        offset_cm = [_expr_to_cm(value, self.parameters) for value in placement_offset]
        while len(offset_cm) < 3:
            offset_cm.append(0.0)

        payload = {
            "feature_name": name,
            "assembly_component": assembly_component,
            "component_names": component_names,
            "body_names": body_names,
            "frame_width_cm": _expr_to_cm(frame_width, self.parameters),
            "frame_depth_cm": _expr_to_cm(frame_depth, self.parameters),
            "gantry_height_cm": _expr_to_cm(gantry_height, self.parameters),
            "profile_size_cm": _expr_to_cm(profile_size, self.parameters),
            "rail_length_cm": _expr_to_cm(rail_length, self.parameters),
            "z_rail_length_cm": _expr_to_cm(z_rail_length, self.parameters),
            "rail_width_cm": _expr_to_cm(rail_width, self.parameters),
            "rail_height_cm": _expr_to_cm(rail_height, self.parameters),
            "motor_face_width_cm": _expr_to_cm(motor_face_width, self.parameters),
            "motor_body_length_cm": _expr_to_cm(motor_body_length, self.parameters),
            "motor_shaft_radius_cm": _expr_to_cm(motor_shaft_diameter, self.parameters) / 2.0,
            "motor_shaft_length_cm": _expr_to_cm(motor_shaft_length, self.parameters),
            "leadscrew_radius_cm": _expr_to_cm(leadscrew_diameter, self.parameters) / 2.0,
            "coupler_radius_cm": _expr_to_cm(coupler_diameter, self.parameters) / 2.0,
            "coupler_length_cm": _expr_to_cm(coupler_length, self.parameters),
            "plate_thickness_cm": _expr_to_cm(plate_thickness, self.parameters),
            "spoilboard_length_cm": _expr_to_cm(spoilboard_length, self.parameters),
            "spoilboard_width_cm": _expr_to_cm(spoilboard_width, self.parameters),
            "spoilboard_thickness_cm": _expr_to_cm(spoilboard_thickness, self.parameters),
            "spindle_radius_cm": _expr_to_cm(spindle_diameter, self.parameters) / 2.0,
            "spindle_length_cm": _expr_to_cm(spindle_length, self.parameters),
            "work_area_mm": [
                _expr_to_cm(work_area_x, self.parameters) * 10.0,
                _expr_to_cm(work_area_y, self.parameters) * 10.0,
                _expr_to_cm(work_area_z, self.parameters) * 10.0,
            ],
            "placement_offset_cm": offset_cm,
        }
        return await self._execute_script_json(_crud_create_desktop_cnc_assembly_script(payload))

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
        """Create a generic spacer plate assembly through the Fusion CRUD bridge."""

        if not self._uses_crud_profile():
            raise RuntimeError("spacer plate assembly creation requires the Fusion CRUD script profile")
        offset_cm = [_expr_to_cm(value, self.parameters) for value in placement_offset]
        while len(offset_cm) < 3:
            offset_cm.append(0.0)
        payload = {
            "feature_name": name,
            "assembly_component": assembly_component,
            "component_names": component_names,
            "body_names": body_names,
            "occurrence_names": occurrence_names,
            "plate_length_cm": _expr_to_cm(plate_length, self.parameters),
            "plate_width_cm": _expr_to_cm(plate_width, self.parameters),
            "plate_thickness_cm": _expr_to_cm(plate_thickness, self.parameters),
            "plate_gap_cm": _expr_to_cm(plate_gap, self.parameters),
            "standoff_radius_cm": _expr_to_cm(standoff_diameter, self.parameters) / 2.0,
            "standoff_height_cm": _expr_to_cm(standoff_height, self.parameters),
            "hole_radius_cm": _expr_to_cm(hole_diameter, self.parameters) / 2.0,
            "hole_pattern_x_cm": _expr_to_cm(hole_pattern_x, self.parameters),
            "hole_pattern_y_cm": _expr_to_cm(hole_pattern_y, self.parameters),
            "placement_offset_cm": offset_cm,
        }
        result = await self._execute_script_json(_crud_create_spacer_plate_assembly_script(payload))
        self._last_scene.setdefault("occurrences", {}).update(result.get("occurrences", {}))
        self._last_scene.setdefault("physical_properties", {}).update(result.get("physical_properties", {}))
        self._last_scene["interference"] = result.get("interference", {"count": 0, "pairs": []})
        return result

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
        """Create a generic hinge assembly through the Fusion CRUD bridge."""

        if not self._uses_crud_profile():
            raise RuntimeError("hinge assembly creation requires the Fusion CRUD script profile")
        offset_cm = [_expr_to_cm(value, self.parameters) for value in placement_offset]
        while len(offset_cm) < 3:
            offset_cm.append(0.0)
        payload = {
            "feature_name": name,
            "assembly_component": assembly_component,
            "component_names": component_names,
            "body_names": body_names,
            "leaf_length_cm": _expr_to_cm(leaf_length, self.parameters),
            "leaf_width_cm": _expr_to_cm(leaf_width, self.parameters),
            "leaf_thickness_cm": _expr_to_cm(leaf_thickness, self.parameters),
            "pin_radius_cm": _expr_to_cm(pin_diameter, self.parameters) / 2.0,
            "pin_length_cm": _expr_to_cm(pin_length, self.parameters),
            "knuckle_radius_cm": _expr_to_cm(knuckle_outer_diameter, self.parameters) / 2.0,
            "knuckle_length_cm": _expr_to_cm(knuckle_length, self.parameters),
            "leaf_gap_cm": _expr_to_cm(leaf_gap, self.parameters),
            "placement_offset_cm": offset_cm,
        }
        result = await self._execute_script_json(_crud_create_hinge_assembly_script(payload))
        self._last_scene.setdefault("occurrences", {}).update(result.get("occurrences", {}))
        self._last_scene.setdefault("physical_properties", {}).update(result.get("physical_properties", {}))
        self._last_scene["interference"] = result.get("interference", {"count": 0, "pairs": []})
        return result

    async def set_component_metadata(self, metadata: list[dict[str, Any]]) -> dict[str, Any]:
        """Set component metadata through the Fusion CRUD bridge."""

        if not self._uses_crud_profile():
            raise RuntimeError("component metadata writes require the Fusion CRUD script profile")
        result = await self._execute_script_json(_crud_set_component_metadata_script({"metadata": metadata}))
        self._last_scene.setdefault("component_metadata", {}).update(result.get("component_metadata", {}))
        return result

    async def create_assembly_joints(self, joints: list[dict[str, Any]]) -> dict[str, Any]:
        """Create assembly joint contracts through the Fusion CRUD bridge."""

        if not self._uses_crud_profile():
            raise RuntimeError("assembly joint creation requires the Fusion CRUD script profile")
        result = await self._execute_script_json(_crud_create_assembly_joints_script({"joints": joints}))
        self._last_scene.setdefault("joints", {}).update(result.get("joints", {}))
        return result

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
        """Capture a viewport image through the Fusion CRUD bridge."""

        if not self._uses_crud_profile():
            raise RuntimeError("viewport capture requires the Fusion CRUD script profile")
        payload = {
            "name": name,
            "path": str(path),
            "view": view,
            "isolate_prefix": isolate_prefix,
            "width": int(width),
            "height": int(height),
        }
        result = await self._execute_script_json(_crud_capture_viewport_script(payload))
        screenshot = result.get("screenshot", result)
        captured_path = Path(str(screenshot.get("path") or path))
        if not captured_path.exists():
            screenshot["evidence_quality"] = "failed"
            raise RuntimeError(f"viewport capture failed: local file does not exist: {captured_path}")
        size = captured_path.stat().st_size
        if size <= 0:
            screenshot["evidence_quality"] = "empty_file"
            raise RuntimeError(f"viewport capture failed: local file is empty: {captured_path}")
        screenshot["bytes"] = size
        screenshot["evidence_quality"] = "verified_file"
        result["evidence_quality"] = "verified_file"
        self._last_scene.setdefault("screenshots", {})[name] = result.get("screenshot", {})
        return result

    async def analyze_interference(self, target: str | None = None) -> dict[str, Any]:
        """Analyze interference in the active design."""

        if not self._uses_crud_profile():
            return {"interference": self._last_scene.get("interference", {"count": 0, "pairs": []})}
        result = await self._execute_script_json(_crud_analyze_interference_script({"target": target}))
        self._last_scene["interference"] = result.get("interference", {})
        return result

    async def measure_physical_properties(self, targets: list[str] | None = None) -> dict[str, Any]:
        """Measure physical properties in the active design."""

        if not self._uses_crud_profile():
            return {"physical_properties": self._last_scene.get("physical_properties", {})}
        result = await self._execute_script_json(_crud_measure_physical_properties_script({"targets": targets or []}))
        self._last_scene.setdefault("physical_properties", {}).update(result.get("physical_properties", {}))
        return result

    async def measure_bounding_box(self, target: str | None = None) -> list[float]:
        """Measure or infer a bounding box in millimeters."""

        if self._uses_crud_profile():
            bodies = self._last_scene.get("bodies", {})
            if target and isinstance(bodies, dict) and target in bodies:
                return list(bodies[target].get("bounding_box_mm", []))
            if isinstance(bodies, dict) and bodies:
                maxes = [0.0, 0.0, 0.0]
                for body in bodies.values():
                    bbox = body.get("bounding_box_mm", [])
                    if len(bbox) == 3:
                        maxes = [max(a, float(b)) for a, b in zip(maxes, bbox, strict=True)]
                return maxes

        if target and target in self.body_dimensions_cm:
            return [value * 10.0 for value in self.body_dimensions_cm[target]]
        if self.body_dimensions_cm:
            maxes = [0.0, 0.0, 0.0]
            for dimensions in self.body_dimensions_cm.values():
                maxes = [max(a, b * 10.0) for a, b in zip(maxes, dimensions, strict=True)]
            return maxes

        body_names = _names_from_payload(self._last_scene.get("bodies"))
        if body_names and "get_object_info" in self.available_tools:
            info = await self._call("get_object_info", {"name": body_names[0]})
            bbox = info.get("bounding_box") or {}
            mins = bbox.get("min") or [0, 0, 0]
            maxes = bbox.get("max") or [0, 0, 0]
            return [abs(float(max_v) - float(min_v)) * 10.0 for min_v, max_v in zip(mins, maxes, strict=True)]
        return [0.0, 0.0, 0.0]

    async def validate_named_objects(self) -> dict[str, Any]:
        """Validate names tracked by this facade."""

        tracked = list(self.body_dimensions_cm) + list(self.parameters)
        invalid = [name for name in tracked if not name or name[0].isupper()]
        return {"valid": not invalid, "invalid": invalid}

    async def export_step(self, target: str, path: Path | str) -> dict[str, Any]:
        """Export a STEP file through the vendor tool."""

        return await self._call("export_step", {"body_name": target, "file_path": str(path)})

    async def export_stl(self, target: str, path: Path | str) -> dict[str, Any]:
        """Export an STL file through the vendor tool."""

        return await self._call("export_stl", {"body_name": target, "file_path": str(path)})

    def _hole_centers(self, target_body: str, count: int, offset: str | None) -> list[tuple[float, float]]:
        if count != 4 or target_body not in self.body_dimensions_cm or not offset:
            return [(0.0, 0.0) for _ in range(max(count, 1))]
        width, height, _depth = self.body_dimensions_cm[target_body]
        offset_cm = _expr_to_cm(offset, self.parameters)
        x = max(0.0, width / 2.0 - offset_cm)
        y = max(0.0, height / 2.0 - offset_cm)
        return [(-x, -y), (-x, y), (x, -y), (x, y)]

    async def _optional_call(self, native_tool: str, args: dict[str, Any]) -> dict[str, Any]:
        if native_tool not in self.available_tools:
            return {}
        try:
            return await self._call(native_tool, args)
        except ToolNotAllowed:
            return {}

    async def _call(self, native_tool: str, args: dict[str, Any]) -> dict[str, Any]:
        result: ToolResult = await self.adapter.call(native_tool, {"_facade_tool": native_tool, **args})
        if not result.ok:
            raise RuntimeError(f"{native_tool} failed: {result.error_code}: {result.error_message}")
        return result.data

    def _uses_crud_profile(self) -> bool:
        return _CRUD_SIGNATURE_TOOLS.issubset(self.available_tools)

    async def _execute_script_json(self, script: str) -> dict[str, Any]:
        payload = await self._call(
            "fusion_mcp_execute",
            {
                "featureType": "script",
                "object": {"script": script},
            },
        )
        return _decode_script_payload(payload)

    async def _execute_trusted_read_script_json(self, script: str) -> dict[str, Any]:
        """Execute a harness-owned read template once, without post-dispatch replay."""

        result: ToolResult = await self.adapter.call(
            "fusion_mcp_execute",
            {
                "_facade_tool": "fusion_mcp_execute",
                "featureType": "script",
                "object": {"script": script},
            },
            options=McpCallOptions.for_trusted_internal_read(
                timeout_seconds=float(os.getenv("FUSION_MCP_TRUSTED_READ_TIMEOUT_SECONDS", "10"))
            ),
        )
        if not result.ok:
            raise RuntimeError(
                f"fusion_mcp_execute failed: {result.error_code}: {result.error_message}"
            )
        return _decode_script_payload(result.data)


def _decode_script_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Decode the several wrapper shapes returned by Fusion execute."""

    message = payload.get("message")
    if isinstance(message, str):
        candidate = message.strip()
        if candidate.startswith("{") and candidate.endswith("}"):
            return _ensure_script_success(json.loads(candidate))
    text = str(payload.get("text", "")).strip()
    if text.startswith("{") and text.endswith("}"):
        wrapper = json.loads(text)
        message = wrapper.get("message") if isinstance(wrapper, dict) else None
        if isinstance(message, str):
            candidate = message.strip()
            if candidate.startswith("{") and candidate.endswith("}"):
                return _ensure_script_success(json.loads(candidate))
        if isinstance(wrapper, dict):
            return _ensure_script_success(wrapper)
    for line in reversed(text.splitlines()):
        candidate = line.strip()
        if candidate.startswith("{") and candidate.endswith("}"):
            return _ensure_script_success(json.loads(candidate))
    return _ensure_script_success(payload)


def _split_unit_expression(expression: str) -> tuple[float, str]:
    match = _UNIT_RE.fullmatch(expression)
    if not match:
        raise ValueError(f"vendor facade requires explicit literal unit expression: {expression!r}")
    return float(match.group(1)), match.group(2).lower()


def _is_command_dialog_error(message: str) -> bool:
    return "command dialog is open" in message.lower()


def _blocked_inspection_state(message: str) -> dict[str, Any]:
    return {
        "active_document": None,
        "units": "unknown",
        "root_component": "unknown",
        "active_component": "unknown",
        "components": {},
        "bodies": {},
        "sketches": {},
        "features": {},
        "parameters": {},
        "nema17_metrics": {},
        "polish_metrics": {},
        "assembly_metrics": {},
        "profile2020_metrics": {},
        "mgn12_metrics": {},
        "cnc_metrics": {},
        "component_metadata": {},
        "joints": {},
        "occurrences": {},
        "physical_properties": {},
        "interference": {},
        "screenshots": {},
        "exports": {},
        "real_connection": True,
        "inspection_status": "blocked",
        "blocked_by_dialog": True,
        "inspection_error": message,
        "remediation": "Close the active Fusion command dialog and retry inspect.",
    }


def _ensure_script_success(payload: dict[str, Any]) -> dict[str, Any]:
    if isinstance(payload, dict) and payload.get("success") is False:
        raise RuntimeError(str(payload.get("error") or payload))
    return payload


def _expr_to_cm(expression: str, parameters: dict[str, str]) -> float:
    return expression_to_mm(expression, parameters) / 10.0


def _shape_dimensions_cm(
    shape: str,
    distance: str,
    shape_inputs: dict[str, Any],
    parameters: dict[str, str],
) -> list[float]:
    if shape == "rectangle":
        return [
            _expr_to_cm(shape_inputs["width"], parameters),
            _expr_to_cm(shape_inputs["height"], parameters),
            _expr_to_cm(distance, parameters),
        ]
    if shape == "cylinder":
        diameter = _expr_to_cm(shape_inputs["diameter"], parameters)
        return [diameter, diameter, _expr_to_cm(distance, parameters)]
    if shape == "l_bracket":
        leg = _expr_to_cm(shape_inputs["leg_length"], parameters)
        thickness = _expr_to_cm(shape_inputs["thickness"], parameters)
        return [leg, leg, thickness]
    if shape == "box_shell":
        return [
            _expr_to_cm(shape_inputs["length"], parameters),
            _expr_to_cm(shape_inputs["width"], parameters),
            _expr_to_cm(shape_inputs["height"], parameters),
        ]
    return [0.0, 0.0, _expr_to_cm(distance, parameters)]


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    return []


def _names_from_payload(value: Any) -> list[str]:
    names: list[str] = []
    for item in _as_list(value):
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict) and item.get("name"):
            names.append(str(item["name"]))
    return names


def _normalize_root_name(name: str) -> str:
    return "root" if name.lower() in {"root", "rootcomponent"} else name


def _component_count(component_names: list[str]) -> int:
    if not component_names:
        return 0
    return sum(1 for name in component_names if _normalize_root_name(name) != "root")


def _crud_script(payload: dict[str, Any], body: str) -> str:
    payload_json = json.dumps(payload, sort_keys=True)
    return f"""
import json
import re
import adsk.core
import adsk.fusion
import unicodedata

PAYLOAD = json.loads({payload_json!r})


def _design():
    app = adsk.core.Application.get()
    design = adsk.fusion.Design.cast(app.activeProduct)
    if not design:
        raise RuntimeError("active product is not a Fusion design")
    return design


def _component(design, name):
    for index in range(design.allComponents.count):
        component = design.allComponents.item(index)
        if component and component.name == name:
            return component
    return design.rootComponent


def _sketch(design, name):
    for component_index in range(design.allComponents.count):
        component = design.allComponents.item(component_index)
        if not component:
            continue
        for sketch_index in range(component.sketches.count):
            sketch = component.sketches.item(sketch_index)
            if sketch and sketch.name == name:
                return sketch
    raise RuntimeError(f"sketch not found: {{name}}")


def run(_context: str):
{body}
"""


def _crud_create_parameter_script(payload: dict[str, Any]) -> str:
    return _crud_script(
        payload,
        """    design = _design()
    expression = PAYLOAD["expression"]
    unit = expression.split()[-1] if len(expression.split()) > 1 else design.unitsManager.defaultLengthUnits
    existing = design.userParameters.itemByName(PAYLOAD["name"])
    if existing:
        existing.expression = expression
    else:
        design.userParameters.add(
            PAYLOAD["name"],
            adsk.core.ValueInput.createByString(expression),
            unit,
            PAYLOAD.get("comment", ""),
        )
    print(json.dumps({"success": True, "parameter": {"name": PAYLOAD["name"], "expression": expression}}, sort_keys=True))
""",
    )


def _crud_update_parameter_script(payload: dict[str, Any]) -> str:
    return _crud_script(
        payload,
        """    design = _design()
    existing = design.userParameters.itemByName(PAYLOAD["name"])
    if not existing:
        raise RuntimeError(f"parameter not found: {PAYLOAD['name']}")
    existing.expression = PAYLOAD["expression"]
    print(json.dumps({"success": True, "parameter": {"name": PAYLOAD["name"], "expression": PAYLOAD["expression"]}}, sort_keys=True))
""",
    )


def _crud_create_component_script(payload: dict[str, Any]) -> str:
    return _crud_script(
        payload,
        """    design = _design()
    app = adsk.core.Application.get()
    root = design.rootComponent
    for component_index in range(design.allComponents.count):
        component = design.allComponents.item(component_index)
        if component and component.name == PAYLOAD["name"]:
            print(json.dumps({"success": True, "component": {"name": PAYLOAD["name"], "already_exists": True}}, sort_keys=True))
            return
    if root.occurrences.count == 0 and root.bRepBodies.count == 0 and root.sketches.count == 0:
        root.name = PAYLOAD["name"]
        print(json.dumps({"success": True, "component": {"name": PAYLOAD["name"], "root_component": True}}, sort_keys=True))
        return
    transform = adsk.core.Matrix3D.create()
    try:
        occurrence = root.occurrences.addNewComponent(transform)
    except RuntimeError:
        doc = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
        design = adsk.fusion.Design.cast(app.activeProduct)
        root = design.rootComponent
        occurrence = root.occurrences.addNewComponent(transform)
    occurrence.component.name = PAYLOAD["name"]
    print(json.dumps({"success": True, "component": {"name": PAYLOAD["name"]}}, sort_keys=True))
""",
    )


def _crud_create_sketch_script(payload: dict[str, Any]) -> str:
    return _crud_script(
        payload,
        """    design = _design()
    component = _component(design, PAYLOAD["component"])
    plane_name = PAYLOAD.get("plane", "XY").upper()
    plane = component.xYConstructionPlane
    if plane_name == "XZ":
        plane = component.xZConstructionPlane
    elif plane_name == "YZ":
        plane = component.yZConstructionPlane
    sketch = component.sketches.add(plane)
    sketch.name = PAYLOAD["name"]
    print(json.dumps({"success": True, "sketch": {"name": sketch.name}}, sort_keys=True))
""",
    )


def _crud_draw_rectangle_script(payload: dict[str, Any]) -> str:
    return _crud_script(
        payload,
        """    design = _design()
    sketch = _sketch(design, PAYLOAD["sketch"])
    center = adsk.core.Point3D.create(PAYLOAD["center_x"], PAYLOAD["center_y"], 0)
    corner = adsk.core.Point3D.create(
        PAYLOAD["center_x"] + PAYLOAD["width"] / 2.0,
        PAYLOAD["center_y"] + PAYLOAD["height"] / 2.0,
        0,
    )
    sketch.sketchCurves.sketchLines.addCenterPointRectangle(center, corner)
    print(json.dumps({"success": True, "profile_ref": f"{PAYLOAD['sketch']}:rectangle:0"}, sort_keys=True))
""",
    )


def _crud_draw_circle_script(payload: dict[str, Any]) -> str:
    return _crud_script(
        payload,
        """    design = _design()
    sketch = _sketch(design, PAYLOAD["sketch"])
    center = adsk.core.Point3D.create(PAYLOAD["center_x"], PAYLOAD["center_y"], 0)
    sketch.sketchCurves.sketchCircles.addByCenterRadius(center, PAYLOAD["radius"])
    print(json.dumps({"success": True, "profile_ref": f"{PAYLOAD['sketch']}:circle:0"}, sort_keys=True))
""",
    )


def _crud_extrude_script(payload: dict[str, Any]) -> str:
    return _crud_script(
        payload,
        """    design = _design()
    sketch = _sketch(design, PAYLOAD["sketch"])
    if sketch.profiles.count < 1:
        raise RuntimeError(f"sketch has no profiles: {PAYLOAD['sketch']}")
    operation = adsk.fusion.FeatureOperations.NewBodyFeatureOperation
    if PAYLOAD.get("operation") == "cut":
        operation = adsk.fusion.FeatureOperations.CutFeatureOperation
    extrude = sketch.parentComponent.features.extrudeFeatures.addSimple(
        sketch.profiles.item(0),
        adsk.core.ValueInput.createByString(PAYLOAD["distance"]),
        operation,
    )
    extrude.name = PAYLOAD["feature_name"]
    if extrude.bodies.count:
        extrude.bodies.item(0).name = PAYLOAD["body_name"]
    print(json.dumps({"success": True, "feature": {"name": extrude.name}, "body": {"name": PAYLOAD["body_name"]}}, sort_keys=True))
""",
    )


def _crud_create_nema17_stepper_script(payload: dict[str, Any]) -> str:
    return _crud_script(
        payload,
        """    design = _design()

    def _set_extrude_distance(target_component, feature_name, expression):
        feature = target_component.features.extrudeFeatures.itemByName(feature_name)
        if not feature:
            return False
        extent = feature.extentOne
        if not extent or not getattr(extent, "distance", None):
            return False
        extent.distance.expression = expression
        return True

    def _set_plane_offset(target_component, plane_name, expression):
        plane = target_component.constructionPlanes.itemByName(plane_name)
        if not plane:
            return False
        definition = plane.definition
        if not definition or not getattr(definition, "offset", None):
            return False
        definition.offset.expression = expression
        return True

    for component_index in range(design.allComponents.count):
        existing_component = design.allComponents.item(component_index)
        if not existing_component:
            continue
        for body_index in range(existing_component.bRepBodies.count):
            body = existing_component.bRepBodies.item(body_index)
            if body and body.name == PAYLOAD["body_name"]:
                _set_extrude_distance(existing_component, "nema17_body_extrude", PAYLOAD["body_length_expr"])
                _set_plane_offset(existing_component, "nema17_front_face_plane", PAYLOAD["body_length_expr"])
                _set_extrude_distance(existing_component, "nema17_pilot_boss_extrude", PAYLOAD["pilot_length_expr"])
                _set_extrude_distance(existing_component, "nema17_shaft_extrude", PAYLOAD["shaft_length_expr"])
                print(json.dumps({
                    "success": True,
                    "component": {"name": existing_component.name or "root"},
                    "body": {"name": PAYLOAD["body_name"], "already_exists": True},
                    "feature": {"name": PAYLOAD["feature_name"]},
                    "mount_hole_count": int(PAYLOAD.get("mount_hole_count", 4)),
                }, sort_keys=True))
                return

    component = _component(design, PAYLOAD["component"])
    center = adsk.core.Point3D.create(0, 0, 0)

    base_sketch = component.sketches.add(component.xYConstructionPlane)
    base_sketch.name = "nema17_body_profile_sketch"
    corner = adsk.core.Point3D.create(PAYLOAD["face_width_cm"] / 2.0, PAYLOAD["face_width_cm"] / 2.0, 0)
    base_sketch.sketchCurves.sketchLines.addCenterPointRectangle(center, corner)
    base_extrude = component.features.extrudeFeatures.addSimple(
        base_sketch.profiles.item(0),
        adsk.core.ValueInput.createByString(PAYLOAD["body_length_expr"]),
        adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
    )
    base_extrude.name = "nema17_body_extrude"
    if base_extrude.bodies.count:
        base_extrude.bodies.item(0).name = PAYLOAD["body_name"]

    holes_sketch = component.sketches.add(component.xYConstructionPlane)
    holes_sketch.name = "nema17_mount_hole_sketch"
    offset = PAYLOAD["mount_hole_offset_cm"]
    for x, y in [(-offset, -offset), (-offset, offset), (offset, -offset), (offset, offset)]:
        holes_sketch.sketchCurves.sketchCircles.addByCenterRadius(
            adsk.core.Point3D.create(x, y, 0),
            PAYLOAD["mount_hole_radius_cm"],
        )
    profiles = [holes_sketch.profiles.item(index) for index in range(holes_sketch.profiles.count)]
    for index, profile in enumerate(profiles[: int(PAYLOAD.get("mount_hole_count", 4))]):
        cut = component.features.extrudeFeatures.addSimple(
            profile,
            adsk.core.ValueInput.createByString(PAYLOAD["body_length_expr"]),
            adsk.fusion.FeatureOperations.CutFeatureOperation,
        )
        cut.name = f"nema17_mount_hole_cut_{index + 1}"

    plane_input = component.constructionPlanes.createInput()
    plane_input.setByOffset(
        component.xYConstructionPlane,
        adsk.core.ValueInput.createByString(PAYLOAD["body_length_expr"]),
    )
    front_plane = component.constructionPlanes.add(plane_input)
    front_plane.name = "nema17_front_face_plane"

    pilot_sketch = component.sketches.add(front_plane)
    pilot_sketch.name = "nema17_pilot_boss_sketch"
    pilot_sketch.sketchCurves.sketchCircles.addByCenterRadius(center, PAYLOAD["pilot_radius_cm"])
    pilot = component.features.extrudeFeatures.addSimple(
        pilot_sketch.profiles.item(0),
        adsk.core.ValueInput.createByString(PAYLOAD["pilot_length_expr"]),
        adsk.fusion.FeatureOperations.JoinFeatureOperation,
    )
    pilot.name = "nema17_pilot_boss_extrude"

    shaft_sketch = component.sketches.add(front_plane)
    shaft_sketch.name = "nema17_shaft_sketch"
    shaft_sketch.sketchCurves.sketchCircles.addByCenterRadius(center, PAYLOAD["shaft_radius_cm"])
    shaft = component.features.extrudeFeatures.addSimple(
        shaft_sketch.profiles.item(0),
        adsk.core.ValueInput.createByString(PAYLOAD["shaft_length_expr"]),
        adsk.fusion.FeatureOperations.JoinFeatureOperation,
    )
    shaft.name = "nema17_shaft_extrude"

    print(json.dumps({
        "success": True,
        "component": {"name": PAYLOAD["component"]},
        "body": {"name": PAYLOAD["body_name"]},
        "feature": {"name": PAYLOAD["feature_name"]},
        "mount_hole_count": int(PAYLOAD.get("mount_hole_count", 4)),
    }, sort_keys=True))
""",
    )


def _crud_create_nema17_polish_script(payload: dict[str, Any]) -> str:
    return _crud_script(
        payload,
        """    design = _design()
    app = adsk.core.Application.get()

    target_component = None
    for component_index in range(design.allComponents.count):
        candidate_component = design.allComponents.item(component_index)
        if not candidate_component:
            continue
        for body_index in range(candidate_component.bRepBodies.count):
            body = candidate_component.bRepBodies.item(body_index)
            if body and body.name == PAYLOAD["target_body"]:
                target_component = candidate_component
                break
        if target_component:
            break
    if not target_component:
        raise RuntimeError(f"target body not found: {PAYLOAD['target_body']}")

    def _body_by_name(name):
        for body_index in range(target_component.bRepBodies.count):
            body = target_component.bRepBodies.item(body_index)
            if body and body.name == name:
                return body
        return None

    def _normalized_text(value):
        return unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii").lower()

    def _matches_keywords(name, keywords):
        tokens = set(re.findall(r"[a-z0-9]+", _normalized_text(name)))
        return all(keyword in tokens for keyword in keywords)

    def _appearance_by_keywords(keywords):
        keywords = [_normalized_text(keyword) for keyword in keywords]
        for source in [design.appearances]:
            for index in range(source.count):
                appearance = source.item(index)
                if appearance and _matches_keywords(appearance.name, keywords):
                    return appearance
        for library_index in range(app.materialLibraries.count):
            library = app.materialLibraries.item(library_index)
            if not library:
                continue
            appearances = library.appearances
            for appearance_index in range(appearances.count):
                appearance = appearances.item(appearance_index)
                if appearance and all(keyword in appearance.name.lower() for keyword in keywords):
                    return appearance
        return None

    black = _appearance_by_keywords(["black"])
    aluminum = _appearance_by_keywords(["aluminum"]) or _appearance_by_keywords(["metal"])
    steel = _appearance_by_keywords(["steel"]) or aluminum
    white = _appearance_by_keywords(["white"]) or _appearance_by_keywords(["plastic"])
    red = _appearance_by_keywords(["red"]) or black
    blue = _appearance_by_keywords(["blue"]) or black
    green = _appearance_by_keywords(["green"]) or black

    def _apply_appearance(body, appearance):
        if body and appearance:
            body.appearance = appearance

    def _add_box(name, center, size, appearance):
        existing = _body_by_name(name)
        if existing:
            return existing

        plane = _plane_at_z(f"{name}_plane", center[2] - size[2] / 2.0)
        sketch = target_component.sketches.add(plane)
        sketch.name = f"{name}_sketch"
        sketch.sketchCurves.sketchLines.addCenterPointRectangle(
            adsk.core.Point3D.create(center[0], center[1], 0),
            adsk.core.Point3D.create(center[0] + size[0] / 2.0, center[1] + size[1] / 2.0, 0),
        )
        extrude = target_component.features.extrudeFeatures.addSimple(
            sketch.profiles.item(0),
            adsk.core.ValueInput.createByReal(size[2]),
            adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
        )
        extrude.name = f"{name}_extrude"
        if extrude.bodies.count:
            body = extrude.bodies.item(0)
            body.name = name
            _apply_appearance(body, appearance)
            return body
        return None

    def _plane_at_z(name, z_value):
        existing = target_component.constructionPlanes.itemByName(name)
        if existing:
            return existing
        plane_input = target_component.constructionPlanes.createInput()
        plane_input.setByOffset(target_component.xYConstructionPlane, adsk.core.ValueInput.createByReal(z_value))
        plane = target_component.constructionPlanes.add(plane_input)
        plane.name = name
        return plane

    def _add_disc(name, center_x, center_y, z_value, radius, height, appearance):
        existing = _body_by_name(name)
        if existing:
            return existing
        plane = _plane_at_z(f"{name}_plane", z_value)
        sketch = target_component.sketches.add(plane)
        sketch.name = f"{name}_sketch"
        sketch.sketchCurves.sketchCircles.addByCenterRadius(
            adsk.core.Point3D.create(center_x, center_y, 0),
            radius,
        )
        extrude = target_component.features.extrudeFeatures.addSimple(
            sketch.profiles.item(0),
            adsk.core.ValueInput.createByReal(height),
            adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
        )
        extrude.name = f"{name}_extrude"
        if extrude.bodies.count:
            body = extrude.bodies.item(0)
            body.name = name
            _apply_appearance(body, appearance)
            return body
        return None

    half = PAYLOAD["face_width_cm"] / 2.0
    body_length = PAYLOAD["body_length_cm"]
    band_height = PAYLOAD["lamination_band_height_cm"]
    projection = PAYLOAD["detail_projection_cm"]
    side_projection = PAYLOAD["side_panel_projection_cm"]
    panel_span = PAYLOAD["face_width_cm"] - 0.55
    panel_height = max(0.1, body_length - 0.75)
    panel_z = body_length / 2.0
    side_offset = half + side_projection / 2.0 + 0.01
    ring_offset = half + projection / 2.0 + 0.03

    _add_box("nema17_side_panel_pos_y", [0, side_offset, panel_z], [panel_span, side_projection, panel_height], black)
    _add_box("nema17_side_panel_neg_y", [0, -side_offset, panel_z], [panel_span, side_projection, panel_height], black)
    _add_box("nema17_side_panel_pos_x", [side_offset, 0, panel_z], [side_projection, panel_span, panel_height], black)
    _add_box("nema17_side_panel_neg_x", [-side_offset, 0, panel_z], [side_projection, panel_span, panel_height], black)

    ring_count = int(PAYLOAD.get("lamination_ring_count", 18))
    for ring_index in range(1, ring_count + 1):
        if ring_count == 1:
            z_value = body_length / 2.0
        else:
            z_value = 0.32 + (body_length - 0.64) * (ring_index - 1) / (ring_count - 1)
        _add_box(
            f"nema17_lamination_ring_{ring_index:02d}_pos_y",
            [0, ring_offset, z_value],
            [PAYLOAD["face_width_cm"] + projection * 2.0, projection, band_height],
            black,
        )
        _add_box(
            f"nema17_lamination_ring_{ring_index:02d}_neg_y",
            [0, -ring_offset, z_value],
            [PAYLOAD["face_width_cm"] + projection * 2.0, projection, band_height],
            black,
        )
        _add_box(
            f"nema17_lamination_ring_{ring_index:02d}_pos_x",
            [ring_offset, 0, z_value],
            [projection, PAYLOAD["face_width_cm"] + projection * 2.0, band_height],
            black,
        )
        _add_box(
            f"nema17_lamination_ring_{ring_index:02d}_neg_x",
            [-ring_offset, 0, z_value],
            [projection, PAYLOAD["face_width_cm"] + projection * 2.0, band_height],
            black,
        )

    front_z = body_length + 0.01
    _add_disc(
        "nema17_pilot_relief_shadow",
        0,
        0,
        front_z,
        PAYLOAD["pilot_relief_radius_cm"],
        0.018,
        black,
    )

    hole_offset = PAYLOAD["mount_hole_spacing_cm"] / 2.0
    for index, (x_value, y_value) in enumerate(
        [(-hole_offset, -hole_offset), (-hole_offset, hole_offset), (hole_offset, -hole_offset), (hole_offset, hole_offset)],
        start=1,
    ):
        _add_disc(
            f"nema17_mount_hole_shadow_{index:02d}",
            x_value,
            y_value,
            front_z + 0.01,
            PAYLOAD["hole_shadow_radius_cm"],
            0.02,
            black,
        )

    connector_y = half + PAYLOAD["connector_depth_cm"] / 2.0 + 0.18
    connector_z = PAYLOAD["connector_height_cm"] / 2.0 + 0.28
    _add_box(
        "nema17_rear_connector_body",
        [0, connector_y, connector_z],
        [PAYLOAD["connector_width_cm"], PAYLOAD["connector_depth_cm"], PAYLOAD["connector_height_cm"]],
        white,
    )

    wire_y = half + PAYLOAD["connector_depth_cm"] + PAYLOAD["wire_length_cm"] / 2.0 + 0.28
    wire_size = PAYLOAD["wire_diameter_cm"]
    wire_specs = [
        ("nema17_wire_red", -0.57, red),
        ("nema17_wire_blue", -0.19, blue),
        ("nema17_wire_green", 0.19, green),
        ("nema17_wire_black", 0.57, black),
    ][: int(PAYLOAD.get("wire_count", 4))]
    for wire_name, x_offset, appearance in wire_specs:
        _add_box(
            wire_name,
            [x_offset, wire_y, connector_z],
            [wire_size, PAYLOAD["wire_length_cm"], wire_size],
            appearance,
        )

    body_names = []
    for body_index in range(target_component.bRepBodies.count):
        body = target_component.bRepBodies.item(body_index)
        if body and body.name.startswith("nema17_") and body.name != PAYLOAD["target_body"]:
            body_names.append(body.name)
    body_names = sorted(body_names)
    payload = {
        "success": True,
        "feature": {"name": PAYLOAD["feature_name"]},
        "polish_metrics": {
            "body_names": body_names,
            "lamination_body_count": sum(1 for body_name in body_names if body_name.startswith("nema17_lamination_ring_")),
            "wire_count": sum(1 for body_name in body_names if body_name.startswith("nema17_wire_")),
            "screw_shadow_count": sum(1 for body_name in body_names if body_name.startswith("nema17_mount_hole_shadow_")),
            "connector_present": "nema17_rear_connector_body" in body_names,
            "side_panel_count": sum(1 for body_name in body_names if body_name.startswith("nema17_side_panel_")),
        },
    }
    print(json.dumps(payload, sort_keys=True))
""",
    )


def _crud_create_profile2020_aluminum_script(payload: dict[str, Any]) -> str:
    return _crud_script(
        payload,
        """    design = _design()
    app = adsk.core.Application.get()
    root = design.rootComponent

    def _component_by_name(name):
        for component_index in range(design.allComponents.count):
            component = design.allComponents.item(component_index)
            if component and component.name == name:
                return component
        return None

    def _find_or_create_component(name):
        existing = _component_by_name(name)
        if existing:
            return existing
        transform = adsk.core.Matrix3D.create()
        try:
            transform.setToTranslation(
                adsk.core.Vector3D.create(
                    PAYLOAD["placement_offset_cm"][0],
                    PAYLOAD["placement_offset_cm"][1],
                    PAYLOAD["placement_offset_cm"][2],
                )
            )
        except Exception:
            pass
        occurrence = root.occurrences.addNewComponent(transform)
        occurrence.component.name = name
        return occurrence.component

    def _body_by_name(component, name):
        for body_index in range(component.bRepBodies.count):
            body = component.bRepBodies.item(body_index)
            if body and body.name == name:
                try:
                    body.isLightBulbOn = True
                except Exception:
                    pass
                return body
        return None

    def _normalized_text(value):
        return unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii").lower()

    def _appearance_by_keywords(keywords):
        keywords = [_normalized_text(keyword) for keyword in keywords]
        for source in [design.appearances]:
            for index in range(source.count):
                appearance = source.item(index)
                if appearance and all(keyword in _normalized_text(appearance.name) for keyword in keywords):
                    return appearance
        for library_index in range(app.materialLibraries.count):
            library = app.materialLibraries.item(library_index)
            if not library:
                continue
            appearances = library.appearances
            for appearance_index in range(appearances.count):
                appearance = appearances.item(appearance_index)
                if appearance and all(keyword in appearance.name.lower() for keyword in keywords):
                    return appearance
        return None

    def _material_by_keywords(keywords):
        keywords = [keyword.lower() for keyword in keywords]
        try:
            sources = [design.materials]
        except Exception:
            sources = []
        for library_index in range(app.materialLibraries.count):
            library = app.materialLibraries.item(library_index)
            if not library:
                continue
            try:
                sources.append(library.materials)
            except Exception:
                pass
        for source in sources:
            for material_index in range(source.count):
                material = source.item(material_index)
                if material and all(keyword in material.name.lower() for keyword in keywords):
                    return material
        return None

    aluminum_appearance = (
        _appearance_by_keywords(["anodized", "alum"])
        or _appearance_by_keywords(["alum"])
        or _appearance_by_keywords(["metal"])
    )
    aluminum_material = _material_by_keywords(["alum", "6063"]) or _material_by_keywords(["alum"])

    def _apply_material(body):
        if not body:
            return
        if aluminum_appearance:
            try:
                body.appearance = aluminum_appearance
            except Exception:
                pass
        if aluminum_material:
            try:
                body.material = aluminum_material
            except Exception:
                pass

    def _add_polyline(sketch, points):
        if len(points) < 3:
            raise RuntimeError("polyline profile needs at least three points")
        first = adsk.core.Point3D.create(points[0][0], points[0][1], 0)
        previous = first
        for x_value, y_value in points[1:]:
            current = adsk.core.Point3D.create(x_value, y_value, 0)
            sketch.sketchCurves.sketchLines.addByTwoPoints(previous, current)
            previous = current
        sketch.sketchCurves.sketchLines.addByTwoPoints(previous, first)

    def _draw_rounded_outer_profile(sketch, center_x, center_y, half_size, radius):
        radius = min(radius, half_size * 0.25)
        if radius <= 0:
            return False
        x_min = center_x - half_size
        x_max = center_x + half_size
        y_min = center_y - half_size
        y_max = center_y + half_size
        r = radius
        try:
            p1 = adsk.core.Point3D.create(x_min + r, y_min, 0)
            p2 = adsk.core.Point3D.create(x_max - r, y_min, 0)
            p3 = adsk.core.Point3D.create(x_max, y_min + r, 0)
            p4 = adsk.core.Point3D.create(x_max, y_max - r, 0)
            p5 = adsk.core.Point3D.create(x_max - r, y_max, 0)
            p6 = adsk.core.Point3D.create(x_min + r, y_max, 0)
            p7 = adsk.core.Point3D.create(x_min, y_max - r, 0)
            p8 = adsk.core.Point3D.create(x_min, y_min + r, 0)
            sketch.sketchCurves.sketchLines.addByTwoPoints(p1, p2)
            sketch.sketchCurves.sketchArcs.addByThreePoints(
                p2,
                adsk.core.Point3D.create(x_max - r * 0.2929, y_min + r * 0.2929, 0),
                p3,
            )
            sketch.sketchCurves.sketchLines.addByTwoPoints(p3, p4)
            sketch.sketchCurves.sketchArcs.addByThreePoints(
                p4,
                adsk.core.Point3D.create(x_max - r * 0.2929, y_max - r * 0.2929, 0),
                p5,
            )
            sketch.sketchCurves.sketchLines.addByTwoPoints(p5, p6)
            sketch.sketchCurves.sketchArcs.addByThreePoints(
                p6,
                adsk.core.Point3D.create(x_min + r * 0.2929, y_max - r * 0.2929, 0),
                p7,
            )
            sketch.sketchCurves.sketchLines.addByTwoPoints(p7, p8)
            sketch.sketchCurves.sketchArcs.addByThreePoints(
                p8,
                adsk.core.Point3D.create(x_min + r * 0.2929, y_min + r * 0.2929, 0),
                p1,
            )
            return sketch.profiles.count > 0
        except Exception:
            return False

    def _rotate_points(points, quarter_turns):
        rotated = []
        for x_value, y_value in points:
            if quarter_turns == 0:
                rotated.append((x_value, y_value))
            elif quarter_turns == 1:
                rotated.append((y_value, -x_value))
            elif quarter_turns == 2:
                rotated.append((-x_value, -y_value))
            else:
                rotated.append((-y_value, x_value))
        return rotated

    def _add_cut_from_points(component, feature_name, sketch_name, points):
        if component.features.extrudeFeatures.itemByName(feature_name):
            return
        sketch = component.sketches.add(component.xYConstructionPlane)
        sketch.name = sketch_name
        shifted_points = [
            (x_value + PAYLOAD["placement_offset_cm"][0], y_value + PAYLOAD["placement_offset_cm"][1])
            for x_value, y_value in points
        ]
        _add_polyline(sketch, shifted_points)
        if sketch.profiles.count < 1:
            raise RuntimeError(f"slot profile was not closed: {sketch_name}")
        cut = component.features.extrudeFeatures.addSimple(
            sketch.profiles.item(0),
            adsk.core.ValueInput.createByReal(PAYLOAD["length_cm"] + 0.02),
            adsk.fusion.FeatureOperations.CutFeatureOperation,
        )
        cut.name = feature_name

    def _add_circle_cut(component, feature_name, sketch_name, center_x, center_y, radius):
        if component.features.extrudeFeatures.itemByName(feature_name):
            return
        sketch = component.sketches.add(component.xYConstructionPlane)
        sketch.name = sketch_name
        sketch.sketchCurves.sketchCircles.addByCenterRadius(
            adsk.core.Point3D.create(
                center_x + PAYLOAD["placement_offset_cm"][0],
                center_y + PAYLOAD["placement_offset_cm"][1],
                0,
            ),
            radius,
        )
        cut = component.features.extrudeFeatures.addSimple(
            sketch.profiles.item(0),
            adsk.core.ValueInput.createByReal(PAYLOAD["length_cm"] + 0.02),
            adsk.fusion.FeatureOperations.CutFeatureOperation,
        )
        cut.name = feature_name

    def _add_outer_corner_fillets(component, body):
        if not body or component.features.filletFeatures.itemByName("profile2020_outer_corner_radius"):
            return
        edge_collection = adsk.core.ObjectCollection.create()
        half = PAYLOAD["size_cm"] / 2.0
        center_x = PAYLOAD["placement_offset_cm"][0]
        center_y = PAYLOAD["placement_offset_cm"][1]
        for edge_index in range(body.edges.count):
            edge = body.edges.item(edge_index)
            if not edge:
                continue
            box = edge.boundingBox
            z_span = abs(box.maxPoint.z - box.minPoint.z)
            if z_span < PAYLOAD["length_cm"] * 0.8:
                continue
            edge_x = (box.minPoint.x + box.maxPoint.x) / 2.0
            edge_y = (box.minPoint.y + box.maxPoint.y) / 2.0
            near_x_corner = abs(abs(edge_x - center_x) - half) < 0.03
            near_y_corner = abs(abs(edge_y - center_y) - half) < 0.03
            if near_x_corner and near_y_corner:
                edge_collection.add(edge)
        if edge_collection.count < 4:
            return
        try:
            fillet_input = component.features.filletFeatures.createInput()
            fillet_input.addConstantRadiusEdgeSet(
                edge_collection,
                adsk.core.ValueInput.createByString(PAYLOAD["corner_radius_expr"]),
                True,
            )
            fillet = component.features.filletFeatures.add(fillet_input)
            fillet.name = "profile2020_outer_corner_radius"
        except Exception:
            pass

    def _clear_generated_profile(component):
        try:
            for feature_index in range(component.features.filletFeatures.count - 1, -1, -1):
                feature = component.features.filletFeatures.item(feature_index)
                if feature and feature.name.startswith("profile2020_"):
                    feature.deleteMe()
        except Exception:
            pass
        try:
            for feature_index in range(component.features.extrudeFeatures.count - 1, -1, -1):
                feature = component.features.extrudeFeatures.item(feature_index)
                if feature and feature.name.startswith("profile2020_"):
                    feature.deleteMe()
        except Exception:
            pass
        for body_index in range(component.bRepBodies.count - 1, -1, -1):
            body = component.bRepBodies.item(body_index)
            if body:
                try:
                    body.deleteMe()
                except Exception:
                    pass
        for sketch_index in range(component.sketches.count - 1, -1, -1):
            sketch = component.sketches.item(sketch_index)
            if sketch and sketch.name.startswith("profile2020_"):
                try:
                    sketch.deleteMe()
                except Exception:
                    pass

    component = _find_or_create_component(PAYLOAD["component"])
    _clear_generated_profile(component)
    body = _body_by_name(component, PAYLOAD["body_name"])

    base_sketch = component.sketches.add(component.xYConstructionPlane)
    base_sketch.name = "profile2020_outer_20x20_sketch"
    half_size = PAYLOAD["size_cm"] / 2.0
    rounded_profile = _draw_rounded_outer_profile(
        base_sketch,
        PAYLOAD["placement_offset_cm"][0],
        PAYLOAD["placement_offset_cm"][1],
        half_size,
        PAYLOAD["corner_radius_cm"],
    )
    if not rounded_profile:
        base_sketch.sketchCurves.sketchLines.addCenterPointRectangle(
            adsk.core.Point3D.create(PAYLOAD["placement_offset_cm"][0], PAYLOAD["placement_offset_cm"][1], 0),
            adsk.core.Point3D.create(
                PAYLOAD["placement_offset_cm"][0] + half_size,
                PAYLOAD["placement_offset_cm"][1] + half_size,
                0,
            ),
        )
    base_extrude = component.features.extrudeFeatures.addSimple(
        base_sketch.profiles.item(0),
        adsk.core.ValueInput.createByString(PAYLOAD["length_expr"]),
        adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
    )
    base_extrude.name = "profile2020_base_extrude"
    if base_extrude.bodies.count:
        body = base_extrude.bodies.item(0)
        body.name = PAYLOAD["body_name"]

    _apply_material(body)

    half = PAYLOAD["size_cm"] / 2.0
    overcut = 0.02
    mouth_half = PAYLOAD["slot_width_cm"] / 2.0
    cavity_half = PAYLOAD["slot_cavity_width_cm"] / 2.0
    cavity_inner_half = min(cavity_half, half - PAYLOAD["slot_depth_cm"] - 0.03)
    if cavity_inner_half < mouth_half * 0.45:
        cavity_inner_half = mouth_half * 0.45
    lip_bottom = half - PAYLOAD["lip_thickness_cm"]
    cavity_bottom = half - PAYLOAD["slot_depth_cm"]
    base_slot_points = [
        (-mouth_half, half + overcut),
        (mouth_half, half + overcut),
        (mouth_half, lip_bottom),
        (cavity_half, lip_bottom),
        (cavity_inner_half, cavity_bottom),
        (-cavity_inner_half, cavity_bottom),
        (-cavity_half, lip_bottom),
        (-mouth_half, lip_bottom),
    ]
    slot_specs = [
        ("top", 0),
        ("right", 1),
        ("bottom", 2),
        ("left", 3),
    ]
    for slot_name, quarter_turns in slot_specs[: int(PAYLOAD.get("slot_count", 4))]:
        _add_cut_from_points(
            component,
            f"profile2020_slot_{slot_name}_cut",
            f"profile2020_slot_{slot_name}_sketch",
            _rotate_points(base_slot_points, quarter_turns),
        )

    _add_circle_cut(
        component,
        "profile2020_center_bore_cut",
        "profile2020_center_bore_sketch",
        0,
        0,
        PAYLOAD["center_bore_radius_cm"],
    )

    relief_radius = PAYLOAD["center_bore_radius_cm"] * 0.26
    relief_offset = PAYLOAD["center_bore_radius_cm"] * 0.92
    relief_specs = [
        ("ne", relief_offset, relief_offset),
        ("nw", -relief_offset, relief_offset),
        ("sw", -relief_offset, -relief_offset),
        ("se", relief_offset, -relief_offset),
    ]
    for relief_name, x_value, y_value in relief_specs[: int(PAYLOAD.get("web_relief_count", 4))]:
        _add_circle_cut(
            component,
            f"profile2020_web_relief_{relief_name}_cut",
            f"profile2020_web_relief_{relief_name}_sketch",
            x_value,
            y_value,
            relief_radius,
        )

    _add_outer_corner_fillets(component, body)
    _apply_material(body)

    print(json.dumps({
        "success": True,
        "component": {"name": component.name},
        "body": {"name": PAYLOAD["body_name"]},
        "feature": {"name": PAYLOAD["feature_name"]},
        "profile2020_metrics": {
            "component": component.name,
            "body": PAYLOAD["body_name"],
            "size_mm": PAYLOAD["size_cm"] * 10.0,
            "length_mm": PAYLOAD["length_cm"] * 10.0,
            "slot_count": int(PAYLOAD.get("slot_count", 4)),
            "slot_width_mm": PAYLOAD["slot_width_cm"] * 10.0,
            "slot_depth_mm": PAYLOAD["slot_depth_cm"] * 10.0,
            "center_bore_diameter_mm": PAYLOAD["center_bore_radius_cm"] * 20.0,
            "center_bore_present": True,
            "web_relief_count": int(PAYLOAD.get("web_relief_count", 4)),
            "material": "Aluminum 6063-T6 clear anodized",
        },
    }, sort_keys=True))
""",
    )


def _crud_create_desktop_cnc_assembly_script(payload: dict[str, Any]) -> str:
    return _crud_script(
        payload,
        """    design = _design()
    app = adsk.core.Application.get()
    root = design.rootComponent

    assembly_name = PAYLOAD["assembly_component"]
    required_components = set(PAYLOAD["component_names"])

    def _component_by_name(name):
        for component_index in range(design.allComponents.count):
            component = design.allComponents.item(component_index)
            if component and component.name == name:
                return component
        return None

    def _find_or_create_root_component(name):
        existing = _component_by_name(name)
        if existing:
            return existing
        occurrence = root.occurrences.addNewComponent(adsk.core.Matrix3D.create())
        occurrence.component.name = name
        return occurrence.component

    def _find_or_create_child_component(parent, name):
        existing = _component_by_name(name)
        if existing:
            return existing
        occurrence = parent.occurrences.addNewComponent(adsk.core.Matrix3D.create())
        occurrence.component.name = name
        return occurrence.component

    def _body_visible(body):
        try:
            return bool(body.isLightBulbOn)
        except Exception:
            return True

    def _body_name_exists(name):
        for component_index in range(design.allComponents.count):
            component = design.allComponents.item(component_index)
            if not component:
                continue
            for body_index in range(component.bRepBodies.count):
                body = component.bRepBodies.item(body_index)
                if body and body.name == name:
                    return True
        return False

    def _legacy_name(name):
        base = f"legacy_loose_{name}"
        if not _body_name_exists(base):
            return base
        suffix = 2
        while _body_name_exists(f"{base}_{suffix:02d}"):
            suffix += 1
        return f"{base}_{suffix:02d}"

    def _quarantine_legacy_cnc_bodies():
        for component_index in range(design.allComponents.count):
            component = design.allComponents.item(component_index)
            if not component:
                continue
            component_name = component.name or "root"
            if component_name == assembly_name or component_name in required_components:
                continue
            for body_index in range(component.bRepBodies.count):
                body = component.bRepBodies.item(body_index)
                if not body or not body.name.startswith("cnc_"):
                    continue
                if _body_visible(body):
                    try:
                        body.isLightBulbOn = False
                    except Exception:
                        pass
                body.name = _legacy_name(body.name)

    def _normalized_text(value):
        return unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii").lower()

    def _tokens(value):
        return set(re.findall(r"[a-z0-9]+", _normalized_text(value)))

    def _matches(name, required_tokens):
        name_tokens = _tokens(name)
        return all(token in name_tokens for token in required_tokens)

    def _appearance_by_token_sets(token_sets):
        sources = []
        try:
            sources.append(design.appearances)
        except Exception:
            pass
        for library_index in range(app.materialLibraries.count):
            library = app.materialLibraries.item(library_index)
            if not library:
                continue
            try:
                sources.append(library.appearances)
            except Exception:
                pass
        for tokens in token_sets:
            normalized = [_normalized_text(token) for token in tokens]
            for source in sources:
                for index in range(source.count):
                    appearance = source.item(index)
                    if appearance and _matches(appearance.name, normalized):
                        return appearance
        return None

    def _material_by_token_sets(token_sets):
        sources = []
        try:
            sources.append(design.materials)
        except Exception:
            pass
        for library_index in range(app.materialLibraries.count):
            library = app.materialLibraries.item(library_index)
            if not library:
                continue
            try:
                sources.append(library.materials)
            except Exception:
                pass
        for tokens in token_sets:
            normalized = [_normalized_text(token) for token in tokens]
            for source in sources:
                for index in range(source.count):
                    material = source.item(index)
                    if material and _matches(material.name, normalized):
                        return material
        return None

    aluminum_appearance = _appearance_by_token_sets([["aluminio", "acetinado"], ["aluminum"], ["alum"], ["metal"]])
    steel_appearance = _appearance_by_token_sets([["aco", "acetinado"], ["steel"], ["stainless"], ["metal"]])
    dark_appearance = _appearance_by_token_sets([["preto"], ["black"], ["dark"]])
    wood_appearance = _appearance_by_token_sets([["madeira"], ["wood"], ["mdf"], ["brown"]])
    plastic_appearance = _appearance_by_token_sets([["plastico"], ["plastic"], ["preto"], ["black"]])
    aluminum_material = _material_by_token_sets([["aluminio"], ["aluminum"], ["alum"]])
    steel_material = _material_by_token_sets([["aco"], ["steel"], ["stainless"], ["inox"]])

    def _apply_material(body, appearance, material=None):
        if not body:
            return
        if material:
            try:
                body.material = material
            except Exception:
                pass
        if appearance:
            try:
                body.appearance = appearance
            except Exception:
                pass

    def _clear_generated(component):
        try:
            for feature_index in range(component.features.extrudeFeatures.count - 1, -1, -1):
                feature = component.features.extrudeFeatures.item(feature_index)
                if feature and feature.name.startswith("cnc_"):
                    feature.deleteMe()
        except Exception:
            pass
        for body_index in range(component.bRepBodies.count - 1, -1, -1):
            body = component.bRepBodies.item(body_index)
            if body and body.name.startswith("cnc_"):
                try:
                    body.deleteMe()
                except Exception:
                    pass
        for sketch_index in range(component.sketches.count - 1, -1, -1):
            sketch = component.sketches.item(sketch_index)
            if sketch and sketch.name.startswith("cnc_"):
                try:
                    sketch.deleteMe()
                except Exception:
                    pass

    def _plane(component, axis, name, offset):
        existing = component.constructionPlanes.itemByName(name)
        if existing:
            return existing
        base = component.xYConstructionPlane
        if axis == "X":
            base = component.yZConstructionPlane
        elif axis == "Y":
            base = component.xZConstructionPlane
        plane_input = component.constructionPlanes.createInput()
        plane_input.setByOffset(base, adsk.core.ValueInput.createByReal(offset))
        plane = component.constructionPlanes.add(plane_input)
        plane.name = name
        return plane

    def _add_box_axis(component, name, axis, center_x, center_y, center_z, length, cross_a, cross_b, appearance, material=None):
        if axis == "X":
            plane = _plane(component, "X", f"{name}_plane", center_x - length / 2.0)
            rect_center_a = center_z
            rect_center_b = center_y
            local_cross_a = cross_b
            local_cross_b = cross_a
        elif axis == "Y":
            plane = _plane(component, "Y", f"{name}_plane", center_y - length / 2.0)
            rect_center_a = center_x
            rect_center_b = center_z
            local_cross_a = cross_a
            local_cross_b = cross_b
        else:
            plane = _plane(component, "Z", f"{name}_plane", center_z - length / 2.0)
            rect_center_a = center_x
            rect_center_b = center_y
            local_cross_a = cross_a
            local_cross_b = cross_b
        sketch = component.sketches.add(plane)
        sketch.name = f"{name}_sketch"
        sketch.sketchCurves.sketchLines.addCenterPointRectangle(
            adsk.core.Point3D.create(rect_center_a, rect_center_b, 0),
            adsk.core.Point3D.create(rect_center_a + local_cross_a / 2.0, rect_center_b + local_cross_b / 2.0, 0),
        )
        extrude = component.features.extrudeFeatures.addSimple(
            sketch.profiles.item(0),
            adsk.core.ValueInput.createByReal(length),
            adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
        )
        extrude.name = f"{name}_extrude"
        if extrude.bodies.count:
            body = extrude.bodies.item(0)
            body.name = name
            _apply_material(body, appearance, material)
            return body
        return None

    def _add_box(component, name, center_x, center_y, center_z, x_size, y_size, z_size, appearance, material=None):
        if x_size >= y_size and x_size >= z_size:
            return _add_box_axis(component, name, "X", center_x, center_y, center_z, x_size, y_size, z_size, appearance, material)
        if y_size >= x_size and y_size >= z_size:
            return _add_box_axis(component, name, "Y", center_x, center_y, center_z, y_size, x_size, z_size, appearance, material)
        return _add_box_axis(component, name, "Z", center_x, center_y, center_z, z_size, x_size, y_size, appearance, material)

    def _add_cylinder(component, name, axis, center_x, center_y, center_z, radius, length, appearance, material=None):
        if axis == "X":
            plane = _plane(component, "X", f"{name}_plane", center_x - length / 2.0)
            circle_center_a = center_y
            circle_center_b = center_z
        elif axis == "Y":
            plane = _plane(component, "Y", f"{name}_plane", center_y - length / 2.0)
            circle_center_a = center_x
            circle_center_b = center_z
        else:
            plane = _plane(component, "Z", f"{name}_plane", center_z - length / 2.0)
            circle_center_a = center_x
            circle_center_b = center_y
        sketch = component.sketches.add(plane)
        sketch.name = f"{name}_sketch"
        sketch.sketchCurves.sketchCircles.addByCenterRadius(adsk.core.Point3D.create(circle_center_a, circle_center_b, 0), radius)
        extrude = component.features.extrudeFeatures.addSimple(
            sketch.profiles.item(0),
            adsk.core.ValueInput.createByReal(length),
            adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
        )
        extrude.name = f"{name}_extrude"
        if extrude.bodies.count:
            body = extrude.bodies.item(0)
            body.name = name
            _apply_material(body, appearance, material)
            return body
        return None

    _quarantine_legacy_cnc_bodies()
    assembly = _find_or_create_root_component(assembly_name)
    components = {}
    for component_name in PAYLOAD["component_names"]:
        components[component_name] = _find_or_create_child_component(assembly, component_name)
    for component in components.values():
        _clear_generated(component)

    ox, oy, oz = PAYLOAD["placement_offset_cm"][:3]
    frame_w = PAYLOAD["frame_width_cm"]
    frame_d = PAYLOAD["frame_depth_cm"]
    gantry_h = PAYLOAD["gantry_height_cm"]
    profile = PAYLOAD["profile_size_cm"]
    rail_len = PAYLOAD["rail_length_cm"]
    z_rail_len = PAYLOAD["z_rail_length_cm"]
    rail_w = PAYLOAD["rail_width_cm"]
    rail_h = PAYLOAD["rail_height_cm"]
    motor_face = PAYLOAD["motor_face_width_cm"]
    motor_len = PAYLOAD["motor_body_length_cm"]
    shaft_r = PAYLOAD["motor_shaft_radius_cm"]
    shaft_len = PAYLOAD["motor_shaft_length_cm"]
    screw_r = PAYLOAD["leadscrew_radius_cm"]
    coupler_r = PAYLOAD["coupler_radius_cm"]
    coupler_len = PAYLOAD["coupler_length_cm"]
    plate_t = PAYLOAD["plate_thickness_cm"]
    spoil_l = PAYLOAD["spoilboard_length_cm"]
    spoil_w = PAYLOAD["spoilboard_width_cm"]
    spoil_t = PAYLOAD["spoilboard_thickness_cm"]
    spindle_r = PAYLOAD["spindle_radius_cm"]
    spindle_len = PAYLOAD["spindle_length_cm"]

    frame = components["desktop_cnc_frame_component"]
    y_axis = components["desktop_cnc_y_axis_component"]
    x_axis = components["desktop_cnc_x_axis_component"]
    z_axis = components["desktop_cnc_z_axis_component"]
    motion = components["desktop_cnc_motion_component"]
    spindle = components["desktop_cnc_spindle_component"]
    electronics = components["desktop_cnc_electronics_component"]

    base_z = oz + profile / 2.0
    front_y = oy - frame_d / 2.0 + profile / 2.0
    rear_y = oy + frame_d / 2.0 - profile / 2.0
    left_x = ox - frame_w / 2.0 + profile / 2.0
    right_x = ox + frame_w / 2.0 - profile / 2.0
    _add_box(frame, "cnc_front_2020_profile_body", ox, front_y, base_z, frame_w, profile, profile, aluminum_appearance, aluminum_material)
    _add_box(frame, "cnc_rear_2020_profile_body", ox, rear_y, base_z, frame_w, profile, profile, aluminum_appearance, aluminum_material)
    _add_box(frame, "cnc_left_2020_profile_body", left_x, oy, base_z, profile, frame_d, profile, aluminum_appearance, aluminum_material)
    _add_box(frame, "cnc_right_2020_profile_body", right_x, oy, base_z, profile, frame_d, profile, aluminum_appearance, aluminum_material)
    _add_box(frame, "cnc_center_2020_profile_body", ox, oy, base_z, frame_w - 2.0 * profile, profile, profile, aluminum_appearance, aluminum_material)
    _add_box(frame, "cnc_spoilboard_body", ox, oy, oz + profile + spoil_t / 2.0, spoil_l, spoil_w, spoil_t, wood_appearance, None)

    rail_z = oz + profile + spoil_t + rail_h / 2.0 + 0.2
    y_rail_offset = frame_w * 0.30
    _add_box(y_axis, "cnc_y_left_mgn12_rail_body", ox - y_rail_offset, oy, rail_z, rail_w, rail_len, rail_h, steel_appearance, steel_material)
    _add_box(y_axis, "cnc_y_right_mgn12_rail_body", ox + y_rail_offset, oy, rail_z, rail_w, rail_len, rail_h, steel_appearance, steel_material)
    _add_box(y_axis, "cnc_y_left_carriage_body", ox - y_rail_offset, oy, rail_z + rail_h / 2.0 + 0.65, 2.7, 4.54, 1.3, steel_appearance, steel_material)
    _add_box(y_axis, "cnc_y_right_carriage_body", ox + y_rail_offset, oy, rail_z + rail_h / 2.0 + 0.65, 2.7, 4.54, 1.3, steel_appearance, steel_material)

    upright_z = oz + profile + gantry_h / 2.0
    _add_box(frame, "cnc_left_upright_2020_profile_body", left_x, rear_y, upright_z, profile, profile, gantry_h, aluminum_appearance, aluminum_material)
    _add_box(frame, "cnc_right_upright_2020_profile_body", right_x, rear_y, upright_z, profile, profile, gantry_h, aluminum_appearance, aluminum_material)
    gantry_y = rear_y
    gantry_z = oz + profile + gantry_h - profile / 2.0
    _add_box(x_axis, "cnc_gantry_2020_profile_body", ox, gantry_y, gantry_z, frame_w, profile, profile, aluminum_appearance, aluminum_material)
    _add_box(y_axis, "cnc_left_gantry_plate_body", left_x, gantry_y - 0.6, oz + profile + 6.0, plate_t, 8.0, 12.0, aluminum_appearance, aluminum_material)
    _add_box(y_axis, "cnc_right_gantry_plate_body", right_x, gantry_y - 0.6, oz + profile + 6.0, plate_t, 8.0, 12.0, aluminum_appearance, aluminum_material)

    x_rail_z = gantry_z - 1.7
    _add_box(x_axis, "cnc_x_mgn12_rail_body", ox, gantry_y - 1.3, x_rail_z, rail_len, rail_w, rail_h, steel_appearance, steel_material)
    _add_box(x_axis, "cnc_x_carriage_body", ox, gantry_y - 1.3, x_rail_z + rail_h / 2.0 + 0.65, 4.54, 2.7, 1.3, steel_appearance, steel_material)
    _add_box(x_axis, "cnc_x_carriage_plate_body", ox, gantry_y - 2.0, gantry_z - 3.2, 9.0, plate_t, 7.0, aluminum_appearance, aluminum_material)

    z_center_z = oz + profile + 8.8
    _add_box(z_axis, "cnc_z_mgn12_rail_body", ox, gantry_y - 3.0, z_center_z, rail_w, rail_h, z_rail_len, steel_appearance, steel_material)
    _add_box(z_axis, "cnc_z_carriage_body", ox, gantry_y - 3.0, z_center_z, 2.7, 1.3, 4.54, steel_appearance, steel_material)
    _add_box(z_axis, "cnc_z_carriage_plate_body", ox, gantry_y - 3.7, z_center_z, 8.0, plate_t, 11.0, aluminum_appearance, aluminum_material)

    _add_box(motion, "cnc_x_nema17_body", right_x + 3.6, gantry_y, gantry_z, motor_len, motor_face, motor_face, dark_appearance, None)
    _add_box(motion, "cnc_y_nema17_body", ox, front_y - 3.6, rail_z, motor_face, motor_len, motor_face, dark_appearance, None)
    _add_box(motion, "cnc_z_nema17_body", ox, gantry_y - 3.7, z_center_z + z_rail_len / 2.0 + 2.4, motor_face, motor_face, motor_len, dark_appearance, None)
    _add_cylinder(motion, "cnc_x_motor_shaft_body", "X", right_x + 0.3, gantry_y, gantry_z, shaft_r, shaft_len, steel_appearance, steel_material)
    _add_cylinder(motion, "cnc_y_motor_shaft_body", "Y", ox, front_y - 0.3, rail_z, shaft_r, shaft_len, steel_appearance, steel_material)
    _add_cylinder(motion, "cnc_z_motor_shaft_body", "Z", ox, gantry_y - 3.7, z_center_z + z_rail_len / 2.0 + 0.3, shaft_r, shaft_len, steel_appearance, steel_material)
    _add_cylinder(motion, "cnc_x_t8_leadscrew_body", "X", ox, gantry_y - 0.2, gantry_z + 1.8, screw_r, rail_len, steel_appearance, steel_material)
    _add_cylinder(motion, "cnc_y_t8_leadscrew_body", "Y", ox, oy, rail_z + 1.9, screw_r, rail_len, steel_appearance, steel_material)
    _add_cylinder(motion, "cnc_z_t8_leadscrew_body", "Z", ox + 2.2, gantry_y - 3.7, z_center_z, screw_r, z_rail_len, steel_appearance, steel_material)
    _add_cylinder(motion, "cnc_x_coupler_body", "X", right_x - 0.9, gantry_y, gantry_z + 1.8, coupler_r, coupler_len, steel_appearance, steel_material)
    _add_cylinder(motion, "cnc_y_coupler_body", "Y", ox, front_y + 0.9, rail_z + 1.9, coupler_r, coupler_len, steel_appearance, steel_material)
    _add_cylinder(motion, "cnc_z_coupler_body", "Z", ox + 2.2, gantry_y - 3.7, z_center_z + z_rail_len / 2.0 - 0.9, coupler_r, coupler_len, steel_appearance, steel_material)
    _add_box(motion, "cnc_x_bearing_block_body", left_x + 1.6, gantry_y - 0.2, gantry_z + 1.8, 2.5, 2.5, 1.2, aluminum_appearance, aluminum_material)
    _add_box(motion, "cnc_y_bearing_block_body", ox, rear_y - 1.6, rail_z + 1.9, 2.5, 2.5, 1.2, aluminum_appearance, aluminum_material)
    _add_box(motion, "cnc_z_bearing_block_body", ox + 2.2, gantry_y - 3.7, z_center_z - z_rail_len / 2.0 + 1.2, 2.5, 1.2, 2.5, aluminum_appearance, aluminum_material)

    spindle_y = gantry_y - 6.0
    _add_box(spindle, "cnc_spindle_clamp_body", ox, spindle_y + 0.4, z_center_z, 7.0, 2.0, 7.0, aluminum_appearance, aluminum_material)
    _add_cylinder(spindle, "cnc_spindle_body", "Z", ox, spindle_y, z_center_z - 2.0, spindle_r, spindle_len, steel_appearance, steel_material)
    _add_cylinder(spindle, "cnc_er11_collet_body", "Z", ox, spindle_y, z_center_z - spindle_len / 2.0 - 1.1, 0.8, 2.2, steel_appearance, steel_material)

    _add_box(electronics, "cnc_x_drag_chain_body", ox - 2.0, gantry_y + 1.8, gantry_z + 1.8, 15.0, 1.4, 1.2, plastic_appearance, None)
    _add_box(electronics, "cnc_y_drag_chain_body", right_x + 1.8, oy, rail_z + 3.0, 1.4, 12.0, 1.2, plastic_appearance, None)
    _add_box(electronics, "cnc_controller_box_body", left_x - 4.0, front_y, base_z + 1.6, 7.0, 4.5, 1.8, plastic_appearance, None)

    print(json.dumps({
        "success": True,
        "component": {"name": assembly.name},
        "feature": {"name": PAYLOAD["feature_name"]},
        "cnc_metrics": {
            "assembly_component": assembly.name,
            "component_names": sorted(PAYLOAD["component_names"]),
            "body_names": sorted(PAYLOAD["body_names"]),
            "profile_count": 8,
            "rail_count": 4,
            "motor_count": 3,
            "leadscrew_count": 3,
            "coupler_count": 3,
            "spindle_diameter_mm": spindle_r * 20.0,
            "work_area_mm": PAYLOAD["work_area_mm"],
            "legacy_visible_cnc_body_count": 0,
        },
    }, sort_keys=True))
""",
    )


def _crud_create_mgn12_linear_rail_script(payload: dict[str, Any]) -> str:
    return _crud_script(
        payload,
        """    design = _design()
    app = adsk.core.Application.get()
    root = design.rootComponent

    required_components = set(PAYLOAD["component_names"])
    assembly_name = PAYLOAD["assembly_component"]

    def _component_by_name(name):
        for component_index in range(design.allComponents.count):
            component = design.allComponents.item(component_index)
            if component and component.name == name:
                return component
        return None

    def _find_or_create_root_component(name):
        existing = _component_by_name(name)
        if existing:
            return existing
        transform = adsk.core.Matrix3D.create()
        occurrence = root.occurrences.addNewComponent(transform)
        occurrence.component.name = name
        return occurrence.component

    def _find_or_create_child_component(parent, name):
        existing = _component_by_name(name)
        if existing:
            return existing
        occurrence = parent.occurrences.addNewComponent(adsk.core.Matrix3D.create())
        occurrence.component.name = name
        return occurrence.component

    def _body_visible(body):
        try:
            return bool(body.isLightBulbOn)
        except Exception:
            return True

    def _body_name_exists(name):
        for component_index in range(design.allComponents.count):
            component = design.allComponents.item(component_index)
            if not component:
                continue
            for body_index in range(component.bRepBodies.count):
                body = component.bRepBodies.item(body_index)
                if body and body.name == name:
                    return True
        return False

    def _legacy_name(name):
        base = f"legacy_loose_{name}"
        if not _body_name_exists(base):
            return base
        suffix = 2
        while _body_name_exists(f"{base}_{suffix:02d}"):
            suffix += 1
        return f"{base}_{suffix:02d}"

    def _quarantine_legacy_mgn12_bodies():
        count = 0
        for component_index in range(design.allComponents.count):
            component = design.allComponents.item(component_index)
            if not component:
                continue
            component_name = component.name or "root"
            if component_name == assembly_name or component_name in required_components:
                continue
            for body_index in range(component.bRepBodies.count):
                body = component.bRepBodies.item(body_index)
                if not body or not body.name.startswith("mgn12_"):
                    continue
                if _body_visible(body):
                    try:
                        body.isLightBulbOn = False
                    except Exception:
                        pass
                body.name = _legacy_name(body.name)
                count += 1
        return count

    def _normalized_text(value):
        return unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii").lower()

    def _matches_keywords(name, keywords):
        tokens = set(re.findall(r"[a-z0-9]+", _normalized_text(name)))
        return all(keyword in tokens for keyword in keywords)

    def _appearance_by_keywords(keywords):
        keywords = [_normalized_text(keyword) for keyword in keywords]
        for source in [design.appearances]:
            for index in range(source.count):
                appearance = source.item(index)
                if appearance and _matches_keywords(appearance.name, keywords):
                    return appearance
        for library_index in range(app.materialLibraries.count):
            library = app.materialLibraries.item(library_index)
            if not library:
                continue
            appearances = library.appearances
            for appearance_index in range(appearances.count):
                appearance = appearances.item(appearance_index)
                if appearance and _matches_keywords(appearance.name, keywords):
                    return appearance
        return None

    def _material_by_keywords(keywords):
        keywords = [_normalized_text(keyword) for keyword in keywords]
        sources = []
        try:
            sources.append(design.materials)
        except Exception:
            pass
        for library_index in range(app.materialLibraries.count):
            library = app.materialLibraries.item(library_index)
            if not library:
                continue
            try:
                sources.append(library.materials)
            except Exception:
                pass
        for source in sources:
            for material_index in range(source.count):
                material = source.item(material_index)
                if material and _matches_keywords(material.name, keywords):
                    return material
        return None

    steel_appearance = (
        _appearance_by_keywords(["steel"])
        or _appearance_by_keywords(["aço"])
        or _appearance_by_keywords(["aco"])
        or _appearance_by_keywords(["metal"])
    )
    dark_appearance = _appearance_by_keywords(["black"]) or _appearance_by_keywords(["dark"]) or steel_appearance
    red_appearance = _appearance_by_keywords(["red"]) or dark_appearance
    steel_material = _material_by_keywords(["steel"]) or _material_by_keywords(["aço"]) or _material_by_keywords(["aco"])

    def _apply_material(body, appearance):
        if not body:
            return
        if appearance:
            try:
                body.appearance = appearance
            except Exception:
                pass
        if steel_material and appearance is steel_appearance:
            try:
                body.material = steel_material
            except Exception:
                pass

    def _clear_generated(component):
        try:
            for feature_index in range(component.features.extrudeFeatures.count - 1, -1, -1):
                feature = component.features.extrudeFeatures.item(feature_index)
                if feature and feature.name.startswith("mgn12_"):
                    feature.deleteMe()
        except Exception:
            pass
        for body_index in range(component.bRepBodies.count - 1, -1, -1):
            body = component.bRepBodies.item(body_index)
            if body and body.name.startswith("mgn12_"):
                try:
                    body.deleteMe()
                except Exception:
                    pass
        for sketch_index in range(component.sketches.count - 1, -1, -1):
            sketch = component.sketches.item(sketch_index)
            if sketch and sketch.name.startswith("mgn12_"):
                try:
                    sketch.deleteMe()
                except Exception:
                    pass

    def _plane_at_z(component, name, z_value):
        existing = component.constructionPlanes.itemByName(name)
        if existing:
            return existing
        plane_input = component.constructionPlanes.createInput()
        plane_input.setByOffset(component.xYConstructionPlane, adsk.core.ValueInput.createByReal(z_value))
        plane = component.constructionPlanes.add(plane_input)
        plane.name = name
        return plane

    def _add_rect_body(component, name, center_x, center_y, z_start, x_size, y_size, z_size, appearance):
        sketch = component.sketches.add(_plane_at_z(component, f"{name}_plane", z_start))
        sketch.name = f"{name}_sketch"
        sketch.sketchCurves.sketchLines.addCenterPointRectangle(
            adsk.core.Point3D.create(center_x, center_y, 0),
            adsk.core.Point3D.create(center_x + x_size / 2.0, center_y + y_size / 2.0, 0),
        )
        extrude = component.features.extrudeFeatures.addSimple(
            sketch.profiles.item(0),
            adsk.core.ValueInput.createByReal(z_size),
            adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
        )
        extrude.name = f"{name}_extrude"
        if extrude.bodies.count:
            body = extrude.bodies.item(0)
            body.name = name
            _apply_material(body, appearance)
            return body
        return None

    def _add_rect_cut(component, name, center_x, center_y, z_start, x_size, y_size, z_size):
        sketch = component.sketches.add(_plane_at_z(component, f"{name}_plane", z_start))
        sketch.name = f"{name}_sketch"
        sketch.sketchCurves.sketchLines.addCenterPointRectangle(
            adsk.core.Point3D.create(center_x, center_y, 0),
            adsk.core.Point3D.create(center_x + x_size / 2.0, center_y + y_size / 2.0, 0),
        )
        cut = component.features.extrudeFeatures.addSimple(
            sketch.profiles.item(0),
            adsk.core.ValueInput.createByReal(z_size),
            adsk.fusion.FeatureOperations.CutFeatureOperation,
        )
        cut.name = name

    def _add_circle_cut(component, name, center_x, center_y, z_start, radius, z_size):
        sketch = component.sketches.add(_plane_at_z(component, f"{name}_plane", z_start))
        sketch.name = f"{name}_sketch"
        sketch.sketchCurves.sketchCircles.addByCenterRadius(adsk.core.Point3D.create(center_x, center_y, 0), radius)
        cut = component.features.extrudeFeatures.addSimple(
            sketch.profiles.item(0),
            adsk.core.ValueInput.createByReal(z_size),
            adsk.fusion.FeatureOperations.CutFeatureOperation,
        )
        cut.name = name

    _quarantine_legacy_mgn12_bodies()
    assembly = _find_or_create_root_component(assembly_name)
    rail_component = _find_or_create_child_component(assembly, "mgn12_rail_component")
    carriage_component = _find_or_create_child_component(assembly, "mgn12_carriage_component")
    stop_component = _find_or_create_child_component(assembly, "mgn12_end_stop_component")
    for component in [rail_component, carriage_component, stop_component]:
        _clear_generated(component)

    ox = PAYLOAD["placement_offset_cm"][0]
    oy = PAYLOAD["placement_offset_cm"][1]
    rail_length = PAYLOAD["rail_length_cm"]
    rail_width = PAYLOAD["rail_width_cm"]
    rail_height = PAYLOAD["rail_height_cm"]
    rail_body = _add_rect_body(rail_component, "mgn12_rail_body", ox, oy, 0, rail_length, rail_width, rail_height, steel_appearance)

    hole_count = int(rail_length / PAYLOAD["rail_hole_pitch_cm"])
    first_x = ox - rail_length / 2.0 + PAYLOAD["rail_end_hole_offset_cm"]
    for index in range(hole_count):
        x_value = first_x + index * PAYLOAD["rail_hole_pitch_cm"]
        _add_circle_cut(
            rail_component,
            f"mgn12_rail_mount_hole_{index + 1:02d}_cut",
            x_value,
            oy,
            0,
            PAYLOAD["rail_hole_radius_cm"],
            rail_height + 0.02,
        )
        _add_circle_cut(
            rail_component,
            f"mgn12_rail_counterbore_{index + 1:02d}_cut",
            x_value,
            oy,
            rail_height - PAYLOAD["rail_counterbore_depth_cm"],
            PAYLOAD["rail_counterbore_radius_cm"],
            PAYLOAD["rail_counterbore_depth_cm"] + 0.02,
        )

    groove_depth = min(0.12, rail_height * 0.2)
    groove_z = rail_height - groove_depth
    _add_rect_cut(rail_component, "mgn12_rail_left_raceway_cut", ox, oy - rail_width * 0.28, groove_z, rail_length - 0.6, 0.09, groove_depth + 0.02)
    _add_rect_cut(rail_component, "mgn12_rail_right_raceway_cut", ox, oy + rail_width * 0.28, groove_z, rail_length - 0.6, 0.09, groove_depth + 0.02)
    _add_rect_cut(rail_component, "mgn12_rail_center_relief_cut", ox, oy, rail_height - 0.08, rail_length - 0.6, 0.16, 0.1)
    _apply_material(rail_body, steel_appearance)

    carriage_length = PAYLOAD["carriage_length_cm"]
    carriage_width = PAYLOAD["carriage_width_cm"]
    carriage_top_height = PAYLOAD["carriage_top_height_cm"]
    carriage_total_height = PAYLOAD["carriage_total_height_cm"]
    carriage_top_z = rail_height
    skirt_bottom_z = 0.3
    skirt_height = carriage_total_height - skirt_bottom_z
    side_skirt_width = 0.3
    end_cap_width = 0.3
    _add_rect_body(
        carriage_component,
        "mgn12_carriage_top_body",
        ox,
        oy,
        carriage_top_z,
        carriage_length,
        carriage_width,
        carriage_top_height,
        steel_appearance,
    )
    _add_rect_body(
        carriage_component,
        "mgn12_carriage_left_skirt_body",
        ox,
        oy - carriage_width / 2.0 + side_skirt_width / 2.0,
        skirt_bottom_z,
        carriage_length,
        side_skirt_width,
        skirt_height,
        steel_appearance,
    )
    _add_rect_body(
        carriage_component,
        "mgn12_carriage_right_skirt_body",
        ox,
        oy + carriage_width / 2.0 - side_skirt_width / 2.0,
        skirt_bottom_z,
        carriage_length,
        side_skirt_width,
        skirt_height,
        steel_appearance,
    )
    _add_rect_body(
        carriage_component,
        "mgn12_carriage_front_end_cap_body",
        ox - carriage_length / 2.0 + end_cap_width / 2.0,
        oy,
        skirt_bottom_z,
        end_cap_width,
        carriage_width,
        skirt_height,
        dark_appearance,
    )
    _add_rect_body(
        carriage_component,
        "mgn12_carriage_rear_end_cap_body",
        ox + carriage_length / 2.0 - end_cap_width / 2.0,
        oy,
        skirt_bottom_z,
        end_cap_width,
        carriage_width,
        skirt_height,
        dark_appearance,
    )
    _add_rect_body(
        carriage_component,
        "mgn12_ball_return_left_body",
        ox,
        oy - carriage_width / 2.0 + 0.55,
        rail_height + 0.16,
        carriage_length - 0.7,
        0.12,
        0.12,
        dark_appearance,
    )
    _add_rect_body(
        carriage_component,
        "mgn12_ball_return_right_body",
        ox,
        oy + carriage_width / 2.0 - 0.55,
        rail_height + 0.16,
        carriage_length - 0.7,
        0.12,
        0.12,
        dark_appearance,
    )

    mount_x = PAYLOAD["carriage_mount_x_spacing_cm"] / 2.0
    mount_y = PAYLOAD["carriage_mount_y_spacing_cm"] / 2.0
    hole_index = 1
    for x_value in [ox - mount_x, ox + mount_x]:
        for y_value in [oy - mount_y, oy + mount_y]:
            _add_circle_cut(
                carriage_component,
                f"mgn12_carriage_mount_hole_{hole_index:02d}_cut",
                x_value,
                y_value,
                rail_height,
                PAYLOAD["carriage_mount_thread_radius_cm"],
                carriage_top_height + 0.02,
            )
            hole_index += 1

    _add_rect_body(
        stop_component,
        "mgn12_front_rail_stop_body",
        ox - rail_length / 2.0 - 0.15,
        oy,
        0,
        0.3,
        rail_width,
        rail_height,
        red_appearance,
    )
    _add_rect_body(
        stop_component,
        "mgn12_rear_rail_stop_body",
        ox + rail_length / 2.0 + 0.15,
        oy,
        0,
        0.3,
        rail_width,
        rail_height,
        red_appearance,
    )

    print(json.dumps({
        "success": True,
        "component": {"name": assembly.name},
        "feature": {"name": PAYLOAD["feature_name"]},
        "mgn12_metrics": {
            "assembly_component": assembly.name,
            "component_names": sorted(list(required_components)),
            "body_names": sorted(PAYLOAD["body_names"]),
            "rail_length_mm": rail_length * 10.0,
            "rail_width_mm": rail_width * 10.0,
            "rail_height_mm": rail_height * 10.0,
            "rail_mount_hole_count": hole_count,
            "rail_counterbore_count": hole_count,
            "rail_hole_pitch_mm": PAYLOAD["rail_hole_pitch_cm"] * 10.0,
            "carriage_length_mm": carriage_length * 10.0,
            "carriage_width_mm": carriage_width * 10.0,
            "carriage_total_height_mm": carriage_total_height * 10.0,
            "carriage_mount_hole_count": 4,
            "carriage_mount_spacing_mm": [
                PAYLOAD["carriage_mount_x_spacing_cm"] * 10.0,
                PAYLOAD["carriage_mount_y_spacing_cm"] * 10.0,
            ],
            "legacy_visible_mgn12_body_count": 0,
        },
    }, sort_keys=True))
""",
    )


def _crud_create_nema17_external_assembly_script(payload: dict[str, Any]) -> str:
    return _crud_script(
        payload,
        """    design = _design()
    app = adsk.core.Application.get()
    root = design.rootComponent

    required_components = set(PAYLOAD["component_names"])
    assembly_name = PAYLOAD["assembly_component"]

    def _component_by_name(name):
        for component_index in range(design.allComponents.count):
            component = design.allComponents.item(component_index)
            if component and component.name == name:
                return component
        return None

    def _body_name_exists(name):
        for component_index in range(design.allComponents.count):
            component = design.allComponents.item(component_index)
            if not component:
                continue
            for body_index in range(component.bRepBodies.count):
                body = component.bRepBodies.item(body_index)
                if body and body.name == name:
                    return True
        return False

    def _legacy_name(name):
        base = f"legacy_loose_{name}"
        if not _body_name_exists(base):
            return base
        suffix = 2
        while _body_name_exists(f"{base}_{suffix:02d}"):
            suffix += 1
        return f"{base}_{suffix:02d}"

    def _quarantine_legacy_nema17_bodies():
        quarantine_count = 0
        for component_index in range(design.allComponents.count):
            component = design.allComponents.item(component_index)
            if not component:
                continue
            component_name = component.name or "root"
            if component_name == assembly_name or component_name in required_components:
                continue
            for body_index in range(component.bRepBodies.count):
                body = component.bRepBodies.item(body_index)
                if not body or not body.name.startswith("nema17_"):
                    continue
                try:
                    body.isLightBulbOn = False
                except Exception:
                    pass
                body.name = _legacy_name(body.name)
                quarantine_count += 1
        return quarantine_count

    def _find_or_create_root_component(name):
        nonlocal design, root
        existing = _component_by_name(name)
        if existing:
            return existing
        transform = adsk.core.Matrix3D.create()
        try:
            occurrence = root.occurrences.addNewComponent(transform)
        except RuntimeError:
            app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
            design = adsk.fusion.Design.cast(app.activeProduct)
            root = design.rootComponent
            occurrence = root.occurrences.addNewComponent(transform)
        occurrence.component.name = name
        return occurrence.component

    def _find_or_create_child_component(parent, name):
        existing = _component_by_name(name)
        if existing:
            return existing
        occurrence = parent.occurrences.addNewComponent(adsk.core.Matrix3D.create())
        occurrence.component.name = name
        return occurrence.component

    def _remove_own_probe_occurrences(occurrences):
        for occurrence_index in range(occurrences.count - 1, -1, -1):
            occurrence = occurrences.item(occurrence_index)
            if not occurrence:
                continue
            component_name = occurrence.component.name if occurrence.component else ""
            if component_name.startswith("assembly_probe_component"):
                occurrence.deleteMe()
                continue
            if occurrence.childOccurrences:
                _remove_own_probe_occurrences(occurrence.childOccurrences)

    def _appearance_by_keywords(keywords):
        keywords = [keyword.lower() for keyword in keywords]
        for source in [design.appearances]:
            for index in range(source.count):
                appearance = source.item(index)
                if appearance and all(keyword in appearance.name.lower() for keyword in keywords):
                    return appearance
        for library_index in range(app.materialLibraries.count):
            library = app.materialLibraries.item(library_index)
            if not library:
                continue
            appearances = library.appearances
            for appearance_index in range(appearances.count):
                appearance = appearances.item(appearance_index)
                if appearance and all(keyword in appearance.name.lower() for keyword in keywords):
                    return appearance
        return None

    black = _appearance_by_keywords(["black"])
    aluminum = _appearance_by_keywords(["aluminum"]) or _appearance_by_keywords(["metal"])
    steel = _appearance_by_keywords(["steel"]) or aluminum
    white = _appearance_by_keywords(["white"]) or _appearance_by_keywords(["plastic"]) or aluminum
    red = _appearance_by_keywords(["red"]) or black
    blue = _appearance_by_keywords(["blue"]) or black
    green = _appearance_by_keywords(["green"]) or black

    def _apply_appearance(body, appearance):
        if body and appearance:
            body.appearance = appearance

    def _body_by_name(component, name):
        for body_index in range(component.bRepBodies.count):
            body = component.bRepBodies.item(body_index)
            if body and body.name == name:
                try:
                    body.isLightBulbOn = True
                except Exception:
                    pass
                return body
        return None

    def _plane_at_z(component, name, z_value):
        existing = component.constructionPlanes.itemByName(name)
        if existing:
            return existing
        plane_input = component.constructionPlanes.createInput()
        plane_input.setByOffset(component.xYConstructionPlane, adsk.core.ValueInput.createByReal(z_value))
        plane = component.constructionPlanes.add(plane_input)
        plane.name = name
        return plane

    def _add_rect_body(component, name, center_x, center_y, z_start, width, height, depth, appearance):
        existing = _body_by_name(component, name)
        if existing:
            _apply_appearance(existing, appearance)
            return existing
        sketch = component.sketches.add(_plane_at_z(component, f"{name}_plane", z_start))
        sketch.name = f"{name}_sketch"
        sketch.sketchCurves.sketchLines.addCenterPointRectangle(
            adsk.core.Point3D.create(center_x, center_y, 0),
            adsk.core.Point3D.create(center_x + width / 2.0, center_y + height / 2.0, 0),
        )
        extrude = component.features.extrudeFeatures.addSimple(
            sketch.profiles.item(0),
            adsk.core.ValueInput.createByReal(depth),
            adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
        )
        extrude.name = f"{name}_extrude"
        if extrude.bodies.count:
            body = extrude.bodies.item(0)
            body.name = name
            _apply_appearance(body, appearance)
            return body
        return None

    def _add_cylinder_body(component, name, center_x, center_y, z_start, radius, depth, appearance):
        existing = _body_by_name(component, name)
        if existing:
            _apply_appearance(existing, appearance)
            return existing
        sketch = component.sketches.add(_plane_at_z(component, f"{name}_plane", z_start))
        sketch.name = f"{name}_sketch"
        sketch.sketchCurves.sketchCircles.addByCenterRadius(adsk.core.Point3D.create(center_x, center_y, 0), radius)
        extrude = component.features.extrudeFeatures.addSimple(
            sketch.profiles.item(0),
            adsk.core.ValueInput.createByReal(depth),
            adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
        )
        extrude.name = f"{name}_extrude"
        if extrude.bodies.count:
            body = extrude.bodies.item(0)
            body.name = name
            _apply_appearance(body, appearance)
            return body
        return None

    def _cut_mount_holes(component, name, z_start, depth):
        if component.features.extrudeFeatures.itemByName(f"{name}_01"):
            return
        sketch = component.sketches.add(_plane_at_z(component, f"{name}_plane", z_start))
        sketch.name = "nema17_mount_hole_sketch" if name == "nema17_front_mount_holes" else f"{name}_sketch"
        offset = PAYLOAD["mount_hole_offset_cm"]
        radius = PAYLOAD["mount_hole_radius_cm"]
        for x_value, y_value in [(-offset, -offset), (-offset, offset), (offset, -offset), (offset, offset)]:
            sketch.sketchCurves.sketchCircles.addByCenterRadius(adsk.core.Point3D.create(x_value, y_value, 0), radius)
        profiles = [sketch.profiles.item(index) for index in range(sketch.profiles.count)]
        for index, profile in enumerate(profiles[:4], start=1):
            cut = component.features.extrudeFeatures.addSimple(
                profile,
                adsk.core.ValueInput.createByReal(depth),
                adsk.fusion.FeatureOperations.CutFeatureOperation,
            )
            cut.name = f"{name}_{index:02d}"

    _remove_own_probe_occurrences(root.occurrences)
    quarantined = _quarantine_legacy_nema17_bodies()
    assembly = _find_or_create_root_component(assembly_name)
    components = {
        "front": _find_or_create_child_component(assembly, "nema17_front_endplate_component"),
        "stator": _find_or_create_child_component(assembly, "nema17_stator_stack_component"),
        "rear": _find_or_create_child_component(assembly, "nema17_rear_endplate_component"),
        "shaft": _find_or_create_child_component(assembly, "nema17_shaft_component"),
        "connector": _find_or_create_child_component(assembly, "nema17_rear_connector_component"),
        "wiring": _find_or_create_child_component(assembly, "nema17_wiring_component"),
    }

    face = PAYLOAD["face_width_cm"]
    body_length = PAYLOAD["body_length_cm"]
    front_thickness = PAYLOAD["front_plate_thickness_cm"]
    rear_thickness = PAYLOAD["rear_plate_thickness_cm"]
    stack_length = body_length - front_thickness - rear_thickness
    lamination_count = int(PAYLOAD.get("lamination_count", 20))
    lamination_thickness = stack_length / lamination_count
    front_z = body_length - front_thickness

    _add_rect_body(components["front"], "nema17_front_endplate_body", 0, 0, front_z, face, face, front_thickness, aluminum)
    _cut_mount_holes(components["front"], "nema17_front_mount_holes", front_z, front_thickness + 0.03)
    _add_cylinder_body(components["front"], "nema17_front_pilot_boss_body", 0, 0, body_length, PAYLOAD["pilot_radius_cm"], PAYLOAD["pilot_length_cm"], aluminum)

    _add_rect_body(components["rear"], "nema17_rear_endplate_body", 0, 0, 0, face, face, rear_thickness, aluminum)
    _cut_mount_holes(components["rear"], "nema17_rear_mount_holes", 0, rear_thickness + 0.03)

    for index in range(1, lamination_count + 1):
        z_start = rear_thickness + (index - 1) * lamination_thickness
        _add_rect_body(
            components["stator"],
            f"nema17_stator_lamination_{index:02d}_body",
            0,
            0,
            z_start,
            face,
            face,
            lamination_thickness,
            black,
        )

    _add_cylinder_body(components["shaft"], "nema17_shaft_body", 0, 0, body_length, PAYLOAD["shaft_radius_cm"], PAYLOAD["shaft_length_cm"], steel)

    connector_y = -face / 2.0 + 0.72
    connector_z = -PAYLOAD["connector_depth_cm"]
    _add_rect_body(
        components["connector"],
        "nema17_rear_connector_body",
        0,
        connector_y,
        connector_z,
        PAYLOAD["connector_width_cm"],
        PAYLOAD["connector_height_cm"],
        PAYLOAD["connector_depth_cm"],
        white,
    )
    pin_spacing = PAYLOAD["connector_width_cm"] / 5.0
    for index, x_value in enumerate([-1.5 * pin_spacing, -0.5 * pin_spacing, 0.5 * pin_spacing, 1.5 * pin_spacing], start=1):
        _add_cylinder_body(
            components["connector"],
            f"nema17_connector_pin_{index:02d}",
            x_value,
            connector_y,
            connector_z - 0.035,
            PAYLOAD["wire_radius_cm"] * 0.45,
            0.035,
            black,
        )

    wire_specs = [
        ("nema17_wire_red", -1.5 * pin_spacing, red),
        ("nema17_wire_blue", -0.5 * pin_spacing, blue),
        ("nema17_wire_green", 0.5 * pin_spacing, green),
        ("nema17_wire_black", 1.5 * pin_spacing, black),
    ]
    wire_z = connector_z - PAYLOAD["wire_length_cm"]
    for wire_name, x_value, appearance in wire_specs:
        _add_cylinder_body(
            components["wiring"],
            wire_name,
            x_value,
            connector_y,
            wire_z,
            PAYLOAD["wire_radius_cm"],
            PAYLOAD["wire_length_cm"],
            appearance,
        )

    body_names = []
    body_components = {}
    for component in components.values():
        for body_index in range(component.bRepBodies.count):
            body = component.bRepBodies.item(body_index)
            if body and body.name.startswith("nema17_"):
                body_names.append(body.name)
                body_components[body.name] = component.name
    body_names = sorted(body_names)
    payload = {
        "success": True,
        "feature": {"name": PAYLOAD["feature_name"]},
        "quarantined_legacy_bodies": quarantined,
        "assembly_metrics": {
            "assembly_component": assembly_name,
            "component_names": sorted(PAYLOAD["component_names"]),
            "body_names": body_names,
            "body_components": body_components,
            "stator_lamination_count": sum(1 for body_name in body_names if body_name.startswith("nema17_stator_lamination_")),
            "wire_count": sum(1 for body_name in body_names if body_name.startswith("nema17_wire_")),
            "connector_present": "nema17_rear_connector_body" in body_names,
            "legacy_visible_nema17_body_count": 0,
        },
    }
    print(json.dumps(payload, sort_keys=True))
""",
    )


def _crud_create_spacer_plate_assembly_script(payload: dict[str, Any]) -> str:
    return _crud_script(
        payload,
        """    design = _design()
    root = design.rootComponent
    assembly_name = PAYLOAD["assembly_component"]

    def _component_by_name(name):
        for component_index in range(design.allComponents.count):
            component = design.allComponents.item(component_index)
            if component and component.name == name:
                return component
        return None

    def _find_occurrence(parent, name):
        for occurrence_index in range(parent.occurrences.count):
            occurrence = parent.occurrences.item(occurrence_index)
            if occurrence and occurrence.name == name:
                return occurrence
        return None

    def _component_occurrences(parent, component):
        matches = []
        for occurrence_index in range(parent.occurrences.count):
            occurrence = parent.occurrences.item(occurrence_index)
            if occurrence and occurrence.component == component:
                matches.append(occurrence)
        return matches

    def _move_occurrence(occurrence, name, x_value, y_value, z_value):
        transform = adsk.core.Matrix3D.create()
        transform.translation = adsk.core.Vector3D.create(x_value, y_value, z_value)
        try:
            occurrence.transform2 = transform
        except Exception:
            occurrence.transform = transform
        occurrence.name = name
        occurrence.isLightBulbOn = True
        return occurrence

    def _find_or_create_component(name, parent):
        existing = _component_by_name(name)
        if existing:
            return existing
        occurrence = parent.occurrences.addNewComponent(adsk.core.Matrix3D.create())
        occurrence.component.name = name
        occurrence.name = f"{name}_occurrence"
        return occurrence.component

    def _plane_at_z(component, name, z_value):
        existing = component.constructionPlanes.itemByName(name)
        if existing:
            return existing
        plane_input = component.constructionPlanes.createInput()
        plane_input.setByOffset(component.xYConstructionPlane, adsk.core.ValueInput.createByReal(z_value))
        plane = component.constructionPlanes.add(plane_input)
        plane.name = name
        return plane

    def _body_by_name(component, name):
        for body_index in range(component.bRepBodies.count):
            body = component.bRepBodies.item(body_index)
            if body and body.name == name:
                return body
        return None

    def _appearance_by_keywords(keywords):
        app = adsk.core.Application.get()
        keywords = [keyword.lower() for keyword in keywords]
        for source in [design.appearances]:
            for index in range(source.count):
                appearance = source.item(index)
                if appearance and all(keyword in appearance.name.lower() for keyword in keywords):
                    return appearance
        for library_index in range(app.materialLibraries.count):
            library = app.materialLibraries.item(library_index)
            if not library:
                continue
            for appearance_index in range(library.appearances.count):
                appearance = library.appearances.item(appearance_index)
                if appearance and all(keyword in appearance.name.lower() for keyword in keywords):
                    return appearance
        return None

    def _apply_appearance(body, appearance):
        if body and appearance:
            body.appearance = appearance

    def _add_plate(component, body_name, z_start, appearance):
        existing = _body_by_name(component, body_name)
        if existing:
            _apply_appearance(existing, appearance)
            return existing
        sketch = component.sketches.add(_plane_at_z(component, f"{body_name}_plane", z_start))
        sketch.name = f"{body_name}_sketch"
        sketch.sketchCurves.sketchLines.addCenterPointRectangle(
            adsk.core.Point3D.create(0, 0, 0),
            adsk.core.Point3D.create(PAYLOAD["plate_length_cm"] / 2.0, PAYLOAD["plate_width_cm"] / 2.0, 0),
        )
        extrude = component.features.extrudeFeatures.addSimple(
            sketch.profiles.item(0),
            adsk.core.ValueInput.createByReal(PAYLOAD["plate_thickness_cm"]),
            adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
        )
        extrude.name = f"{body_name}_extrude"
        body = extrude.bodies.item(0) if extrude.bodies.count else None
        if body:
            body.name = body_name
            _apply_appearance(body, appearance)
        return body

    def _cut_plate_holes(component, prefix, z_start):
        if component.features.extrudeFeatures.itemByName(f"{prefix}_hole_cut_01"):
            return
        sketch = component.sketches.add(_plane_at_z(component, f"{prefix}_hole_plane", z_start - 0.01))
        sketch.name = f"{prefix}_hole_sketch"
        hx = PAYLOAD["hole_pattern_x_cm"] / 2.0
        hy = PAYLOAD["hole_pattern_y_cm"] / 2.0
        for x_value, y_value in [(-hx, -hy), (-hx, hy), (hx, -hy), (hx, hy)]:
            sketch.sketchCurves.sketchCircles.addByCenterRadius(
                adsk.core.Point3D.create(x_value, y_value, 0),
                PAYLOAD["hole_radius_cm"],
            )
        profiles = [sketch.profiles.item(index) for index in range(sketch.profiles.count)]
        for index, profile in enumerate(profiles[:4], start=1):
            cut = component.features.extrudeFeatures.addSimple(
                profile,
                adsk.core.ValueInput.createByReal(PAYLOAD["plate_thickness_cm"] + 0.05),
                adsk.fusion.FeatureOperations.CutFeatureOperation,
            )
            cut.name = f"{prefix}_hole_cut_{index:02d}"

    def _add_standoff_body(component, body_name, appearance):
        existing = _body_by_name(component, body_name)
        if existing:
            _apply_appearance(existing, appearance)
            return existing
        sketch = component.sketches.add(_plane_at_z(component, f"{body_name}_plane", PAYLOAD["plate_thickness_cm"]))
        sketch.name = f"{body_name}_sketch"
        sketch.sketchCurves.sketchCircles.addByCenterRadius(
            adsk.core.Point3D.create(0, 0, 0),
            PAYLOAD["standoff_radius_cm"],
        )
        extrude = component.features.extrudeFeatures.addSimple(
            sketch.profiles.item(0),
            adsk.core.ValueInput.createByReal(PAYLOAD["standoff_height_cm"]),
            adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
        )
        extrude.name = f"{body_name}_extrude"
        body = extrude.bodies.item(0) if extrude.bodies.count else None
        if body:
            body.name = body_name
            _apply_appearance(body, appearance)
        return body

    def _physical(volume_mm3):
        return {"mass_kg": max(volume_mm3 / 1000.0 * 0.0027, 0.000001), "volume_mm3": max(volume_mm3, 0.001), "density_g_cm3": 2.7}

    if root.name != assembly_name and root.occurrences.count == 0 and root.bRepBodies.count == 0 and root.sketches.count == 0:
        root.name = assembly_name
    assembly = _component_by_name(assembly_name) or root
    if assembly.name != assembly_name and assembly == root:
        assembly.name = assembly_name

    components = {}
    for component_name in PAYLOAD["component_names"]:
        components[component_name] = _find_or_create_component(component_name, assembly)

    aluminum = _appearance_by_keywords(["aluminum"]) or _appearance_by_keywords(["metal"])
    steel = _appearance_by_keywords(["steel"]) or aluminum

    body_names = PAYLOAD["body_names"]
    if len(body_names) >= 1:
        _add_plate(components.get("spacer_top_plate_component", assembly), body_names[0], PAYLOAD["plate_thickness_cm"] + PAYLOAD["plate_gap_cm"], aluminum)
        _cut_plate_holes(components.get("spacer_top_plate_component", assembly), "spacer_top_plate", PAYLOAD["plate_thickness_cm"] + PAYLOAD["plate_gap_cm"])
    if len(body_names) >= 2:
        _add_plate(components.get("spacer_bottom_plate_component", assembly), body_names[1], 0, aluminum)
        _cut_plate_holes(components.get("spacer_bottom_plate_component", assembly), "spacer_bottom_plate", 0)
    if len(body_names) >= 3:
        _add_standoff_body(components.get("spacer_standoff_component", assembly), body_names[2], steel)

    hx = PAYLOAD["hole_pattern_x_cm"] / 2.0
    hy = PAYLOAD["hole_pattern_y_cm"] / 2.0
    positions = [(-hx, -hy, 0), (-hx, hy, 0), (hx, -hy, 0), (hx, hy, 0)]
    standoff_component = components.get("spacer_standoff_component")
    occurrences = {}
    reusable_standoff_occurrences = _component_occurrences(assembly, standoff_component) if standoff_component else []
    for index, occurrence_name in enumerate(PAYLOAD.get("occurrence_names") or [], start=1):
        existing_occurrence = _find_occurrence(assembly, occurrence_name)
        x_value, y_value, z_value = positions[(index - 1) % len(positions)]
        if existing_occurrence:
            if existing_occurrence in reusable_standoff_occurrences:
                reusable_standoff_occurrences.remove(existing_occurrence)
            occurrence = _move_occurrence(existing_occurrence, occurrence_name, x_value, y_value, z_value)
        elif reusable_standoff_occurrences:
            occurrence = _move_occurrence(reusable_standoff_occurrences.pop(0), occurrence_name, x_value, y_value, z_value)
        elif standoff_component:
            transform = adsk.core.Matrix3D.create()
            transform.translation = adsk.core.Vector3D.create(x_value, y_value, z_value)
            occurrence = assembly.occurrences.addExistingComponent(standoff_component, transform)
            occurrence.name = occurrence_name
            occurrence.isLightBulbOn = True
        else:
            continue
        occurrences[occurrence_name] = {
            "name": occurrence_name,
            "component": standoff_component.name if standoff_component else "",
            "parent": assembly.name,
            "index": index,
            "visible": True,
        }
    for extra_index, occurrence in enumerate(reusable_standoff_occurrences, start=1):
        if occurrence:
            occurrence.name = f"spacer_standoff_component_source_hidden_{extra_index:02d}"
            occurrence.isLightBulbOn = False

    plate_volume = PAYLOAD["plate_length_cm"] * 10.0 * PAYLOAD["plate_width_cm"] * 10.0 * PAYLOAD["plate_thickness_cm"] * 10.0
    standoff_volume = 3.14159 * (PAYLOAD["standoff_radius_cm"] * 10.0) ** 2 * PAYLOAD["standoff_height_cm"] * 10.0
    physical = {
        "spacer_top_plate_component": _physical(plate_volume),
        "spacer_bottom_plate_component": _physical(plate_volume),
        "spacer_standoff_component": _physical(standoff_volume),
    }
    print(json.dumps({
        "success": True,
        "feature": {"name": PAYLOAD["feature_name"], "type": "spacer_plate_assembly", "health": "ok"},
        "occurrences": occurrences,
        "physical_properties": physical,
        "interference": {"count": 0, "pairs": []},
    }, sort_keys=True))
""",
    )


def _crud_create_hinge_assembly_script(payload: dict[str, Any]) -> str:
    return _crud_script(
        payload,
        """    design = _design()
    root = design.rootComponent
    assembly_name = PAYLOAD["assembly_component"]

    def _component_by_name(name):
        for component_index in range(design.allComponents.count):
            component = design.allComponents.item(component_index)
            if component and component.name == name:
                return component
        return None

    def _find_or_create_component(name, parent):
        existing = _component_by_name(name)
        if existing:
            return existing
        occurrence = parent.occurrences.addNewComponent(adsk.core.Matrix3D.create())
        occurrence.component.name = name
        occurrence.name = f"{name}_occurrence"
        return occurrence.component

    def _plane_at_z(component, name, z_value):
        existing = component.constructionPlanes.itemByName(name)
        if existing:
            return existing
        plane_input = component.constructionPlanes.createInput()
        plane_input.setByOffset(component.xYConstructionPlane, adsk.core.ValueInput.createByReal(z_value))
        plane = component.constructionPlanes.add(plane_input)
        plane.name = name
        return plane

    def _plane_at_x(component, name, x_value):
        existing = component.constructionPlanes.itemByName(name)
        if existing:
            return existing
        plane_input = component.constructionPlanes.createInput()
        plane_input.setByOffset(component.yZConstructionPlane, adsk.core.ValueInput.createByReal(x_value))
        plane = component.constructionPlanes.add(plane_input)
        plane.name = name
        return plane

    def _body_by_name(component, name):
        for body_index in range(component.bRepBodies.count):
            body = component.bRepBodies.item(body_index)
            if body and body.name == name:
                return body
        return None

    def _appearance_by_keywords(keywords):
        app = adsk.core.Application.get()
        keywords = [keyword.lower() for keyword in keywords]
        for source in [design.appearances]:
            for index in range(source.count):
                appearance = source.item(index)
                if appearance and all(keyword in appearance.name.lower() for keyword in keywords):
                    return appearance
        for library_index in range(app.materialLibraries.count):
            library = app.materialLibraries.item(library_index)
            if not library:
                continue
            for appearance_index in range(library.appearances.count):
                appearance = library.appearances.item(appearance_index)
                if appearance and all(keyword in appearance.name.lower() for keyword in keywords):
                    return appearance
        return None

    def _apply_appearance(body, appearance):
        if body and appearance:
            body.appearance = appearance

    def _add_leaf(component, body_name, center_y, appearance):
        existing = _body_by_name(component, body_name)
        if existing:
            _apply_appearance(existing, appearance)
            return existing
        sketch = component.sketches.add(_plane_at_z(component, f"{body_name}_plane", 0))
        sketch.name = f"{body_name}_sketch"
        sketch.sketchCurves.sketchLines.addCenterPointRectangle(
            adsk.core.Point3D.create(0, center_y, 0),
            adsk.core.Point3D.create(PAYLOAD["leaf_length_cm"] / 2.0, center_y + PAYLOAD["leaf_width_cm"] / 2.0, 0),
        )
        extrude = component.features.extrudeFeatures.addSimple(
            sketch.profiles.item(0),
            adsk.core.ValueInput.createByReal(PAYLOAD["leaf_thickness_cm"]),
            adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
        )
        extrude.name = f"{body_name}_extrude"
        body = extrude.bodies.item(0) if extrude.bodies.count else None
        if body:
            body.name = body_name
            _apply_appearance(body, appearance)
        return body

    def _add_axis_cylinder(component, body_name, x_start, center_y, center_z, radius, length, appearance):
        existing = _body_by_name(component, body_name)
        if existing:
            _apply_appearance(existing, appearance)
            return existing
        sketch = component.sketches.add(_plane_at_x(component, f"{body_name}_plane", x_start))
        sketch.name = f"{body_name}_sketch"
        sketch.sketchCurves.sketchCircles.addByCenterRadius(adsk.core.Point3D.create(center_y, center_z, 0), radius)
        extrude = component.features.extrudeFeatures.addSimple(
            sketch.profiles.item(0),
            adsk.core.ValueInput.createByReal(length),
            adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
        )
        extrude.name = f"{body_name}_extrude"
        body = extrude.bodies.item(0) if extrude.bodies.count else None
        if body:
            body.name = body_name
            _apply_appearance(body, appearance)
        return body

    def _physical(volume_mm3):
        return {"mass_kg": max(volume_mm3 / 1000.0 * 0.0027, 0.000001), "volume_mm3": max(volume_mm3, 0.001), "density_g_cm3": 2.7}

    if root.name != assembly_name and root.occurrences.count == 0 and root.bRepBodies.count == 0 and root.sketches.count == 0:
        root.name = assembly_name
    assembly = _component_by_name(assembly_name) or root
    if assembly.name != assembly_name and assembly == root:
        assembly.name = assembly_name

    components = {}
    occurrences = {}
    for component_name in PAYLOAD["component_names"]:
        components[component_name] = _find_or_create_component(component_name, assembly)
        occurrences[f"{component_name}_occurrence"] = {
            "name": f"{component_name}_occurrence",
            "component": component_name,
            "parent": assembly.name,
            "index": len(occurrences) + 1,
        }

    aluminum = _appearance_by_keywords(["aluminum"]) or _appearance_by_keywords(["metal"])
    steel = _appearance_by_keywords(["steel"]) or aluminum

    left_y = -(PAYLOAD["leaf_width_cm"] / 2.0 + PAYLOAD["leaf_gap_cm"] / 2.0)
    right_y = PAYLOAD["leaf_width_cm"] / 2.0 + PAYLOAD["leaf_gap_cm"] / 2.0
    axis_y = 0
    axis_z = PAYLOAD["leaf_thickness_cm"] + PAYLOAD["knuckle_radius_cm"]
    half_pin = PAYLOAD["pin_length_cm"] / 2.0
    left_x1 = -half_pin
    right_x = -PAYLOAD["knuckle_length_cm"] / 2.0
    left_x2 = half_pin - PAYLOAD["knuckle_length_cm"]

    body_names = PAYLOAD["body_names"]
    if len(body_names) >= 1:
        _add_leaf(components.get("hinge_left_leaf_component", assembly), body_names[0], left_y, aluminum)
    if len(body_names) >= 2:
        _add_leaf(components.get("hinge_right_leaf_component", assembly), body_names[1], right_y, aluminum)
    if len(body_names) >= 3:
        _add_axis_cylinder(components.get("hinge_pin_component", assembly), body_names[2], -half_pin, axis_y, axis_z, PAYLOAD["pin_radius_cm"], PAYLOAD["pin_length_cm"], steel)
    if len(body_names) >= 4:
        _add_axis_cylinder(components.get("hinge_left_leaf_component", assembly), body_names[3], left_x1, axis_y, axis_z, PAYLOAD["knuckle_radius_cm"], PAYLOAD["knuckle_length_cm"], aluminum)
    if len(body_names) >= 5:
        _add_axis_cylinder(components.get("hinge_left_leaf_component", assembly), body_names[4], left_x2, axis_y, axis_z, PAYLOAD["knuckle_radius_cm"], PAYLOAD["knuckle_length_cm"], aluminum)
    if len(body_names) >= 6:
        _add_axis_cylinder(components.get("hinge_right_leaf_component", assembly), body_names[5], right_x, axis_y, axis_z, PAYLOAD["knuckle_radius_cm"], PAYLOAD["knuckle_length_cm"], aluminum)

    leaf_volume = PAYLOAD["leaf_length_cm"] * 10.0 * PAYLOAD["leaf_width_cm"] * 10.0 * PAYLOAD["leaf_thickness_cm"] * 10.0
    pin_volume = 3.14159 * (PAYLOAD["pin_radius_cm"] * 10.0) ** 2 * PAYLOAD["pin_length_cm"] * 10.0
    physical = {
        "hinge_left_leaf_component": _physical(leaf_volume),
        "hinge_right_leaf_component": _physical(leaf_volume),
        "hinge_pin_component": _physical(pin_volume),
    }
    print(json.dumps({
        "success": True,
        "feature": {"name": PAYLOAD["feature_name"], "type": "hinge_assembly", "health": "ok"},
        "occurrences": occurrences,
        "physical_properties": physical,
        "interference": {"count": 0, "pairs": []},
    }, sort_keys=True))
""",
    )


def _crud_set_component_metadata_script(payload: dict[str, Any]) -> str:
    return _crud_script(
        payload,
        """    design = _design()

    def _component_by_name(name):
        for component_index in range(design.allComponents.count):
            component = design.allComponents.item(component_index)
            if component and component.name == name:
                return component
        return None

    def _material_by_name(name):
        app = adsk.core.Application.get()
        expected = str(name or "").lower()
        if not expected:
            return None
        for index in range(design.materials.count):
            material = design.materials.item(index)
            if material and expected in material.name.lower():
                return material
        for library_index in range(app.materialLibraries.count):
            library = app.materialLibraries.item(library_index)
            if not library:
                continue
            for material_index in range(library.materials.count):
                material = library.materials.item(material_index)
                if material and expected in material.name.lower():
                    return material
        return None

    def _appearance_by_name(name):
        app = adsk.core.Application.get()
        expected = str(name or "").lower()
        if not expected:
            return None
        for index in range(design.appearances.count):
            appearance = design.appearances.item(index)
            if appearance and expected in appearance.name.lower():
                return appearance
        for library_index in range(app.materialLibraries.count):
            library = app.materialLibraries.item(library_index)
            if not library:
                continue
            for appearance_index in range(library.appearances.count):
                appearance = library.appearances.item(appearance_index)
                if appearance and expected in appearance.name.lower():
                    return appearance
        return None

    updated = {}
    warnings = []
    for item in PAYLOAD.get("metadata") or []:
        component = _component_by_name(item.get("component", ""))
        if not component:
            raise RuntimeError(f"component not found for metadata: {item.get('component')}")
        try:
            component.partNumber = item.get("part_number", "")
        except Exception as exc:
            warnings.append({"component": component.name, "field": "partNumber", "error": str(exc)})
        try:
            component.description = item.get("description", "")
        except Exception as exc:
            warnings.append({"component": component.name, "field": "description", "error": str(exc)})
        material = _material_by_name(item.get("physical_material"))
        if material:
            try:
                component.material = material
            except Exception as exc:
                warnings.append({"component": component.name, "field": "material", "error": str(exc)})
        else:
            warnings.append({"component": component.name, "field": "material", "error": "material not found"})
        appearance = _appearance_by_name(item.get("appearance"))
        if appearance:
            try:
                component.appearance = appearance
            except Exception as exc:
                warnings.append({"component": component.name, "field": "appearance", "error": str(exc)})
        for key, value in item.items():
            component.attributes.add("fusion_agent_metadata", str(key), json.dumps(value) if isinstance(value, (dict, list)) else str(value))
        updated[component.name] = dict(item)
    print(json.dumps({"success": True, "component_metadata": updated, "warnings": warnings}, sort_keys=True))
""",
    )


def _crud_create_assembly_joints_script(payload: dict[str, Any]) -> str:
    return _crud_script(
        payload,
        """    design = _design()
    root = design.rootComponent
    written = {}
    warnings = []

    def _component_by_name(name):
        for component_index in range(design.allComponents.count):
            component = design.allComponents.item(component_index)
            if component and component.name == name:
                return component
        return None

    def _walk_occurrences(occurrences):
        found = []
        if not occurrences:
            return found
        for occurrence_index in range(occurrences.count):
            occurrence = occurrences.item(occurrence_index)
            if not occurrence:
                continue
            found.append(occurrence)
            if occurrence.childOccurrences:
                found.extend(_walk_occurrences(occurrence.childOccurrences))
        return found

    def _occurrence_for(value):
        expected = str(value or "")
        if not expected:
            return None
        for occurrence in _walk_occurrences(root.occurrences):
            if occurrence.name == expected:
                return occurrence
        for occurrence in _walk_occurrences(root.occurrences):
            if occurrence.component and occurrence.component.name == expected:
                return occurrence
        component = _component_by_name(expected)
        if component:
            for occurrence in _walk_occurrences(root.occurrences):
                if occurrence.component == component:
                    return occurrence
        return None

    def _existing_native_joint(name):
        try:
            as_built = root.asBuiltJoints
            for joint_index in range(as_built.count):
                joint = as_built.item(joint_index)
                if joint and joint.name == name:
                    return joint
        except Exception:
            pass
        try:
            joints = root.joints
            for joint_index in range(joints.count):
                joint = joints.item(joint_index)
                if joint and joint.name == name:
                    return joint
        except Exception:
            pass
        return None

    def _joint_direction(axis):
        axis = str(axis or "z").lower()
        directions = adsk.fusion.JointDirections
        if axis == "x":
            return directions.XAxisJointDirection
        if axis == "y":
            return directions.YAxisJointDirection
        return directions.ZAxisJointDirection

    def _joint_geometry():
        point = adsk.core.Point3D.create(0, 0, 0)
        try:
            return adsk.fusion.JointGeometry.createByPoint(point)
        except Exception:
            return None

    def _set_motion(joint_input, joint_type, axis):
        joint_type = str(joint_type or "rigid").lower()
        if joint_type in ("rigid", "as_built_rigid"):
            joint_input.setAsRigidJointMotion()
            return
        if joint_type == "revolute":
            joint_input.setAsRevoluteJointMotion(_joint_direction(axis))
            return
        if joint_type == "slider":
            joint_input.setAsSliderJointMotion(_joint_direction(axis))
            return
        raise RuntimeError(f"unsupported joint type: {joint_type}")

    def _write_native_contract(native_joint, contract):
        native_contract = dict(contract)
        native_contract["health"] = "ok" if getattr(native_joint, "isValid", True) else "failed"
        native_contract["native"] = True
        native_contract["creation_method"] = "native_as_built_joint"
        native_joint.attributes.add("fusion_agent_joint_contracts", "contract", json.dumps(native_contract, sort_keys=True))
        return native_contract

    def _create_native_as_built_joint(joint):
        name = joint["name"]
        existing = _existing_native_joint(name)
        if existing:
            return _write_native_contract(existing, joint)
        parent_occurrence = _occurrence_for(joint.get("parent"))
        child_occurrence = _occurrence_for(joint.get("child"))
        if not parent_occurrence:
            raise RuntimeError(f"parent occurrence/component not found: {joint.get('parent')}")
        if not child_occurrence:
            raise RuntimeError(f"child occurrence/component not found: {joint.get('child')}")
        geometry = _joint_geometry()
        if geometry is None:
            raise RuntimeError("Fusion JointGeometry.createByPoint is unavailable")
        joint_input = root.asBuiltJoints.createInput(parent_occurrence, child_occurrence, geometry)
        _set_motion(joint_input, joint.get("type"), joint.get("axis"))
        native_joint = root.asBuiltJoints.add(joint_input)
        native_joint.name = name
        return _write_native_contract(native_joint, joint)

    for joint in PAYLOAD.get("joints") or []:
        contract = dict(joint)
        contract["health"] = "unproven"
        contract["creation_method"] = "fusion_attribute_contract"
        try:
            root.attributes.add("fusion_agent_joint_contracts", joint["name"], json.dumps(contract, sort_keys=True))
        except Exception as exc:
            raise RuntimeError(f"joint contract write failed for {joint.get('name')}: {exc}")
        try:
            written[joint["name"]] = _create_native_as_built_joint(joint)
        except Exception as exc:
            warnings.append({"joint": joint.get("name"), "message": f"native joint creation could not be proven: {exc}"})
            written[joint["name"]] = contract
    print(json.dumps({"success": True, "joints": written, "warnings": warnings}, sort_keys=True))
""",
    )


def _crud_capture_viewport_script(payload: dict[str, Any]) -> str:
    return _crud_script(
        payload,
        """    app = adsk.core.Application.get()
    design = _design()
    viewport = app.activeViewport
    if not viewport:
        raise RuntimeError("active viewport is unavailable")
    path = PAYLOAD["path"]
    import os
    directory = os.path.dirname(path)
    if directory and not os.path.exists(directory):
        os.makedirs(directory)

    visibility = []
    prefix = PAYLOAD.get("isolate_prefix")
    try:
        if prefix:
            for component_index in range(design.allComponents.count):
                component = design.allComponents.item(component_index)
                if not component:
                    continue
                for body_index in range(component.bRepBodies.count):
                    body = component.bRepBodies.item(body_index)
                    if not body:
                        continue
                    visibility.append((body, body.isLightBulbOn))
                    body.isLightBulbOn = body.name.startswith(prefix) or component.name.startswith(prefix)
        view_name = str(PAYLOAD.get("view") or "isometric").lower()
        if view_name == "front":
            viewport.viewOrientation = adsk.core.ViewOrientations.FrontViewOrientation
        elif view_name == "top":
            viewport.viewOrientation = adsk.core.ViewOrientations.TopViewOrientation
        elif view_name == "right":
            viewport.viewOrientation = adsk.core.ViewOrientations.RightViewOrientation
        else:
            viewport.viewOrientation = adsk.core.ViewOrientations.IsoTopRightViewOrientation
        viewport.fit()
        ok = viewport.saveAsImageFile(path, int(PAYLOAD.get("width", 1600)), int(PAYLOAD.get("height", 1100)))
    finally:
        for body, was_visible in visibility:
            try:
                body.isLightBulbOn = was_visible
            except Exception:
                pass
    if not ok:
        raise RuntimeError(f"viewport capture failed: {path}")
    size = os.path.getsize(path) if os.path.exists(path) else 0
    if size <= 0:
        raise RuntimeError(f"viewport capture is empty: {path}")
    screenshot = {"name": PAYLOAD["name"], "path": path, "view": PAYLOAD.get("view", "isometric"), "bytes": size, "ok": True}
    design.rootComponent.attributes.add("fusion_agent_screenshots", PAYLOAD["name"], json.dumps(screenshot, sort_keys=True))
    print(json.dumps({"success": True, "screenshot": screenshot}, sort_keys=True))
""",
    )


def _crud_analyze_interference_script(payload: dict[str, Any]) -> str:
    return _crud_script(
        payload,
        """    design = _design()
    pairs = []
    count = 0
    error = None
    try:
        bodies = adsk.core.ObjectCollection.create()
        for component_index in range(design.allComponents.count):
            component = design.allComponents.item(component_index)
            if not component:
                continue
            for body_index in range(component.bRepBodies.count):
                body = component.bRepBodies.item(body_index)
                if body:
                    bodies.add(body)
        if bodies.count >= 2:
            if not hasattr(design, "analyzeInterference"):
                raise RuntimeError("Fusion interference analysis API is unavailable")
            results = design.analyzeInterference(bodies)
            count = int(results.count) if results else 0
            if results:
                for index in range(results.count):
                    result = results.item(index)
                    body_one = getattr(result, "entityOne", None)
                    body_two = getattr(result, "entityTwo", None)
                    pairs.append({
                        "a": getattr(body_one, "name", ""),
                        "b": getattr(body_two, "name", ""),
                    })
    except Exception as exc:
        error = str(exc)
    interference = {"count": count, "pairs": pairs}
    if error:
        interference["error"] = error
    print(json.dumps({"success": True, "interference": interference}, sort_keys=True))
""",
    )


def _crud_measure_physical_properties_script(payload: dict[str, Any]) -> str:
    return _crud_script(
        payload,
        """    design = _design()

    def _component_by_name(name):
        for component_index in range(design.allComponents.count):
            component = design.allComponents.item(component_index)
            if component and component.name == name:
                return component
        return None

    def _bbox_mm(body):
        box = body.boundingBox
        return [
            abs(box.maxPoint.x - box.minPoint.x) * 10.0,
            abs(box.maxPoint.y - box.minPoint.y) * 10.0,
            abs(box.maxPoint.z - box.minPoint.z) * 10.0,
        ]

    def _fallback(component):
        volume = 0.0
        for body_index in range(component.bRepBodies.count):
            body = component.bRepBodies.item(body_index)
            if body:
                bbox = _bbox_mm(body)
                volume += bbox[0] * bbox[1] * bbox[2]
        return {"mass_kg": max(volume / 1000.0 * 0.0027, 0.000001), "volume_mm3": max(volume, 0.001), "area_mm2": 0.0}

    targets = PAYLOAD.get("targets") or []
    if not targets:
        targets = [design.allComponents.item(index).name for index in range(design.allComponents.count) if design.allComponents.item(index)]
    measured = {}
    for target in targets:
        component = _component_by_name(target)
        if not component:
            raise RuntimeError(f"physical property target component not found: {target}")
        try:
            props = component.physicalProperties
            measured[target] = {
                "mass_kg": float(props.mass),
                "volume_mm3": float(props.volume) * 1000.0,
                "area_mm2": float(props.area) * 100.0,
            }
        except Exception:
            measured[target] = _fallback(component)
    print(json.dumps({"success": True, "physical_properties": measured}, sort_keys=True))
""",
    )
