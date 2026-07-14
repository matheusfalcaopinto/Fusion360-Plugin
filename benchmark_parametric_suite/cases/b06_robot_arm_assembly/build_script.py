import adsk.core
import adsk.fusion
import json
import math


def _xz_point(sketch, x, z):
    return sketch.modelToSketchSpace(adsk.core.Point3D.create(x, 0, z))


def _yz_point(sketch, x, y, z):
    return sketch.modelToSketchSpace(adsk.core.Point3D.create(x, y, z))


def _add_xz_box_component(
    root,
    component_name,
    body_name,
    feature_name,
    center_x_expression,
    center_z_expression,
    width_expression,
    height_expression,
    thickness_expression,
    center_x_cm,
    center_z_cm,
    width_cm,
    height_cm,
):
    occurrence = root.occurrences.addNewComponent(adsk.core.Matrix3D.create())
    component = occurrence.component
    component.name = component_name
    sketch = component.sketches.add(component.xZConstructionPlane)
    sketch.name = "SK01_Profile"
    center = sketch.sketchPoints.add(_xz_point(sketch, center_x_cm, center_z_cm))
    half_width = width_cm / 2.0
    half_height = height_cm / 2.0
    lower_left = _xz_point(sketch, center_x_cm - half_width, center_z_cm - half_height)
    lower_right = _xz_point(sketch, center_x_cm + half_width, center_z_cm - half_height)
    upper_right = _xz_point(sketch, center_x_cm + half_width, center_z_cm + half_height)
    upper_left = _xz_point(sketch, center_x_cm - half_width, center_z_cm + half_height)
    lines = sketch.sketchCurves.sketchLines
    bottom = lines.addByTwoPoints(lower_left, lower_right)
    right = lines.addByTwoPoints(bottom.endSketchPoint, upper_right)
    top = lines.addByTwoPoints(right.endSketchPoint, upper_left)
    left = lines.addByTwoPoints(top.endSketchPoint, bottom.startSketchPoint)
    diagonal = lines.addByTwoPoints(bottom.startSketchPoint, right.endSketchPoint)
    diagonal.isConstruction = True
    constraints = sketch.geometricConstraints
    constraints.addHorizontal(bottom)
    constraints.addVertical(right)
    constraints.addHorizontal(top)
    constraints.addVertical(left)
    constraints.addMidPoint(center, diagonal)
    if math.fabs(center_x_cm) < 0.000001:
        constraints.addVerticalPoints(sketch.originPoint, center)
    else:
        center_x_dimension = sketch.sketchDimensions.addDistanceDimension(
            sketch.originPoint,
            center,
            adsk.fusion.DimensionOrientations.HorizontalDimensionOrientation,
            _xz_point(sketch, center_x_cm * 0.5, center_z_cm + 0.8),
        )
        center_x_dimension.parameter.expression = center_x_expression
    if math.fabs(center_z_cm) < 0.000001:
        constraints.addHorizontalPoints(sketch.originPoint, center)
    else:
        center_z_dimension = sketch.sketchDimensions.addDistanceDimension(
            sketch.originPoint,
            center,
            adsk.fusion.DimensionOrientations.VerticalDimensionOrientation,
            _xz_point(sketch, center_x_cm + 0.8, center_z_cm * 0.5),
        )
        center_z_dimension.parameter.expression = center_z_expression
    width_dimension = sketch.sketchDimensions.addDistanceDimension(
        bottom.startSketchPoint,
        bottom.endSketchPoint,
        adsk.fusion.DimensionOrientations.HorizontalDimensionOrientation,
        _xz_point(sketch, center_x_cm, center_z_cm - half_height - 0.7),
    )
    width_dimension.parameter.expression = width_expression
    height_dimension = sketch.sketchDimensions.addDistanceDimension(
        right.startSketchPoint,
        right.endSketchPoint,
        adsk.fusion.DimensionOrientations.VerticalDimensionOrientation,
        _xz_point(sketch, center_x_cm + half_width + 0.7, center_z_cm),
    )
    height_dimension.parameter.expression = height_expression
    extrudes = component.features.extrudeFeatures
    extrude_input = extrudes.createInput(
        sketch.profiles.item(0),
        adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
    )
    extrude_input.setSymmetricExtent(
        adsk.core.ValueInput.createByString(thickness_expression),
        True,
    )
    feature = extrudes.add(extrude_input)
    feature.name = feature_name
    body = feature.bodies.item(0)
    body.name = body_name
    return occurrence, body, center


