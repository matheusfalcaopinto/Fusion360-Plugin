import adsk.core
import adsk.fusion
import json
import math


def run(_context: str):
    root = target_components["root"]
    design = root.parentDesign

    parameter_specs = [
        ("CaseLength", "120 mm", "mm", "Overall enclosure length"),
        ("CaseWidth", "80 mm", "mm", "Overall enclosure width"),
        ("CaseHeight", "35 mm", "mm", "Overall enclosure height"),
        ("WallThickness", "2.4 mm", "mm", "Side wall thickness"),
        ("FloorThickness", "3 mm", "mm", "Bottom floor thickness"),
        ("BossDiameter", "8 mm", "mm", "PCB boss outside diameter"),
        ("BossHoleDiameter", "3.2 mm", "mm", "PCB boss pilot diameter"),
        ("BossHeight", "18 mm", "mm", "Boss height above the floor"),
        ("BossPitchX", "100 mm", "mm", "Boss center spacing in X"),
        ("BossPitchY", "60 mm", "mm", "Boss center spacing in Y"),
        ("VentLength", "12 mm", "mm", "Overall vent slot length"),
        ("VentWidth", "3 mm", "mm", "Vent slot width"),
        ("VentCols", "5", "", "Vent columns per wall"),
        ("VentRows", "3", "", "Vent rows per wall"),
        ("VentPitchX", "18 mm", "mm", "Vent column pitch"),
        ("VentPitchZ", "9 mm", "mm", "Vent row pitch"),
        ("VentCenterZ", "20 mm", "mm", "Vertical center of the vent lattice"),
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

    wall_sketch = root.sketches.add(root.xYConstructionPlane)
    wall_sketch.name = "SK01_Wall_Ring"
    outer_rect = wall_sketch.sketchCurves.sketchLines.addCenterPointRectangle(
        adsk.core.Point3D.create(0, 0, 0),
        adsk.core.Point3D.create(6.0, 4.0, 0),
    )
    wall_sketch.geometricConstraints.addHorizontal(outer_rect.item(0))
    wall_sketch.geometricConstraints.addHorizontal(outer_rect.item(2))
    wall_sketch.geometricConstraints.addVertical(outer_rect.item(1))
    wall_sketch.geometricConstraints.addVertical(outer_rect.item(3))
    outer_diagonal = wall_sketch.sketchCurves.sketchLines.addByTwoPoints(
        outer_rect.item(0).startSketchPoint,
        outer_rect.item(2).startSketchPoint,
    )
    outer_diagonal.isConstruction = True
    wall_sketch.geometricConstraints.addMidPoint(
        wall_sketch.originPoint,
        outer_diagonal,
    )
    outer_width = wall_sketch.sketchDimensions.addDistanceDimension(
        outer_rect.item(0).startSketchPoint,
        outer_rect.item(0).endSketchPoint,
        horizontal,
        adsk.core.Point3D.create(0, -4.8, 0),
    )
    outer_width.parameter.expression = "CaseLength"
    outer_depth = wall_sketch.sketchDimensions.addDistanceDimension(
        outer_rect.item(1).startSketchPoint,
        outer_rect.item(1).endSketchPoint,
        vertical,
        adsk.core.Point3D.create(6.8, 0, 0),
    )
    outer_depth.parameter.expression = "CaseWidth"

    inner_rect = wall_sketch.sketchCurves.sketchLines.addCenterPointRectangle(
        adsk.core.Point3D.create(0, 0, 0),
        adsk.core.Point3D.create(5.76, 3.76, 0),
    )
    wall_sketch.geometricConstraints.addHorizontal(inner_rect.item(0))
    wall_sketch.geometricConstraints.addHorizontal(inner_rect.item(2))
    wall_sketch.geometricConstraints.addVertical(inner_rect.item(1))
    wall_sketch.geometricConstraints.addVertical(inner_rect.item(3))
    inner_diagonal = wall_sketch.sketchCurves.sketchLines.addByTwoPoints(
        inner_rect.item(0).startSketchPoint,
        inner_rect.item(2).startSketchPoint,
    )
    inner_diagonal.isConstruction = True
    wall_sketch.geometricConstraints.addMidPoint(
        wall_sketch.originPoint,
        inner_diagonal,
    )
    inner_width = wall_sketch.sketchDimensions.addDistanceDimension(
        inner_rect.item(0).startSketchPoint,
        inner_rect.item(0).endSketchPoint,
        horizontal,
        adsk.core.Point3D.create(0, -4.5, 0),
    )
    inner_width.parameter.expression = "CaseLength - 2 * WallThickness"
    inner_depth = wall_sketch.sketchDimensions.addDistanceDimension(
        inner_rect.item(1).startSketchPoint,
        inner_rect.item(1).endSketchPoint,
        vertical,
        adsk.core.Point3D.create(6.4, 0, 0),
    )
    inner_depth.parameter.expression = "CaseWidth - 2 * WallThickness"

    wall_profile = None
    for candidate_profile in wall_sketch.profiles:
        if candidate_profile.profileLoops.count == 2:
            wall_profile = candidate_profile
    if wall_profile is None:
        raise RuntimeError("wall ring profile not found")

    floor_sketch = root.sketches.add(root.xYConstructionPlane)
    floor_sketch.name = "SK02_Floor"
    floor_rect = floor_sketch.sketchCurves.sketchLines.addCenterPointRectangle(
        adsk.core.Point3D.create(0, 0, 0),
        adsk.core.Point3D.create(6.0, 4.0, 0),
    )
    floor_sketch.geometricConstraints.addHorizontal(floor_rect.item(0))
    floor_sketch.geometricConstraints.addHorizontal(floor_rect.item(2))
    floor_sketch.geometricConstraints.addVertical(floor_rect.item(1))
    floor_sketch.geometricConstraints.addVertical(floor_rect.item(3))
    floor_diagonal = floor_sketch.sketchCurves.sketchLines.addByTwoPoints(
        floor_rect.item(0).startSketchPoint,
        floor_rect.item(2).startSketchPoint,
    )
    floor_diagonal.isConstruction = True
    floor_sketch.geometricConstraints.addMidPoint(
        floor_sketch.originPoint,
        floor_diagonal,
    )
    floor_width = floor_sketch.sketchDimensions.addDistanceDimension(
        floor_rect.item(0).startSketchPoint,
        floor_rect.item(0).endSketchPoint,
        horizontal,
        adsk.core.Point3D.create(0, -4.8, 0),
    )
    floor_width.parameter.expression = "CaseLength"
    floor_depth = floor_sketch.sketchDimensions.addDistanceDimension(
        floor_rect.item(1).startSketchPoint,
        floor_rect.item(1).endSketchPoint,
        vertical,
        adsk.core.Point3D.create(6.8, 0, 0),
    )
    floor_depth.parameter.expression = "CaseWidth"
    floor_extrude = root.features.extrudeFeatures.addSimple(
        floor_sketch.profiles.item(0),
        adsk.core.ValueInput.createByString("FloorThickness"),
        adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
    )
    floor_extrude.name = "EX01_Floor"
    enclosure_body = floor_extrude.bodies.item(0)
    enclosure_body.name = "Parametric_Vented_Enclosure"

    wall_extrude = root.features.extrudeFeatures.addSimple(
        wall_profile,
        adsk.core.ValueInput.createByString("CaseHeight"),
        adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
    )
    wall_extrude.name = "EX02_Wall_Ring"
    wall_tools = adsk.core.ObjectCollection.create()
    wall_tools.add(wall_extrude.bodies.item(0))
    wall_combine_input = root.features.combineFeatures.createInput(
        enclosure_body,
        wall_tools,
    )
    wall_combine_input.operation = adsk.fusion.FeatureOperations.JoinFeatureOperation
    wall_combine = root.features.combineFeatures.add(wall_combine_input)
    wall_combine.name = "CB01_Floor_Wall_Join"

    floor_plane_input = root.constructionPlanes.createInput()
    floor_plane_input.setByOffset(
        root.xYConstructionPlane,
        adsk.core.ValueInput.createByString("FloorThickness"),
    )
    floor_plane = root.constructionPlanes.add(floor_plane_input)
    floor_plane.name = "CP01_Interior_Floor"

    boss_sketch = root.sketches.add(root.xYConstructionPlane)
    boss_sketch.name = "SK03_Boss_Seed"
    boss_center = boss_sketch.sketchPoints.add(adsk.core.Point3D.create(-5.0, -3.0, 0))
    boss_center.isFixed = True
    boss_circle = boss_sketch.sketchCurves.sketchCircles.addByCenterRadius(
        boss_center,
        0.4,
    )
    boss_diameter = boss_sketch.sketchDimensions.addDiameterDimension(
        boss_circle,
        adsk.core.Point3D.create(-4.3, -3.0, 0),
    )
    boss_diameter.parameter.expression = "BossDiameter"

    boss_seed = root.features.extrudeFeatures.addSimple(
        boss_sketch.profiles.item(0),
        adsk.core.ValueInput.createByString("FloorThickness + BossHeight"),
        adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
    )
    boss_seed.name = "EX03_Boss_Seed"
    boss_seed.bodies.item(0).name = "Boss_Seed_Body"

    boss_entities = adsk.core.ObjectCollection.create()
    boss_entities.add(boss_seed)
    boss_patterns = root.features.rectangularPatternFeatures
    boss_pattern_input = boss_patterns.createInput(
        boss_entities,
        root.xConstructionAxis,
        adsk.core.ValueInput.createByString("2"),
        adsk.core.ValueInput.createByString("BossPitchX"),
        adsk.fusion.PatternDistanceType.ExtentPatternDistanceType,
    )
    boss_pattern_input.setDirectionTwo(
        root.yConstructionAxis,
        adsk.core.ValueInput.createByString("2"),
        adsk.core.ValueInput.createByString("BossPitchY"),
    )
    boss_pattern = boss_patterns.add(boss_pattern_input)
    boss_pattern.name = "RP01_Boss_2x2"

    boss_tools = adsk.core.ObjectCollection.create()
    for candidate_body in root.bRepBodies:
        if candidate_body.entityToken != enclosure_body.entityToken:
            boss_tools.add(candidate_body)
    boss_combine_input = root.features.combineFeatures.createInput(
        enclosure_body,
        boss_tools,
    )
    boss_combine_input.operation = adsk.fusion.FeatureOperations.JoinFeatureOperation
    boss_combine = root.features.combineFeatures.add(boss_combine_input)
    boss_combine.name = "CB02_Bosses_To_Enclosure"

    pilot_sketch = root.sketches.add(root.xYConstructionPlane)
    pilot_sketch.name = "SK04_Boss_Pilot_Seed"
    pilot_center = pilot_sketch.sketchPoints.add(adsk.core.Point3D.create(-5.0, -3.0, 0))
    pilot_center.isFixed = True
    pilot_circle = pilot_sketch.sketchCurves.sketchCircles.addByCenterRadius(
        pilot_center,
        0.16,
    )
    pilot_diameter = pilot_sketch.sketchDimensions.addDiameterDimension(
        pilot_circle,
        adsk.core.Point3D.create(-4.6, -3.0, 0),
    )
    pilot_diameter.parameter.expression = "BossHoleDiameter"

    pilot_seed = root.features.extrudeFeatures.addSimple(
        pilot_sketch.profiles.item(0),
        adsk.core.ValueInput.createByString("FloorThickness + BossHeight"),
        adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
    )
    pilot_seed.name = "EX04_Boss_Pilot_Seed"
    pilot_seed.bodies.item(0).name = "Boss_Pilot_Seed_Tool"

    pilot_entities = adsk.core.ObjectCollection.create()
    pilot_entities.add(pilot_seed)
    pilot_patterns = root.features.rectangularPatternFeatures
    pilot_pattern_input = pilot_patterns.createInput(
        pilot_entities,
        root.xConstructionAxis,
        adsk.core.ValueInput.createByString("2"),
        adsk.core.ValueInput.createByString("BossPitchX"),
        adsk.fusion.PatternDistanceType.ExtentPatternDistanceType,
    )
    pilot_pattern_input.setDirectionTwo(
        root.yConstructionAxis,
        adsk.core.ValueInput.createByString("2"),
        adsk.core.ValueInput.createByString("BossPitchY"),
    )
    pilot_pattern = pilot_patterns.add(pilot_pattern_input)
    pilot_pattern.name = "RP02_Boss_Pilot_2x2"

    pilot_tools = adsk.core.ObjectCollection.create()
    for candidate_body in root.bRepBodies:
        if candidate_body.entityToken != enclosure_body.entityToken:
            pilot_tools.add(candidate_body)
    pilot_combine_input = root.features.combineFeatures.createInput(
        enclosure_body,
        pilot_tools,
    )
    pilot_combine_input.operation = adsk.fusion.FeatureOperations.CutFeatureOperation
    pilot_combine = root.features.combineFeatures.add(pilot_combine_input)
    pilot_combine.name = "CB03_Boss_Pilot_Tools_Cut"

    front_plane_input = root.constructionPlanes.createInput()
    front_plane_input.setByOffset(
        root.xZConstructionPlane,
        adsk.core.ValueInput.createByString("-CaseWidth / 2"),
    )
    front_plane = root.constructionPlanes.add(front_plane_input)
    front_plane.name = "CP02_Front_Wall"

    vent_sketch = root.sketches.add(root.xZConstructionPlane)
    vent_sketch.name = "SK05_Front_Vent_Seed"
    vent_center_geometry = adsk.core.Point3D.create(-3.6, -1.1, 0)
    vent_center = vent_sketch.sketchPoints.add(vent_center_geometry)
    vent_center.isFixed = True
    vent_geometry = vent_sketch.addCenterPointSlot(
        vent_center,
        adsk.core.Point3D.create(-2.6, -1.1, 0),
        adsk.core.ValueInput.createByString("VentWidth"),
        True,
        adsk.core.ValueInput.createByString("(VentLength - VentWidth) / 2"),
    )
    vent_sketch.geometricConstraints.addHorizontal(vent_geometry[4])

    vent_seed = root.features.extrudeFeatures.addSimple(
        vent_sketch.profiles.item(0),
        adsk.core.ValueInput.createByString("CaseWidth / 2 + WallThickness"),
        adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
    )
    vent_seed.name = "EX05_Front_Vent_Seed"
    vent_seed.bodies.item(0).name = "Vent_Seed_Tool"

    vent_entities = adsk.core.ObjectCollection.create()
    vent_entities.add(vent_seed)
    vent_patterns = root.features.rectangularPatternFeatures
    vent_pattern_input = vent_patterns.createInput(
        vent_entities,
        root.xConstructionAxis,
        adsk.core.ValueInput.createByString("VentCols"),
        adsk.core.ValueInput.createByString("VentPitchX * (VentCols - 1)"),
        adsk.fusion.PatternDistanceType.ExtentPatternDistanceType,
    )
    vent_pattern_input.setDirectionTwo(
        root.zConstructionAxis,
        adsk.core.ValueInput.createByString("VentRows"),
        adsk.core.ValueInput.createByString("VentPitchZ * (VentRows - 1)"),
    )
    vent_pattern = vent_patterns.add(vent_pattern_input)
    vent_pattern.name = "RP03_Front_Vent_5x3"

    mirror_entities = adsk.core.ObjectCollection.create()
    mirror_entities.add(vent_seed)
    mirror_entities.add(vent_pattern)
    mirror_input = root.features.mirrorFeatures.createInput(
        mirror_entities,
        root.xZConstructionPlane,
    )
    rear_vents = root.features.mirrorFeatures.add(mirror_input)
    rear_vents.name = "MR01_Rear_Vent_5x3"

    vent_tools = adsk.core.ObjectCollection.create()
    for candidate_body in root.bRepBodies:
        if candidate_body.entityToken != enclosure_body.entityToken:
            vent_tools.add(candidate_body)
    vent_combine_input = root.features.combineFeatures.createInput(
        enclosure_body,
        vent_tools,
    )
    vent_combine_input.operation = adsk.fusion.FeatureOperations.CutFeatureOperation
    vent_combine = root.features.combineFeatures.add(vent_combine_input)
    vent_combine.name = "CB04_Vent_Tools_Cut"

    result = {
        "success": True,
        "case_id": "b02_vented_enclosure",
        "body": enclosure_body.name,
        "parameters": design.userParameters.count,
        "bodies": root.bRepBodies.count,
        "features": root.features.count,
        "sketches": root.sketches.count,
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return json.dumps(result, ensure_ascii=False, sort_keys=True)
