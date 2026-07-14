import adsk.core
import adsk.fusion
import json
import math


def run(_context: str):
    root = target_components["root"]
    design = root.parentDesign

    parameter_specs = [
        ("BlockLength", "90 mm", "mm", "Overall length along the bore axis"),
        ("BlockWidth", "50 mm", "mm", "Overall width across the split block"),
        ("BlockHeight", "44 mm", "mm", "Overall assembled height"),
        ("SplitCenterZ", "22 mm", "mm", "Nominal split center height"),
        ("SplitGap", "0.5 mm", "mm", "Assembly clearance between lower and cap"),
        ("LowerHeight", "SplitCenterZ - SplitGap / 2", "mm", "Lower body height"),
        ("UpperHeight", "BlockHeight - SplitCenterZ - SplitGap / 2", "mm", "Upper cap height"),
        ("BoreDiameter", "24 mm", "mm", "Horizontal shaft bore diameter"),
        ("BoreCenterZ", "SplitCenterZ", "mm", "Shaft bore center height"),
        ("ClampPitchX", "56 mm", "mm", "Clamp-hole pitch along X"),
        ("ClampPitchY", "30 mm", "mm", "Clamp-hole pitch across Y"),
        ("ClampHoleDiameter", "5.5 mm", "mm", "Clamp through-hole diameter"),
        ("CounterboreDiameter", "10 mm", "mm", "Cap counterbore diameter"),
        ("CounterboreDepth", "4 mm", "mm", "Counterbore depth from cap top"),
        ("MountingPitchY", "36 mm", "mm", "Lower mounting-hole pitch across Y"),
        ("MountingHoleDiameter", "7 mm", "mm", "Lower mounting-hole diameter"),
        ("ToolOvertravel", "2 mm", "mm", "Cutting-tool overtravel"),
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
    matrix = adsk.core.Matrix3D.create()

    assembly_reference = root.sketches.add(root.xYConstructionPlane)
    assembly_reference.name = "SK00_Assembly_Reference"

    lower_occurrence = root.occurrences.addNewComponent(matrix)
    lower_component = lower_occurrence.component
    lower_component.name = "CMP01_Lower_Block"

    upper_occurrence = root.occurrences.addNewComponent(matrix)
    upper_component = upper_occurrence.component
    upper_component.name = "CMP02_Upper_Cap"

    lower_outer_sketch = lower_component.sketches.add(lower_component.yZConstructionPlane)
    lower_outer_sketch.name = "SK01_Lower_Outer_Profile"
    lower_lines = lower_outer_sketch.sketchCurves.sketchLines
    lower_bottom = lower_lines.addByTwoPoints(
        lower_outer_sketch.modelToSketchSpace(adsk.core.Point3D.create(0, -2.5, 0)),
        lower_outer_sketch.modelToSketchSpace(adsk.core.Point3D.create(0, 2.5, 0)),
    )
    lower_right = lower_lines.addByTwoPoints(
        lower_bottom.endSketchPoint,
        lower_outer_sketch.modelToSketchSpace(adsk.core.Point3D.create(0, 2.5, 2.175)),
    )
    lower_top = lower_lines.addByTwoPoints(
        lower_right.endSketchPoint,
        lower_outer_sketch.modelToSketchSpace(adsk.core.Point3D.create(0, -2.5, 2.175)),
    )
    lower_left = lower_lines.addByTwoPoints(
        lower_top.endSketchPoint,
        lower_bottom.startSketchPoint,
    )
    lower_constraints = lower_outer_sketch.geometricConstraints
    lower_constraints.addVertical(lower_bottom)
    lower_constraints.addHorizontal(lower_right)
    lower_constraints.addVertical(lower_top)
    lower_constraints.addHorizontal(lower_left)
    lower_constraints.addMidPoint(lower_outer_sketch.originPoint, lower_bottom)
    lower_width_dimension = lower_outer_sketch.sketchDimensions.addDistanceDimension(
        lower_bottom.startSketchPoint,
        lower_bottom.endSketchPoint,
        vertical,
        adsk.core.Point3D.create(0, -0.8, 0),
    )
    lower_width_dimension.parameter.expression = "BlockWidth"
    lower_height_dimension = lower_outer_sketch.sketchDimensions.addDistanceDimension(
        lower_right.startSketchPoint,
        lower_right.endSketchPoint,
        horizontal,
        adsk.core.Point3D.create(3.2, 1.1, 0),
    )
    lower_height_dimension.parameter.expression = "LowerHeight"

    lower_extrudes = lower_component.features.extrudeFeatures
    lower_outer_input = lower_extrudes.createInput(
        lower_outer_sketch.profiles.item(0),
        adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
    )
    lower_outer_input.setSymmetricExtent(
        adsk.core.ValueInput.createByString("BlockLength"),
        True,
    )
    lower_outer_feature = lower_extrudes.add(lower_outer_input)
    lower_outer_feature.name = "EX01_Lower_Block"
    lower_body = lower_outer_feature.bodies.item(0)
    lower_body.name = "B01_Lower_Block"

    lower_bore_sketch = lower_component.sketches.add(lower_component.yZConstructionPlane)
    lower_bore_sketch.name = "SK02_Lower_Bore_Tool"
    lower_bore_center = lower_bore_sketch.sketchPoints.add(
        lower_bore_sketch.modelToSketchSpace(adsk.core.Point3D.create(0, 0, 2.2))
    )
    lower_bore_sketch.geometricConstraints.addHorizontalPoints(
        lower_bore_sketch.originPoint,
        lower_bore_center,
    )
    lower_bore_height = lower_bore_sketch.sketchDimensions.addDistanceDimension(
        lower_bore_sketch.originPoint,
        lower_bore_center,
        horizontal,
        adsk.core.Point3D.create(0.8, 1.1, 0),
    )
    lower_bore_height.parameter.expression = "BoreCenterZ"
    lower_bore_circle = lower_bore_sketch.sketchCurves.sketchCircles.addByCenterRadius(
        lower_bore_center,
        1.2,
    )
    lower_bore_diameter = lower_bore_sketch.sketchDimensions.addDiameterDimension(
        lower_bore_circle,
        adsk.core.Point3D.create(1.5, 2.2, 0),
    )
    lower_bore_diameter.parameter.expression = "BoreDiameter"
    lower_bore_input = lower_extrudes.createInput(
        lower_bore_sketch.profiles.item(0),
        adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
    )
    lower_bore_input.setSymmetricExtent(
        adsk.core.ValueInput.createByString("BlockLength + ToolOvertravel"),
        True,
    )
    lower_bore_tool = lower_extrudes.add(lower_bore_input)
    lower_bore_tool.name = "EX02_Lower_Bore_Tool"
    lower_bore_tools = adsk.core.ObjectCollection.create()
    lower_bore_tools.add(lower_bore_tool.bodies.item(0))
    lower_bore_combine_input = lower_component.features.combineFeatures.createInput(
        lower_body,
        lower_bore_tools,
    )
    lower_bore_combine_input.operation = adsk.fusion.FeatureOperations.CutFeatureOperation
    lower_bore_combine_input.isKeepToolBodies = False
    lower_bore_cut = lower_component.features.combineFeatures.add(lower_bore_combine_input)
    lower_bore_cut.name = "CB01_Lower_Bore_Cut"

    lower_clamp_sketch = lower_component.sketches.add(lower_component.xYConstructionPlane)
    lower_clamp_sketch.name = "SK03_Lower_Clamp_Holes"
    lower_clamp_center = lower_clamp_sketch.sketchPoints.add(
        adsk.core.Point3D.create(-2.8, -1.5, 0)
    )
    lower_clamp_center.isFixed = True
    lower_clamp_circle = lower_clamp_sketch.sketchCurves.sketchCircles.addByCenterRadius(
        lower_clamp_center,
        0.275,
    )
    lower_clamp_diameter = lower_clamp_sketch.sketchDimensions.addDiameterDimension(
        lower_clamp_circle,
        adsk.core.Point3D.create(-2.3, -1.5, 0),
    )
    lower_clamp_diameter.parameter.expression = "ClampHoleDiameter"
    lower_clamp_tool = lower_extrudes.addSimple(
        lower_clamp_sketch.profiles.item(0),
        adsk.core.ValueInput.createByString("LowerHeight + ToolOvertravel"),
        adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
    )
    lower_clamp_tool.name = "EX03_Lower_Clamp_Tools"
    lower_clamp_entities = adsk.core.ObjectCollection.create()
    lower_clamp_entities.add(lower_clamp_tool)
    lower_clamp_pattern_input = lower_component.features.rectangularPatternFeatures.createInput(
        lower_clamp_entities,
        lower_component.xConstructionAxis,
        adsk.core.ValueInput.createByString("2"),
        adsk.core.ValueInput.createByString("ClampPitchX"),
        adsk.fusion.PatternDistanceType.ExtentPatternDistanceType,
    )
    lower_clamp_pattern_input.setDirectionTwo(
        lower_component.yConstructionAxis,
        adsk.core.ValueInput.createByString("2"),
        adsk.core.ValueInput.createByString("ClampPitchY"),
    )
    lower_clamp_pattern = lower_component.features.rectangularPatternFeatures.add(
        lower_clamp_pattern_input
    )
    lower_clamp_pattern.name = "RP01_Lower_Clamp_2x2"
    lower_clamp_tools = adsk.core.ObjectCollection.create()
    for candidate_body in lower_component.bRepBodies:
        if candidate_body.entityToken != lower_body.entityToken:
            lower_clamp_tools.add(candidate_body)
    lower_clamp_combine_input = lower_component.features.combineFeatures.createInput(
        lower_body,
        lower_clamp_tools,
    )
    lower_clamp_combine_input.operation = adsk.fusion.FeatureOperations.CutFeatureOperation
    lower_clamp_combine_input.isKeepToolBodies = False
    lower_clamp_cut = lower_component.features.combineFeatures.add(lower_clamp_combine_input)
    lower_clamp_cut.name = "CB02_Lower_Clamp_Cut"

    lower_mount_sketch = lower_component.sketches.add(lower_component.xYConstructionPlane)
    lower_mount_sketch.name = "SK04_Lower_Mounting_Holes"
    lower_mount_center = lower_mount_sketch.sketchPoints.add(
        adsk.core.Point3D.create(0, -1.8, 0)
    )
    lower_mount_center.isFixed = True
    lower_mount_circle = lower_mount_sketch.sketchCurves.sketchCircles.addByCenterRadius(
        lower_mount_center,
        0.35,
    )
    lower_mount_diameter = lower_mount_sketch.sketchDimensions.addDiameterDimension(
        lower_mount_circle,
        adsk.core.Point3D.create(0.6, -1.8, 0),
    )
    lower_mount_diameter.parameter.expression = "MountingHoleDiameter"
    lower_mount_tool = lower_extrudes.addSimple(
        lower_mount_sketch.profiles.item(0),
        adsk.core.ValueInput.createByString("LowerHeight + ToolOvertravel"),
        adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
    )
    lower_mount_tool.name = "EX04_Lower_Mounting_Tools"
    lower_mount_entities = adsk.core.ObjectCollection.create()
    lower_mount_entities.add(lower_mount_tool)
    lower_mount_pattern_input = lower_component.features.rectangularPatternFeatures.createInput(
        lower_mount_entities,
        lower_component.yConstructionAxis,
        adsk.core.ValueInput.createByString("2"),
        adsk.core.ValueInput.createByString("MountingPitchY"),
        adsk.fusion.PatternDistanceType.ExtentPatternDistanceType,
    )
    lower_mount_pattern = lower_component.features.rectangularPatternFeatures.add(
        lower_mount_pattern_input
    )
    lower_mount_pattern.name = "RP02_Lower_Mounting_2x1"
    lower_mount_tools = adsk.core.ObjectCollection.create()
    for candidate_body in lower_component.bRepBodies:
        if candidate_body.entityToken != lower_body.entityToken:
            lower_mount_tools.add(candidate_body)
    lower_mount_combine_input = lower_component.features.combineFeatures.createInput(
        lower_body,
        lower_mount_tools,
    )
    lower_mount_combine_input.operation = adsk.fusion.FeatureOperations.CutFeatureOperation
    lower_mount_combine_input.isKeepToolBodies = False
    lower_mount_cut = lower_component.features.combineFeatures.add(lower_mount_combine_input)
    lower_mount_cut.name = "CB03_Lower_Mounting_Cut"

    upper_outer_sketch = upper_component.sketches.add(upper_component.yZConstructionPlane)
    upper_outer_sketch.name = "SK05_Upper_Outer_Profile"
    upper_lines = upper_outer_sketch.sketchCurves.sketchLines
    upper_bottom = upper_lines.addByTwoPoints(
        upper_outer_sketch.modelToSketchSpace(adsk.core.Point3D.create(0, -2.5, 2.225)),
        upper_outer_sketch.modelToSketchSpace(adsk.core.Point3D.create(0, 2.5, 2.225)),
    )
    upper_right = upper_lines.addByTwoPoints(
        upper_bottom.endSketchPoint,
        upper_outer_sketch.modelToSketchSpace(adsk.core.Point3D.create(0, 2.5, 4.4)),
    )
    upper_top = upper_lines.addByTwoPoints(
        upper_right.endSketchPoint,
        upper_outer_sketch.modelToSketchSpace(adsk.core.Point3D.create(0, -2.5, 4.4)),
    )
    upper_left = upper_lines.addByTwoPoints(
        upper_top.endSketchPoint,
        upper_bottom.startSketchPoint,
    )
    upper_anchor = upper_outer_sketch.sketchPoints.add(
        upper_outer_sketch.modelToSketchSpace(adsk.core.Point3D.create(0, 0, 2.225))
    )
    upper_constraints = upper_outer_sketch.geometricConstraints
    upper_constraints.addVertical(upper_bottom)
    upper_constraints.addHorizontal(upper_right)
    upper_constraints.addVertical(upper_top)
    upper_constraints.addHorizontal(upper_left)
    upper_constraints.addMidPoint(upper_anchor, upper_bottom)
    upper_constraints.addHorizontalPoints(upper_outer_sketch.originPoint, upper_anchor)
    upper_base_dimension = upper_outer_sketch.sketchDimensions.addDistanceDimension(
        upper_outer_sketch.originPoint,
        upper_anchor,
        horizontal,
        adsk.core.Point3D.create(0.8, 1.1, 0),
    )
    upper_base_dimension.parameter.expression = "SplitCenterZ + SplitGap / 2"
    upper_width_dimension = upper_outer_sketch.sketchDimensions.addDistanceDimension(
        upper_bottom.startSketchPoint,
        upper_bottom.endSketchPoint,
        vertical,
        adsk.core.Point3D.create(0, 1.7, 0),
    )
    upper_width_dimension.parameter.expression = "BlockWidth"
    upper_height_dimension = upper_outer_sketch.sketchDimensions.addDistanceDimension(
        upper_right.startSketchPoint,
        upper_right.endSketchPoint,
        horizontal,
        adsk.core.Point3D.create(3.2, 3.3, 0),
    )
    upper_height_dimension.parameter.expression = "UpperHeight"

    upper_extrudes = upper_component.features.extrudeFeatures
    upper_outer_input = upper_extrudes.createInput(
        upper_outer_sketch.profiles.item(0),
        adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
    )
    upper_outer_input.setSymmetricExtent(
        adsk.core.ValueInput.createByString("BlockLength"),
        True,
    )
    upper_outer_feature = upper_extrudes.add(upper_outer_input)
    upper_outer_feature.name = "EX05_Upper_Cap"
    upper_body = upper_outer_feature.bodies.item(0)
    upper_body.name = "B02_Upper_Cap"

    upper_bore_sketch = upper_component.sketches.add(upper_component.yZConstructionPlane)
    upper_bore_sketch.name = "SK06_Upper_Bore_Tool"
    upper_bore_center = upper_bore_sketch.sketchPoints.add(
        upper_bore_sketch.modelToSketchSpace(adsk.core.Point3D.create(0, 0, 2.2))
    )
    upper_bore_sketch.geometricConstraints.addHorizontalPoints(
        upper_bore_sketch.originPoint,
        upper_bore_center,
    )
    upper_bore_height = upper_bore_sketch.sketchDimensions.addDistanceDimension(
        upper_bore_sketch.originPoint,
        upper_bore_center,
        horizontal,
        adsk.core.Point3D.create(0.8, 1.1, 0),
    )
    upper_bore_height.parameter.expression = "BoreCenterZ"
    upper_bore_circle = upper_bore_sketch.sketchCurves.sketchCircles.addByCenterRadius(
        upper_bore_center,
        1.2,
    )
    upper_bore_diameter = upper_bore_sketch.sketchDimensions.addDiameterDimension(
        upper_bore_circle,
        adsk.core.Point3D.create(1.5, 2.2, 0),
    )
    upper_bore_diameter.parameter.expression = "BoreDiameter"
    upper_bore_input = upper_extrudes.createInput(
        upper_bore_sketch.profiles.item(0),
        adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
    )
    upper_bore_input.setSymmetricExtent(
        adsk.core.ValueInput.createByString("BlockLength + ToolOvertravel"),
        True,
    )
    upper_bore_tool = upper_extrudes.add(upper_bore_input)
    upper_bore_tool.name = "EX06_Upper_Bore_Tool"
    upper_bore_tools = adsk.core.ObjectCollection.create()
    upper_bore_tools.add(upper_bore_tool.bodies.item(0))
    upper_bore_combine_input = upper_component.features.combineFeatures.createInput(
        upper_body,
        upper_bore_tools,
    )
    upper_bore_combine_input.operation = adsk.fusion.FeatureOperations.CutFeatureOperation
    upper_bore_combine_input.isKeepToolBodies = False
    upper_bore_cut = upper_component.features.combineFeatures.add(upper_bore_combine_input)
    upper_bore_cut.name = "CB04_Upper_Bore_Cut"

    upper_clamp_sketch = upper_component.sketches.add(upper_component.xYConstructionPlane)
    upper_clamp_sketch.name = "SK07_Upper_Clamp_Holes"
    upper_clamp_center = upper_clamp_sketch.sketchPoints.add(
        adsk.core.Point3D.create(-2.8, -1.5, 0)
    )
    upper_clamp_center.isFixed = True
    upper_clamp_circle = upper_clamp_sketch.sketchCurves.sketchCircles.addByCenterRadius(
        upper_clamp_center,
        0.275,
    )
    upper_clamp_diameter = upper_clamp_sketch.sketchDimensions.addDiameterDimension(
        upper_clamp_circle,
        adsk.core.Point3D.create(-2.3, -1.5, 0),
    )
    upper_clamp_diameter.parameter.expression = "ClampHoleDiameter"
    upper_clamp_tool = upper_extrudes.addSimple(
        upper_clamp_sketch.profiles.item(0),
        adsk.core.ValueInput.createByString("BlockHeight + ToolOvertravel"),
        adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
    )
    upper_clamp_tool.name = "EX07_Upper_Clamp_Tools"
    upper_clamp_entities = adsk.core.ObjectCollection.create()
    upper_clamp_entities.add(upper_clamp_tool)
    upper_clamp_pattern_input = upper_component.features.rectangularPatternFeatures.createInput(
        upper_clamp_entities,
        upper_component.xConstructionAxis,
        adsk.core.ValueInput.createByString("2"),
        adsk.core.ValueInput.createByString("ClampPitchX"),
        adsk.fusion.PatternDistanceType.ExtentPatternDistanceType,
    )
    upper_clamp_pattern_input.setDirectionTwo(
        upper_component.yConstructionAxis,
        adsk.core.ValueInput.createByString("2"),
        adsk.core.ValueInput.createByString("ClampPitchY"),
    )
    upper_clamp_pattern = upper_component.features.rectangularPatternFeatures.add(
        upper_clamp_pattern_input
    )
    upper_clamp_pattern.name = "RP03_Upper_Clamp_2x2"
    upper_clamp_tools = adsk.core.ObjectCollection.create()
    for candidate_body in upper_component.bRepBodies:
        if candidate_body.entityToken != upper_body.entityToken:
            upper_clamp_tools.add(candidate_body)
    upper_clamp_combine_input = upper_component.features.combineFeatures.createInput(
        upper_body,
        upper_clamp_tools,
    )
    upper_clamp_combine_input.operation = adsk.fusion.FeatureOperations.CutFeatureOperation
    upper_clamp_combine_input.isKeepToolBodies = False
    upper_clamp_cut = upper_component.features.combineFeatures.add(upper_clamp_combine_input)
    upper_clamp_cut.name = "CB05_Upper_Clamp_Cut"

    upper_xy_normal_z = upper_component.xYConstructionPlane.geometry.normal.z
    counterbore_offset_expression = "BlockHeight - CounterboreDepth"
    counterbore_distance_expression = "CounterboreDepth + ToolOvertravel / 2"
    if upper_xy_normal_z < 0:
        counterbore_offset_expression = "-(BlockHeight - CounterboreDepth)"
        counterbore_distance_expression = "-(CounterboreDepth + ToolOvertravel / 2)"
    counterbore_plane_input = upper_component.constructionPlanes.createInput()
    counterbore_plane_input.setByOffset(
        upper_component.xYConstructionPlane,
        adsk.core.ValueInput.createByString(counterbore_offset_expression),
    )
    counterbore_plane = upper_component.constructionPlanes.add(counterbore_plane_input)
    counterbore_plane.name = "CP01_Counterbore_Start"
    counterbore_sketch = upper_component.sketches.add(counterbore_plane)
    counterbore_sketch.name = "SK08_Upper_Counterbores"
    counterbore_center = counterbore_sketch.sketchPoints.add(
        counterbore_sketch.modelToSketchSpace(
            adsk.core.Point3D.create(-2.8, -1.5, 4.0)
        )
    )
    counterbore_center.isFixed = True
    counterbore_circle = counterbore_sketch.sketchCurves.sketchCircles.addByCenterRadius(
        counterbore_center,
        0.5,
    )
    counterbore_diameter = counterbore_sketch.sketchDimensions.addDiameterDimension(
        counterbore_circle,
        adsk.core.Point3D.create(-2.1, -1.5, 0),
    )
    counterbore_diameter.parameter.expression = "CounterboreDiameter"
    counterbore_tool = upper_extrudes.addSimple(
        counterbore_sketch.profiles.item(0),
        adsk.core.ValueInput.createByString(counterbore_distance_expression),
        adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
    )
    counterbore_tool.name = "EX08_Upper_Counterbore_Tools"
    counterbore_entities = adsk.core.ObjectCollection.create()
    counterbore_entities.add(counterbore_tool)
    counterbore_pattern_input = upper_component.features.rectangularPatternFeatures.createInput(
        counterbore_entities,
        upper_component.xConstructionAxis,
        adsk.core.ValueInput.createByString("2"),
        adsk.core.ValueInput.createByString("ClampPitchX"),
        adsk.fusion.PatternDistanceType.ExtentPatternDistanceType,
    )
    counterbore_pattern_input.setDirectionTwo(
        upper_component.yConstructionAxis,
        adsk.core.ValueInput.createByString("2"),
        adsk.core.ValueInput.createByString("ClampPitchY"),
    )
    counterbore_pattern = upper_component.features.rectangularPatternFeatures.add(
        counterbore_pattern_input
    )
    counterbore_pattern.name = "RP04_Upper_Counterbore_2x2"
    counterbore_tools = adsk.core.ObjectCollection.create()
    for candidate_body in upper_component.bRepBodies:
        if candidate_body.entityToken != upper_body.entityToken:
            counterbore_tools.add(candidate_body)
    counterbore_combine_input = upper_component.features.combineFeatures.createInput(
        upper_body,
        counterbore_tools,
    )
    counterbore_combine_input.operation = adsk.fusion.FeatureOperations.CutFeatureOperation
    counterbore_combine_input.isKeepToolBodies = False
    counterbore_cut = upper_component.features.combineFeatures.add(counterbore_combine_input)
    counterbore_cut.name = "CB06_Upper_Counterbore_Cut"

    result = {
        "success": True,
        "case_id": "b03_split_pillow_block",
        "components": [lower_component.name, upper_component.name],
        "occurrences": [lower_occurrence.name, upper_occurrence.name],
        "bodies": [lower_body.name, upper_body.name],
        "parameters": design.userParameters.count,
        "root_sketches": root.sketches.count,
        "root_bodies": root.bRepBodies.count,
        "lower_bodies": lower_component.bRepBodies.count,
        "upper_bodies": upper_component.bRepBodies.count,
    }
    payload = json.dumps(result, ensure_ascii=False, sort_keys=True)
    print(payload)
    return payload
