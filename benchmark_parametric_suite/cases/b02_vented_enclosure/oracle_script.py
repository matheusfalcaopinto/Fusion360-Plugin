import adsk.core
import adsk.fusion
import json
import math


def _items(collection):
    result = []
    if collection is None:
        return result
    for index in range(collection.count):
        item = collection.item(index)
        if item is not None:
            result.append(item)
    return result


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
    value = int(body.pointContainment(point))
    names = {0: "inside", 1: "on", 2: "outside", 3: "unknown"}
    return names.get(value, "unknown")


def run(_context: str):
    app = adsk.core.Application.get()
    document = app.activeDocument
    design = adsk.fusion.Design.cast(app.activeProduct)
    if document is None or design is None:
        raise RuntimeError("B02 oracle requires an active Fusion design")

    root = design.rootComponent
    marker_attribute = root.attributes.itemByName(
        "fusion_agent_benchmark", "trial_marker"
    )
    marker = marker_attribute.value if marker_attribute is not None else None
    bodies = _items(root.bRepBodies)
    body = bodies[0] if len(bodies) == 1 else None
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
        True,
        {
            "marker": marker,
            "saved": document.dataFile is not None,
        },
    )
    check("topology.body_count", len(bodies) == 1, 1, len(bodies))
    check(
        "topology.single_solid_lump",
        body is not None and body.isSolid and body.lumps.count == 1,
        {"solid": True, "lumps": 1},
        None if body is None else {"solid": body.isSolid, "lumps": body.lumps.count},
    )

    parameter_values = {}
    for parameter in _items(design.userParameters):
        parameter_values[parameter.name] = {
            "expression": parameter.expression,
            "unit": parameter.unit,
            "value": parameter.value,
        }
    expected_parameters = {
        "CaseLength": 12.0,
        "CaseWidth": 8.0,
        "CaseHeight": 3.5,
        "WallThickness": 0.24,
        "FloorThickness": 0.3,
        "BossDiameter": 0.8,
        "BossHoleDiameter": 0.32,
        "BossHeight": 1.8,
        "BossPitchX": 10.0,
        "BossPitchY": 6.0,
        "VentLength": 1.2,
        "VentWidth": 0.3,
        "VentCols": 5.0,
        "VentRows": 3.0,
        "VentPitchX": 1.8,
        "VentPitchZ": 0.9,
        "VentCenterZ": 2.0,
    }
    parameter_failures = []
    for name, expected in expected_parameters.items():
        actual = parameter_values.get(name, {}).get("value")
        if not _close(actual, expected, 0.0001):
            parameter_failures.append(
                {"name": name, "expected": expected, "actual": actual}
            )
    check(
        "parameters.exact_values",
        len(parameter_values) == 17 and not parameter_failures,
        {"count": 17, "values": expected_parameters},
        {"count": len(parameter_values), "failures": parameter_failures},
    )

    sketches = _items(root.sketches)
    sketch_data = [
        {
            "name": sketch.name,
            "valid": sketch.isValid,
            "fully_constrained": sketch.isFullyConstrained,
            "visible": sketch.isVisible,
            "profiles": sketch.profiles.count,
        }
        for sketch in sketches
    ]
    check(
        "sketches.valid_and_constrained",
        len(sketches) == 5
        and all(item["valid"] and item["fully_constrained"] for item in sketch_data),
        {"count": 5, "fully_constrained": 5},
        sketch_data,
    )
    check(
        "sketches.hidden",
        all(not item["visible"] for item in sketch_data),
        0,
        sum(1 for item in sketch_data if item["visible"]),
    )

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
    check(
        "features.healthy",
        len(features) >= 9
        and all(item["valid"] and not item["message"] for item in feature_data),
        {"minimum": 9, "errors": 0},
        feature_data,
    )

    if body is not None:
        bbox = _bbox_mm(body)
        bbox_ok = all(
            _close(bbox["size"][index], expected, 0.1)
            for index, expected in enumerate([120.0, 80.0, 35.0])
        ) and all(
            _close(bbox["min"][index], expected, 0.1)
            for index, expected in enumerate([-60.0, -40.0, 0.0])
        )
        check(
            "geometry.global_bbox",
            bbox_ok,
            {"min": [-60.0, -40.0, 0.0], "size": [120.0, 80.0, 35.0]},
            bbox,
        )

        probe_specs = {
            "floor": ([0.0, 0.0, 1.5], "inside"),
            "open_cavity": ([0.0, 0.0, 20.0], "outside"),
            "open_top": ([0.0, 0.0, 34.0], "outside"),
            "side_wall": ([59.0, 0.0, 20.0], "inside"),
            "outside": ([61.0, 0.0, 20.0], "outside"),
        }
        probe_observed = {
            name: _point_state(body, spec[0]) for name, spec in probe_specs.items()
        }
        check(
            "geometry.open_top_floor_and_walls",
            all(probe_observed[name] == spec[1] for name, spec in probe_specs.items()),
            {name: spec[1] for name, spec in probe_specs.items()},
            probe_observed,
        )

        boss_positions = [(-50.0, -30.0), (50.0, -30.0), (-50.0, 30.0), (50.0, 30.0)]
        boss_probes = []
        for center_x, center_y in boss_positions:
            boss_probes.append(
                {
                    "center": [center_x, center_y],
                    "hole": _point_state(body, [center_x, center_y, 10.0]),
                    "annulus": _point_state(body, [center_x + 3.0, center_y, 10.0]),
                }
            )
        check(
            "bosses.connected_and_hollow",
            all(
                item["hole"] == "outside" and item["annulus"] == "inside"
                for item in boss_probes
            ),
            {"count": 4, "hole": "outside", "annulus": "inside"},
            boss_probes,
        )

        cylinders = []
        for face in _items(body.faces):
            geometry = face.geometry
            if (
                geometry is None
                or geometry.objectType != adsk.core.Cylinder.classType()
            ):
                continue
            axis = geometry.axis
            origin = geometry.origin
            face_box = face.boundingBox
            cylinders.append(
                {
                    "radius_mm": geometry.radius * 10.0,
                    "origin_mm": [origin.x * 10.0, origin.y * 10.0, origin.z * 10.0],
                    "axis": [axis.x, axis.y, axis.z],
                    "bbox_min_mm": [
                        face_box.minPoint.x * 10.0,
                        face_box.minPoint.y * 10.0,
                        face_box.minPoint.z * 10.0,
                    ],
                    "bbox_max_mm": [
                        face_box.maxPoint.x * 10.0,
                        face_box.maxPoint.y * 10.0,
                        face_box.maxPoint.z * 10.0,
                    ],
                }
            )
        boss_outer = [
            item
            for item in cylinders
            if _close(item["radius_mm"], 4.0, 0.05)
            and math.fabs(item["axis"][2]) > 0.99
        ]
        boss_holes = [
            item
            for item in cylinders
            if _close(item["radius_mm"], 1.6, 0.05)
            and math.fabs(item["axis"][2]) > 0.99
        ]
        vent_ends = [
            item
            for item in cylinders
            if _close(item["radius_mm"], 1.5, 0.05)
            and math.fabs(item["axis"][1]) > 0.99
        ]
        check(
            "bosses.cylindrical_signature",
            len(boss_outer) == 4 and len(boss_holes) == 4,
            {"outer_r4": 4, "pilot_r1_6": 4},
            {"outer_r4": len(boss_outer), "pilot_r1_6": len(boss_holes)},
        )

        vent_x = sorted(set(round(item["origin_mm"][0], 2) for item in vent_ends))
        vent_z = sorted(set(round(item["origin_mm"][2], 2) for item in vent_ends))
        front_count = sum(1 for item in vent_ends if item["bbox_max_mm"][1] < 0)
        rear_count = sum(1 for item in vent_ends if item["bbox_min_mm"][1] > 0)
        consecutive_x = [
            round(vent_x[index + 1] - vent_x[index], 2)
            for index in range(len(vent_x) - 1)
        ]
        check(
            "vents.count_lattice_and_length",
            len(vent_ends) == 60
            and front_count == 30
            and rear_count == 30
            and len(vent_x) == 10
            and len(vent_z) == 3
            and all(_close(value, 9.0, 0.1) for value in consecutive_x),
            {
                "slot_count": 30,
                "cylindrical_ends": 60,
                "per_wall_ends": 30,
                "unique_x": 10,
                "unique_z": 3,
                "arc_center_step_mm": 9.0,
                "overall_length_mm": 12.0,
            },
            {
                "cylindrical_ends": len(vent_ends),
                "front_ends": front_count,
                "rear_ends": rear_count,
                "x_mm": vent_x,
                "z_mm": vent_z,
                "consecutive_x_mm": consecutive_x,
            },
        )

        vent_pattern = root.features.rectangularPatternFeatures.itemByName(
            "RP03_Front_Vent_5x3"
        )
        pattern_data = None
        if vent_pattern is not None:
            pattern_data = {
                "quantity_one": vent_pattern.quantityOne.value,
                "quantity_two": vent_pattern.quantityTwo.value,
                "elements": vent_pattern.patternElements.count,
                "distance_one_expression": vent_pattern.distanceOne.expression,
                "distance_two_expression": vent_pattern.distanceTwo.expression,
            }
        check(
            "vents.pattern_definition",
            pattern_data is not None
            and _close(pattern_data["quantity_one"], 5.0, 0.001)
            and _close(pattern_data["quantity_two"], 3.0, 0.001)
            and pattern_data["elements"] == 15,
            {"quantity_one": 5, "quantity_two": 3, "elements": 15},
            pattern_data,
        )
    else:
        check("geometry.global_bbox", False, "one body", None)
        check("geometry.open_top_floor_and_walls", False, "one body", None)
        check("bosses.connected_and_hollow", False, "one body", None)
        check("bosses.cylindrical_signature", False, "one body", None)
        check("vents.count_lattice_and_length", False, "one body", None)
        check("vents.pattern_definition", False, "one body", None)

    failed = [item["id"] for item in checks if item["status"] != "pass"]
    result = {
        "ok": True,
        "schema_version": "fusion_parametric_oracle.v2",
        "oracle_id": "b02_vented_enclosure_geometry",
        "case_id": "b02_vented_enclosure",
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
            "body_volume_mm3": None if body is None else body.volume * 1000.0,
            "faces": None if body is None else body.faces.count,
            "edges": None if body is None else body.edges.count,
            "parameter_expressions": {
                name: value["expression"] for name, value in parameter_values.items()
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
