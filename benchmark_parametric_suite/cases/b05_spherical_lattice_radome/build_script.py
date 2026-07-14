import adsk.core
import adsk.fusion
import json
import math


def _model_point(sketch, x, z):
    return sketch.modelToSketchSpace(adsk.core.Point3D.create(x, 0, z))


def _profile_with_loops(sketch, loop_count):
    for profile in sketch.profiles:
        if profile.profileLoops.count == loop_count:
            return profile
    raise RuntimeError("Required sketch profile was not created")


def _add_quarter_dome_profile(root, sketch_name, radius_cm, expression):
    sketch = root.sketches.add(root.xZConstructionPlane)
    sketch.name = sketch_name
    center = _model_point(sketch, 0, 0)
    start = _model_point(sketch, radius_cm, 0)
    middle = _model_point(
        sketch,
        radius_cm / math.sqrt(2.0),
        radius_cm / math.sqrt(2.0),
    )
    end = _model_point(sketch, 0, radius_cm)
    arc = sketch.sketchCurves.sketchArcs.addByThreePoints(start, middle, end)
    axis_line = sketch.sketchCurves.sketchLines.addByTwoPoints(
        arc.endSketchPoint,
        sketch.originPoint,
    )
    base_line = sketch.sketchCurves.sketchLines.addByTwoPoints(
        sketch.originPoint,
        arc.startSketchPoint,
    )
    constraints = sketch.geometricConstraints
    constraints.addCoincident(arc.centerSketchPoint, sketch.originPoint)
    constraints.addVertical(axis_line)
    constraints.addHorizontal(base_line)
    radial = sketch.sketchDimensions.addRadialDimension(
        arc,
        _model_point(sketch, radius_cm * 0.72, radius_cm * 0.72),
    )
    radial.parameter.expression = expression
    return sketch, sketch.profiles.item(0)


def _add_latitude_ring(root, angle_degrees, index):
    design = root.parentDesign
    radius_name = f"Ring{index}Radius"
    height_name = f"Ring{index}Height"
    angle_name = f"Ring{index}Angle"
    angle_radians = math.radians(angle_degrees)
    radius_cm = 9.0 * math.cos(angle_radians)
    height_cm = 9.0 * math.sin(angle_radians)
    design.userParameters.add(
        angle_name,
        adsk.core.ValueInput.createByString(f"{angle_degrees} deg"),
        "deg",
        f"Latitude ring {index} angle from the equator",
    )
    design.userParameters.add(
        radius_name,
        adsk.core.ValueInput.createByString(f"DomeRadius * cos({angle_name})"),
        "mm",
        f"Latitude ring {index} radial location",
    )
    design.userParameters.add(
        height_name,
        adsk.core.ValueInput.createByString(f"DomeRadius * sin({angle_name})"),
        "mm",
        f"Latitude ring {index} height",
    )

    sketch = root.sketches.add(root.xZConstructionPlane)
    sketch.name = f"SK{index + 4:02d}_Latitude_Ring_{int(angle_degrees):02d}deg"
    circle = sketch.sketchCurves.sketchCircles.addByCenterRadius(
        _model_point(sketch, radius_cm, height_cm),
        0.2,
    )
    center = circle.centerSketchPoint
    horizontal = sketch.sketchDimensions.addDistanceDimension(
        sketch.originPoint,
        center,
        adsk.fusion.DimensionOrientations.HorizontalDimensionOrientation,
        _model_point(sketch, radius_cm * 0.55, height_cm + 0.7),
    )
    horizontal.parameter.expression = radius_name
    vertical = sketch.sketchDimensions.addDistanceDimension(
        sketch.originPoint,
        center,
        adsk.fusion.DimensionOrientations.VerticalDimensionOrientation,
        _model_point(sketch, radius_cm + 0.7, height_cm * 0.55),
    )
    vertical.parameter.expression = height_name
    diameter = sketch.sketchDimensions.addDiameterDimension(
        circle,
        _model_point(sketch, radius_cm + 0.5, height_cm + 0.5),
    )
    diameter.parameter.expression = "2 * GridRibRadius"

    revolves = root.features.revolveFeatures
    ring_input = revolves.createInput(
        sketch.profiles.item(0),
        root.zConstructionAxis,
        adsk.fusion.FeatureOperations.JoinFeatureOperation,
    )
    ring_input.setAngleExtent(
        False,
        adsk.core.ValueInput.createByString("360 deg"),
    )
    ring = revolves.add(ring_input)
    ring.name = f"RV{index + 3:02d}_Latitude_Ring_{int(angle_degrees):02d}deg"
    return ring


