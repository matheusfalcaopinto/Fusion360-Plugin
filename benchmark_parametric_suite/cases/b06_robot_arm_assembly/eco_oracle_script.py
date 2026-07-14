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


def _bbox_matches(box, minimum, maximum, tolerance=0.3):
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
        raise RuntimeError("B06 ECO oracle requires an active Fusion design")
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
        "document.identity_preserved",
        bool(marker) and document.dataFile is None,
        {"marked": True, "saved": False},
        {"marker": marker, "saved": document.dataFile is not None},
    )
    parameters = {
        parameter.name: {"value": parameter.value, "expression": parameter.expression}
        for parameter in _items(design.userParameters)
    }
    expected_parameters = {
        "UpperArmLength": (19.5, "195 mm"),
        "ForearmLength": (15.5, "155 mm"),
        "WristLength": (8.5, "85 mm"),
        "ShoulderZ": (12.0, "BaseHeight + ColumnHeight"),
        "ElbowX": (19.5, "UpperArmLength"),
        "WristZ": (27.5, "ShoulderZ + ForearmLength"),
        "WristTipX": (28.0, "ElbowX + WristLength"),
    }
    parameter_failures = []
    for name, expected in expected_parameters.items():
        observed = parameters.get(name, {})
        normalized_expression = str(observed.get("expression") or "").replace(" ", "")
        normalized_expected = expected[1].replace(" ", "")
        if not _close(observed.get("value"), expected[0], 0.0001) or normalized_expression != normalized_expected:
            parameter_failures.append({"name": name, "expected": expected, "observed": observed})
    check(
        "parameters.eco_and_dependencies",
        len(parameters) == 36 and not parameter_failures,
        {"count": 36, "values": expected_parameters},
        {"count": len(parameters), "failures": parameter_failures},
    )

    child_components = [component for component in _items(design.allComponents) if component != root]
    occurrences = _items(root.allOccurrences)
    bodies = []
    body_by_name = {}
    errors = []
    feature_count = 0
    sketch_count = 0
    for component in child_components:
        component_bodies = _items(component.bRepBodies)
        feature_count += component.features.count
        sketch_count += component.sketches.count
        if len(component_bodies) != 1:
            errors.append({"component": component.name, "body_count": len(component_bodies)})
            continue
        body = component_bodies[0]
        bodies.append(body)
        body_by_name[body.name] = body
        if not body.isValid or not body.isSolid or body.lumps.count != 1:
            errors.append({"component": component.name, "body": body.name, "valid": body.isValid})
        for feature in _items(component.features):
            if not feature.isValid or feature.errorOrWarningMessage:
                errors.append({"component": component.name, "feature": feature.name, "message": feature.errorOrWarningMessage})
    check(
        "assembly.counts_health_and_identity_after_eco",
        len(child_components) == 16
        and len(occurrences) == 16
        and all(_identity_transform(occurrence) for occurrence in occurrences)
        and len(bodies) == 16
        and feature_count == 16
        and sketch_count == 16
        and root.asBuiltJoints.count == 12
        and not errors,
        {"components": 16, "occurrences": 16, "bodies": 16, "features": 16, "joints": 12, "identity": True},
        {
            "components": len(child_components),
            "occurrences": len(occurrences),
            "bodies": len(bodies),
            "features": feature_count,
            "joints": root.asBuiltJoints.count,
            "non_identity": [occurrence.fullPathName for occurrence in occurrences if not _identity_transform(occurrence)],
            "errors": errors,
        },
    )

    if bodies:
        boxes = [_bbox_mm(body) for body in bodies]
        minimum = [min(box["min"][axis] for box in boxes) for axis in range(3)]
        maximum = [max(box["max"][axis] for box in boxes) for axis in range(3)]
        global_box = {"min": minimum, "max": maximum, "size": [maximum[axis] - minimum[axis] for axis in range(3)]}
        check(
            "geometry.eco_workspace_bbox",
            _bbox_matches(global_box, [-80.0, -80.0, 0.0], [450.0, 80.0, 315.0], 0.4),
            {"min": [-80.0, -80.0, 0.0], "max": [450.0, 80.0, 315.0]},
            global_box,
        )
    else:
        check("geometry.eco_workspace_bbox", False, "sixteen bodies", None)

    chain_expected = {
        "B04_Upper_Arm": ([0.0, -18.0, 98.0], [195.0, 18.0, 142.0]),
        "B06_Forearm": ([175.0, -16.0, 120.0], [215.0, 16.0, 275.0]),
        "B08_Wrist_Link": ([195.0, -15.0, 257.0], [280.0, 15.0, 293.0]),
        "B09_Wrist_Roll_Motor": ([280.0, -22.0, 253.0], [340.0, 22.0, 297.0]),
        "B10_Tool_Flange": ([340.0, -40.0, 235.0], [350.0, 40.0, 315.0]),
        "B11_Gripper_Palm": ([350.0, -30.0, 235.0], [380.0, 30.0, 315.0]),
        "B12_Gripper_Finger_Upper": ([380.0, -7.5, 300.0], [450.0, 7.5, 315.0]),
        "B13_Gripper_Finger_Lower": ([380.0, -7.5, 235.0], [450.0, 7.5, 250.0]),
    }
    chain_failures = []
    chain_boxes = {}
    for name, expected in chain_expected.items():
        body = body_by_name.get(name)
        box = None if body is None else _bbox_mm(body)
        chain_boxes[name] = box
        if box is None or not _bbox_matches(box, expected[0], expected[1], 0.3):
            chain_failures.append(name)
    check(
        "geometry.eco_joint_to_tool_continuity",
        not chain_failures,
        chain_expected,
        {"failures": chain_failures, "boxes": chain_boxes},
    )

    cable_expected = {
        "B14_Cable_Upper": ([16.0, -4.0, 142.0], [179.0, 4.0, 150.0]),
        "B15_Cable_Forearm": ([215.0, -4.0, 132.5], [223.0, 4.0, 262.5]),
        "B16_Cable_Wrist": ([207.5, -4.0, 293.0], [267.5, 4.0, 301.0]),
    }
    cable_failures = []
    cable_boxes = {}
    for name, expected in cable_expected.items():
        body = body_by_name.get(name)
        box = None if body is None else _bbox_mm(body)
        cable_boxes[name] = box
        if box is None or not _bbox_matches(box, expected[0], expected[1], 0.3):
            cable_failures.append(name)
    check(
        "geometry.eco_cable_propagation",
        not cable_failures,
        cable_expected,
        {"failures": cable_failures, "boxes": cable_boxes},
    )

    joint_errors = [
        {"name": joint.name, "valid": joint.isValid}
        for joint in _items(root.asBuiltJoints)
        if not joint.isValid
    ]
    revolute_count = sum(
        1
        for joint in _items(root.asBuiltJoints)
        if joint.jointMotion.objectType == adsk.fusion.RevoluteJointMotion.classType()
    )
    check(
        "joints.healthy_after_eco",
        root.asBuiltJoints.count == 12 and revolute_count == 4 and not joint_errors,
        {"joints": 12, "revolute": 4, "errors": 0},
        {"joints": root.asBuiltJoints.count, "revolute": revolute_count, "errors": joint_errors},
    )

    failed = [item["id"] for item in checks if item["status"] != "pass"]
    result = {
        "ok": True,
        "schema_version": "fusion_parametric_oracle.v2",
        "oracle_id": "b06_robot_arm_assembly_eco",
        "case_id": "b06_robot_arm_assembly",
        "phase": "eco",
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
        },
    }
    payload = json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    print(payload)
    return payload