def _add_y_cylinder_component(
    root,
    component_name,
    body_name,
    feature_name,
    center_x_expression,
    center_z_expression,
    diameter_expression,
    length_expression,
    center_x_cm,
    center_z_cm,
    radius_cm,
):
    occurrence = root.occurrences.addNewComponent(adsk.core.Matrix3D.create())
    component = occurrence.component
    component.name = component_name
    sketch = component.sketches.add(component.xZConstructionPlane)
    sketch.name = "SK01_Joint_Profile"
    circle = sketch.sketchCurves.sketchCircles.addByCenterRadius(
        _xz_point(sketch, center_x_cm, center_z_cm),
        radius_cm,
    )
    center = circle.centerSketchPoint
    constraints = sketch.geometricConstraints
    if math.fabs(center_x_cm) < 0.000001:
        constraints.addVerticalPoints(sketch.originPoint, center)
    else:
        center_x_dimension = sketch.sketchDimensions.addDistanceDimension(
            sketch.originPoint,
            center,
            adsk.fusion.DimensionOrientations.HorizontalDimensionOrientation,
            _xz_point(sketch, center_x_cm * 0.5, center_z_cm + 0.8),
        )
        center_x_dimension.parameter.expression = center_x_expression
    if math.fabs(center_z_cm) < 0.000001:
        constraints.addHorizontalPoints(sketch.originPoint, center)
    else:
        center_z_dimension = sketch.sketchDimensions.addDistanceDimension(
            sketch.originPoint,
            center,
            adsk.fusion.DimensionOrientations.VerticalDimensionOrientation,
            _xz_point(sketch, center_x_cm + 0.8, center_z_cm * 0.5),
        )
        center_z_dimension.parameter.expression = center_z_expression
    diameter = sketch.sketchDimensions.addDiameterDimension(
        circle,
        _xz_point(sketch, center_x_cm + radius_cm + 0.5, center_z_cm + radius_cm),
    )
    diameter.parameter.expression = diameter_expression
    extrudes = component.features.extrudeFeatures
    extrude_input = extrudes.createInput(
        sketch.profiles.item(0),
        adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
    )
    extrude_input.setSymmetricExtent(
        adsk.core.ValueInput.createByString(length_expression),
        True,
    )
    feature = extrudes.add(extrude_input)
    feature.name = feature_name
    body = feature.bodies.item(0)
    body.name = body_name
    return occurrence, body, center


def _add_x_cylinder_component(
    root,
    component_name,
    body_name,
    feature_name,
    center_x_expression,
    center_z_expression,
    diameter_expression,
    length_expression,
    center_x_cm,
    center_z_cm,
    radius_cm,
):
    occurrence = root.occurrences.addNewComponent(adsk.core.Matrix3D.create())
    component = occurrence.component
    component.name = component_name
    plane_input = component.constructionPlanes.createInput()
    plane_input.setByOffset(
        component.yZConstructionPlane,
        adsk.core.ValueInput.createByString(center_x_expression),
    )
    plane = component.constructionPlanes.add(plane_input)
    plane.name = "CP01_Axis_X_Center"
    sketch = component.sketches.add(plane)
    sketch.name = "SK01_Axis_X_Profile"
    circle = sketch.sketchCurves.sketchCircles.addByCenterRadius(
        _yz_point(sketch, center_x_cm, 0, center_z_cm),
        radius_cm,
    )
    center = circle.centerSketchPoint
    # On a YZ plane Fusion's sketch X direction maps to global Z, while the
    # sketch Y direction maps to global -Y.  Keep the axis on global Y=0 and
    # drive its global Z elevation with a horizontal sketch dimension.
    sketch.geometricConstraints.addHorizontalPoints(sketch.originPoint, center)
    if math.fabs(center_z_cm) < 0.000001:
        sketch.geometricConstraints.addVerticalPoints(sketch.originPoint, center)
    else:
        elevation = sketch.sketchDimensions.addDistanceDimension(
            sketch.originPoint,
            center,
            adsk.fusion.DimensionOrientations.HorizontalDimensionOrientation,
            sketch.modelToSketchSpace(
                adsk.core.Point3D.create(center_x_cm, 0.8, center_z_cm * 0.5)
            ),
        )
        elevation.parameter.expression = center_z_expression
    diameter = sketch.sketchDimensions.addDiameterDimension(
        circle,
        sketch.modelToSketchSpace(
            adsk.core.Point3D.create(center_x_cm, radius_cm + 0.5, center_z_cm + radius_cm)
        ),
    )
    diameter.parameter.expression = diameter_expression
    extrudes = component.features.extrudeFeatures
    extrude_input = extrudes.createInput(
        sketch.profiles.item(0),
        adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
    )
    extrude_input.setSymmetricExtent(
        adsk.core.ValueInput.createByString(length_expression),
        True,
    )
    feature = extrudes.add(extrude_input)
    feature.name = feature_name
    body = feature.bodies.item(0)
    body.name = body_name
    return occurrence, body, center


