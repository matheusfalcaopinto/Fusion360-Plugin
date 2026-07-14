import adsk.core
import adsk.fusion
import json
import math


def run(_context: str):
    root = target_components["root"]
    design = root.parentDesign

    parameter_specs = [
        ("BaseWidth", "100 mm", "mm", "Bottom flange width"),
        ("BaseDepth", "70 mm", "mm", "Bottom flange depth"),
        ("BaseThickness", "5 mm", "mm", "Bottom flange thickness"),
        ("InletWidth", "80 mm", "mm", "Rectangular inlet clear width"),
        ("InletDepth", "50 mm", "mm", "Rectangular inlet clear depth"),
        ("WallThickness", "3 mm", "mm", "Duct wall thickness at the ports"),
        ("TransitionHeight", "90 mm", "mm", "Transition height above the bottom flange"),
        ("OutletInnerDiameter", "54 mm", "mm", "Circular outlet clear diameter"),
        ("OutletOuterDiameter", "60 mm", "mm", "Circular outlet outside diameter"),
        ("OutletOffsetX", "14 mm", "mm", "Outlet axis X offset"),
        ("OutletOffsetY", "8 mm", "mm", "Outlet axis Y offset"),
        ("TopFlangeDiameter", "82 mm", "mm", "Top flange outside diameter"),
        ("TopFlangeThickness", "5 mm", "mm", "Top flange thickness"),
        ("BottomBoltPitchX", "84 mm", "mm", "Bottom bolt grid pitch in X"),
        ("BottomBoltPitchY", "54 mm", "mm", "Bottom bolt grid pitch in Y"),
        ("BottomBoltDiameter", "5 mm", "mm", "Bottom flange bolt diameter"),
        ("TopBoltCircleDiameter", "72 mm", "mm", "Top flange bolt circle diameter"),
        ("TopBoltDiameter", "4.5 mm", "mm", "Top flange bolt diameter"),
        ("TopBoltCount", "6", "", "Top flange bolt count"),
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

    base_sketch = root.sketches.add(root.xYConstructionPlane)
    base_sketch.name = "SK01_Base_Flange_Ring"
    base_lines = base_sketch.sketchCurves.sketchLines
    base_outer = base_lines.addCenterPointRectangle(
        adsk.core.Point3D.create(0, 0, 0),
        adsk.core.Point3D.create(5.0, 3.5, 0),
    )
    base_inner = base_lines.addCenterPointRectangle(
        adsk.core.Point3D.create(0, 0, 0),
        adsk.core.Point3D.create(4.0, 2.5, 0),
    )
    for rectangle in (base_outer, base_inner):
        base_sketch.geometricConstraints.addHorizontal(rectangle.item(0))
        base_sketch.geometricConstraints.addHorizontal(rectangle.item(2))
        base_sketch.geometricConstraints.addVertical(rectangle.item(1))
        base_sketch.geometricConstraints.addVertical(rectangle.item(3))
        diagonal = base_lines.addByTwoPoints(
            rectangle.item(0).startSketchPoint,
            rectangle.item(2).startSketchPoint,
        )
        diagonal.isConstruction = True
        base_sketch.geometricConstraints.addMidPoint(base_sketch.originPoint, diagonal)
    base_outer_width = base_sketch.sketchDimensions.addDistanceDimension(
        base_outer.item(0).startSketchPoint,
        base_outer.item(0).endSketchPoint,
        horizontal,
        adsk.core.Point3D.create(0, -4.2, 0),
    )
    base_outer_width.parameter.expression = "BaseWidth"
    base_outer_depth = base_sketch.sketchDimensions.addDistanceDimension(
        base_outer.item(1).startSketchPoint,
        base_outer.item(1).endSketchPoint,
        vertical,
        adsk.core.Point3D.create(5.8, 0, 0),
    )
    base_outer_depth.parameter.expression = "BaseDepth"
    base_inner_width = base_sketch.sketchDimensions.addDistanceDimension(
        base_inner.item(0).startSketchPoint,
        base_inner.item(0).endSketchPoint,
        horizontal,
        adsk.core.Point3D.create(0, -2.9, 0),
    )
    base_inner_width.parameter.expression = "InletWidth"
    base_inner_depth = base_sketch.sketchDimensions.addDistanceDimension(
        base_inner.item(1).startSketchPoint,
        base_inner.item(1).endSketchPoint,
        vertical,
        adsk.core.Point3D.create(4.5, 0, 0),
    )
    base_inner_depth.parameter.expression = "InletDepth"
    base_ring_profile = None
    for profile in base_sketch.profiles:
        if profile.profileLoops.count == 2:
            base_ring_profile = profile
    if base_ring_profile is None:
        raise RuntimeError("Base flange ring profile was not found")
    base_extrude = root.features.extrudeFeatures.addSimple(
        base_ring_profile,
        adsk.core.ValueInput.createByString("BaseThickness"),
        adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
    )
    base_extrude.name = "EX01_Base_Flange"
    adapter_body = base_extrude.bodies.item(0)
    adapter_body.name = "Offset_Duct_Adapter"

    lower_plane_input = root.constructionPlanes.createInput()
    lower_plane_input.setByOffset(
        root.xYConstructionPlane,
        adsk.core.ValueInput.createByString("BaseThickness"),
    )
    lower_plane = root.constructionPlanes.add(lower_plane_input)
    lower_plane.name = "CP01_Transition_Lower"
    upper_plane_input = root.constructionPlanes.createInput()
    upper_plane_input.setByOffset(
        root.xYConstructionPlane,
        adsk.core.ValueInput.createByString("BaseThickness + TransitionHeight"),
    )
    upper_plane = root.constructionPlanes.add(upper_plane_input)
    upper_plane.name = "CP02_Transition_Upper"

    outer_lower_sketch = root.sketches.add(lower_plane)
    outer_lower_sketch.name = "SK02_Outer_Lower_86x56"
    outer_lower_lines = outer_lower_sketch.sketchCurves.sketchLines
    outer_lower_rect = outer_lower_lines.addCenterPointRectangle(
        adsk.core.Point3D.create(0, 0, 0),
        adsk.core.Point3D.create(4.3, 2.8, 0),
    )
    outer_lower_sketch.geometricConstraints.addHorizontal(outer_lower_rect.item(0))
    outer_lower_sketch.geometricConstraints.addHorizontal(outer_lower_rect.item(2))
    outer_lower_sketch.geometricConstraints.addVertical(outer_lower_rect.item(1))
    outer_lower_sketch.geometricConstraints.addVertical(outer_lower_rect.item(3))
    outer_lower_diagonal = outer_lower_lines.addByTwoPoints(
        outer_lower_rect.item(0).startSketchPoint,
        outer_lower_rect.item(2).startSketchPoint,
    )
    outer_lower_diagonal.isConstruction = True
    outer_lower_sketch.geometricConstraints.addMidPoint(
        outer_lower_sketch.originPoint,
        outer_lower_diagonal,
    )
    outer_lower_width = outer_lower_sketch.sketchDimensions.addDistanceDimension(
        outer_lower_rect.item(0).startSketchPoint,
        outer_lower_rect.item(0).endSketchPoint,
        horizontal,
        adsk.core.Point3D.create(0, -3.3, 0),
    )
    outer_lower_width.parameter.expression = "InletWidth + 2 * WallThickness"
    outer_lower_depth = outer_lower_sketch.sketchDimensions.addDistanceDimension(
        outer_lower_rect.item(1).startSketchPoint,
        outer_lower_rect.item(1).endSketchPoint,
        vertical,
        adsk.core.Point3D.create(4.8, 0, 0),
    )
    outer_lower_depth.parameter.expression = "InletDepth + 2 * WallThickness"

    outer_upper_sketch = root.sketches.add(upper_plane)
    outer_upper_sketch.name = "SK03_Outer_Outlet_OD60"
    outer_upper_circle = outer_upper_sketch.sketchCurves.sketchCircles.addByCenterRadius(
        adsk.core.Point3D.create(1.4, 0.8, 0),
        3.0,
    )
    outer_upper_center = outer_upper_circle.centerSketchPoint
    outer_upper_x = outer_upper_sketch.sketchDimensions.addDistanceDimension(
        outer_upper_sketch.originPoint,
        outer_upper_center,
        horizontal,
        adsk.core.Point3D.create(0.7, -0.5, 0),
    )
    outer_upper_x.parameter.expression = "OutletOffsetX"
    outer_upper_y = outer_upper_sketch.sketchDimensions.addDistanceDimension(
        outer_upper_sketch.originPoint,
        outer_upper_center,
        vertical,
        adsk.core.Point3D.create(2.0, 0.4, 0),
    )
    outer_upper_y.parameter.expression = "OutletOffsetY"
    outer_upper_diameter = outer_upper_sketch.sketchDimensions.addDiameterDimension(
        outer_upper_circle,
        adsk.core.Point3D.create(4.8, 0.8, 0),
    )
    outer_upper_diameter.parameter.expression = "OutletOuterDiameter"

    lofts = root.features.loftFeatures
    outer_loft_input = lofts.createInput(
        adsk.fusion.FeatureOperations.NewBodyFeatureOperation
    )
    outer_loft_input.isSolid = True
    outer_loft_input.loftSections.add(outer_lower_sketch.profiles.item(0))
    outer_loft_input.loftSections.add(outer_upper_sketch.profiles.item(0))
    outer_loft = lofts.add(outer_loft_input)
    outer_loft.name = "LF01_Outer_Transition"
    outer_tools = adsk.core.ObjectCollection.create()
    outer_tools.add(outer_loft.bodies.item(0))
    outer_join_input = root.features.combineFeatures.createInput(adapter_body, outer_tools)
    outer_join_input.operation = adsk.fusion.FeatureOperations.JoinFeatureOperation
    outer_join = root.features.combineFeatures.add(outer_join_input)
    outer_join.name = "CB01_Base_Outer_Join"

    top_flange_sketch = root.sketches.add(upper_plane)
    top_flange_sketch.name = "SK04_Top_Flange_Ring"
    top_outer_circle = top_flange_sketch.sketchCurves.sketchCircles.addByCenterRadius(
        adsk.core.Point3D.create(1.4, 0.8, 0),
        4.1,
    )
    top_inner_circle = top_flange_sketch.sketchCurves.sketchCircles.addByCenterRadius(
        adsk.core.Point3D.create(1.4, 0.8, 0),
        2.7,
    )
    top_flange_sketch.geometricConstraints.addConcentric(
        top_outer_circle,
        top_inner_circle,
    )
    top_center = top_outer_circle.centerSketchPoint
    top_center_x = top_flange_sketch.sketchDimensions.addDistanceDimension(
        top_flange_sketch.originPoint,
        top_center,
        horizontal,
        adsk.core.Point3D.create(0.7, -0.5, 0),
    )
    top_center_x.parameter.expression = "OutletOffsetX"
    top_center_y = top_flange_sketch.sketchDimensions.addDistanceDimension(
        top_flange_sketch.originPoint,
        top_center,
        vertical,
        adsk.core.Point3D.create(2.0, 0.4, 0),
    )
    top_center_y.parameter.expression = "OutletOffsetY"
    top_outer_diameter = top_flange_sketch.sketchDimensions.addDiameterDimension(
        top_outer_circle,
        adsk.core.Point3D.create(5.8, 0.8, 0),
    )
    top_outer_diameter.parameter.expression = "TopFlangeDiameter"
    top_inner_diameter = top_flange_sketch.sketchDimensions.addDiameterDimension(
        top_inner_circle,
        adsk.core.Point3D.create(4.4, 0.8, 0),
    )
    top_inner_diameter.parameter.expression = "OutletInnerDiameter"
    top_ring_profile = None
    for profile in top_flange_sketch.profiles:
        if profile.profileLoops.count == 2:
            top_ring_profile = profile
    if top_ring_profile is None:
        raise RuntimeError("Top flange ring profile was not found")
    top_flange_extrude = root.features.extrudeFeatures.addSimple(
        top_ring_profile,
        adsk.core.ValueInput.createByString("TopFlangeThickness"),
        adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
    )
    top_flange_extrude.name = "EX02_Top_Flange"
    top_tools = adsk.core.ObjectCollection.create()
    top_tools.add(top_flange_extrude.bodies.item(0))
    top_join_input = root.features.combineFeatures.createInput(adapter_body, top_tools)
    top_join_input.operation = adsk.fusion.FeatureOperations.JoinFeatureOperation
    top_join = root.features.combineFeatures.add(top_join_input)
    top_join.name = "CB02_Top_Flange_Join"

    outlet_axis_face = None
    for face in adapter_body.faces:
        geometry = face.geometry
        if geometry is not None and geometry.objectType == adsk.core.Cylinder.classType():
            if (
                math.fabs(geometry.radius - 4.1) < 0.01
                and math.fabs(geometry.origin.x - 1.4) < 0.01
                and math.fabs(geometry.origin.y - 0.8) < 0.01
                and math.fabs(geometry.axis.z) > 0.99
            ):
                outlet_axis_face = face
    if outlet_axis_face is None:
        raise RuntimeError("Offset outlet axis reference face was not found")
    outlet_axis_input = root.constructionAxes.createInput()
    outlet_axis_input.setByCircularFace(outlet_axis_face)
    outlet_axis = root.constructionAxes.add(outlet_axis_input)
    outlet_axis.name = "CA01_Offset_Outlet_Axis"

    inner_lower_sketch = root.sketches.add(lower_plane)
    inner_lower_sketch.name = "SK05_Inner_Inlet_80x50"
    inner_lower_lines = inner_lower_sketch.sketchCurves.sketchLines
    inner_lower_rect = inner_lower_lines.addCenterPointRectangle(
        adsk.core.Point3D.create(0, 0, 0),
        adsk.core.Point3D.create(4.0, 2.5, 0),
    )
    inner_lower_sketch.geometricConstraints.addHorizontal(inner_lower_rect.item(0))
    inner_lower_sketch.geometricConstraints.addHorizontal(inner_lower_rect.item(2))
    inner_lower_sketch.geometricConstraints.addVertical(inner_lower_rect.item(1))
    inner_lower_sketch.geometricConstraints.addVertical(inner_lower_rect.item(3))
    inner_lower_diagonal = inner_lower_lines.addByTwoPoints(
        inner_lower_rect.item(0).startSketchPoint,
        inner_lower_rect.item(2).startSketchPoint,
    )
    inner_lower_diagonal.isConstruction = True
    inner_lower_sketch.geometricConstraints.addMidPoint(
        inner_lower_sketch.originPoint,
        inner_lower_diagonal,
    )
    inner_lower_width = inner_lower_sketch.sketchDimensions.addDistanceDimension(
        inner_lower_rect.item(0).startSketchPoint,
        inner_lower_rect.item(0).endSketchPoint,
        horizontal,
        adsk.core.Point3D.create(0, -3.0, 0),
    )
    inner_lower_width.parameter.expression = "InletWidth"
    inner_lower_depth = inner_lower_sketch.sketchDimensions.addDistanceDimension(
        inner_lower_rect.item(1).startSketchPoint,
        inner_lower_rect.item(1).endSketchPoint,
        vertical,
        adsk.core.Point3D.create(4.6, 0, 0),
    )
    inner_lower_depth.parameter.expression = "InletDepth"

    inner_upper_sketch = root.sketches.add(upper_plane)
    inner_upper_sketch.name = "SK06_Inner_Outlet_ID54"
    inner_upper_circle = inner_upper_sketch.sketchCurves.sketchCircles.addByCenterRadius(
        adsk.core.Point3D.create(1.4, 0.8, 0),
        2.7,
    )
    inner_upper_center = inner_upper_circle.centerSketchPoint
    inner_upper_x = inner_upper_sketch.sketchDimensions.addDistanceDimension(
        inner_upper_sketch.originPoint,
        inner_upper_center,
        horizontal,
        adsk.core.Point3D.create(0.7, -0.5, 0),
    )
    inner_upper_x.parameter.expression = "OutletOffsetX"
    inner_upper_y = inner_upper_sketch.sketchDimensions.addDistanceDimension(
        inner_upper_sketch.originPoint,
        inner_upper_center,
        vertical,
        adsk.core.Point3D.create(2.0, 0.4, 0),
    )
    inner_upper_y.parameter.expression = "OutletOffsetY"
    inner_upper_diameter = inner_upper_sketch.sketchDimensions.addDiameterDimension(
        inner_upper_circle,
        adsk.core.Point3D.create(4.4, 0.8, 0),
    )
    inner_upper_diameter.parameter.expression = "OutletInnerDiameter"
    inner_loft_input = lofts.createInput(
        adsk.fusion.FeatureOperations.NewBodyFeatureOperation
    )
    inner_loft_input.isSolid = True
    inner_loft_input.loftSections.add(inner_lower_sketch.profiles.item(0))
    inner_loft_input.loftSections.add(inner_upper_sketch.profiles.item(0))
    inner_loft = lofts.add(inner_loft_input)
    inner_loft.name = "LF02_Inner_Passage_Tool"
    inner_tools = adsk.core.ObjectCollection.create()
    inner_tools.add(inner_loft.bodies.item(0))
    inner_cut_input = root.features.combineFeatures.createInput(adapter_body, inner_tools)
    inner_cut_input.operation = adsk.fusion.FeatureOperations.CutFeatureOperation
    inner_cut = root.features.combineFeatures.add(inner_cut_input)
    inner_cut.name = "CB03_Inner_Passage_Cut"

    bottom_hole_sketch = root.sketches.add(root.xYConstructionPlane)
    bottom_hole_sketch.name = "SK07_Bottom_Bolt_Seed"
    bottom_hole_circle = bottom_hole_sketch.sketchCurves.sketchCircles.addByCenterRadius(
        adsk.core.Point3D.create(-4.2, -2.7, 0),
        0.25,
    )
    bottom_hole_center = bottom_hole_circle.centerSketchPoint
    bottom_hole_x = bottom_hole_sketch.sketchDimensions.addDistanceDimension(
        bottom_hole_sketch.originPoint,
        bottom_hole_center,
        horizontal,
        adsk.core.Point3D.create(-2.1, -3.2, 0),
    )
    bottom_hole_x.parameter.expression = "BottomBoltPitchX / 2"
    bottom_hole_y = bottom_hole_sketch.sketchDimensions.addDistanceDimension(
        bottom_hole_sketch.originPoint,
        bottom_hole_center,
        vertical,
        adsk.core.Point3D.create(-4.8, -1.3, 0),
    )
    bottom_hole_y.parameter.expression = "BottomBoltPitchY / 2"
    bottom_hole_diameter = bottom_hole_sketch.sketchDimensions.addDiameterDimension(
        bottom_hole_circle,
        adsk.core.Point3D.create(-3.8, -2.7, 0),
    )
    bottom_hole_diameter.parameter.expression = "BottomBoltDiameter"
    bottom_hole_seed = root.features.extrudeFeatures.addSimple(
        bottom_hole_sketch.profiles.item(0),
        adsk.core.ValueInput.createByString("BaseThickness"),
        adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
    )
    bottom_hole_seed.name = "EX03_Bottom_Bolt_Seed_Tool"
    bottom_pattern_entities = adsk.core.ObjectCollection.create()
    bottom_pattern_entities.add(bottom_hole_seed)
    bottom_pattern_input = root.features.rectangularPatternFeatures.createInput(
        bottom_pattern_entities,
        root.xConstructionAxis,
        adsk.core.ValueInput.createByString("2"),
        adsk.core.ValueInput.createByString("BottomBoltPitchX"),
        adsk.fusion.PatternDistanceType.ExtentPatternDistanceType,
    )
    bottom_pattern_input.setDirectionTwo(
        root.yConstructionAxis,
        adsk.core.ValueInput.createByString("2"),
        adsk.core.ValueInput.createByString("BottomBoltPitchY"),
    )
    bottom_pattern = root.features.rectangularPatternFeatures.add(bottom_pattern_input)
    bottom_pattern.name = "RP01_Bottom_Bolt_2x2"
    bottom_tools = adsk.core.ObjectCollection.create()
    for candidate_body in root.bRepBodies:
        if candidate_body.entityToken != adapter_body.entityToken:
            bottom_tools.add(candidate_body)
    bottom_cut_input = root.features.combineFeatures.createInput(adapter_body, bottom_tools)
    bottom_cut_input.operation = adsk.fusion.FeatureOperations.CutFeatureOperation
    bottom_cut = root.features.combineFeatures.add(bottom_cut_input)
    bottom_cut.name = "CB04_Bottom_Bolts_Cut"

    top_hole_sketch = root.sketches.add(upper_plane)
    top_hole_sketch.name = "SK08_Top_Bolt_Seed"
    top_hole_circle = top_hole_sketch.sketchCurves.sketchCircles.addByCenterRadius(
        adsk.core.Point3D.create(5.0, 0.8, 0),
        0.225,
    )
    top_hole_center = top_hole_circle.centerSketchPoint
    top_hole_x = top_hole_sketch.sketchDimensions.addDistanceDimension(
        top_hole_sketch.originPoint,
        top_hole_center,
        horizontal,
        adsk.core.Point3D.create(2.5, -0.2, 0),
    )
    top_hole_x.parameter.expression = "OutletOffsetX + TopBoltCircleDiameter / 2"
    top_hole_y = top_hole_sketch.sketchDimensions.addDistanceDimension(
        top_hole_sketch.originPoint,
        top_hole_center,
        vertical,
        adsk.core.Point3D.create(5.5, 0.4, 0),
    )
    top_hole_y.parameter.expression = "OutletOffsetY"
    top_hole_diameter = top_hole_sketch.sketchDimensions.addDiameterDimension(
        top_hole_circle,
        adsk.core.Point3D.create(5.4, 0.8, 0),
    )
    top_hole_diameter.parameter.expression = "TopBoltDiameter"
    top_hole_seed = root.features.extrudeFeatures.addSimple(
        top_hole_sketch.profiles.item(0),
        adsk.core.ValueInput.createByString("TopFlangeThickness"),
        adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
    )
    top_hole_seed.name = "EX04_Top_Bolt_Seed_Tool"
    top_pattern_entities = adsk.core.ObjectCollection.create()
    top_pattern_entities.add(top_hole_seed)
    top_pattern_input = root.features.circularPatternFeatures.createInput(
        top_pattern_entities,
        outlet_axis,
    )
    top_pattern_input.quantity = adsk.core.ValueInput.createByString("TopBoltCount")
    top_pattern_input.totalAngle = adsk.core.ValueInput.createByString("360 deg")
    top_pattern_input.isSymmetric = False
    top_pattern = root.features.circularPatternFeatures.add(top_pattern_input)
    top_pattern.name = "CP01_Top_Bolt_6x"
    top_hole_tools = adsk.core.ObjectCollection.create()
    for candidate_body in root.bRepBodies:
        if candidate_body.entityToken != adapter_body.entityToken:
            top_hole_tools.add(candidate_body)
    top_cut_input = root.features.combineFeatures.createInput(
        adapter_body,
        top_hole_tools,
    )
    top_cut_input.operation = adsk.fusion.FeatureOperations.CutFeatureOperation
    top_cut = root.features.combineFeatures.add(top_cut_input)
    top_cut.name = "CB05_Top_Bolts_Cut"

    if root.bRepBodies.count != 1:
        raise RuntimeError("Final adapter must contain exactly one body")
    final_body = root.bRepBodies.item(0)
    if not final_body.isSolid or final_body.lumps.count != 1:
        raise RuntimeError("Final adapter must be one connected solid lump")
    for sketch in root.sketches:
        if not sketch.isValid or not sketch.isFullyConstrained:
            raise RuntimeError("Every adapter sketch must be valid and fully constrained")
    for feature in root.features:
        if not feature.isValid or feature.errorOrWarningMessage:
            raise RuntimeError("Every adapter feature must be valid and healthy")

    result = {
        "success": True,
        "case_id": "b04_offset_duct_adapter",
        "body": final_body.name,
        "parameters": design.userParameters.count,
        "bodies": root.bRepBodies.count,
        "lumps": final_body.lumps.count,
        "features": root.features.count,
        "sketches": root.sketches.count,
    }
    payload = json.dumps(result, ensure_ascii=False, sort_keys=True)
    print(payload)
    return payload
