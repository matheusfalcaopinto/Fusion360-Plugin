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
    return len(values) == 16 and all(
        _close(values[index], expected[index], 0.000001) for index in range(16)
    )


def _box_set_matches(body_by_name, expected, tolerance=0.3):
    failures = []
    observed = {}
    for name, limits in expected.items():
        body = body_by_name.get(name)
        box = None if body is None else _bbox_mm(body)
        observed[name] = box
        if box is None or not _bbox_matches(box, limits[0], limits[1], tolerance):
            failures.append(name)
    return failures, observed


def _interference_summary(design, occurrence_one, occurrence_two):
    entities = adsk.core.ObjectCollection.create()
    entities.add(occurrence_one)
    entities.add(occurrence_two)
    interference_input = design.createInterferenceInput(entities)
    if interference_input is None:
        return {"available": False, "count": None, "volume_mm3": None}
    interference_input.areCoincidentFacesIncluded = False
    results = design.analyzeInterference(interference_input)
    if results is None:
        return {"available": False, "count": None, "volume_mm3": None}
    volume_mm3 = sum(results.item(index).interferenceBody.volume * 1000.0 for index in range(results.count))
    return {"available": True, "count": results.count, "volume_mm3": volume_mm3}


def _joint_graph(joints, expected_nodes):
    adjacency = {name: set() for name in expected_nodes}
    endpoints = {}
    invalid_endpoints = []
    self_edges = []
    for joint in joints:
        occurrence_one = joint.occurrenceOne
        occurrence_two = joint.occurrenceTwo
        name_one = None if occurrence_one is None else occurrence_one.component.name
        name_two = None if occurrence_two is None else occurrence_two.component.name
        endpoints[joint.name] = sorted([name for name in [name_one, name_two] if name is not None])
        if name_one not in adjacency or name_two not in adjacency:
            invalid_endpoints.append({"joint": joint.name, "one": name_one, "two": name_two})
        elif name_one == name_two:
            self_edges.append(joint.name)
        else:
            adjacency[name_one].add(name_two)
            adjacency[name_two].add(name_one)
    visited = set()
    frontier = [sorted(expected_nodes)[0]] if expected_nodes else []
    while frontier:
        current = frontier.pop(0)
        if current in visited:
            continue
        visited.add(current)
        frontier.extend(sorted(adjacency[current] - visited))
    return {
        "endpoints": endpoints,
        "invalid_endpoints": invalid_endpoints,
        "self_edges": self_edges,
        "visited": sorted(visited),
        "unreachable": sorted(expected_nodes - visited),
    }