def _add_base_component(root):
    occurrence = root.occurrences.addNewComponent(adsk.core.Matrix3D.create())
    component = occurrence.component
    component.name = "CMP01_Base"
    sketch = component.sketches.add(component.xYConstructionPlane)
    sketch.name = "SK01_Base_Profile"
    circle = sketch.sketchCurves.sketchCircles.addByCenterRadius(
        adsk.core.Point3D.create(0, 0, 0),
        8.0,
    )
    sketch.geometricConstraints.addCoincident(circle.centerSketchPoint, sketch.originPoint)
    diameter = sketch.sketchDimensions.addDiameterDimension(
        circle,
        adsk.core.Point3D.create(8.5, 0, 0),
    )
    diameter.parameter.expression = "BaseDiameter"
    feature = component.features.extrudeFeatures.addSimple(
        sketch.profiles.item(0),
        adsk.core.ValueInput.createByString("BaseHeight"),
        adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
    )
    feature.name = "EX01_Base"
    body = feature.bodies.item(0)
    body.name = "B01_Base"
    return occurrence, body


def _add_revolute_joint(
    root,
    occurrence_one,
    occurrence_two,
    point,
    point_occurrence,
    name,
    direction,
    custom_axis=None,
):
    point_proxy = point.createForAssemblyContext(point_occurrence)
    geometry = adsk.fusion.JointGeometry.createByPoint(point_proxy)
    joint_input = root.asBuiltJoints.createInput(occurrence_one, occurrence_two, geometry)
    if custom_axis is None:
        joint_input.setAsRevoluteJointMotion(direction)
    else:
        joint_input.setAsRevoluteJointMotion(direction, custom_axis)
    joint = root.asBuiltJoints.add(joint_input)
    joint.name = name
    return joint


def _add_rigid_joint(root, occurrence_one, occurrence_two, name):
    joint_input = root.asBuiltJoints.createInput(occurrence_one, occurrence_two, None)
    joint = root.asBuiltJoints.add(joint_input)
    joint.name = name
    return joint


