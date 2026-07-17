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
        result.append([geometry.origin.x * 10.0, geometry.origin.y * 10.0])
    return result


def run(_context: str):
    app = adsk.core.Application.get()
    document = app.activeDocument
    design = adsk.fusion.Design.cast(app.activeProduct)
    if document is None or design is None:
        raise RuntimeError("B05 ECO oracle requires an active Fusion design")
    root = design.rootComponent
    marker_attribute = root.attributes.itemByName(
        "fusion_agent_benchmark", "trial_marker"
    )
    marker = marker_attribute.value if marker_attribute is not None else None
    checks = []

    def check(check_id, passed, expected, observed):
        checks.append(
            {
                "id": check_id,
                "status": "pass" if passed else "fail",
                "expected": expected,
                "observed": observed,
            }
        )

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
    changed = {
        "DomeRadius": (10.5, "105 mm"),
        "BaseFlangeOD": (23.0, "230 mm"),
        "BaseBoltCircleDiameter": (21.0, "210 mm"),
        "BaseBoltCount": (16.0, "16"),
        "MeridianCount": (16.0, "16"),
    }
    failures = []
    for name, expected in changed.items():
        observed = parameters.get(name, {})
        if (
            not _close(observed.get("value"), expected[0], 0.0001)
            or observed.get("expression") != expected[1]
        ):
            failures.append({"name": name, "expected": expected, "observed": observed})
    derived_failures = []
    if not _close(parameters.get("BaseFlangeID", {}).get("value"), 20.4, 0.0001):
        derived_failures.append(
            {"name": "BaseFlangeID", "observed": parameters.get("BaseFlangeID")}
        )
    for index, angle_degrees in enumerate([15.0, 30.0, 45.0, 60.0, 75.0], start=1):
        angle = math.radians(angle_degrees)
        expected_radius = 10.5 * math.cos(angle)
        expected_height = 10.5 * math.sin(angle)
        if not (
            _close(
                parameters.get(f"Ring{index}Radius", {}).get("value"),
                expected_radius,
                0.0001,
            )
            and _close(
                parameters.get(f"Ring{index}Height", {}).get("value"),
                expected_height,
                0.0001,
            )
        ):
            derived_failures.append(
                {
                    "ring": index,
                    "radius": parameters.get(f"Ring{index}Radius"),
                    "height": parameters.get(f"Ring{index}Height"),
                }
            )
    check(
        "parameters.eco_and_dependencies",
        len(parameters) == 28 and not failures and not derived_failures,
        {"changed": changed, "derived_ring_radius_mm": 105.0, "parameter_count": 28},
        {
            "failures": failures,
            "derived_failures": derived_failures,
            "count": len(parameters),
        },
    )

    bodies = _items(root.bRepBodies)
    body = bodies[0] if len(bodies) == 1 else None
    check(
        "topology.single_connected_solid_after_eco",
        body is not None and body.isValid and body.isSolid and body.lumps.count == 1,
        {"bodies": 1, "solid": True, "lumps": 1},
        None
        if body is None
        else {"bodies": len(bodies), "solid": body.isSolid, "lumps": body.lumps.count},
    )
    if body is not None:
        bbox = _bbox_mm(body)
        bbox_ok = all(
            _close(bbox["min"][index], expected, 0.4)
            for index, expected in enumerate([-115.0, -115.0, -6.0])
        ) and all(
            _close(bbox["max"][index], expected, 0.4)
            for index, expected in enumerate([115.0, 115.0, 105.0])
        )
        check(
            "geometry.eco_bbox",
            bbox_ok,
            {"min": [-115.0, -115.0, -6.0], "max": [115.0, 115.0, 105.0]},
            bbox,
        )
        probes = {
            "cavity": _point_state(body, [0.0, 0.0, 50.0]),
            "crown_shell": _point_state(body, [0.0, 0.0, 104.0]),
            "above": _point_state(body, [0.0, 0.0, 110.0]),
            "base_opening": _point_state(body, [0.0, 0.0, -3.0]),
            "flange": _point_state(body, [107.89, 21.46, -3.0]),
        }
        check(
            "geometry.eco_hollow_and_flange",
            probes
            == {
                "cavity": "outside",
                "crown_shell": "inside",
                "above": "outside",
                "base_opening": "outside",
                "flange": "inside",
            },
            {
                "cavity": "outside",
                "crown_shell": "inside",
                "above": "outside",
                "base_opening": "outside",
                "flange": "inside",
            },
            probes,
        )
        bolt_centers = _bolt_cylinders(body)
        bolt_radii = [math.hypot(center[0], center[1]) for center in bolt_centers]
        check(
            "holes.eco_bolt_circle_16x",
            len(bolt_centers) == 16
            and all(_close(radius, 105.0, 0.1) for radius in bolt_radii),
            {"count": 16, "circle_radius_mm": 105.0},
            {"count": len(bolt_centers), "radii_mm": bolt_radii},
        )
    else:
        for check_id in (
            "geometry.eco_bbox",
            "geometry.eco_hollow_and_flange",
            "holes.eco_bolt_circle_16x",
        ):
            check(check_id, False, "one valid body", None)

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
        "patterns.eco_cardinality",
        pattern_data["meridians"] is not None
        and pattern_data["bolts"] is not None
        and _close(pattern_data["meridians"]["quantity"], 16.0, 0.001)
        and pattern_data["meridians"]["elements"] == 16
        and _close(pattern_data["bolts"]["quantity"], 16.0, 0.001)
        and pattern_data["bolts"]["elements"] == 16,
        {"meridians": 16, "bolts": 16},
        pattern_data,
    )
    features = _items(root.features)
    feature_errors = [
        {
            "name": feature.name,
            "valid": feature.isValid,
            "message": feature.errorOrWarningMessage,
        }
        for feature in features
        if not feature.isValid or feature.errorOrWarningMessage
    ]
    check(
        "features.healthy_after_eco",
        len(features) == 13 and not feature_errors,
        {"count": 13, "errors": 0},
        {"count": len(features), "errors": feature_errors},
    )

    failed = [item["id"] for item in checks if item["status"] != "pass"]
    result = {
        "ok": True,
        "schema_version": "fusion_parametric_oracle.v2",
        "oracle_id": "b05_spherical_lattice_radome_eco",
        "case_id": "b05_spherical_lattice_radome",
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
            "volume_mm3": None if body is None else body.volume * 1000.0,
            "faces": None if body is None else body.faces.count,
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
