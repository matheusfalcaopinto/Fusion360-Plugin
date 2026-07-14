import adsk.core
import adsk.fusion
import json
import math


def _point(sketch, x, y, z):
    return sketch.modelToSketchSpace(adsk.core.Point3D.create(x, y, z))


def _add_box(
    root, index, label, cx_expression, cy_expression, z_expression,
    width_expression, depth_expression, height_expression,
    cx, cy, z, width, depth,
):
    occurrence = root.occurrences.addNewComponent(adsk.core.Matrix3D.create())
    component = occurrence.component
    component.name = f"CMP{index:02d}_{label}"
    plane = component.xYConstructionPlane
    if math.fabs(z) >= 0.000001:
        plane_input = component.constructionPlanes.createInput()
        plane_input.setByOffset(
            plane, adsk.core.ValueInput.createByString(z_expression)
        )
        plane = component.constructionPlanes.add(plane_input)
    sketch = component.sketches.add(plane)
    sketch.name = "SK01_Profile"
    center = sketch.sketchPoints.add(_point(sketch, cx, cy, z))
    half_width, half_depth = width / 2.0, depth / 2.0
    lines = sketch.sketchCurves.sketchLines
    bottom = lines.addByTwoPoints(
        _point(sketch, cx - half_width, cy - half_depth, z),
        _point(sketch, cx + half_width, cy - half_depth, z),
    )
    right = lines.addByTwoPoints(
        bottom.endSketchPoint, _point(sketch, cx + half_width, cy + half_depth, z)
    )
    top = lines.addByTwoPoints(
        right.endSketchPoint, _point(sketch, cx - half_width, cy + half_depth, z)
    )
    left = lines.addByTwoPoints(top.endSketchPoint, bottom.startSketchPoint)
    diagonal = lines.addByTwoPoints(bottom.startSketchPoint, right.endSketchPoint)
    diagonal.isConstruction = True
    constraints = sketch.geometricConstraints
    for line in (bottom, top):
        constraints.addHorizontal(line)
    for line in (right, left):
        constraints.addVertical(line)
    constraints.addMidPoint(center, diagonal)
    dimension_specs = []
    if math.fabs(cx) < 0.000001:
        constraints.addVerticalPoints(sketch.originPoint, center)
    else:
        dimension_specs.append((
            sketch.originPoint, center,
            adsk.fusion.DimensionOrientations.HorizontalDimensionOrientation,
            _point(sketch, cx / 2.0, cy + 0.8, z), cx_expression,
        ))
    if math.fabs(cy) < 0.000001:
        constraints.addHorizontalPoints(sketch.originPoint, center)
    else:
        dimension_specs.append((
            sketch.originPoint, center,
            adsk.fusion.DimensionOrientations.VerticalDimensionOrientation,
            _point(sketch, cx + 0.8, cy / 2.0, z), cy_expression,
        ))
    dimension_specs.append((
        bottom.startSketchPoint, bottom.endSketchPoint,
        adsk.fusion.DimensionOrientations.HorizontalDimensionOrientation,
        _point(sketch, cx, cy - half_depth - 0.7, z), width_expression,
    ))
    dimension_specs.append((
        right.startSketchPoint, right.endSketchPoint,
        adsk.fusion.DimensionOrientations.VerticalDimensionOrientation,
        _point(sketch, cx + half_width + 0.7, cy, z), depth_expression,
    ))
    for first, second, orientation, text_point, expression in dimension_specs:
        dimension = sketch.sketchDimensions.addDistanceDimension(
            first, second, orientation, text_point
        )
        dimension.parameter.expression = expression
    feature = component.features.extrudeFeatures.addSimple(
        sketch.profiles.item(0),
        adsk.core.ValueInput.createByString(height_expression),
        adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
    )
    feature.name = f"EX01_{label}"
    feature.bodies.item(0).name = f"B{index:02d}_{label}"
    return occurrence