def run(_context: str):
    root = target_components["root"]
    design = root.parentDesign
    parameter_specs = [
        ("BaseDiameter", "160 mm", "mm", "Rotary base diameter"),
        ("BaseHeight", "20 mm", "mm", "Rotary base height"),
        ("ColumnWidth", "60 mm", "mm", "Pedestal width in X"),
        ("ColumnDepth", "70 mm", "mm", "Pedestal depth in Y"),
        ("ColumnHeight", "100 mm", "mm", "Pedestal height above base"),
        ("ShoulderZ", "BaseHeight + ColumnHeight", "mm", "Shoulder axis height"),
        ("UpperArmLength", "160 mm", "mm", "Shoulder to elbow distance"),
        ("UpperArmHeight", "44 mm", "mm", "Upper arm vertical section"),
        ("UpperArmThickness", "36 mm", "mm", "Upper arm thickness in Y"),
        ("ElbowX", "UpperArmLength", "mm", "Elbow axis X position"),
        ("ForearmLength", "130 mm", "mm", "Elbow to wrist vertical distance"),
        ("ForearmWidth", "40 mm", "mm", "Forearm width in X"),
        ("ForearmThickness", "32 mm", "mm", "Forearm thickness in Y"),
        ("WristZ", "ShoulderZ + ForearmLength", "mm", "Wrist pitch axis height"),
        ("WristLength", "70 mm", "mm", "Wrist link length"),
        ("WristHeight", "36 mm", "mm", "Wrist link height"),
        ("WristThickness", "30 mm", "mm", "Wrist link thickness"),
        ("WristTipX", "ElbowX + WristLength", "mm", "Wrist roll start position"),
        ("ShoulderMotorDiameter", "64 mm", "mm", "Shoulder motor envelope"),
        ("ShoulderMotorLength", "80 mm", "mm", "Shoulder motor length"),
        ("ElbowMotorDiameter", "58 mm", "mm", "Elbow motor envelope"),
        ("ElbowMotorLength", "70 mm", "mm", "Elbow motor length"),
        ("WristMotorDiameter", "50 mm", "mm", "Wrist pitch motor envelope"),
        ("WristMotorLength", "60 mm", "mm", "Wrist pitch motor length"),
        ("WristRollDiameter", "44 mm", "mm", "Wrist roll motor envelope"),
        ("WristRollLength", "60 mm", "mm", "Wrist roll motor length"),
        ("ToolFlangeDiameter", "80 mm", "mm", "Tool flange diameter"),
        ("ToolFlangeThickness", "10 mm", "mm", "Tool flange thickness"),
        ("PalmLength", "30 mm", "mm", "Gripper palm length"),
        ("PalmHeight", "80 mm", "mm", "Gripper palm height"),
        ("PalmThickness", "60 mm", "mm", "Gripper palm thickness"),
        ("FingerLength", "70 mm", "mm", "Gripper finger length"),
        ("FingerThickness", "15 mm", "mm", "Gripper finger thickness"),
        ("FingerDepth", "15 mm", "mm", "Gripper finger depth"),
        ("JawGap", "50 mm", "mm", "Clear gap between fingers"),
        ("CableThickness", "8 mm", "mm", "Square cable harness envelope"),
    ]
    for name, expression, units, comment in parameter_specs:
        design.userParameters.add(
            name,
            adsk.core.ValueInput.createByString(expression),
            units,
            comment,
        )

    assembly_reference = root.sketches.add(root.xZConstructionPlane)
    assembly_reference.name = "SK00_Kinematic_Envelope"

    base_occurrence, base_body = _add_base_component(root)
    column_occurrence, column_body, column_center = _add_xz_box_component(
        root,
        "CMP02_Column",
        "B02_Column",
        "EX01_Column",
        "0 mm",
        "BaseHeight + ColumnHeight / 2",
        "ColumnWidth",
        "ColumnHeight",
        "ColumnDepth",
        0.0,
        7.0,
        6.0,
        10.0,
    )
    shoulder_occurrence, shoulder_body, shoulder_center = _add_y_cylinder_component(
        root,
        "CMP03_Shoulder_Motor",
        "B03_Shoulder_Motor",
        "EX01_Shoulder_Motor",
        "0 mm",
        "ShoulderZ",
        "ShoulderMotorDiameter",
        "ShoulderMotorLength",
        0.0,
        12.0,
        3.2,
    )
    upper_occurrence, upper_body, upper_center = _add_xz_box_component(
        root,
        "CMP04_Upper_Arm",
        "B04_Upper_Arm",
        "EX01_Upper_Arm",
        "UpperArmLength / 2",
        "ShoulderZ",
        "UpperArmLength",
        "UpperArmHeight",
        "UpperArmThickness",
        8.0,
        12.0,
        16.0,
        4.4,
    )
    elbow_occurrence, elbow_body, elbow_center = _add_y_cylinder_component(
        root,
        "CMP05_Elbow_Motor",
        "B05_Elbow_Motor",
        "EX01_Elbow_Motor",
        "ElbowX",
        "ShoulderZ",
        "ElbowMotorDiameter",
        "ElbowMotorLength",
        16.0,
        12.0,
        2.9,
    )
    forearm_occurrence, forearm_body, forearm_center = _add_xz_box_component(
        root,
        "CMP06_Forearm",
        "B06_Forearm",
        "EX01_Forearm",
        "ElbowX",
        "ShoulderZ + ForearmLength / 2",
        "ForearmWidth",
        "ForearmLength",
        "ForearmThickness",
        16.0,
        18.5,
        4.0,
        13.0,
    )
    wrist_pitch_occurrence, wrist_pitch_body, wrist_pitch_center = _add_y_cylinder_component(
        root,
        "CMP07_Wrist_Pitch_Motor",
        "B07_Wrist_Pitch_Motor",
        "EX01_Wrist_Pitch_Motor",
        "ElbowX",
        "WristZ",
        "WristMotorDiameter",
        "WristMotorLength",
        16.0,
        25.0,
        2.5,
    )
    wrist_link_occurrence, wrist_link_body, wrist_link_center = _add_xz_box_component(
        root,
        "CMP08_Wrist_Link",
        "B08_Wrist_Link",
        "EX01_Wrist_Link",
        "ElbowX + WristLength / 2",
        "WristZ",
        "WristLength",
        "WristHeight",
        "WristThickness",
        19.5,
        25.0,
        7.0,
        3.6,
    )
    wrist_roll_occurrence, wrist_roll_body, wrist_roll_center = _add_x_cylinder_component(
        root,
        "CMP09_Wrist_Roll_Motor",
        "B09_Wrist_Roll_Motor",
        "EX01_Wrist_Roll_Motor",
        "WristTipX + WristRollLength / 2",
        "WristZ",
        "WristRollDiameter",
        "WristRollLength",
        26.0,
        25.0,
        2.2,
    )
    flange_occurrence, flange_body, flange_center = _add_x_cylinder_component(
        root,
        "CMP10_Tool_Flange",
        "B10_Tool_Flange",
        "EX01_Tool_Flange",
        "WristTipX + WristRollLength + ToolFlangeThickness / 2",
        "WristZ",
        "ToolFlangeDiameter",
        "ToolFlangeThickness",
        29.5,
        25.0,
        4.0,
    )
    palm_occurrence, palm_body, palm_center = _add_xz_box_component(
        root,
        "CMP11_Gripper_Palm",
        "B11_Gripper_Palm",
        "EX01_Gripper_Palm",
        "WristTipX + WristRollLength + ToolFlangeThickness + PalmLength / 2",
        "WristZ",
        "PalmLength",
        "PalmHeight",
        "PalmThickness",
        31.5,
        25.0,
        3.0,
        8.0,
    )
    finger_x_expression = "WristTipX + WristRollLength + ToolFlangeThickness + PalmLength + FingerLength / 2"
    upper_finger_occurrence, upper_finger_body, upper_finger_center = _add_xz_box_component(
        root,
        "CMP12_Gripper_Finger_Upper",
        "B12_Gripper_Finger_Upper",
        "EX01_Gripper_Finger_Upper",
        finger_x_expression,
        "WristZ + JawGap / 2 + FingerThickness / 2",
        "FingerLength",
        "FingerThickness",
        "FingerDepth",
        36.5,
        28.25,
        7.0,
        1.5,
    )
    lower_finger_occurrence, lower_finger_body, lower_finger_center = _add_xz_box_component(
        root,
        "CMP13_Gripper_Finger_Lower",
        "B13_Gripper_Finger_Lower",
        "EX01_Gripper_Finger_Lower",
        finger_x_expression,
        "WristZ - JawGap / 2 - FingerThickness / 2",
        "FingerLength",
        "FingerThickness",
        "FingerDepth",
        36.5,
        21.75,
        7.0,
        1.5,
    )
    cable_upper_occurrence, cable_upper_body, cable_upper_center = _add_xz_box_component(
        root,
        "CMP14_Cable_Upper",
        "B14_Cable_Upper",
        "EX01_Cable_Upper",
        "UpperArmLength / 2",
        "ShoulderZ + UpperArmHeight / 2 + CableThickness / 2",
        "UpperArmLength - ShoulderMotorDiameter / 2",
        "CableThickness",
        "CableThickness",
        8.0,
        14.6,
        12.8,
        0.8,
    )
    cable_forearm_occurrence, cable_forearm_body, cable_forearm_center = _add_xz_box_component(
        root,
        "CMP15_Cable_Forearm",
        "B15_Cable_Forearm",
        "EX01_Cable_Forearm",
        "ElbowX + ForearmWidth / 2 + CableThickness / 2",
        "ShoulderZ + ForearmLength / 2",
        "CableThickness",
        "ForearmLength - WristMotorDiameter / 2",
        "CableThickness",
        18.4,
        18.5,
        0.8,
        10.5,
    )
    cable_wrist_occurrence, cable_wrist_body, cable_wrist_center = _add_xz_box_component(
        root,
        "CMP16_Cable_Wrist",
        "B16_Cable_Wrist",
        "EX01_Cable_Wrist",
        "ElbowX + WristLength / 2",
        "WristZ + WristHeight / 2 + CableThickness / 2",
        "WristLength - WristMotorDiameter / 2",
        "CableThickness",
        "CableThickness",
        19.5,
        27.2,
        4.5,
        0.8,
    )

    _add_rigid_joint(root, base_occurrence, column_occurrence, "J01_Base_Column_Rigid")
    _add_revolute_joint(
        root,
        column_occurrence,
        shoulder_occurrence,
        shoulder_center,
        shoulder_occurrence,
        "J02_Shoulder_Revolute",
        adsk.fusion.JointDirections.YAxisJointDirection,
    )
    _add_rigid_joint(root, shoulder_occurrence, upper_occurrence, "J03_Shoulder_Link_Rigid")
    _add_revolute_joint(
        root,
        upper_occurrence,
        elbow_occurrence,
        elbow_center,
        elbow_occurrence,
        "J04_Elbow_Revolute",
        adsk.fusion.JointDirections.YAxisJointDirection,
    )
    _add_rigid_joint(root, elbow_occurrence, forearm_occurrence, "J05_Elbow_Link_Rigid")
    _add_revolute_joint(
        root,
        forearm_occurrence,
        wrist_pitch_occurrence,
        wrist_pitch_center,
        wrist_pitch_occurrence,
        "J06_Wrist_Pitch_Revolute",
        adsk.fusion.JointDirections.YAxisJointDirection,
    )
    _add_rigid_joint(root, wrist_pitch_occurrence, wrist_link_occurrence, "J07_Wrist_Link_Rigid")
    _add_revolute_joint(
        root,
        wrist_link_occurrence,
        wrist_roll_occurrence,
        wrist_roll_center,
        wrist_roll_occurrence,
        "J08_Wrist_Roll_Revolute",
        adsk.fusion.JointDirections.CustomJointDirection,
        root.xConstructionAxis,
    )
    _add_rigid_joint(root, wrist_roll_occurrence, flange_occurrence, "J09_Tool_Flange_Rigid")
    _add_rigid_joint(root, flange_occurrence, palm_occurrence, "J10_Gripper_Palm_Rigid")
    _add_rigid_joint(root, palm_occurrence, upper_finger_occurrence, "J11_Upper_Finger_Rigid")
    _add_rigid_joint(root, palm_occurrence, lower_finger_occurrence, "J12_Lower_Finger_Rigid")

    child_components = [component for component in design.allComponents if component != root]
    if len(child_components) != 16 or root.allOccurrences.count != 16:
        raise RuntimeError("Robot arm must contain exactly sixteen component occurrences")
    for component in child_components:
        if component.bRepBodies.count != 1:
            raise RuntimeError("Every robot component must contain exactly one body")
        body = component.bRepBodies.item(0)
        if not body.isValid or not body.isSolid or body.lumps.count != 1:
            raise RuntimeError("Every robot component body must be one valid solid lump")
        for feature in component.features:
            if not feature.isValid or feature.errorOrWarningMessage:
                raise RuntimeError("Every robot feature must be valid and healthy")

    result = {
        "success": True,
        "case_id": "b06_robot_arm_assembly",
        "parameters": design.userParameters.count,
        "components": len(child_components),
        "occurrences": root.allOccurrences.count,
        "bodies": sum(component.bRepBodies.count for component in child_components),
        "joints": root.asBuiltJoints.count,
        "root_bodies": root.bRepBodies.count,
    }
    payload = json.dumps(result, ensure_ascii=False, sort_keys=True)
    print(payload)
    return payload
