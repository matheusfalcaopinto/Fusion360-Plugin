import adsk.core
import adsk.fusion
import json
import math


def _items(collection):
    return [collection.item(index) for index in range(collection.count)]


def _close(actual, expected, tolerance=0.1):
    if (
        isinstance(actual, bool)
        or isinstance(expected, bool)
        or isinstance(tolerance, bool)
    ):
        return False
    try:
        values = (float(actual), float(expected), float(tolerance))
    except (TypeError, ValueError):
        return False
    return (
        all(math.isfinite(value) for value in values)
        and values[2] >= 0
        and math.fabs(values[0] - values[1]) <= values[2]
    )


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


def _point_state(body, xyz_mm):
    point = adsk.core.Point3D.create(
        xyz_mm[0] / 10.0,
        xyz_mm[1] / 10.0,
        xyz_mm[2] / 10.0,
    )
    names = {0: "inside", 1: "on", 2: "outside", 3: "unknown"}
    return names.get(int(body.pointContainment(point)), "unknown")


def _bolt_cylinders(body):
    result = []
    for face in _items(body.faces):
        geometry = face.geometry
        if geometry is None or geometry.objectType != adsk.core.Cylinder.classType():
            continue
        if not _close(geometry.radius * 10.0, 3.25, 0.03):
            continue
        if math.fabs(geometry.axis.z) < 0.99:
            continue
        result.append(
            {
                "radius_mm": geometry.radius * 10.0,
                "center_mm": [geometry.origin.x * 10.0, geometry.origin.y * 10.0],
            }
        )
    return result