def _add_cylinder(
    root, index, label, axis, cx_expression, cy_expression, z_expression,
    diameter_expression, length_expression, cx, cy, z, radius,
):
    occurrence = root.occurrences.addNewComponent(adsk.core.Matrix3D.create())
    component = occurrence.component
    component.name = f"CMP{index:02d}_{label}"
    if axis == "x":
        plane = component.yZConstructionPlane
        if math.fabs(cx) >= 0.000001:
            plane_input = component.constructionPlanes.createInput()
            plane_input.setByOffset(
                plane, adsk.core.ValueInput.createByString(cx_expression)
            )
            plane = component.constructionPlanes.add(plane_input)
    else:
        plane_input = component.constructionPlanes.createInput()
        plane_input.setByOffset(
            component.xYConstructionPlane,
            adsk.core.ValueInput.createByString(z_expression),
        )
        plane = component.constructionPlanes.add(plane_input)
    sketch = component.sketches.add(plane)
    sketch.name = "SK01_Profile"
    circle = sketch.sketchCurves.sketchCircles.addByCenterRadius(
        _point(sketch, cx, cy, z), radius
    )
    center = circle.centerSketchPoint
    constraints = sketch.geometricConstraints
    dimension_specs = []
    if axis == "x":
        # On Fusion's YZ construction plane the sketch horizontal axis maps
        # to model Z, while the sketch vertical axis maps to model Y.  Keep
        # the initial model-space point to select the signed side, then drive
        # each local axis with the corresponding model-space expression.
        if math.fabs(z) < 0.000001:
            constraints.addVerticalPoints(sketch.originPoint, center)
        else:
            dimension_specs.append((
                sketch.originPoint, center,
                adsk.fusion.DimensionOrientations.HorizontalDimensionOrientation,
                _point(sketch, cx, cy + 0.8, z / 2.0), z_expression,
            ))
        if math.fabs(cy) < 0.000001:
            constraints.addHorizontalPoints(sketch.originPoint, center)
        else:
            dimension_specs.append((
                sketch.originPoint, center,
                adsk.fusion.DimensionOrientations.VerticalDimensionOrientation,
                _point(sketch, cx, cy / 2.0, z + 0.8), cy_expression,
            ))
        diameter_point = _point(sketch, cx, cy + radius + 0.5, z + radius)
    else:
        if math.fabs(cx) < 0.000001:
            constraints.addVerticalPoints(sketch.originPoint, center)
        else:
            dimension_specs.append((
                sketch.originPoint, center,
                adsk.fusion.DimensionOrientations.HorizontalDimensionOrientation,
                _point(sketch, cx / 2.0, cy + 0.8, z), cx_expression,
            ))
        if math.fabs(cy) < 0.000001:
            constraints.addHorizontalPoints(sketch.originPoint, center)
        else:
            dimension_specs.append((
                sketch.originPoint, center,
                adsk.fusion.DimensionOrientations.VerticalDimensionOrientation,
                _point(sketch, cx + 0.8, cy / 2.0, z), cy_expression,
            ))
        diameter_point = _point(sketch, cx + radius + 0.5, cy + radius, z)
    for first, second, orientation, text_point, expression in dimension_specs:
        dimension = sketch.sketchDimensions.addDistanceDimension(
            first, second, orientation, text_point
        )
        dimension.parameter.expression = expression
    diameter = sketch.sketchDimensions.addDiameterDimension(circle, diameter_point)
    diameter.parameter.expression = diameter_expression
    if axis == "x":
        feature_input = component.features.extrudeFeatures.createInput(
            sketch.profiles.item(0), adsk.fusion.FeatureOperations.NewBodyFeatureOperation
        )
        feature_input.setSymmetricExtent(
            adsk.core.ValueInput.createByString(length_expression), True
        )
        feature = component.features.extrudeFeatures.add(feature_input)
    else:
        feature = component.features.extrudeFeatures.addSimple(
            sketch.profiles.item(0),
            adsk.core.ValueInput.createByString(length_expression),
            adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
        )
    feature.name = f"EX01_{label}"
    feature.bodies.item(0).name = f"B{index:02d}_{label}"
    return occurrence, center


