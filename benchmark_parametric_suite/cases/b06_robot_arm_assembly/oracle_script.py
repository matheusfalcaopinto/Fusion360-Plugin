import adsk.core
import adsk.fusion
import json
import math


def _items(collection):
    return [collection.item(index) for index in range(collection.count)]


def _close(actual, expected, tolerance=0.1):
    return actual is not None and math.fabs(float(actual) - float(expected)) <= tolerance


def _bbox_mm(body):
    box = body.preciseBoundingBox
    return {
        "min": [box.minPoint.x * 10.0, box.minPoint.y * 10.0, box.minPoint.z * 10.0],
        "max": [box.maxPoint.x * 10.0, box.maxPoint.y * 10.0, box.maxPoint.z * 10.0],
        "size": [
            (box.maxPoint.x - box.minPoint.x) * 10.0,
            (box.maxPoint.y - box.minPoint.y) * 10.0,
            (box.maxPoint.z - box.minPoint.z) * 10.0,
        ],
    }


def _global_bbox(bodies):
    boxes = [_bbox_mm(body) for body in bodies]
    minimum = [min(box["min"][axis] for box in boxes) for axis in range(3)]
    maximum = [max(box["max"][axis] for box in boxes) for axis in range(3)]
    return {
        "min": minimum,
        "max": maximum,
        "size": [maximum[axis] - minimum[axis] for axis in range(3)],
    }


def _bbox_matches(box, minimum, maximum, tolerance=0.2):
    return all(_close(box["min"][axis], minimum[axis], tolerance) for axis in range(3)) and all(
        _close(box["max"][axis], maximum[axis], tolerance) for axis in range(3)
    )


def _identity_transform(occurrence):
    values = list(occurrence.transform2.asArray())
    expected = [
        1.0, 0.0, 0.0, 0.0,
        0.0, 1.0, 0.0, 0.0,
        0.0, 0.0, 1.0, 0.0,
        0.0, 0.0, 0.0, 1.0,
    ]
    return len(values) == 16 and all(_close(values[index], expected[index], 0.000001) for index in range(16))