def run(_context: str):
    root = target_components["root"]
    design = root.parentDesign

    parameter_specs = [
        ("DomeRadius", "90 mm", "mm", "Outer spherical radius"),
        ("ShellThickness", "3 mm", "mm", "Radome shell thickness"),
        ("BaseFlangeOD", "200 mm", "mm", "Base flange outside diameter"),
        ("BaseFlangeID", "2 * (DomeRadius - ShellThickness)", "mm", "Base opening diameter"),
        ("BaseFlangeThickness", "6 mm", "mm", "Base flange thickness below datum"),
        ("GridRibHeight", "4 mm", "mm", "Meridional rib height outside shell"),
        ("GridRibEmbed", "0.8 mm", "mm", "Meridional rib overlap into shell"),
        ("GridRibRadius", "2 mm", "mm", "Latitude ring section radius"),
        ("GridRibAngularWidth", "2.4 deg", "deg", "Meridional rib angular width"),
        ("MeridianCount", "12", "", "Number of meridional ribs"),
        ("BaseBoltCircleDiameter", "184 mm", "mm", "Base bolt circle"),
        ("BaseBoltCount", "12", "", "Base bolt count"),
        ("BaseBoltDiameter", "6.5 mm", "mm", "Base through-hole diameter"),
    ]
    for parameter_name, expression, units, comment in parameter_specs:
        design.userParameters.add(
            parameter_name,
            adsk.core.ValueInput.createByString(expression),
            units,
            comment,
        )

    outer_sketch, outer_profile = _add_quarter_dome_profile(
        root,
        "SK01_Outer_Dome_Profile",
        9.0,
        "DomeRadius",
    )
    revolves = root.features.revolveFeatures
    outer_input = revolves.createInput(
        outer_profile,
        root.zConstructionAxis,
        adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
    )
    outer_input.setAngleExtent(False, adsk.core.ValueInput.createByString("360 deg"))
    outer_revolve = revolves.add(outer_input)
    outer_revolve.name = "RV01_Outer_Dome"
    radome_body = outer_revolve.bodies.item(0)
    radome_body.name = "B01_Spherical_Lattice_Radome"

    inner_sketch, inner_profile = _add_quarter_dome_profile(
        root,
        "SK02_Inner_Dome_Tool",
        8.7,
        "DomeRadius - ShellThickness",
    )
    inner_input = revolves.createInput(
        inner_profile,
        root.zConstructionAxis,
        adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
    )
    inner_input.setAngleExtent(False, adsk.core.ValueInput.createByString("360 deg"))
    inner_revolve = revolves.add(inner_input)
    inner_revolve.name = "RV02_Inner_Dome_Tool"
    inner_tools = adsk.core.ObjectCollection.create()
    inner_tools.add(inner_revolve.bodies.item(0))
    hollow_input = root.features.combineFeatures.createInput(radome_body, inner_tools)
    hollow_input.operation = adsk.fusion.FeatureOperations.CutFeatureOperation
    hollow_input.isKeepToolBodies = False
    hollow = root.features.combineFeatures.add(hollow_input)
    hollow.name = "CB01_Hollow_Dome"

    flange_sketch = root.sketches.add(root.xYConstructionPlane)
    flange_sketch.name = "SK03_Base_Flange"
    outer_circle = flange_sketch.sketchCurves.sketchCircles.addByCenterRadius(
        adsk.core.Point3D.create(0, 0, 0),
        10.0,
    )
    inner_circle = flange_sketch.sketchCurves.sketchCircles.addByCenterRadius(
        adsk.core.Point3D.create(0, 0, 0),
        8.7,
    )
    outer_diameter = flange_sketch.sketchDimensions.addDiameterDimension(
        outer_circle,
        adsk.core.Point3D.create(10.5, 0, 0),
    )
    outer_diameter.parameter.expression = "BaseFlangeOD"
    inner_diameter = flange_sketch.sketchDimensions.addDiameterDimension(
        inner_circle,
        adsk.core.Point3D.create(8.2, 0, 0),
    )
    inner_diameter.parameter.expression = "BaseFlangeID"
    flange = root.features.extrudeFeatures.addSimple(
        _profile_with_loops(flange_sketch, 2),
        adsk.core.ValueInput.createByString("-BaseFlangeThickness"),
        adsk.fusion.FeatureOperations.JoinFeatureOperation,
    )
    flange.name = "EX01_Base_Flange"

    meridian_sketch = root.sketches.add(root.xZConstructionPlane)
    meridian_sketch.name = "SK04_Meridional_Rib_Profile"
    center = _model_point(meridian_sketch, 0, 0)
    outer_radius = 9.4
    inner_radius = 8.92
    outer_arc = meridian_sketch.sketchCurves.sketchArcs.addByThreePoints(
        _model_point(meridian_sketch, outer_radius, 0),
        _model_point(
            meridian_sketch,
            outer_radius / math.sqrt(2.0),
            outer_radius / math.sqrt(2.0),
        ),
        _model_point(meridian_sketch, 0, outer_radius),
    )
    inner_arc = meridian_sketch.sketchCurves.sketchArcs.addByThreePoints(
        _model_point(meridian_sketch, 0, inner_radius),
        _model_point(
            meridian_sketch,
            inner_radius / math.sqrt(2.0),
            inner_radius / math.sqrt(2.0),
        ),
        _model_point(meridian_sketch, inner_radius, 0),
    )
    meridian_sketch.sketchCurves.sketchLines.addByTwoPoints(
        outer_arc.endSketchPoint,
        inner_arc.startSketchPoint,
    )
    meridian_sketch.sketchCurves.sketchLines.addByTwoPoints(
        inner_arc.endSketchPoint,
        outer_arc.startSketchPoint,
    )
    constraints = meridian_sketch.geometricConstraints
    constraints.addCoincident(outer_arc.centerSketchPoint, meridian_sketch.originPoint)
    constraints.addCoincident(inner_arc.centerSketchPoint, meridian_sketch.originPoint)
    outer_radius_dimension = meridian_sketch.sketchDimensions.addRadialDimension(
        outer_arc,
        _model_point(meridian_sketch, 7.0, 7.0),
    )
    outer_radius_dimension.parameter.expression = "DomeRadius + GridRibHeight"
    inner_radius_dimension = meridian_sketch.sketchDimensions.addRadialDimension(
        inner_arc,
        _model_point(meridian_sketch, 6.2, 6.2),
    )
    inner_radius_dimension.parameter.expression = "DomeRadius - GridRibEmbed"
    meridian_input = revolves.createInput(
        meridian_sketch.profiles.item(0),
        root.zConstructionAxis,
        adsk.fusion.FeatureOperations.JoinFeatureOperation,
    )
    meridian_input.setAngleExtent(
        True,
        adsk.core.ValueInput.createByString("GridRibAngularWidth / 2"),
    )
    meridian = revolves.add(meridian_input)
    meridian.name = "RV03_Meridional_Rib_Seed"
    meridian_entities = adsk.core.ObjectCollection.create()
    meridian_entities.add(meridian)
    meridian_pattern_input = root.features.circularPatternFeatures.createInput(
        meridian_entities,
        root.zConstructionAxis,
    )
    meridian_pattern_input.quantity = adsk.core.ValueInput.createByString("MeridianCount")
    meridian_pattern_input.totalAngle = adsk.core.ValueInput.createByString("360 deg")
    meridian_pattern_input.isSymmetric = False
    meridian_pattern = root.features.circularPatternFeatures.add(meridian_pattern_input)
    meridian_pattern.name = "CP01_Meridional_Ribs"

    ring_angles = [15.0, 30.0, 45.0, 60.0, 75.0]
    for ring_index, ring_angle in enumerate(ring_angles, start=1):
        _add_latitude_ring(root, ring_angle, ring_index)

    bolt_sketch = root.sketches.add(root.xYConstructionPlane)
    bolt_sketch.name = "SK10_Base_Bolt_Seed"
    bolt_circle = bolt_sketch.sketchCurves.sketchCircles.addByCenterRadius(
        adsk.core.Point3D.create(9.2, 0, 0),
        0.325,
    )
    bolt_center = bolt_circle.centerSketchPoint
    bolt_sketch.geometricConstraints.addHorizontalPoints(
        bolt_sketch.originPoint,
        bolt_center,
    )
    bolt_radius_dimension = bolt_sketch.sketchDimensions.addDistanceDimension(
        bolt_sketch.originPoint,
        bolt_center,
        adsk.fusion.DimensionOrientations.HorizontalDimensionOrientation,
        adsk.core.Point3D.create(4.6, -0.8, 0),
    )
    bolt_radius_dimension.parameter.expression = "BaseBoltCircleDiameter / 2"
    bolt_diameter_dimension = bolt_sketch.sketchDimensions.addDiameterDimension(
        bolt_circle,
        adsk.core.Point3D.create(9.8, 0.4, 0),
    )
    bolt_diameter_dimension.parameter.expression = "BaseBoltDiameter"
    bolt_seed = root.features.extrudeFeatures.addSimple(
        bolt_sketch.profiles.item(0),
        adsk.core.ValueInput.createByString("-BaseFlangeThickness"),
        adsk.fusion.FeatureOperations.CutFeatureOperation,
    )
    bolt_seed.name = "EX02_Base_Bolt_Cut_Seed"
    bolt_entities = adsk.core.ObjectCollection.create()
    bolt_entities.add(bolt_seed)
    bolt_pattern_input = root.features.circularPatternFeatures.createInput(
        bolt_entities,
        root.zConstructionAxis,
    )
    bolt_pattern_input.quantity = adsk.core.ValueInput.createByString("BaseBoltCount")
    bolt_pattern_input.totalAngle = adsk.core.ValueInput.createByString("360 deg")
    bolt_pattern_input.isSymmetric = False
    bolt_pattern = root.features.circularPatternFeatures.add(bolt_pattern_input)
    bolt_pattern.name = "CP02_Base_Bolts"

    if root.bRepBodies.count != 1:
        raise RuntimeError("Radome must finish as one connected body")
    final_body = root.bRepBodies.item(0)
    if not final_body.isSolid or final_body.lumps.count != 1:
        raise RuntimeError("Radome must be one valid solid lump")
    for feature in root.features:
        if not feature.isValid or feature.errorOrWarningMessage:
            raise RuntimeError("Every radome feature must be valid and healthy")

    result = {
        "success": True,
        "case_id": "b05_spherical_lattice_radome",
        "body": final_body.name,
        "parameters": design.userParameters.count,
        "features": root.features.count,
        "sketches": root.sketches.count,
        "meridians": int(design.userParameters.itemByName("MeridianCount").value),
        "base_bolts": int(design.userParameters.itemByName("BaseBoltCount").value),
    }
    payload = json.dumps(result, ensure_ascii=False, sort_keys=True)
    print(payload)
    return payload