def _add_hopper(root):
    occurrence = root.occurrences.addNewComponent(adsk.core.Matrix3D.create())
    component = occurrence.component
    component.name = "CMP25_Hopper"
    profiles = []
    sections = (
        ("HopperBottomZ", 32.0, "HopperBottomWidth", "HopperBottomDepth", 10.0, 8.0, "SK01_Hopper_Outlet"),
        ("HopperBottomZ + HopperHeight", 47.0, "HopperTopWidth", "HopperTopDepth", 26.0, 22.0, "SK02_Hopper_Inlet"),
    )
    for z_expression, z, width_expression, depth_expression, width, depth, name in sections:
        plane_input = component.constructionPlanes.createInput()
        plane_input.setByOffset(
            component.xYConstructionPlane,
            adsk.core.ValueInput.createByString(z_expression),
        )
        plane = component.constructionPlanes.add(plane_input)
        sketch = component.sketches.add(plane)
        sketch.name = name
        center = sketch.sketchPoints.add(_point(sketch, 0.0, 10.0, z))
        half_width, half_depth = width / 2.0, depth / 2.0
        lines = sketch.sketchCurves.sketchLines
        bottom = lines.addByTwoPoints(
            _point(sketch, -half_width, 10.0 - half_depth, z),
            _point(sketch, half_width, 10.0 - half_depth, z),
        )
        right = lines.addByTwoPoints(
            bottom.endSketchPoint, _point(sketch, half_width, 10.0 + half_depth, z)
        )
        top = lines.addByTwoPoints(
            right.endSketchPoint, _point(sketch, -half_width, 10.0 + half_depth, z)
        )
        left = lines.addByTwoPoints(top.endSketchPoint, bottom.startSketchPoint)
        diagonal = lines.addByTwoPoints(bottom.startSketchPoint, right.endSketchPoint)
        diagonal.isConstruction = True
        constraints = sketch.geometricConstraints
        for line in (bottom, top):
            constraints.addHorizontal(line)
        for line in (right, left):
            constraints.addVertical(line)
        constraints.addMidPoint(center, diagonal)
        constraints.addVerticalPoints(sketch.originPoint, center)
        dimensions = (
            (
                sketch.originPoint, center,
                adsk.fusion.DimensionOrientations.VerticalDimensionOrientation,
                _point(sketch, 0.8, 5.0, z), "HopperCenterY",
            ),
            (
                bottom.startSketchPoint, bottom.endSketchPoint,
                adsk.fusion.DimensionOrientations.HorizontalDimensionOrientation,
                _point(sketch, 0.0, 10.0 - half_depth - 0.7, z), width_expression,
            ),
            (
                right.startSketchPoint, right.endSketchPoint,
                adsk.fusion.DimensionOrientations.VerticalDimensionOrientation,
                _point(sketch, half_width + 0.7, 10.0, z), depth_expression,
            ),
        )
        for first, second, orientation, text_point, expression in dimensions:
            dimension = sketch.sketchDimensions.addDistanceDimension(
                first, second, orientation, text_point
            )
            dimension.parameter.expression = expression
        profiles.append(sketch.profiles.item(0))
    loft_input = component.features.loftFeatures.createInput(
        adsk.fusion.FeatureOperations.NewBodyFeatureOperation
    )
    for profile in profiles:
        loft_input.loftSections.add(profile)
    feature = component.features.loftFeatures.add(loft_input)
    feature.name = "LF01_Hopper"
    feature.bodies.item(0).name = "B25_Hopper"
    return occurrence


def _add_rigid_joint(root, occurrence_one, occurrence_two, name):
    joint_input = root.asBuiltJoints.createInput(occurrence_one, occurrence_two, None)
    joint = root.asBuiltJoints.add(joint_input)
    if joint is None:
        raise RuntimeError("Unable to create rigid as-built joint")
    joint.name = name


def _add_revolute_joint(root, occurrence_one, occurrence_two, center, name, direction):
    center_proxy = center.createForAssemblyContext(occurrence_one)
    geometry = adsk.fusion.JointGeometry.createByPoint(center_proxy)
    joint_input = root.asBuiltJoints.createInput(occurrence_one, occurrence_two, geometry)
    if not joint_input.setAsRevoluteJointMotion(direction):
        raise RuntimeError("Unable to define revolute as-built joint motion")
    joint = root.asBuiltJoints.add(joint_input)
    if joint is None:
        raise RuntimeError("Unable to create revolute as-built joint")
    joint.name = name


