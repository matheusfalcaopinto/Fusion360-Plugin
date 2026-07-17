import adsk.core
import adsk.fusion
import json
import math


def _items(collection):
    return [collection.item(index) for index in range(collection.count)]


def _close(actual, expected, tolerance=0.1):
    if (
        isinstance(actual, bool)
        or isinstance(expected, bool)
        or isinstance(tolerance, bool)
    ):
        return False
    try:
        values = (float(actual), float(expected), float(tolerance))
    except (TypeError, ValueError):
        return False
    return (
        all(math.isfinite(value) for value in values)
        and values[2] >= 0
        and math.fabs(values[0] - values[1]) <= values[2]
    )


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
    return all(
        _close(box["min"][axis], minimum[axis], tolerance) for axis in range(3)
    ) and all(_close(box["max"][axis], maximum[axis], tolerance) for axis in range(3))


def _identity_transform(occurrence):
    values = list(occurrence.transform2.asArray())
    expected = [
        1.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
    ]
    return len(values) == 16 and all(
        _close(values[index], expected[index], 0.000001) for index in range(16)
    )


def run(_context: str):
    app = adsk.core.Application.get()
    document = app.activeDocument
    design = adsk.fusion.Design.cast(app.activeProduct)
    if document is None or design is None:
        raise RuntimeError("B06 oracle requires an active Fusion design")
    root = design.rootComponent
    marker_attribute = root.attributes.itemByName(
        "fusion_agent_benchmark", "trial_marker"
    )
    marker = marker_attribute.value if marker_attribute is not None else None
    checks = []

    def check(check_id, passed, expected, observed):
        checks.append(
            {
                "id": check_id,
                "status": "pass" if passed else "fail",
                "expected": expected,
                "observed": observed,
            }
        )

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
            parameter_failures.append(
                {"name": name, "expected": expected, "actual": actual}
            )
    check(
        "parameters.chain_values",
        len(parameters) == 36 and not parameter_failures,
        {"count": 36, "critical_values_cm": expected_parameters},
        {"count": len(parameters), "failures": parameter_failures},
    )

    child_components = [
        component for component in _items(design.allComponents) if component != root
    ]
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
            "non_identity": [
                occurrence.fullPathName
                for occurrence in occurrences
                if not _identity_transform(occurrence)
            ],
            "root_bodies": root.bRepBodies.count,
        },
    )

    bodies = []
    body_by_name = {}
    body_by_identity = {}
    expected_body_owners = {
        "CMP01_Base": "B01_Base",
        "CMP02_Column": "B02_Column",
        "CMP03_Shoulder_Motor": "B03_Shoulder_Motor",
        "CMP04_Upper_Arm": "B04_Upper_Arm",
        "CMP05_Elbow_Motor": "B05_Elbow_Motor",
        "CMP06_Forearm": "B06_Forearm",
        "CMP07_Wrist_Pitch_Motor": "B07_Wrist_Pitch_Motor",
        "CMP08_Wrist_Link": "B08_Wrist_Link",
        "CMP09_Wrist_Roll_Motor": "B09_Wrist_Roll_Motor",
        "CMP10_Tool_Flange": "B10_Tool_Flange",
        "CMP11_Gripper_Palm": "B11_Gripper_Palm",
        "CMP12_Gripper_Finger_Upper": "B12_Gripper_Finger_Upper",
        "CMP13_Gripper_Finger_Lower": "B13_Gripper_Finger_Lower",
        "CMP14_Cable_Upper": "B14_Cable_Upper",
        "CMP15_Cable_Forearm": "B15_Cable_Forearm",
        "CMP16_Cable_Wrist": "B16_Cable_Wrist",
    }
    ownership_errors = []
    topology_errors = []
    feature_errors = []
    child_sketch_count = 0
    child_feature_count = 0
    for component in child_components:
        component_bodies = _items(component.bRepBodies)
        child_sketch_count += component.sketches.count
        child_feature_count += component.features.count
        if len(component_bodies) != 1:
            topology_errors.append(
                {"component": component.name, "bodies": len(component_bodies)}
            )
            continue
        body = component_bodies[0]
        bodies.append(body)
        body_by_name[body.name] = body
        body_by_identity[(component.name, body.name)] = body
        if expected_body_owners.get(component.name) != body.name:
            ownership_errors.append(
                {
                    "component": component.name,
                    "expected_body": expected_body_owners.get(component.name),
                    "observed_body": body.name,
                }
            )
        if (
            not body.isValid
            or not body.isSolid
            or body.lumps.count != 1
            or not body.isVisible
        ):
            topology_errors.append(
                {
                    "component": component.name,
                    "body": body.name,
                    "valid": body.isValid,
                    "solid": body.isSolid,
                    "lumps": body.lumps.count,
                    "visible": body.isVisible,
                }
            )
        for feature in _items(component.features):
            if not feature.isValid or feature.errorOrWarningMessage:
                feature_errors.append(
                    {
                        "component": component.name,
                        "feature": feature.name,
                        "valid": feature.isValid,
                        "message": feature.errorOrWarningMessage,
                    }
                )
    check(
        "topology.one_healthy_body_per_component",
        len(bodies) == 16
        and len(body_by_name) == 16
        and len(body_by_identity) == 16
        and not ownership_errors
        and not topology_errors
        and not feature_errors
        and child_feature_count == 16
        and child_sketch_count == 16
        and root.sketches.count == 1
        and root.sketches.item(0).name == "SK00_Kinematic_Envelope",
        {
            "bodies": 16,
            "features": 16,
            "child_sketches": 16,
            "root_reference_sketches": 1,
        },
        {
            "bodies": sorted(body_by_name),
            "features": child_feature_count,
            "child_sketches": child_sketch_count,
            "root_sketches": root.sketches.count,
            "topology_errors": topology_errors,
            "ownership_errors": ownership_errors,
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
        component_name = next(
            (
                owner
                for owner, body_name in expected_body_owners.items()
                if body_name == name
            ),
            None,
        )
        body = body_by_identity.get((component_name, name))
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
        component_name = next(
            (
                owner
                for owner, body_name in expected_body_owners.items()
                if body_name == name
            ),
            None,
        )
        body = body_by_identity.get((component_name, name))
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
    expected_joint_endpoints = {
        "J01_Base_Column_Rigid": sorted(["CMP01_Base", "CMP02_Column"]),
        "J02_Shoulder_Revolute": sorted(["CMP02_Column", "CMP03_Shoulder_Motor"]),
        "J03_Shoulder_Link_Rigid": sorted(["CMP03_Shoulder_Motor", "CMP04_Upper_Arm"]),
        "J04_Elbow_Revolute": sorted(["CMP04_Upper_Arm", "CMP05_Elbow_Motor"]),
        "J05_Elbow_Link_Rigid": sorted(["CMP05_Elbow_Motor", "CMP06_Forearm"]),
        "J06_Wrist_Pitch_Revolute": sorted(
            ["CMP06_Forearm", "CMP07_Wrist_Pitch_Motor"]
        ),
        "J07_Wrist_Link_Rigid": sorted(["CMP07_Wrist_Pitch_Motor", "CMP08_Wrist_Link"]),
        "J08_Wrist_Roll_Revolute": sorted(
            ["CMP08_Wrist_Link", "CMP09_Wrist_Roll_Motor"]
        ),
        "J09_Tool_Flange_Rigid": sorted(
            ["CMP09_Wrist_Roll_Motor", "CMP10_Tool_Flange"]
        ),
        "J10_Gripper_Palm_Rigid": sorted(["CMP10_Tool_Flange", "CMP11_Gripper_Palm"]),
        "J11_Upper_Finger_Rigid": sorted(
            ["CMP11_Gripper_Palm", "CMP12_Gripper_Finger_Upper"]
        ),
        "J12_Lower_Finger_Rigid": sorted(
            ["CMP11_Gripper_Palm", "CMP13_Gripper_Finger_Lower"]
        ),
    }
    for joint in joints:
        motion_type = joint.jointMotion.objectType
        if motion_type == adsk.fusion.RevoluteJointMotion.classType():
            revolute_count += 1
        occurrence_one = joint.occurrenceOne
        occurrence_two = joint.occurrenceTwo
        endpoints = sorted(
            [
                occurrence_one.component.name if occurrence_one is not None else "",
                occurrence_two.component.name if occurrence_two is not None else "",
            ]
        )
        item = {
            "name": joint.name,
            "valid": joint.isValid,
            "motion": motion_type,
            "endpoints": endpoints,
        }
        joint_data.append(item)
        if not joint.isValid:
            joint_errors.append(item)
    expected_joint_names = {
        f"J{index:02d}_{suffix}"
        for index, suffix in [
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
        ]
    }
    check(
        "joints.named_graph_and_dof",
        len(joints) == 12
        and {item["name"] for item in joint_data} == expected_joint_names
        and revolute_count == 4
        and {item["name"]: item["endpoints"] for item in joint_data}
        == expected_joint_endpoints
        and not joint_errors,
        {
            "joints": 12,
            "revolute": 4,
            "names": sorted(expected_joint_names),
            "endpoints": expected_joint_endpoints,
        },
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
            "parameter_expressions": {
                name: value["expression"] for name, value in parameters.items()
            },
        },
    }
    payload = json.dumps(
        result,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )
    print(payload)
    return payload
