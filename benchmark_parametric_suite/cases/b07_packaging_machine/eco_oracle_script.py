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


def _bbox_matches(box, minimum, maximum, tolerance=0.4):
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


def _box_set_matches(body_by_name, expected, tolerance=0.4):
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
        raise RuntimeError("B07 ECO oracle requires an active Fusion design")
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
        "MachineWidth": (76.0, "760 mm"),
        "BeltWidth": (40.0, "400 mm"),
        "DoorWidth": (46.0, "460 mm"),
        "MachineDepth": (50.0, None),
        "MachineHeight": (50.0, None),
        "ConveyorLength": (62.0, None),
        "ConveyorOpeningTopZ": (25.0, None),
        "DoorBottomZ": (25.0, None),
        "DoorHeight": (22.0, None),
        "DoorCenterX": (-12.0, None),
        "RearPanelBottomZ": (25.0, None),
        "RearPanelHeight": (22.0, None),
        "HingeCenterX": (-35.6, None),
        "LowerHingeZ": (27.0, None),
        "UpperHingeZ": (39.0, None),
        "RollerLength": (44.0, None),
        "ConveyorRailBottomZ": (16.0, None),
        "ConveyorSupportHeight": (13.0, None),
        "MotorCenterX": (28.0, None),
        "HopperTopWidth": (36.0, None),
        "HopperCenterY": (10.0, None),
        "HopperSupportCenterY": (20.0, None),
        "CabinetWidth": (7.0, None),
        "CabinetCenterX": (31.3, None),
    }
    parameter_failures = []
    for name, expected in expected_parameters.items():
        observed = parameters.get(name, {})
        expression_ok = True
        if expected[1] is not None:
            expression_ok = str(observed.get("expression") or "").replace(" ", "") == expected[1].replace(" ", "")
        if not _close(observed.get("value"), expected[0], 0.0001) or not expression_ok:
            parameter_failures.append({"name": name, "expected": expected, "observed": observed})
    check(
        "parameters.eco_and_dependency_propagation",
        len(parameters) == 59 and not parameter_failures,
        {"count": 59, "values": expected_parameters},
        {"count": len(parameters), "failures": parameter_failures},
    )

    child_components = [component for component in _items(design.allComponents) if component != root]
    occurrences = _items(root.allOccurrences)
    bodies = []
    body_by_name = {}
    errors = []
    sketch_errors = []
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
        if not body.isValid or not body.isSolid or body.lumps.count != 1 or not body.isVisible:
            errors.append({
                "component": component.name,
                "body": body.name,
                "valid": body.isValid,
                "solid": body.isSolid,
                "lumps": body.lumps.count,
                "visible": body.isVisible,
            })
        for feature in _items(component.features):
            if not feature.isValid or feature.errorOrWarningMessage:
                errors.append({
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
    expected_component_names = {f"CMP{index:02d}_{suffix}" for index, suffix in [
        (1, "Base_Left_Rail"),
        (2, "Base_Right_Rail"),
        (3, "Base_Front_Crossbar"),
        (4, "Base_Rear_Crossbar"),
        (5, "Post_Front_Left"),
        (6, "Post_Front_Right"),
        (7, "Post_Rear_Left"),
        (8, "Post_Rear_Right"),
        (9, "Top_Front_Crossbar"),
        (10, "Top_Rear_Crossbar"),
        (11, "Top_Left_Rail"),
        (12, "Top_Right_Rail"),
        (13, "Panel_Left"),
        (14, "Panel_Right"),
        (15, "Panel_Rear"),
        (16, "Access_Door"),
        (17, "Door_Hinge_Lower"),
        (18, "Door_Hinge_Upper"),
        (19, "Conveyor_Belt"),
        (20, "Roller_Infeed"),
        (21, "Roller_Quarter"),
        (22, "Roller_Three_Quarter"),
        (23, "Roller_Outfeed"),
        (24, "Drive_Motor"),
        (25, "Hopper"),
        (26, "Feed_Throat"),
        (27, "Control_Cabinet"),
        (28, "Conveyor_Rail_Left"),
        (29, "Conveyor_Rail_Right"),
        (30, "Conveyor_Support_Front_Left"),
        (31, "Conveyor_Support_Front_Right"),
        (32, "Conveyor_Support_Rear_Left"),
        (33, "Conveyor_Support_Rear_Right"),
        (34, "Hopper_Support_Crossbar"),
    ]}
    check(
        "assembly.counts_health_identity_after_eco",
        len(child_components) == 34
        and {component.name for component in child_components} == expected_component_names
        and len(occurrences) == 34
        and all(_identity_transform(occurrence) for occurrence in occurrences)
        and len(bodies) == 34
        and len(body_by_name) == 34
        and feature_count == 34
        and sketch_count == 35
        and root.bRepBodies.count == 0
        and not errors
        and not sketch_errors,
        {
            "components": 34,
            "occurrences": 34,
            "bodies": 34,
            "features": 34,
            "sketches": 35,
            "identity": True,
        },
        {
            "components": len(child_components),
            "occurrences": len(occurrences),
            "bodies": len(bodies),
            "features": feature_count,
            "sketches": sketch_count,
            "non_identity": [
                occurrence.fullPathName
                for occurrence in occurrences
                if not _identity_transform(occurrence)
            ],
            "errors": errors,
            "sketch_errors": sketch_errors,
        },
    )

    if bodies:
        global_box = _global_bbox(bodies)
        check(
            "geometry.eco_global_machine_bbox",
            _bbox_matches(global_box, [-380.0, -310.0, 0.0], [380.0, 310.0, 500.0], 0.5),
            {"min": [-380.0, -310.0, 0.0], "max": [380.0, 310.0, 500.0]},
            global_box,
        )
    else:
        check("geometry.eco_global_machine_bbox", False, "thirty-four bodies", None)

    frame_and_enclosure_expected = {
        "B01_Base_Left_Rail": ([-380.0, -250.0, 0.0], [-350.0, 250.0, 30.0]),
        "B02_Base_Right_Rail": ([350.0, -250.0, 0.0], [380.0, 250.0, 30.0]),
        "B09_Top_Front_Crossbar": ([-350.0, -250.0, 470.0], [350.0, -220.0, 500.0]),
        "B13_Panel_Left": ([-350.0, -220.0, 30.0], [-348.0, 220.0, 470.0]),
        "B14_Panel_Right": ([348.0, -220.0, 30.0], [350.0, 220.0, 470.0]),
        "B15_Panel_Rear": ([-350.0, 218.0, 250.0], [350.0, 220.0, 470.0]),
        "B16_Access_Door": ([-350.0, -220.0, 250.0], [110.0, -218.0, 470.0]),
        "B17_Door_Hinge_Lower": ([-362.0, -225.0, 270.0], [-350.0, -213.0, 330.0]),
        "B18_Door_Hinge_Upper": ([-362.0, -225.0, 390.0], [-350.0, -213.0, 450.0]),
    }
    frame_failures, frame_boxes = _box_set_matches(
        body_by_name,
        frame_and_enclosure_expected,
        0.4,
    )
    door_box = frame_boxes.get("B16_Access_Door")
    lower_hinge_box = frame_boxes.get("B17_Door_Hinge_Lower")
    enclosure_contact = (
        door_box is not None
        and lower_hinge_box is not None
        and _close(lower_hinge_box["max"][0], door_box["min"][0], 0.2)
    )
    check(
        "geometry.eco_frame_enclosure_and_left_door_anchor",
        not frame_failures and enclosure_contact,
        {"boxes": frame_and_enclosure_expected, "left_anchor_preserved": True},
        {"failures": frame_failures, "boxes": frame_boxes, "contact": enclosure_contact},
    )

    process_expected = {
        "B19_Conveyor_Belt": ([-200.0, -310.0, 220.0], [200.0, 310.0, 228.0]),
        "B20_Roller_Infeed": ([-220.0, -310.0, 180.0], [220.0, -270.0, 220.0]),
        "B21_Roller_Quarter": ([-220.0, -123.333333, 180.0], [220.0, -83.333333, 220.0]),
        "B22_Roller_Three_Quarter": ([-220.0, 83.333333, 180.0], [220.0, 123.333333, 220.0]),
        "B23_Roller_Outfeed": ([-220.0, 270.0, 180.0], [220.0, 310.0, 220.0]),
        "B24_Drive_Motor": ([240.0, 270.0, 180.0], [320.0, 310.0, 220.0]),
        "B25_Hopper": ([-180.0, -10.0, 320.0], [180.0, 210.0, 470.0]),
        "B26_Feed_Throat": ([-50.0, 60.0, 228.0], [50.0, 140.0, 320.0]),
    }
    process_failures, process_boxes = _box_set_matches(body_by_name, process_expected, 0.5)
    belt_box = process_boxes.get("B19_Conveyor_Belt")
    roller_boxes = [
        process_boxes.get("B20_Roller_Infeed"),
        process_boxes.get("B21_Roller_Quarter"),
        process_boxes.get("B22_Roller_Three_Quarter"),
        process_boxes.get("B23_Roller_Outfeed"),
    ]
    motor_box = process_boxes.get("B24_Drive_Motor")
    hopper_box = process_boxes.get("B25_Hopper")
    throat_box = process_boxes.get("B26_Feed_Throat")
    tangency_and_feed = (
        belt_box is not None
        and all(box is not None and _close(box["max"][2], belt_box["min"][2], 0.2) for box in roller_boxes)
        and motor_box is not None
        and _close(motor_box["min"][0] - roller_boxes[3]["max"][0], 20.0, 0.2)
        and all(
            _close(motor_box[key][axis], roller_boxes[3][key][axis], 0.2)
            for key in ("min", "max")
            for axis in (1, 2)
        )
        and hopper_box is not None
        and throat_box is not None
        and _close(throat_box["min"][2], belt_box["max"][2], 0.2)
        and _close(throat_box["max"][2], hopper_box["min"][2], 0.2)
        and _close(hopper_box["max"][1], 210.0, 0.2)
        and _close(hopper_box["max"][2], 470.0, 0.2)
    )
    check(
        "geometry.eco_conveyor_drive_hopper_propagation",
        not process_failures and tangency_and_feed,
        {
            "boxes": process_expected,
            "roller_tangency": True,
            "motor_coaxial": True,
            "motor_to_roller_axial_gap_mm": 20.0,
            "drive_coupling_modeled": False,
            "feed_continuity": True,
            "solid_metering_reservoir": True,
        },
        {"failures": process_failures, "boxes": process_boxes, "continuity": tangency_and_feed},
    )

    cabinet_expected = {
        "B27_Control_Cabinet": ([278.0, -90.0, 30.0], [348.0, 90.0, 210.0]),
    }
    cabinet_failures, cabinet_boxes = _box_set_matches(body_by_name, cabinet_expected, 0.4)
    cabinet_box = cabinet_boxes.get("B27_Control_Cabinet")
    right_panel_box = frame_boxes.get("B14_Panel_Right")
    cabinet_contact = (
        cabinet_box is not None
        and right_panel_box is not None
        and _close(cabinet_box["max"][0], right_panel_box["min"][0], 0.2)
    )
    check(
        "geometry.eco_control_cabinet_right_anchor",
        not cabinet_failures and cabinet_contact,
        {"box": cabinet_expected["B27_Control_Cabinet"], "right_panel_contact": True},
        {"failures": cabinet_failures, "box": cabinet_box, "contact": cabinet_contact},
    )

    conveyor_support_expected = {
        "B28_Conveyor_Rail_Left": ([-240.0, -310.0, 160.0], [-220.0, 310.0, 240.0]),
        "B29_Conveyor_Rail_Right": ([220.0, -310.0, 160.0], [240.0, 310.0, 240.0]),
        "B30_Conveyor_Support_Front_Left": ([-240.0, -250.0, 30.0], [-220.0, -220.0, 160.0]),
        "B31_Conveyor_Support_Front_Right": ([220.0, -250.0, 30.0], [240.0, -220.0, 160.0]),
        "B32_Conveyor_Support_Rear_Left": ([-240.0, 220.0, 30.0], [-220.0, 250.0, 160.0]),
        "B33_Conveyor_Support_Rear_Right": ([220.0, 220.0, 30.0], [240.0, 250.0, 160.0]),
        "B34_Hopper_Support_Crossbar": ([-350.0, 190.0, 470.0], [350.0, 210.0, 500.0]),
    }
    support_failures, support_boxes = _box_set_matches(body_by_name, conveyor_support_expected, 0.4)
    left_rail_box = support_boxes.get("B28_Conveyor_Rail_Left")
    right_rail_box = support_boxes.get("B29_Conveyor_Rail_Right")
    hopper_support_box = support_boxes.get("B34_Hopper_Support_Crossbar")
    support_contact = (
        left_rail_box is not None
        and right_rail_box is not None
        and hopper_support_box is not None
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
        and _close(hopper_box["max"][2], hopper_support_box["min"][2], 0.2)
        and _close(hopper_box["max"][1], hopper_support_box["max"][1], 0.2)
    )
    check(
        "geometry.eco_conveyor_and_hopper_support_propagation",
        not support_failures and support_contact,
        {"boxes": conveyor_support_expected, "support_contacts_preserved": True},
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
        "geometry.eco_critical_pairs_zero_interference",
        interference_free,
        {"pairs": critical_pairs, "count_each": 0, "volume_mm3_each": 0.0},
        interference_results,
    )

    joints = _items(root.asBuiltJoints)
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
    invalid_joints = [joint.name for joint in joints if not joint.isValid]
    graph = _joint_graph(joints, expected_component_names)
    check(
        "joints.spanning_tree_healthy_after_eco",
        len(joints) == 33
        and {joint.name for joint in joints} == set(expected_joint_endpoints)
        and revolute_names == expected_revolute_names
        and not invalid_joints
        and graph["endpoints"] == expected_joint_endpoints
        and not graph["invalid_endpoints"]
        and not graph["self_edges"]
        and not graph["unreachable"]
        and len(graph["visited"]) == 34,
        {
            "joints": 33,
            "nodes": 34,
            "revolute": sorted(expected_revolute_names),
            "endpoints": expected_joint_endpoints,
            "bfs_connected": True,
        },
        {
            "joints": len(joints),
            "revolute": sorted(revolute_names),
            "invalid": invalid_joints,
            "graph": graph,
        },
    )

    failed = [item["id"] for item in checks if item["status"] != "pass"]
    result = {
        "ok": True,
        "schema_version": "fusion_parametric_oracle.v2",
        "oracle_id": "b07_packaging_machine_eco",
        "case_id": "b07_packaging_machine",
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