def run(_context: str):
    root = target_components["root"]
    design = root.parentDesign
    parameters = (
        ("MachineWidth", "600 mm"),
        ("MachineDepth", "500 mm"),
        ("MachineHeight", "500 mm"),
        ("FrameProfile", "30 mm"),
        ("PostHeight", "MachineHeight - 2 * FrameProfile"),
        ("PostCenterZ", "FrameProfile + PostHeight / 2"),
        ("TopRailZ", "MachineHeight - FrameProfile"),
        ("PanelThickness", "2 mm"),
        ("PanelHeight", "MachineHeight - 2 * FrameProfile"),
        ("PanelCenterZ", "FrameProfile + PanelHeight / 2"),
        ("DoorThickness", "2 mm"),
        ("DoorWidth", "360 mm"),
        ("DoorCenterX", "-(MachineWidth - 2 * FrameProfile - DoorWidth) / 2"),
        ("FrontPanelY", "-MachineDepth / 2 + FrameProfile + DoorThickness / 2"),
        ("HingeDiameter", "12 mm"),
        ("HingeHeight", "60 mm"),
        ("HingeCenterX", "-MachineWidth / 2 + FrameProfile - HingeDiameter / 2"),
        ("BeltWidth", "300 mm"),
        ("ConveyorLength", "620 mm"),
        ("BeltHeight", "220 mm"),
        ("BeltThickness", "8 mm"),
        ("ConveyorOpeningTopZ", "BeltHeight + BeltThickness + 22 mm"),
        ("DoorBottomZ", "ConveyorOpeningTopZ"),
        ("DoorHeight", "MachineHeight - FrameProfile - DoorBottomZ"),
        ("RearPanelBottomZ", "ConveyorOpeningTopZ"),
        ("RearPanelHeight", "MachineHeight - FrameProfile - RearPanelBottomZ"),
        ("LowerHingeZ", "DoorBottomZ + 20 mm"),
        ("UpperHingeZ", "MachineHeight - FrameProfile - HingeHeight - 20 mm"),
        ("RollerDiameter", "40 mm"),
        ("RollerOverhang", "20 mm"),
        ("RollerLength", "BeltWidth + 2 * RollerOverhang"),
        ("RollerZ", "BeltHeight - RollerDiameter / 2"),
        ("InfeedRollerY", "-ConveyorLength / 2 + RollerDiameter / 2"),
        ("QuarterRollerY", "-ConveyorLength / 6"),
        ("ThreeQuarterRollerY", "ConveyorLength / 6"),
        ("OutfeedRollerY", "ConveyorLength / 2 - RollerDiameter / 2"),
        ("MotorDiameter", "40 mm"),
        ("MotorLength", "80 mm"),
        ("ConveyorRailThickness", "20 mm"),
        ("ConveyorRailHeight", "80 mm"),
        ("ConveyorRailBottomZ", "RollerZ - ConveyorRailHeight / 2"),
        ("ConveyorSupportWidth", "20 mm"),
        ("ConveyorSupportDepth", "30 mm"),
        ("ConveyorSupportHeight", "ConveyorRailBottomZ - FrameProfile"),
        ("MotorCenterX", "BeltWidth / 2 + RollerOverhang + ConveyorRailThickness + MotorLength / 2"),
        ("HopperBottomWidth", "100 mm"),
        ("HopperBottomDepth", "80 mm"),
        ("HopperTopWidth", "BeltWidth - 40 mm"),
        ("HopperTopDepth", "220 mm"),
        ("HopperHeight", "150 mm"),
        ("HopperBottomZ", "320 mm"),
        ("HopperCenterY", "100 mm"),
        ("ThroatHeight", "HopperBottomZ - BeltHeight - BeltThickness"),
        ("HopperSupportDepth", "20 mm"),
        ("HopperSupportCenterY", "200 mm"),
        ("CabinetWidth", "70 mm"),
        ("CabinetDepth", "180 mm"),
        ("CabinetHeight", "180 mm"),
        ("CabinetCenterX", "MachineWidth / 2 - FrameProfile - PanelThickness - CabinetWidth / 2"),
    )
    for name, expression in parameters:
        design.userParameters.add(
            name, adsk.core.ValueInput.createByString(expression), "mm", "B07"
        )

    reference = root.sketches.add(root.xYConstructionPlane)
    reference.name = "SK00_Machine_Envelope"
    zero = "0 mm"
    frame = "FrameProfile"
    frame_x = "MachineWidth / 2 - FrameProfile / 2"
    frame_y = "MachineDepth / 2 - FrameProfile / 2"
    inner_width = "MachineWidth - 2 * FrameProfile"
    inner_depth = "MachineDepth - 2 * FrameProfile"
    rail_x = "BeltWidth / 2 + RollerOverhang + ConveyorRailThickness / 2"
    boxes = (
        (1, "Base_Left_Rail", frame_x, zero, zero, frame, "MachineDepth", frame, -28.5, 0.0, 0.0, 3.0, 50.0),
        (2, "Base_Right_Rail", frame_x, zero, zero, frame, "MachineDepth", frame, 28.5, 0.0, 0.0, 3.0, 50.0),
        (3, "Base_Front_Crossbar", zero, frame_y, zero, inner_width, frame, frame, 0.0, -23.5, 0.0, 54.0, 3.0),
        (4, "Base_Rear_Crossbar", zero, frame_y, zero, inner_width, frame, frame, 0.0, 23.5, 0.0, 54.0, 3.0),
        (5, "Post_Front_Left", frame_x, frame_y, "PostCenterZ - PostHeight / 2", frame, frame, "PostHeight", -28.5, -23.5, 3.0, 3.0, 3.0),
        (6, "Post_Front_Right", frame_x, frame_y, "PostCenterZ - PostHeight / 2", frame, frame, "PostHeight", 28.5, -23.5, 3.0, 3.0, 3.0),
        (7, "Post_Rear_Left", frame_x, frame_y, "PostCenterZ - PostHeight / 2", frame, frame, "PostHeight", -28.5, 23.5, 3.0, 3.0, 3.0),
        (8, "Post_Rear_Right", frame_x, frame_y, "PostCenterZ - PostHeight / 2", frame, frame, "PostHeight", 28.5, 23.5, 3.0, 3.0, 3.0),
        (9, "Top_Front_Crossbar", zero, frame_y, "TopRailZ", inner_width, frame, frame, 0.0, -23.5, 47.0, 54.0, 3.0),
        (10, "Top_Rear_Crossbar", zero, frame_y, "TopRailZ", inner_width, frame, frame, 0.0, 23.5, 47.0, 54.0, 3.0),
        (11, "Top_Left_Rail", frame_x, zero, "TopRailZ", frame, inner_depth, frame, -28.5, 0.0, 47.0, 3.0, 44.0),
        (12, "Top_Right_Rail", frame_x, zero, "TopRailZ", frame, inner_depth, frame, 28.5, 0.0, 47.0, 3.0, 44.0),
        (13, "Panel_Left", "MachineWidth / 2 - FrameProfile - PanelThickness / 2", zero, "PanelCenterZ - PanelHeight / 2", "PanelThickness", inner_depth, "PanelHeight", -26.9, 0.0, 3.0, 0.2, 44.0),
        (14, "Panel_Right", "MachineWidth / 2 - FrameProfile - PanelThickness / 2", zero, "PanelCenterZ - PanelHeight / 2", "PanelThickness", inner_depth, "PanelHeight", 26.9, 0.0, 3.0, 0.2, 44.0),
        (15, "Panel_Rear", zero, "MachineDepth / 2 - FrameProfile - PanelThickness / 2", "RearPanelBottomZ", inner_width, "PanelThickness", "RearPanelHeight", 0.0, 21.9, 25.0, 54.0, 0.2),
        (16, "Access_Door", "-DoorCenterX", "-FrontPanelY", "DoorBottomZ", "DoorWidth", "DoorThickness", "DoorHeight", -9.0, -21.9, 25.0, 36.0, 0.2),
        (19, "Conveyor_Belt", zero, zero, "BeltHeight", "BeltWidth", "ConveyorLength", "BeltThickness", 0.0, 0.0, 22.0, 30.0, 62.0),
        (26, "Feed_Throat", zero, "HopperCenterY", "BeltHeight + BeltThickness", "HopperBottomWidth", "HopperBottomDepth", "ThroatHeight", 0.0, 10.0, 22.8, 10.0, 8.0),
        (27, "Control_Cabinet", "CabinetCenterX", zero, frame, "CabinetWidth", "CabinetDepth", "CabinetHeight", 23.3, 0.0, 3.0, 7.0, 18.0),
        (28, "Conveyor_Rail_Left", rail_x, zero, "ConveyorRailBottomZ", "ConveyorRailThickness", "ConveyorLength", "ConveyorRailHeight", -18.0, 0.0, 16.0, 2.0, 62.0),
        (29, "Conveyor_Rail_Right", rail_x, zero, "ConveyorRailBottomZ", "ConveyorRailThickness", "ConveyorLength", "ConveyorRailHeight", 18.0, 0.0, 16.0, 2.0, 62.0),
        (30, "Conveyor_Support_Front_Left", rail_x, frame_y, frame, "ConveyorSupportWidth", "ConveyorSupportDepth", "ConveyorSupportHeight", -18.0, -23.5, 3.0, 2.0, 3.0),
        (31, "Conveyor_Support_Front_Right", rail_x, frame_y, frame, "ConveyorSupportWidth", "ConveyorSupportDepth", "ConveyorSupportHeight", 18.0, -23.5, 3.0, 2.0, 3.0),
        (32, "Conveyor_Support_Rear_Left", rail_x, frame_y, frame, "ConveyorSupportWidth", "ConveyorSupportDepth", "ConveyorSupportHeight", -18.0, 23.5, 3.0, 2.0, 3.0),
        (33, "Conveyor_Support_Rear_Right", rail_x, frame_y, frame, "ConveyorSupportWidth", "ConveyorSupportDepth", "ConveyorSupportHeight", 18.0, 23.5, 3.0, 2.0, 3.0),
        (34, "Hopper_Support_Crossbar", zero, "HopperSupportCenterY", "MachineHeight - FrameProfile", inner_width, "HopperSupportDepth", frame, 0.0, 20.0, 47.0, 54.0, 2.0),
    )
    occurrences = {}
    for spec in boxes:
        occurrences[spec[0]] = _add_box(root, *spec)

    cylinders = (
        (17, "Door_Hinge_Lower", "z", "-HingeCenterX", "-FrontPanelY", "LowerHingeZ", "HingeDiameter", "HingeHeight", -27.6, -21.9, 27.0, 0.6),
        (18, "Door_Hinge_Upper", "z", "-HingeCenterX", "-FrontPanelY", "UpperHingeZ", "HingeDiameter", "HingeHeight", -27.6, -21.9, 39.0, 0.6),
        (20, "Roller_Infeed", "x", zero, "-InfeedRollerY", "RollerZ", "RollerDiameter", "RollerLength", 0.0, -29.0, 20.0, 2.0),
        (21, "Roller_Quarter", "x", zero, "-QuarterRollerY", "RollerZ", "RollerDiameter", "RollerLength", 0.0, -10.3333333333, 20.0, 2.0),
        (22, "Roller_Three_Quarter", "x", zero, "ThreeQuarterRollerY", "RollerZ", "RollerDiameter", "RollerLength", 0.0, 10.3333333333, 20.0, 2.0),
        (23, "Roller_Outfeed", "x", zero, "OutfeedRollerY", "RollerZ", "RollerDiameter", "RollerLength", 0.0, 29.0, 20.0, 2.0),
        (24, "Drive_Motor", "x", "MotorCenterX", "OutfeedRollerY", "RollerZ", "MotorDiameter", "MotorLength", 23.0, 29.0, 20.0, 2.0),
    )
    centers = {}
    for spec in cylinders:
        occurrence, center = _add_cylinder(root, *spec)
        occurrences[spec[0]] = occurrence
        centers[spec[0]] = center
    occurrences[25] = _add_hopper(root)

    rigid_joints = (
        ("J01_Base_Right_Front", 2, 3),
        ("J02_Base_Left_Front", 1, 3),
        ("J03_Base_Rear_Left", 4, 1),
        ("J04_Post_Front_Left", 5, 1),
        ("J05_Post_Front_Right", 6, 2),
        ("J06_Post_Rear_Left", 7, 1),
        ("J07_Post_Rear_Right", 8, 2),
        ("J08_Top_Front", 9, 5),
        ("J09_Top_Rear", 10, 7),
        ("J10_Top_Left", 11, 5),
        ("J11_Top_Right", 12, 6),
        ("J12_Panel_Left", 13, 5),
        ("J13_Panel_Right", 14, 6),
        ("J14_Panel_Rear", 15, 7),
        ("J16_Hinge_Lower", 17, 16),
        ("J17_Hinge_Upper", 18, 16),
        ("J18_Conveyor_Frame", 19, 3),
        ("J23_Drive_Motor", 24, 23),
        ("J24_Hopper_Throat", 25, 26),
        ("J25_Throat_Belt", 26, 19),
        ("J26_Control_Cabinet", 27, 14),
        ("J27_Rail_Left_Front_Support", 28, 30),
        ("J28_Rail_Right_Front_Support", 29, 31),
        ("J29_Front_Left_Support_Frame", 30, 3),
        ("J30_Front_Right_Support_Frame", 31, 3),
        ("J31_Rear_Left_Support_Rail", 32, 28),
        ("J32_Rear_Right_Support_Rail", 33, 29),
        ("J33_Hopper_Support_Frame", 34, 11),
    )
    for name, first, second in rigid_joints:
        _add_rigid_joint(root, occurrences[first], occurrences[second], name)
    revolute_joints = (
        ("J15_Access_Door_Pivot", 17, 5, adsk.fusion.JointDirections.ZAxisJointDirection),
        ("J19_Roller_Infeed", 20, 19, adsk.fusion.JointDirections.XAxisJointDirection),
        ("J20_Roller_Quarter", 21, 19, adsk.fusion.JointDirections.XAxisJointDirection),
        ("J21_Roller_Three_Quarter", 22, 19, adsk.fusion.JointDirections.XAxisJointDirection),
        ("J22_Roller_Outfeed", 23, 19, adsk.fusion.JointDirections.XAxisJointDirection),
    )
    for name, first, second, direction in revolute_joints:
        _add_revolute_joint(
            root, occurrences[first], occurrences[second], centers[first], name, direction
        )

    child_components = [component for component in design.allComponents if component != root]
    if len(child_components) != 34 or root.allOccurrences.count != 34:
        raise RuntimeError("Packaging machine must contain exactly thirty-four occurrences")
    for component in child_components:
        if component.bRepBodies.count != 1:
            raise RuntimeError("Every packaging component must contain exactly one body")
        body = component.bRepBodies.item(0)
        if not body.isValid or not body.isSolid or body.lumps.count != 1:
            raise RuntimeError("Every packaging component body must be one valid solid lump")
        for feature in component.features:
            if not feature.isValid or feature.errorOrWarningMessage:
                raise RuntimeError("Every packaging feature must be valid and healthy")
    if root.asBuiltJoints.count != 33:
        raise RuntimeError("Packaging machine joint graph must have thirty-three edges")
    for joint in root.asBuiltJoints:
        if not joint.isValid:
            raise RuntimeError("Every packaging machine joint must remain valid")
    result = {
        "success": True,
        "case_id": "b07_packaging_machine",
        "parameters": design.userParameters.count,
        "components": len(child_components),
        "occurrences": root.allOccurrences.count,
        "bodies": sum(component.bRepBodies.count for component in child_components),
        "features": sum(component.features.count for component in child_components),
        "joints": root.asBuiltJoints.count,
        "root_bodies": root.bRepBodies.count,
    }
    payload = json.dumps(result, ensure_ascii=False, sort_keys=True)
    print(payload)
    return payload