def run(_context: str):
    app = adsk.core.Application.get()
    document = app.activeDocument
    design = adsk.fusion.Design.cast(app.activeProduct)
    if document is None or design is None:
        raise RuntimeError("B06 oracle requires an active Fusion design")
    root = design.rootComponent
    marker_attribute = root.attributes.itemByName("fusion_agent_benchmark", "trial_marker")
    marker = marker_attribute.value if marker_attribute is not None else None
    checks = []

    def check(check_id, passed, expected, observed):
        checks.append({
            "id": check_id,
            "status": "pass" if passed else "fail",
            "expected": expected,
            "observed": observed,
        })

    check(
        "document.marked_unsaved",
        bool(marker) and document.dataFile is None,
        {"marked": True, "saved": False},
        {"marker": marker, "saved": document.dataFile is not None},
    )
    parameters = {
        parameter.name: {"value": parameter.value, "expression": parameter.expression}
        for parameter in _items(design.userParameters)
    }
    expected_parameters = {
        "BaseDiameter": 16.0,
        "BaseHeight": 2.0,
        "ColumnHeight": 10.0,
        "ShoulderZ": 12.0,
        "UpperArmLength": 16.0,
        "ElbowX": 16.0,
        "ForearmLength": 13.0,
        "WristZ": 25.0,
        "WristLength": 7.0,
        "WristTipX": 23.0,
        "JawGap": 5.0,
        "CableThickness": 0.8,
    }
    parameter_failures = []
    for name, expected in expected_parameters.items():
        actual = parameters.get(name, {}).get("value")
        if not _close(actual, expected, 0.0001):
            parameter_failures.append({"name": name, "expected": expected, "actual": actual})
    check(
        "parameters.chain_values",
        len(parameters) == 36 and not parameter_failures,
        {"count": 36, "critical_values_cm": expected_parameters},
        {"count": len(parameters), "failures": parameter_failures},
    )

    child_components = [component for component in _items(design.allComponents) if component != root]
    component_by_name = {component.name: component for component in child_components}
    expected_component_names = {
        "CMP01_Base",
        "CMP02_Column",
        "CMP03_Shoulder_Motor",
        "CMP04_Upper_Arm",
        "CMP05_Elbow_Motor",
        "CMP06_Forearm",
        "CMP07_Wrist_Pitch_Motor",
        "CMP08_Wrist_Link",
        "CMP09_Wrist_Roll_Motor",
        "CMP10_Tool_Flange",
        "CMP11_Gripper_Palm",
        "CMP12_Gripper_Finger_Upper",
        "CMP13_Gripper_Finger_Lower",
        "CMP14_Cable_Upper",
        "CMP15_Cable_Forearm",
        "CMP16_Cable_Wrist",
    }
    occurrences = _items(root.allOccurrences)
    check(
        "assembly.component_hierarchy_and_identity",
        len(child_components) == 16
        and set(component_by_name) == expected_component_names
        and len(occurrences) == 16
        and all(_identity_transform(occurrence) for occurrence in occurrences)
        and root.bRepBodies.count == 0,
        {"components": 16, "occurrences": 16, "identity": True, "root_bodies": 0},
        {
            "components": sorted(component_by_name),
            "occurrences": len(occurrences),
            "non_identity": [occurrence.fullPathName for occurrence in occurrences if not _identity_transform(occurrence)],
            "root_bodies": root.bRepBodies.count,
        },
    )

    bodies = []
    body_by_name = {}
    topology_errors = []
    feature_errors = []
    child_sketch_count = 0
    child_feature_count = 0
    for component in child_components:
        component_bodies = _items(component.bRepBodies)
        child_sketch_count += component.sketches.count
        child_feature_count += component.features.count
        if len(component_bodies) != 1:
            topology_errors.append({"component": component.name, "bodies": len(component_bodies)})
            continue
        body = component_bodies[0]
        bodies.append(body)
        body_by_name[body.name] = body
        if not body.isValid or not body.isSolid or body.lumps.count != 1 or not body.isVisible:
            topology_errors.append({
                "component": component.name,
                "body": body.name,
                "valid": body.isValid,
                "solid": body.isSolid,
                "lumps": body.lumps.count,
                "visible": body.isVisible,
            })
        for feature in _items(component.features):
            if not feature.isValid or feature.errorOrWarningMessage:
                feature_errors.append({
                    "component": component.name,
                    "feature": feature.name,
                    "valid": feature.isValid,
                    "message": feature.errorOrWarningMessage,
                })
    check(
        "topology.one_healthy_body_per_component",
        len(bodies) == 16
        and len(body_by_name) == 16
        and not topology_errors
        and not feature_errors
        and child_feature_count == 16
        and child_sketch_count == 16
        and root.sketches.count == 1
        and root.sketches.item(0).name == "SK00_Kinematic_Envelope",
        {"bodies": 16, "features": 16, "child_sketches": 16, "root_reference_sketches": 1},
        {
            "bodies": sorted(body_by_name),
            "features": child_feature_count,
            "child_sketches": child_sketch_count,
            "root_sketches": root.sketches.count,
            "topology_errors": topology_errors,
            "feature_errors": feature_errors,
        },
    )

    if bodies:
        global_box = _global_bbox(bodies)
        check(
            "geometry.global_workspace_bbox",
            _bbox_matches(global_box, [-80.0, -80.0, 0.0], [400.0, 80.0, 290.0], 0.3),
            {"min": [-80.0, -80.0, 0.0], "max": [400.0, 80.0, 290.0]},
            global_box,
        )
    else:
        check("geometry.global_workspace_bbox", False, "sixteen bodies", None)

    chain_expected = {
        "B04_Upper_Arm": ([0.0, -18.0, 98.0], [160.0, 18.0, 142.0]),
        "B06_Forearm": ([140.0, -16.0, 120.0], [180.0, 16.0, 250.0]),
        "B08_Wrist_Link": ([160.0, -15.0, 232.0], [230.0, 15.0, 268.0]),
        "B09_Wrist_Roll_Motor": ([230.0, -22.0, 228.0], [290.0, 22.0, 272.0]),
        "B10_Tool_Flange": ([290.0, -40.0, 210.0], [300.0, 40.0, 290.0]),
        "B11_Gripper_Palm": ([300.0, -30.0, 210.0], [330.0, 30.0, 290.0]),
        "B12_Gripper_Finger_Upper": ([330.0, -7.5, 275.0], [400.0, 7.5, 290.0]),
        "B13_Gripper_Finger_Lower": ([330.0, -7.5, 210.0], [400.0, 7.5, 225.0]),
    }
    chain_failures = []
    chain_boxes = {}
    for name, expected in chain_expected.items():
        body = body_by_name.get(name)
        box = None if body is None else _bbox_mm(body)
        chain_boxes[name] = box
        if box is None or not _bbox_matches(box, expected[0], expected[1], 0.2):
            chain_failures.append(name)
    check(
        "geometry.continuous_joint_to_tool_chain",
        not chain_failures,
        chain_expected,
        {"failures": chain_failures, "boxes": chain_boxes},
    )

    cable_expected = {
        "B14_Cable_Upper": ([16.0, -4.0, 142.0], [144.0, 4.0, 150.0]),
        "B15_Cable_Forearm": ([180.0, -4.0, 132.5], [188.0, 4.0, 237.5]),
        "B16_Cable_Wrist": ([172.5, -4.0, 268.0], [217.5, 4.0, 276.0]),
    }
    cable_failures = []
    cable_boxes = {}
    for name, expected in cable_expected.items():
        body = body_by_name.get(name)
        box = None if body is None else _bbox_mm(body)
        cable_boxes[name] = box
        if box is None or not _bbox_matches(box, expected[0], expected[1], 0.2):
            cable_failures.append(name)
    check(
        "geometry.cable_harness_three_segments",
        not cable_failures,
        cable_expected,
        {"failures": cable_failures, "boxes": cable_boxes},
    )

    joints = _items(root.asBuiltJoints)
    joint_data = []
    revolute_count = 0
    joint_errors = []
    for joint in joints:
        motion_type = joint.jointMotion.objectType
        if motion_type == adsk.fusion.RevoluteJointMotion.classType():
            revolute_count += 1
        item = {
            "name": joint.name,
            "valid": joint.isValid,
            "motion": motion_type,
        }
        joint_data.append(item)
        if not joint.isValid:
            joint_errors.append(item)
    expected_joint_names = {f"J{index:02d}_{suffix}" for index, suffix in [
        (1, "Base_Column_Rigid"),
        (2, "Shoulder_Revolute"),
        (3, "Shoulder_Link_Rigid"),
        (4, "Elbow_Revolute"),
        (5, "Elbow_Link_Rigid"),
        (6, "Wrist_Pitch_Revolute"),
        (7, "Wrist_Link_Rigid"),
        (8, "Wrist_Roll_Revolute"),
        (9, "Tool_Flange_Rigid"),
        (10, "Gripper_Palm_Rigid"),
        (11, "Upper_Finger_Rigid"),
        (12, "Lower_Finger_Rigid"),
    ]}
    check(
        "joints.named_graph_and_dof",
        len(joints) == 12
        and {item["name"] for item in joint_data} == expected_joint_names
        and revolute_count == 4
        and not joint_errors,
        {"joints": 12, "revolute": 4, "names": sorted(expected_joint_names)},
        {"joints": joint_data, "revolute": revolute_count, "errors": joint_errors},
    )

    failed = [item["id"] for item in checks if item["status"] != "pass"]
    result = {
        "ok": True,
        "schema_version": "fusion_parametric_oracle.v2",
        "oracle_id": "b06_robot_arm_assembly_geometry",
        "case_id": "b06_robot_arm_assembly",
        "phase": "initial",
        "passed": not failed,
        "coverage": {
            "mandatory": len(checks),
            "passed": len(checks) - len(failed),
            "failed": len(failed),
            "unverified": 0,
        },
        "failed_checks": failed,
        "checks": checks,
        "diagnostics": {
            "marker": marker,
            "total_volume_mm3": sum(body.volume * 1000.0 for body in bodies),
            "parameter_expressions": {name: value["expression"] for name, value in parameters.items()},
        },
    }
    payload = json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    print(payload)
    return payload