def run(_context: str):
    app = adsk.core.Application.get()
    document = app.activeDocument
    design = adsk.fusion.Design.cast(app.activeProduct)
    if document is None or design is None:
        raise RuntimeError("B05 oracle requires an active Fusion design")
    root = design.rootComponent
    marker_attribute = root.attributes.itemByName(
        "fusion_agent_benchmark", "trial_marker"
    )
    marker = marker_attribute.value if marker_attribute is not None else None
    checks = []

    def check(check_id, passed, expected, observed, evidence=None):
        checks.append(
            {
                "id": check_id,
                "status": "pass" if passed else "fail",
                "expected": expected,
                "observed": observed,
                "evidence": evidence or {},
            }
        )

    check(
        "document.marked_unsaved",
        bool(marker) and document.dataFile is None,
        {"marked": True, "saved": False},
        {"marker": marker, "saved": document.dataFile is not None},
    )
    check(
        "assembly.root_only",
        design.allComponents.count == 1 and root.allOccurrences.count == 0,
        {"components": 1, "occurrences": 0},
        {
            "components": design.allComponents.count,
            "occurrences": root.allOccurrences.count,
        },
    )

    parameters = {
        parameter.name: {
            "value": parameter.value,
            "expression": parameter.expression,
            "unit": parameter.unit,
        }
        for parameter in _items(design.userParameters)
    }
    expected_parameters = {
        "DomeRadius": 9.0,
        "ShellThickness": 0.3,
        "BaseFlangeOD": 20.0,
        "BaseFlangeID": 17.4,
        "BaseFlangeThickness": 0.6,
        "GridRibHeight": 0.4,
        "GridRibEmbed": 0.08,
        "GridRibRadius": 0.2,
        "GridRibAngularWidth": math.radians(2.4),
        "MeridianCount": 12.0,
        "BaseBoltCircleDiameter": 18.4,
        "BaseBoltCount": 12.0,
        "BaseBoltDiameter": 0.65,
    }
    parameter_failures = []
    for name, expected in expected_parameters.items():
        actual = parameters.get(name, {}).get("value")
        if not _close(actual, expected, 0.0001):
            parameter_failures.append(
                {"name": name, "expected": expected, "actual": actual}
            )
    ring_relationship_failures = []
    for index, angle_degrees in enumerate([15.0, 30.0, 45.0, 60.0, 75.0], start=1):
        angle = math.radians(angle_degrees)
        expected_radius = 9.0 * math.cos(angle)
        expected_height = 9.0 * math.sin(angle)
        actual_angle = parameters.get(f"Ring{index}Angle", {}).get("value")
        actual_radius = parameters.get(f"Ring{index}Radius", {}).get("value")
        actual_height = parameters.get(f"Ring{index}Height", {}).get("value")
        if not (
            _close(actual_angle, angle, 0.0001)
            and _close(actual_radius, expected_radius, 0.0001)
            and _close(actual_height, expected_height, 0.0001)
        ):
            ring_relationship_failures.append(
                {
                    "index": index,
                    "angle": actual_angle,
                    "radius": actual_radius,
                    "height": actual_height,
                }
            )
    check(
        "parameters.values_and_derived_rings",
        len(parameters) == 28
        and not parameter_failures
        and not ring_relationship_failures,
        {"count": 28, "ring_relationships": 5},
        {
            "count": len(parameters),
            "parameter_failures": parameter_failures,
            "ring_failures": ring_relationship_failures,
        },
    )

    bodies = _items(root.bRepBodies)
    body = bodies[0] if len(bodies) == 1 else None
    body_data = (
        None
        if body is None
        else {
            "name": body.name,
            "solid": body.isSolid,
            "lumps": body.lumps.count,
            "valid": body.isValid,
        }
    )
    check(
        "topology.single_connected_solid",
        body is not None
        and body.name == "B01_Spherical_Lattice_Radome"
        and body.isValid
        and body.isSolid
        and body.lumps.count == 1,
        {"body": "B01_Spherical_Lattice_Radome", "solid": True, "lumps": 1},
        body_data,
    )

    if body is not None:
        bbox = _bbox_mm(body)
        bbox_ok = all(
            _close(bbox["min"][index], expected, 0.3)
            for index, expected in enumerate([-100.0, -100.0, -6.0])
        ) and all(
            _close(bbox["max"][index], expected, 0.3)
            for index, expected in enumerate([100.0, 100.0, 90.0])
        )
        check(
            "geometry.global_bbox",
            bbox_ok,
            {"min": [-100.0, -100.0, -6.0], "max": [100.0, 100.0, 90.0]},
            bbox,
        )
        hollow_probes = {
            "cavity": _point_state(body, [0.0, 0.0, 45.0]),
            "crown_shell": _point_state(body, [0.0, 0.0, 89.0]),
            "above_crown": _point_state(body, [0.0, 0.0, 96.0]),
        }
        check(
            "geometry.open_hollow_shell",
            hollow_probes
            == {
                "cavity": "outside",
                "crown_shell": "inside",
                "above_crown": "outside",
            },
            {"cavity": "outside", "crown_shell": "inside", "above_crown": "outside"},
            hollow_probes,
        )
        flange_probes = {
            "opening": _point_state(body, [0.0, 0.0, -3.0]),
            "flange": _point_state(body, [91.76, 24.59, -3.0]),
            "outside": _point_state(body, [101.0, 0.0, -3.0]),
        }
        check(
            "geometry.annular_base_flange",
            flange_probes
            == {"opening": "outside", "flange": "inside", "outside": "outside"},
            {"opening": "outside", "flange": "inside", "outside": "outside"},
            flange_probes,
        )
        bolt_cylinders = _bolt_cylinders(body)
        bolt_radii = [
            math.hypot(item["center_mm"][0], item["center_mm"][1])
            for item in bolt_cylinders
        ]
        check(
            "holes.base_bolt_circle_12x",
            len(bolt_cylinders) == 12
            and all(_close(radius, 92.0, 0.1) for radius in bolt_radii),
            {"count": 12, "diameter_mm": 6.5, "bolt_circle_mm": 184.0},
            {"count": len(bolt_cylinders), "radii_mm": bolt_radii},
        )
    else:
        for check_id in (
            "geometry.global_bbox",
            "geometry.open_hollow_shell",
            "geometry.annular_base_flange",
            "holes.base_bolt_circle_12x",
        ):
            check(check_id, False, "one valid body", None)

    features = _items(root.features)
    feature_data = [
        {
            "name": feature.name,
            "valid": feature.isValid,
            "health": str(feature.healthState),
            "message": feature.errorOrWarningMessage,
        }
        for feature in features
    ]
    expected_feature_names = {
        "RV01_Outer_Dome",
        "RV02_Inner_Dome_Tool",
        "CB01_Hollow_Dome",
        "EX01_Base_Flange",
        "RV03_Meridional_Rib_Seed",
        "CP01_Meridional_Ribs",
        "RV04_Latitude_Ring_15deg",
        "RV05_Latitude_Ring_30deg",
        "RV06_Latitude_Ring_45deg",
        "RV07_Latitude_Ring_60deg",
        "RV08_Latitude_Ring_75deg",
        "EX02_Base_Bolt_Cut_Seed",
        "CP02_Base_Bolts",
    }
    check(
        "features.named_and_healthy",
        len(features) == 13
        and {item["name"] for item in feature_data} == expected_feature_names
        and all(item["valid"] and not item["message"] for item in feature_data),
        {"count": 13, "names": sorted(expected_feature_names), "errors": 0},
        feature_data,
    )
    meridian_pattern = root.features.circularPatternFeatures.itemByName(
        "CP01_Meridional_Ribs"
    )
    bolt_pattern = root.features.circularPatternFeatures.itemByName("CP02_Base_Bolts")
    pattern_data = {
        "meridians": None
        if meridian_pattern is None
        else {
            "quantity": meridian_pattern.quantity.value,
            "elements": meridian_pattern.patternElements.count,
        },
        "bolts": None
        if bolt_pattern is None
        else {
            "quantity": bolt_pattern.quantity.value,
            "elements": bolt_pattern.patternElements.count,
        },
    }
    check(
        "patterns.initial_cardinality",
        pattern_data["meridians"] is not None
        and pattern_data["bolts"] is not None
        and _close(pattern_data["meridians"]["quantity"], 12.0, 0.001)
        and pattern_data["meridians"]["elements"] == 12
        and _close(pattern_data["bolts"]["quantity"], 12.0, 0.001)
        and pattern_data["bolts"]["elements"] == 12,
        {"meridians": 12, "bolts": 12},
        pattern_data,
    )
    sketches = _items(root.sketches)
    check(
        "sketches.valid_named_set",
        len(sketches) == 10
        and len({sketch.name for sketch in sketches}) == 10
        and all(sketch.isValid for sketch in sketches),
        {"count": 10, "unique": 10, "valid": True},
        [{"name": sketch.name, "valid": sketch.isValid} for sketch in sketches],
    )

    failed = [item["id"] for item in checks if item["status"] != "pass"]
    result = {
        "ok": True,
        "schema_version": "fusion_parametric_oracle.v2",
        "oracle_id": "b05_spherical_lattice_radome_geometry",
        "case_id": "b05_spherical_lattice_radome",
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
            "document": document.name,
            "marker": marker,
            "volume_mm3": None if body is None else body.volume * 1000.0,
            "faces": None if body is None else body.faces.count,
            "parameter_expressions": {
                name: value["expression"] for name, value in parameters.items()
            },
        },
    }
    payload = json.dumps(
        result,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )
    print(payload)
    return payload
