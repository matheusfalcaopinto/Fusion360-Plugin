import adsk.core
import adsk.fusion
import json
import math


def _point(sketch, world_x, world_y, world_z):
    x_direction = sketch.xDirection
    y_direction = sketch.yDirection
    sketch_x = (
        world_x * x_direction.x
        + world_y * x_direction.y
        + world_z * x_direction.z
    )
    sketch_y = (
        world_x * y_direction.x
        + world_y * y_direction.y
        + world_z * y_direction.z
    )
    return adsk.core.Point3D.create(sketch_x, sketch_y, 0)


def run(_context: str):
    root = target_components["root"]
    design = root.parentDesign

    component = root

    parameter_specs = [
        ("BaseWidth", "90 mm", "mm", "Overall base width"),
        ("BaseDepth", "70 mm", "mm", "Overall base depth"),
        ("BaseThickness", "6 mm", "mm", "Base plate thickness"),
        ("FlangeWidth", "70 mm", "mm", "Rear flange width"),
        ("FlangeHeight", "60 mm", "mm", "Flange height above base"),
        ("FlangeThickness", "6 mm", "mm", "Rear flange thickness"),
        ("MotorCenterHeight", "30 mm", "mm", "Motor center above base top"),
        ("MotorBoltSpacing", "31 mm", "mm", "NEMA 17 bolt spacing"),
        ("MotorBoltDiameter", "4.5 mm", "mm", "NEMA 17 mounting hole diameter"),
        ("MotorClearanceDiameter", "24 mm", "mm", "Motor shaft clearance diameter"),
        ("SlotSpacing", "60 mm", "mm", "Base slot center spacing"),
        ("SlotLength", "24 mm", "mm", "Overall base slot length"),
        ("SlotWidth", "6.5 mm", "mm", "Base slot width"),
        ("SlotCenterY", "30 mm", "mm", "Slot center from front edge"),
        ("GussetLeg", "24 mm", "mm", "Equal gusset leg length"),
        ("GussetThickness", "6 mm", "mm", "Gusset thickness"),
        ("EdgeFillet", "3 mm", "mm", "Flange upper corner fillet"),
    ]
    for parameter_name, expression, units, comment in parameter_specs:
        design.userParameters.add(
            parameter_name,
            adsk.core.ValueInput.createByString(expression),
            units,
            comment,
        )

    horizontal = adsk.fusion.DimensionOrientations.HorizontalDimensionOrientation
    vertical = adsk.fusion.DimensionOrientations.VerticalDimensionOrientation

    base_sketch = component.sketches.add(component.xYConstructionPlane)
    base_sketch.name = "SK01_Base_Profile"
    base_lines = base_sketch.sketchCurves.sketchLines

    base_front = base_lines.addByTwoPoints(
        _point(base_sketch, -4.5, 0, 0),
        _point(base_sketch, 4.5, 0, 0),
    )
    base_right = base_lines.addByTwoPoints(
        base_front.endSketchPoint,
        _point(base_sketch, 4.5, 7, 0),
    )
    base_back = base_lines.addByTwoPoints(
        base_right.endSketchPoint,
        _point(base_sketch, -4.5, 7, 0),
    )
    base_left = base_lines.addByTwoPoints(
        base_back.endSketchPoint,
        base_front.startSketchPoint,
    )

    base_constraints = base_sketch.geometricConstraints
    base_constraints.addHorizontal(base_front)
    base_constraints.addVertical(base_right)
    base_constraints.addHorizontal(base_back)
    base_constraints.addVertical(base_left)
    base_constraints.addMidPoint(base_sketch.originPoint, base_front)

    base_width_dimension = base_sketch.sketchDimensions.addDistanceDimension(
        base_front.startSketchPoint,
        base_front.endSketchPoint,
        horizontal,
        _point(base_sketch, 0, -1, 0),
    )
    base_width_dimension.parameter.expression = "BaseWidth"

    base_depth_dimension = base_sketch.sketchDimensions.addDistanceDimension(
        base_right.startSketchPoint,
        base_right.endSketchPoint,
        vertical,
        _point(base_sketch, 5.5, 3.5, 0),
    )
    base_depth_dimension.parameter.expression = "BaseDepth"

    base_normal_z = component.xYConstructionPlane.geometry.normal.z
    base_distance_expression = "BaseThickness"
    if base_normal_z < 0:
        base_distance_expression = "-BaseThickness"

    base_extrude = component.features.extrudeFeatures.addSimple(
        base_sketch.profiles.item(0),
        adsk.core.ValueInput.createByString(base_distance_expression),
        adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
    )
    base_extrude.name = "EX01_Base_Plate"
    part_body = base_extrude.bodies.item(0)
    part_body.name = "NEMA17_Adjustable_Bracket"

    xz_normal_y = component.xZConstructionPlane.geometry.normal.y
    flange_plane_offset_expression = "BaseDepth - FlangeThickness/2"
    if xz_normal_y < 0:
        flange_plane_offset_expression = "-(BaseDepth - FlangeThickness/2)"

    flange_plane_input = component.constructionPlanes.createInput()
    flange_plane_input.setByOffset(
        component.xZConstructionPlane,
        adsk.core.ValueInput.createByString(flange_plane_offset_expression),
    )
    flange_plane = component.constructionPlanes.add(flange_plane_input)
    flange_plane.name = "CP01_Flange_Midplane"

    flange_sketch = component.sketches.add(flange_plane)
    flange_sketch.name = "SK02_Flange_Profile"
    flange_lines = flange_sketch.sketchCurves.sketchLines

    flange_bottom = flange_lines.addByTwoPoints(
        _point(flange_sketch, -3.5, 6.7, 0.6),
        _point(flange_sketch, 3.5, 6.7, 0.6),
    )
    flange_right = flange_lines.addByTwoPoints(
        flange_bottom.endSketchPoint,
        _point(flange_sketch, 3.5, 6.7, 6.6),
    )
    flange_top = flange_lines.addByTwoPoints(
        flange_right.endSketchPoint,
        _point(flange_sketch, -3.5, 6.7, 6.6),
    )
    flange_left = flange_lines.addByTwoPoints(
        flange_top.endSketchPoint,
        flange_bottom.startSketchPoint,
    )
    flange_anchor = flange_sketch.sketchPoints.add(
        _point(flange_sketch, 0, 6.7, 0.6)
    )

    flange_constraints = flange_sketch.geometricConstraints
    flange_constraints.addHorizontal(flange_bottom)
    flange_constraints.addVertical(flange_right)
    flange_constraints.addHorizontal(flange_top)
    flange_constraints.addVertical(flange_left)
    flange_constraints.addMidPoint(flange_anchor, flange_bottom)
    flange_constraints.addVerticalPoints(
        flange_sketch.originPoint,
        flange_anchor,
    )

    flange_base_height_dimension = (
        flange_sketch.sketchDimensions.addDistanceDimension(
            flange_sketch.originPoint,
            flange_anchor,
            vertical,
            _point(flange_sketch, 1, 6.7, 0.3),
        )
    )
    flange_base_height_dimension.parameter.expression = (
        "BaseThickness - BaseThickness/100"
    )

    flange_width_dimension = flange_sketch.sketchDimensions.addDistanceDimension(
        flange_bottom.startSketchPoint,
        flange_bottom.endSketchPoint,
        horizontal,
        _point(flange_sketch, 0, 6.7, -0.4),
    )
    flange_width_dimension.parameter.expression = "FlangeWidth"

    flange_height_dimension = flange_sketch.sketchDimensions.addDistanceDimension(
        flange_right.startSketchPoint,
        flange_right.endSketchPoint,
        vertical,
        _point(flange_sketch, 4.5, 6.7, 3.6),
    )
    flange_height_dimension.parameter.expression = (
        "FlangeHeight + BaseThickness/100"
    )

    flange_extrudes = component.features.extrudeFeatures
    flange_input = flange_extrudes.createInput(
        flange_sketch.profiles.item(0),
        adsk.fusion.FeatureOperations.JoinFeatureOperation,
    )
    flange_input.setSymmetricExtent(
        adsk.core.ValueInput.createByString("FlangeThickness"),
        True,
    )
    flange_extrude = flange_extrudes.add(flange_input)
    flange_extrude.name = "EX02_Rear_Flange"

    fillet_edges = adsk.core.ObjectCollection.create()
    tolerance = 0.01
    expected_top_z = 6.6
    expected_half_flange_width = 3.5
    expected_flange_y_span = 0.6

    for edge_index in range(part_body.edges.count):
        edge = part_body.edges.item(edge_index)
        edge_box = edge.boundingBox
        x_span = edge_box.maxPoint.x - edge_box.minPoint.x
        y_span = edge_box.maxPoint.y - edge_box.minPoint.y
        z_span = edge_box.maxPoint.z - edge_box.minPoint.z
        fixed_x = math.fabs(x_span) < tolerance
        fixed_z = math.fabs(z_span) < tolerance
        at_top = math.fabs(edge_box.minPoint.z - expected_top_z) < tolerance
        at_side = (
            math.fabs(
                math.fabs(edge_box.minPoint.x)
                - expected_half_flange_width
            )
            < tolerance
        )
        through_thickness = (
            math.fabs(y_span - expected_flange_y_span) < tolerance
        )
        if fixed_x and fixed_z and at_top and at_side and through_thickness:
            fillet_edges.add(edge)

    if fillet_edges.count != 2:
        raise RuntimeError("Expected exactly two flange top edges for fillet")

    fillet_input = component.features.filletFeatures.createInput()
    fillet_input.edgeSetInputs.addConstantRadiusEdgeSet(
        fillet_edges,
        adsk.core.ValueInput.createByString("EdgeFillet"),
        False,
    )
    flange_fillet = component.features.filletFeatures.add(fillet_input)
    flange_fillet.name = "FL01_Flange_Top_Corners"

    slot_sketch = component.sketches.add(component.xYConstructionPlane)
    slot_sketch.name = "SK03_Adjustment_Slots"

    left_slot_center = slot_sketch.sketchPoints.add(
        _point(slot_sketch, -3, 3, 0)
    )
    right_slot_center = slot_sketch.sketchPoints.add(
        _point(slot_sketch, 3, 3, 0)
    )
    slot_constraints = slot_sketch.geometricConstraints
    slot_constraints.addHorizontalPoints(
        left_slot_center,
        right_slot_center,
    )

    left_slot_x_dimension = slot_sketch.sketchDimensions.addDistanceDimension(
        slot_sketch.originPoint,
        left_slot_center,
        horizontal,
        _point(slot_sketch, -1.5, 1.2, 0),
    )
    left_slot_x_dimension.parameter.expression = "SlotSpacing/2"

    left_slot_y_dimension = slot_sketch.sketchDimensions.addDistanceDimension(
        slot_sketch.originPoint,
        left_slot_center,
        vertical,
        _point(slot_sketch, -4.3, 1.5, 0),
    )
    left_slot_y_dimension.parameter.expression = "SlotCenterY"

    slot_spacing_dimension = slot_sketch.sketchDimensions.addDistanceDimension(
        left_slot_center,
        right_slot_center,
        horizontal,
        _point(slot_sketch, 0, 1.2, 0),
    )
    slot_spacing_dimension.parameter.expression = "SlotSpacing"

    slot_angle_expression = "90 deg"
    if math.fabs(slot_sketch.yDirection.y) < 0.9:
        slot_angle_expression = "0 deg"

    left_slot = slot_sketch.addCenterPointSlot(
        left_slot_center,
        _point(slot_sketch, -3, 4, 0),
        adsk.core.ValueInput.createByString("SlotWidth"),
        True,
        adsk.core.ValueInput.createByString("SlotLength/2"),
        adsk.core.ValueInput.createByString(slot_angle_expression),
    )
    right_slot = slot_sketch.addCenterPointSlot(
        right_slot_center,
        _point(slot_sketch, 3, 4, 0),
        adsk.core.ValueInput.createByString("SlotWidth"),
        True,
        adsk.core.ValueInput.createByString("SlotLength/2"),
        adsk.core.ValueInput.createByString(slot_angle_expression),
    )

    slot_profiles = adsk.core.ObjectCollection.create()
    for slot_profile_index in range(slot_sketch.profiles.count):
        slot_profiles.add(slot_sketch.profiles.item(slot_profile_index))

    slot_distance_expression = "BaseThickness"
    if base_normal_z < 0:
        slot_distance_expression = "-BaseThickness"

    slot_cut = component.features.extrudeFeatures.addSimple(
        slot_profiles,
        adsk.core.ValueInput.createByString(slot_distance_expression),
        adsk.fusion.FeatureOperations.CutFeatureOperation,
    )
    slot_cut.name = "EX03_Base_Adjustment_Slots"

    hole_sketch = component.sketches.add(flange_plane)
    hole_sketch.name = "SK04_NEMA17_Hole_Pattern"
    hole_circles = hole_sketch.sketchCurves.sketchCircles
    hole_constraints = hole_sketch.geometricConstraints
    hole_dimensions = hole_sketch.sketchDimensions

    motor_clearance = hole_circles.addByCenterRadius(
        _point(hole_sketch, 0, 6.7, 3.6),
        1.2,
    )
    motor_center = motor_clearance.centerSketchPoint
    hole_constraints.addVerticalPoints(hole_sketch.originPoint, motor_center)

    motor_center_height_dimension = hole_dimensions.addDistanceDimension(
        hole_sketch.originPoint,
        motor_center,
        vertical,
        _point(hole_sketch, 1, 6.7, 1.8),
    )
    motor_center_height_dimension.parameter.expression = (
        "BaseThickness + MotorCenterHeight"
    )

    clearance_diameter_dimension = hole_dimensions.addDiameterDimension(
        motor_clearance,
        _point(hole_sketch, 1.5, 6.7, 4.8),
    )
    clearance_diameter_dimension.parameter.expression = "MotorClearanceDiameter"

    hole_bottom_left = hole_circles.addByCenterRadius(
        _point(hole_sketch, -1.55, 6.7, 2.05),
        0.225,
    )
    hole_bottom_right = hole_circles.addByCenterRadius(
        _point(hole_sketch, 1.55, 6.7, 2.05),
        0.225,
    )
    hole_top_left = hole_circles.addByCenterRadius(
        _point(hole_sketch, -1.55, 6.7, 5.15),
        0.225,
    )
    hole_top_right = hole_circles.addByCenterRadius(
        _point(hole_sketch, 1.55, 6.7, 5.15),
        0.225,
    )

    hole_centers = [
        hole_bottom_left.centerSketchPoint,
        hole_bottom_right.centerSketchPoint,
        hole_top_left.centerSketchPoint,
        hole_top_right.centerSketchPoint,
    ]
    hole_text_points = [
        _point(hole_sketch, -0.8, 6.7, 2.8),
        _point(hole_sketch, 0.8, 6.7, 2.8),
        _point(hole_sketch, -0.8, 6.7, 4.4),
        _point(hole_sketch, 0.8, 6.7, 4.4),
    ]
    for hole_center, hole_text_point in zip(hole_centers, hole_text_points):
        hole_x_dimension = hole_dimensions.addDistanceDimension(
            motor_center,
            hole_center,
            horizontal,
            hole_text_point,
        )
        hole_x_dimension.parameter.expression = "MotorBoltSpacing/2"

        hole_z_dimension = hole_dimensions.addDistanceDimension(
            motor_center,
            hole_center,
            vertical,
            hole_text_point,
        )
        hole_z_dimension.parameter.expression = "MotorBoltSpacing/2"

    bolt_diameter_dimension = hole_dimensions.addDiameterDimension(
        hole_bottom_left,
        _point(hole_sketch, -2.3, 6.7, 1.4),
    )
    bolt_diameter_dimension.parameter.expression = "MotorBoltDiameter"
    hole_constraints.addEqual(hole_bottom_left, hole_bottom_right)
    hole_constraints.addEqual(hole_bottom_left, hole_top_left)
    hole_constraints.addEqual(hole_bottom_left, hole_top_right)

    hole_profiles = adsk.core.ObjectCollection.create()
    for hole_profile_index in range(hole_sketch.profiles.count):
        hole_profiles.add(hole_sketch.profiles.item(hole_profile_index))

    hole_extrudes = component.features.extrudeFeatures
    hole_input = hole_extrudes.createInput(
        hole_profiles,
        adsk.fusion.FeatureOperations.CutFeatureOperation,
    )
    hole_input.setSymmetricExtent(
        adsk.core.ValueInput.createByString("FlangeThickness"),
        True,
    )
    hole_cut = hole_extrudes.add(hole_input)
    hole_cut.name = "EX04_NEMA17_Through_Holes"

    yz_normal_x = component.yZConstructionPlane.geometry.normal.x
    gusset_plane_offset_expression = "-SlotSpacing/2"
    if yz_normal_x < 0:
        gusset_plane_offset_expression = "SlotSpacing/2"

    gusset_plane_input = component.constructionPlanes.createInput()
    gusset_plane_input.setByOffset(
        component.yZConstructionPlane,
        adsk.core.ValueInput.createByString(gusset_plane_offset_expression),
    )
    gusset_plane = component.constructionPlanes.add(gusset_plane_input)
    gusset_plane.name = "CP02_Left_Gusset_Midplane"

    gusset_sketch = component.sketches.add(gusset_plane)
    gusset_sketch.name = "SK05_Left_Gusset_Profile"
    gusset_lines = gusset_sketch.sketchCurves.sketchLines

    gusset_bottom = gusset_lines.addByTwoPoints(
        _point(gusset_sketch, -3, 4, 0.6),
        _point(gusset_sketch, -3, 6.4, 0.6),
    )
    gusset_rear = gusset_lines.addByTwoPoints(
        gusset_bottom.endSketchPoint,
        _point(gusset_sketch, -3, 6.4, 3),
    )
    gusset_diagonal = gusset_lines.addByTwoPoints(
        gusset_rear.endSketchPoint,
        gusset_bottom.startSketchPoint,
    )

    gusset_constraints = gusset_sketch.geometricConstraints
    gusset_constraints.addHorizontal(gusset_bottom)
    gusset_constraints.addVertical(gusset_rear)

    gusset_y_dimension = gusset_sketch.sketchDimensions.addDistanceDimension(
        gusset_sketch.originPoint,
        gusset_bottom.endSketchPoint,
        horizontal,
        _point(gusset_sketch, -3, 3.2, -0.2),
    )
    gusset_y_dimension.parameter.expression = (
        "BaseDepth - FlangeThickness + FlangeThickness/100"
    )

    gusset_z_dimension = gusset_sketch.sketchDimensions.addDistanceDimension(
        gusset_sketch.originPoint,
        gusset_bottom.endSketchPoint,
        vertical,
        _point(gusset_sketch, -3, 7, 0.3),
    )
    gusset_z_dimension.parameter.expression = (
        "BaseThickness - BaseThickness/100"
    )

    gusset_bottom_dimension = gusset_sketch.sketchDimensions.addDistanceDimension(
        gusset_bottom.startSketchPoint,
        gusset_bottom.endSketchPoint,
        horizontal,
        _point(gusset_sketch, -3, 5.2, 0),
    )
    gusset_bottom_dimension.parameter.expression = (
        "GussetLeg + FlangeThickness/100"
    )

    gusset_rear_dimension = gusset_sketch.sketchDimensions.addDistanceDimension(
        gusset_rear.startSketchPoint,
        gusset_rear.endSketchPoint,
        vertical,
        _point(gusset_sketch, -3, 6.9, 1.8),
    )
    gusset_rear_dimension.parameter.expression = (
        "GussetLeg + BaseThickness/100"
    )

    gusset_extrudes = component.features.extrudeFeatures
    gusset_input = gusset_extrudes.createInput(
        gusset_sketch.profiles.item(0),
        adsk.fusion.FeatureOperations.JoinFeatureOperation,
    )
    gusset_input.setSymmetricExtent(
        adsk.core.ValueInput.createByString("GussetThickness"),
        True,
    )
    gusset_extrude = gusset_extrudes.add(gusset_input)
    gusset_extrude.name = "EX05_Left_Gusset"

    gusset_mirror_entities = adsk.core.ObjectCollection.create()
    gusset_mirror_entities.add(gusset_extrude)
    gusset_mirror_input = component.features.mirrorFeatures.createInput(
        gusset_mirror_entities,
        component.yZConstructionPlane,
    )
    gusset_mirror = component.features.mirrorFeatures.add(gusset_mirror_input)
    gusset_mirror.name = "MR01_Right_Gusset"

    result = {
        "success": True,
        "component": component.name,
        "body": part_body.name,
        "body_count": component.bRepBodies.count,
        "parameter_count": design.userParameters.count,
        "sketches": {
            "SK01_Base_Profile": base_sketch.isFullyConstrained,
            "SK02_Flange_Profile": flange_sketch.isFullyConstrained,
            "SK03_Adjustment_Slots": slot_sketch.isFullyConstrained,
            "SK04_NEMA17_Hole_Pattern": hole_sketch.isFullyConstrained,
            "SK05_Left_Gusset_Profile": gusset_sketch.isFullyConstrained,
        },
        "features": [
            base_extrude.name,
            flange_extrude.name,
            slot_cut.name,
            hole_cut.name,
            gusset_extrude.name,
            gusset_mirror.name,
            flange_fillet.name,
        ],
        "feature_health": [
            str(base_extrude.healthState),
            str(flange_extrude.healthState),
            str(slot_cut.healthState),
            str(hole_cut.healthState),
            str(gusset_extrude.healthState),
            str(gusset_mirror.healthState),
            str(flange_fillet.healthState),
        ],
    }
    print(json.dumps(result, sort_keys=True))