def run(_context: str):
    app = adsk.core.Application.get()
    document = app.activeDocument
    design = adsk.fusion.Design.cast(app.activeProduct)
    if document is None or design is None:
        raise RuntimeError("B07 oracle requires an active Fusion design")
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
        "MachineWidth": 60.0,
        "MachineDepth": 50.0,
        "MachineHeight": 50.0,
        "FrameProfile": 3.0,
        "PostHeight": 44.0,
        "TopRailZ": 47.0,
        "PanelThickness": 0.2,
        "DoorWidth": 36.0,
        "ConveyorOpeningTopZ": 25.0,
        "DoorBottomZ": 25.0,
        "DoorHeight": 22.0,
        "DoorCenterX": -9.0,
        "FrontPanelY": -21.9,
        "RearPanelBottomZ": 25.0,
        "RearPanelHeight": 22.0,
        "HingeCenterX": -27.6,
        "LowerHingeZ": 27.0,
        "UpperHingeZ": 39.0,
        "BeltWidth": 30.0,
        "ConveyorLength": 62.0,
        "BeltHeight": 22.0,
        "RollerLength": 34.0,
        "InfeedRollerY": -29.0,
        "OutfeedRollerY": 29.0,
        "ConveyorRailBottomZ": 16.0,
        "ConveyorSupportHeight": 13.0,
        "MotorCenterX": 23.0,
        "HopperTopWidth": 26.0,
        "HopperCenterY": 10.0,
        "ThroatHeight": 9.2,
        "HopperSupportCenterY": 20.0,
        "CabinetWidth": 7.0,
        "CabinetCenterX": 23.3,
    }
    parameter_failures = []
    for name, expected_value in expected_parameters.items():
        actual = parameters.get(name, {}).get("value")
        if not _close(actual, expected_value, 0.0001):
            parameter_failures.append({"name": name, "expected": expected_value, "actual": actual})
    check(
        "parameters.initial_dependency_values",
        len(parameters) == 59 and not parameter_failures,
        {"count": 59, "critical_values_cm": expected_parameters},
        {"count": len(parameters), "failures": parameter_failures},
    )

    child_components = [component for component in _items(design.allComponents) if component != root]
    component_by_name = {component.name: component for component in child_components}
    expected_component_names = {
        "CMP01_Base_Left_Rail",
        "CMP02_Base_Right_Rail",
        "CMP03_Base_Front_Crossbar",
        "CMP04_Base_Rear_Crossbar",
        "CMP05_Post_Front_Left",
        "CMP06_Post_Front_Right",
        "CMP07_Post_Rear_Left",
        "CMP08_Post_Rear_Right",
        "CMP09_Top_Front_Crossbar",
        "CMP10_Top_Rear_Crossbar",
        "CMP11_Top_Left_Rail",
        "CMP12_Top_Right_Rail",
        "CMP13_Panel_Left",
        "CMP14_Panel_Right",
        "CMP15_Panel_Rear",
        "CMP16_Access_Door",
        "CMP17_Door_Hinge_Lower",
        "CMP18_Door_Hinge_Upper",
        "CMP19_Conveyor_Belt",
        "CMP20_Roller_Infeed",
        "CMP21_Roller_Quarter",
        "CMP22_Roller_Three_Quarter",
        "CMP23_Roller_Outfeed",
        "CMP24_Drive_Motor",
        "CMP25_Hopper",
        "CMP26_Feed_Throat",
        "CMP27_Control_Cabinet",
        "CMP28_Conveyor_Rail_Left",
        "CMP29_Conveyor_Rail_Right",
        "CMP30_Conveyor_Support_Front_Left",
        "CMP31_Conveyor_Support_Front_Right",
        "CMP32_Conveyor_Support_Rear_Left",
        "CMP33_Conveyor_Support_Rear_Right",
        "CMP34_Hopper_Support_Crossbar",
    }
    occurrences = _items(root.allOccurrences)
    check(
        "assembly.identity_component_hierarchy",
        len(child_components) == 34
        and set(component_by_name) == expected_component_names
        and len(occurrences) == 34
        and all(_identity_transform(occurrence) for occurrence in occurrences)
        and root.bRepBodies.count == 0,
        {"components": 34, "occurrences": 34, "identity": True, "root_bodies": 0},
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
    topology_errors = []
    feature_errors = []
    sketch_errors = []
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
        for sketch in _items(component.sketches):
            if (
                not sketch.isValid
                or not sketch.isFullyConstrained
                or sketch.healthState != adsk.fusion.FeatureHealthStates.HealthyFeatureHealthState
                or sketch.errorOrWarningMessage
            ):
                sketch_errors.append({
                    "component": component.name,
                    "sketch": sketch.name,
                    "valid": sketch.isValid,
                    "fully_constrained": sketch.isFullyConstrained,
                    "health": sketch.healthState,
                    "message": sketch.errorOrWarningMessage,
                })
    check(
        "topology.one_healthy_solid_per_component",
        len(bodies) == 34
        and len(body_by_name) == 34
        and not topology_errors
        and not feature_errors
        and not sketch_errors
        and child_feature_count == 34
        and child_sketch_count == 35
        and root.sketches.count == 1
        and root.sketches.item(0).name == "SK00_Machine_Envelope",
        {"bodies": 34, "features": 34, "child_sketches": 35, "root_reference_sketches": 1},
        {
            "bodies": sorted(body_by_name),
            "features": child_feature_count,
            "child_sketches": child_sketch_count,
            "root_sketches": root.sketches.count,
            "topology_errors": topology_errors,
            "feature_errors": feature_errors,
            "sketch_errors": sketch_errors,
        },
    )

    if bodies:
        global_box = _global_bbox(bodies)
        check(
            "geometry.global_machine_bbox",
            _bbox_matches(global_box, [-300.0, -310.0, 0.0], [300.0, 310.0, 500.0], 0.5),
            {"min": [-300.0, -310.0, 0.0], "max": [300.0, 310.0, 500.0]},
            global_box,
        )
    else:
        check("geometry.global_machine_bbox", False, "thirty-four bodies", None)

    frame_expected = {
        "B01_Base_Left_Rail": ([-300.0, -250.0, 0.0], [-270.0, 250.0, 30.0]),
        "B02_Base_Right_Rail": ([270.0, -250.0, 0.0], [300.0, 250.0, 30.0]),
        "B03_Base_Front_Crossbar": ([-270.0, -250.0, 0.0], [270.0, -220.0, 30.0]),
        "B04_Base_Rear_Crossbar": ([-270.0, 220.0, 0.0], [270.0, 250.0, 30.0]),
        "B05_Post_Front_Left": ([-300.0, -250.0, 30.0], [-270.0, -220.0, 470.0]),
        "B06_Post_Front_Right": ([270.0, -250.0, 30.0], [300.0, -220.0, 470.0]),
        "B07_Post_Rear_Left": ([-300.0, 220.0, 30.0], [-270.0, 250.0, 470.0]),
        "B08_Post_Rear_Right": ([270.0, 220.0, 30.0], [300.0, 250.0, 470.0]),
        "B09_Top_Front_Crossbar": ([-270.0, -250.0, 470.0], [270.0, -220.0, 500.0]),
        "B10_Top_Rear_Crossbar": ([-270.0, 220.0, 470.0], [270.0, 250.0, 500.0]),
        "B11_Top_Left_Rail": ([-300.0, -220.0, 470.0], [-270.0, 220.0, 500.0]),
        "B12_Top_Right_Rail": ([270.0, -220.0, 470.0], [300.0, 220.0, 500.0]),
    }
    frame_failures, frame_boxes = _box_set_matches(body_by_name, frame_expected, 0.3)
    check(
        "geometry.closed_twelve_member_frame",
        not frame_failures,
        frame_expected,
        {"failures": frame_failures, "boxes": frame_boxes},
    )

    enclosure_expected = {
        "B13_Panel_Left": ([-270.0, -220.0, 30.0], [-268.0, 220.0, 470.0]),
        "B14_Panel_Right": ([268.0, -220.0, 30.0], [270.0, 220.0, 470.0]),
        "B15_Panel_Rear": ([-270.0, 218.0, 250.0], [270.0, 220.0, 470.0]),
        "B16_Access_Door": ([-270.0, -220.0, 250.0], [90.0, -218.0, 470.0]),
        "B17_Door_Hinge_Lower": ([-282.0, -225.0, 270.0], [-270.0, -213.0, 330.0]),
        "B18_Door_Hinge_Upper": ([-282.0, -225.0, 390.0], [-270.0, -213.0, 450.0]),
    }
    enclosure_failures, enclosure_boxes = _box_set_matches(body_by_name, enclosure_expected, 0.3)
    door_box = enclosure_boxes.get("B16_Access_Door")
    lower_hinge_box = enclosure_boxes.get("B17_Door_Hinge_Lower")
    hinge_door_contact = (
        door_box is not None
        and lower_hinge_box is not None
        and _close(lower_hinge_box["max"][0], door_box["min"][0], 0.2)
    )
    check(
        "geometry.enclosure_door_and_hinges",
        not enclosure_failures and hinge_door_contact,
        {"boxes": enclosure_expected, "hinge_touches_door_left_edge": True},
        {
            "failures": enclosure_failures,
            "boxes": enclosure_boxes,
            "hinge_touches_door_left_edge": hinge_door_contact,
        },
    )

    conveyor_expected = {
        "B19_Conveyor_Belt": ([-150.0, -310.0, 220.0], [150.0, 310.0, 228.0]),
        "B20_Roller_Infeed": ([-170.0, -310.0, 180.0], [170.0, -270.0, 220.0]),
        "B21_Roller_Quarter": ([-170.0, -123.333333, 180.0], [170.0, -83.333333, 220.0]),
        "B22_Roller_Three_Quarter": ([-170.0, 83.333333, 180.0], [170.0, 123.333333, 220.0]),
        "B23_Roller_Outfeed": ([-170.0, 270.0, 180.0], [170.0, 310.0, 220.0]),
        "B24_Drive_Motor": ([190.0, 270.0, 180.0], [270.0, 310.0, 220.0]),
    }
    conveyor_failures, conveyor_boxes = _box_set_matches(body_by_name, conveyor_expected, 0.4)
    belt_box = conveyor_boxes.get("B19_Conveyor_Belt")
    roller_boxes = [
        conveyor_boxes.get("B20_Roller_Infeed"),
        conveyor_boxes.get("B21_Roller_Quarter"),
        conveyor_boxes.get("B22_Roller_Three_Quarter"),
        conveyor_boxes.get("B23_Roller_Outfeed"),
    ]
    motor_box = conveyor_boxes.get("B24_Drive_Motor")
    tangency = (
        belt_box is not None
        and all(box is not None and _close(box["max"][2], belt_box["min"][2], 0.2) for box in roller_boxes)
        and _close(roller_boxes[0]["min"][1], belt_box["min"][1], 0.2)
        and _close(roller_boxes[3]["max"][1], belt_box["max"][1], 0.2)
        and motor_box is not None
        and _close(motor_box["min"][0] - roller_boxes[3]["max"][0], 20.0, 0.2)
        and all(
            _close(motor_box[key][axis], roller_boxes[3][key][axis], 0.2)
            for key in ("min", "max")
            for axis in (1, 2)
        )
    )
    check(
        "geometry.conveyor_tangency_and_drive_chain",
        not conveyor_failures and tangency,
        {
            "boxes": conveyor_expected,
            "four_rollers_tangent": True,
            "motor_coaxial": True,
            "motor_to_roller_axial_gap_mm": 20.0,
            "drive_coupling_modeled": False,
        },
        {"failures": conveyor_failures, "boxes": conveyor_boxes, "tangency": tangency},
    )

    feed_expected = {
        "B25_Hopper": ([-130.0, -10.0, 320.0], [130.0, 210.0, 470.0]),
        "B26_Feed_Throat": ([-50.0, 60.0, 228.0], [50.0, 140.0, 320.0]),
    }
    feed_failures, feed_boxes = _box_set_matches(body_by_name, feed_expected, 0.5)
    hopper_box = feed_boxes.get("B25_Hopper")
    throat_box = feed_boxes.get("B26_Feed_Throat")
    hopper_support_expected = {
        "B34_Hopper_Support_Crossbar": ([-270.0, 190.0, 470.0], [270.0, 210.0, 500.0]),
    }
    hopper_support_failures, hopper_support_boxes = _box_set_matches(
        body_by_name,
        hopper_support_expected,
        0.4,
    )
    hopper_support_box = hopper_support_boxes.get("B34_Hopper_Support_Crossbar")
    feed_continuity = (
        belt_box is not None
        and throat_box is not None
        and hopper_box is not None
        and hopper_support_box is not None
        and _close(throat_box["min"][2], belt_box["max"][2], 0.2)
        and _close(throat_box["max"][2], hopper_box["min"][2], 0.2)
        and _close(hopper_box["max"][2], hopper_support_box["min"][2], 0.2)
        and _close(hopper_box["max"][1], hopper_support_box["max"][1], 0.2)
    )
    check(
        "geometry.continuous_belt_throat_hopper_structure_path",
        not feed_failures and not hopper_support_failures and feed_continuity,
        {
            "boxes": feed_expected,
            "support": hopper_support_expected,
            "vertical_gaps": 0,
            "solid_metering_reservoir": True,
        },
        {
            "failures": feed_failures + hopper_support_failures,
            "boxes": feed_boxes,
            "support_boxes": hopper_support_boxes,
            "continuity": feed_continuity,
        },
    )

    cabinet_expected = {
        "B27_Control_Cabinet": ([198.0, -90.0, 30.0], [268.0, 90.0, 210.0]),
    }
    cabinet_failures, cabinet_boxes = _box_set_matches(body_by_name, cabinet_expected, 0.3)
    cabinet_box = cabinet_boxes.get("B27_Control_Cabinet")
    right_panel_box = enclosure_boxes.get("B14_Panel_Right")
    cabinet_contact = (
        cabinet_box is not None
        and right_panel_box is not None
        and _close(cabinet_box["max"][0], right_panel_box["min"][0], 0.2)
        and _close(cabinet_box["min"][2], 30.0, 0.2)
    )
    check(
        "geometry.control_cabinet_anchored",
        not cabinet_failures and cabinet_contact,
        {"box": cabinet_expected["B27_Control_Cabinet"], "right_panel_contact": True},
        {"failures": cabinet_failures, "box": cabinet_box, "contact": cabinet_contact},
    )

    conveyor_support_expected = {
        "B28_Conveyor_Rail_Left": ([-190.0, -310.0, 160.0], [-170.0, 310.0, 240.0]),
        "B29_Conveyor_Rail_Right": ([170.0, -310.0, 160.0], [190.0, 310.0, 240.0]),
        "B30_Conveyor_Support_Front_Left": ([-190.0, -250.0, 30.0], [-170.0, -220.0, 160.0]),
        "B31_Conveyor_Support_Front_Right": ([170.0, -250.0, 30.0], [190.0, -220.0, 160.0]),
        "B32_Conveyor_Support_Rear_Left": ([-190.0, 220.0, 30.0], [-170.0, 250.0, 160.0]),
        "B33_Conveyor_Support_Rear_Right": ([170.0, 220.0, 30.0], [190.0, 250.0, 160.0]),
    }
    support_failures, support_boxes = _box_set_matches(body_by_name, conveyor_support_expected, 0.4)
    left_rail_box = support_boxes.get("B28_Conveyor_Rail_Left")
    right_rail_box = support_boxes.get("B29_Conveyor_Rail_Right")
    support_contact = (
        left_rail_box is not None
        and right_rail_box is not None
        and all(
            support_boxes.get(name) is not None
            and _close(support_boxes[name]["max"][2], 160.0, 0.2)
            and _close(support_boxes[name]["min"][2], 30.0, 0.2)
            for name in [
                "B30_Conveyor_Support_Front_Left",
                "B31_Conveyor_Support_Front_Right",
                "B32_Conveyor_Support_Rear_Left",
                "B33_Conveyor_Support_Rear_Right",
            ]
        )
        and _close(left_rail_box["min"][2], 160.0, 0.2)
        and _close(right_rail_box["min"][2], 160.0, 0.2)
        and roller_boxes[0] is not None
        and _close(left_rail_box["max"][0], roller_boxes[0]["min"][0], 0.2)
        and _close(right_rail_box["min"][0], roller_boxes[0]["max"][0], 0.2)
    )
    check(
        "geometry.conveyor_rails_physically_supported_by_frame",
        not support_failures and support_contact,
        {
            "boxes": conveyor_support_expected,
            "support_to_frame_and_rail_contact": True,
            "rail_to_roller_contact": True,
        },
        {"failures": support_failures, "boxes": support_boxes, "contact": support_contact},
    )

    occurrence_by_component = {
        occurrence.component.name: occurrence
        for occurrence in occurrences
    }
    critical_pairs = {
        "door_belt": ("CMP16_Access_Door", "CMP19_Conveyor_Belt"),
        "rear_panel_belt": ("CMP15_Panel_Rear", "CMP19_Conveyor_Belt"),
        "rear_panel_hopper": ("CMP15_Panel_Rear", "CMP25_Hopper"),
        "cabinet_right_rail": ("CMP27_Control_Cabinet", "CMP29_Conveyor_Rail_Right"),
        "motor_right_rail": ("CMP24_Drive_Motor", "CMP29_Conveyor_Rail_Right"),
    }
    interference_results = {}
    for pair_name, pair in critical_pairs.items():
        occurrence_one = occurrence_by_component.get(pair[0])
        occurrence_two = occurrence_by_component.get(pair[1])
        if occurrence_one is None or occurrence_two is None:
            interference_results[pair_name] = {
                "available": False,
                "count": None,
                "volume_mm3": None,
            }
        else:
            interference_results[pair_name] = _interference_summary(
                design,
                occurrence_one,
                occurrence_two,
            )
    interference_free = all(
        result["available"]
        and result["count"] == 0
        and _close(result["volume_mm3"], 0.0, 0.000001)
        for result in interference_results.values()
    )
    check(
        "geometry.critical_pairs_zero_interference",
        interference_free,
        {"pairs": critical_pairs, "count_each": 0, "volume_mm3_each": 0.0},
        interference_results,
    )

    joints = _items(root.asBuiltJoints)
    joint_data = [
        {"name": joint.name, "valid": joint.isValid, "motion": joint.jointMotion.objectType}
        for joint in joints
    ]
    expected_joint_endpoints_raw = {
        "J01_Base_Right_Front": ("CMP02_Base_Right_Rail", "CMP03_Base_Front_Crossbar"),
        "J02_Base_Left_Front": ("CMP01_Base_Left_Rail", "CMP03_Base_Front_Crossbar"),
        "J03_Base_Rear_Left": ("CMP04_Base_Rear_Crossbar", "CMP01_Base_Left_Rail"),
        "J04_Post_Front_Left": ("CMP05_Post_Front_Left", "CMP01_Base_Left_Rail"),
        "J05_Post_Front_Right": ("CMP06_Post_Front_Right", "CMP02_Base_Right_Rail"),
        "J06_Post_Rear_Left": ("CMP07_Post_Rear_Left", "CMP01_Base_Left_Rail"),
        "J07_Post_Rear_Right": ("CMP08_Post_Rear_Right", "CMP02_Base_Right_Rail"),
        "J08_Top_Front": ("CMP09_Top_Front_Crossbar", "CMP05_Post_Front_Left"),
        "J09_Top_Rear": ("CMP10_Top_Rear_Crossbar", "CMP07_Post_Rear_Left"),
        "J10_Top_Left": ("CMP11_Top_Left_Rail", "CMP05_Post_Front_Left"),
        "J11_Top_Right": ("CMP12_Top_Right_Rail", "CMP06_Post_Front_Right"),
        "J12_Panel_Left": ("CMP13_Panel_Left", "CMP05_Post_Front_Left"),
        "J13_Panel_Right": ("CMP14_Panel_Right", "CMP06_Post_Front_Right"),
        "J14_Panel_Rear": ("CMP15_Panel_Rear", "CMP07_Post_Rear_Left"),
        "J15_Access_Door_Pivot": ("CMP17_Door_Hinge_Lower", "CMP05_Post_Front_Left"),
        "J16_Hinge_Lower": ("CMP17_Door_Hinge_Lower", "CMP16_Access_Door"),
        "J17_Hinge_Upper": ("CMP18_Door_Hinge_Upper", "CMP16_Access_Door"),
        "J18_Conveyor_Frame": ("CMP19_Conveyor_Belt", "CMP03_Base_Front_Crossbar"),
        "J19_Roller_Infeed": ("CMP20_Roller_Infeed", "CMP19_Conveyor_Belt"),
        "J20_Roller_Quarter": ("CMP21_Roller_Quarter", "CMP19_Conveyor_Belt"),
        "J21_Roller_Three_Quarter": ("CMP22_Roller_Three_Quarter", "CMP19_Conveyor_Belt"),
        "J22_Roller_Outfeed": ("CMP23_Roller_Outfeed", "CMP19_Conveyor_Belt"),
        "J23_Drive_Motor": ("CMP24_Drive_Motor", "CMP23_Roller_Outfeed"),
        "J24_Hopper_Throat": ("CMP25_Hopper", "CMP26_Feed_Throat"),
        "J25_Throat_Belt": ("CMP26_Feed_Throat", "CMP19_Conveyor_Belt"),
        "J26_Control_Cabinet": ("CMP27_Control_Cabinet", "CMP14_Panel_Right"),
        "J27_Rail_Left_Front_Support": ("CMP28_Conveyor_Rail_Left", "CMP30_Conveyor_Support_Front_Left"),
        "J28_Rail_Right_Front_Support": ("CMP29_Conveyor_Rail_Right", "CMP31_Conveyor_Support_Front_Right"),
        "J29_Front_Left_Support_Frame": ("CMP30_Conveyor_Support_Front_Left", "CMP03_Base_Front_Crossbar"),
        "J30_Front_Right_Support_Frame": ("CMP31_Conveyor_Support_Front_Right", "CMP03_Base_Front_Crossbar"),
        "J31_Rear_Left_Support_Rail": ("CMP32_Conveyor_Support_Rear_Left", "CMP28_Conveyor_Rail_Left"),
        "J32_Rear_Right_Support_Rail": ("CMP33_Conveyor_Support_Rear_Right", "CMP29_Conveyor_Rail_Right"),
        "J33_Hopper_Support_Frame": ("CMP34_Hopper_Support_Crossbar", "CMP11_Top_Left_Rail"),
    }
    expected_joint_endpoints = {
        name: sorted(pair)
        for name, pair in expected_joint_endpoints_raw.items()
    }
    expected_joint_names = set(expected_joint_endpoints)
    revolute_names = {
        joint.name
        for joint in joints
        if joint.jointMotion.objectType == adsk.fusion.RevoluteJointMotion.classType()
    }
    expected_revolute_names = {
        "J15_Access_Door_Pivot",
        "J19_Roller_Infeed",
        "J20_Roller_Quarter",
        "J21_Roller_Three_Quarter",
        "J22_Roller_Outfeed",
    }
    graph = _joint_graph(joints, expected_component_names)
    check(
        "joints.connected_named_spanning_tree",
        len(joints) == 33
        and {item["name"] for item in joint_data} == expected_joint_names
        and revolute_names == expected_revolute_names
        and all(item["valid"] for item in joint_data)
        and graph["endpoints"] == expected_joint_endpoints
        and not graph["invalid_endpoints"]
        and not graph["self_edges"]
        and not graph["unreachable"]
        and len(graph["visited"]) == 34,
        {
            "edges": 33,
            "nodes": 34,
            "revolute": sorted(expected_revolute_names),
            "endpoints": expected_joint_endpoints,
            "bfs_connected": True,
        },
        {
            "edges": len(joints),
            "revolute": sorted(revolute_names),
            "joints": joint_data,
            "graph": graph,
        },
    )

    failed = [item["id"] for item in checks if item["status"] != "pass"]
    result = {
        "ok": True,
        "schema_version": "fusion_parametric_oracle.v2",
        "oracle_id": "b07_packaging_machine_geometry",
        "case_id": "b07_packaging_machine",
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
    payload = json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    print(payload)
    return payload
