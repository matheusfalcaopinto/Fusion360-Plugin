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
    names = {0: "inside", 1: "on", 2: "outside", 3: "unknown"}
    return names.get(int(body.pointContainment(point)), "unknown")


def _cylinders(body):
    result = []
    for face in _items(body.faces):
        geometry = face.geometry
        if geometry is None or geometry.objectType != adsk.core.Cylinder.classType():
            continue
        box = face.boundingBox
        result.append(
            {
                "radius_mm": geometry.radius * 10.0,
                "origin_mm": [
                    geometry.origin.x * 10.0,
                    geometry.origin.y * 10.0,
                    geometry.origin.z * 10.0,
                ],
                "axis": [geometry.axis.x, geometry.axis.y, geometry.axis.z],
                "bbox_min_mm": [
                    box.minPoint.x * 10.0,
                    box.minPoint.y * 10.0,
                    box.minPoint.z * 10.0,
                ],
                "bbox_max_mm": [
                    box.maxPoint.x * 10.0,
                    box.maxPoint.y * 10.0,
                    box.maxPoint.z * 10.0,
                ],
            }
        )
    return result


def _global_bbox_mm(bodies):
    boxes = [_bbox_mm(body) for body in bodies]
    if not boxes:
        return None
    minimum = [min(box["min"][index] for box in boxes) for index in range(3)]
    maximum = [max(box["max"][index] for box in boxes) for index in range(3)]
    return {
        "min": minimum,
        "max": maximum,
        "size": [maximum[index] - minimum[index] for index in range(3)],
    }


def run(_context: str):
    app = adsk.core.Application.get()
    document = app.activeDocument
    design = adsk.fusion.Design.cast(app.activeProduct)
    if document is None or design is None:
        raise RuntimeError("B03 oracle requires an active Fusion design")

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

    all_components = _items(design.allComponents)
    child_components = [component for component in all_components if component != root]
    component_by_name = {component.name: component for component in child_components}
    lower_component = component_by_name.get("CMP01_Lower_Block")
    upper_component = component_by_name.get("CMP02_Upper_Cap")
    occurrences = _items(root.allOccurrences)
    occurrence_data = []
    identity_ok = True
    for occurrence in occurrences:
        values = list(occurrence.transform2.asArray())
        expected_values = [
            1.0,
            0.0,
            0.0,
            0.0,
            0.0,
            1.0,
            0.0,
            0.0,
            0.0,
            0.0,
            1.0,
            0.0,
            0.0,
            0.0,
            0.0,
            1.0,
        ]
        current_identity = len(values) == 16 and all(
            _close(values[index], expected_values[index], 0.000001)
            for index in range(16)
        )
        identity_ok = identity_ok and current_identity
        occurrence_data.append(
            {
                "name": occurrence.name,
                "full_path": occurrence.fullPathName,
                "component": occurrence.component.name,
                "identity": current_identity,
            }
        )
    check(
        "assembly.two_named_components",
        len(child_components) == 2
        and lower_component is not None
        and upper_component is not None,
        ["CMP01_Lower_Block", "CMP02_Upper_Cap"],
        sorted(component_by_name),
    )
    check(
        "assembly.two_identity_occurrences",
        len(occurrences) == 2 and identity_ok,
        {"count": 2, "identity": True},
        occurrence_data,
    )
    check(
        "topology.root_has_no_bodies",
        root.bRepBodies.count == 0,
        0,
        root.bRepBodies.count,
    )

    lower_bodies = [] if lower_component is None else _items(lower_component.bRepBodies)
    upper_bodies = [] if upper_component is None else _items(upper_component.bRepBodies)
    lower_body = lower_bodies[0] if len(lower_bodies) == 1 else None
    upper_body = upper_bodies[0] if len(upper_bodies) == 1 else None
    body_data = {
        "lower": None
        if lower_body is None
        else {
            "name": lower_body.name,
            "solid": lower_body.isSolid,
            "lumps": lower_body.lumps.count,
            "visible": lower_body.isVisible,
        },
        "upper": None
        if upper_body is None
        else {
            "name": upper_body.name,
            "solid": upper_body.isSolid,
            "lumps": upper_body.lumps.count,
            "visible": upper_body.isVisible,
        },
    }
    check(
        "topology.one_solid_lump_per_component",
        lower_body is not None
        and upper_body is not None
        and lower_body.name == "B01_Lower_Block"
        and upper_body.name == "B02_Upper_Cap"
        and lower_body.isSolid
        and upper_body.isSolid
        and lower_body.lumps.count == 1
        and upper_body.lumps.count == 1
        and lower_body.isVisible
        and upper_body.isVisible,
        {"bodies": 2, "solid": True, "lumps_each": 1, "visible": True},
        body_data,
    )

    parameter_values = {
        parameter.name: {
            "expression": parameter.expression,
            "unit": parameter.unit,
            "value": parameter.value,
        }
        for parameter in _items(design.userParameters)
    }
    expected_parameters = {
        "BlockLength": 9.0,
        "BlockWidth": 5.0,
        "BlockHeight": 4.4,
        "SplitCenterZ": 2.2,
        "SplitGap": 0.05,
        "LowerHeight": 2.175,
        "UpperHeight": 2.175,
        "BoreDiameter": 2.4,
        "BoreCenterZ": 2.2,
        "ClampPitchX": 5.6,
        "ClampPitchY": 3.0,
        "ClampHoleDiameter": 0.55,
        "CounterboreDiameter": 1.0,
        "CounterboreDepth": 0.4,
        "MountingPitchY": 3.6,
        "MountingHoleDiameter": 0.7,
        "ToolOvertravel": 0.2,
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
        {"count": 17, "values_cm": expected_parameters},
        {"count": len(parameter_values), "failures": parameter_failures},
    )

    root_sketches = _items(root.sketches)
    component_sketches = []
    if lower_component is not None:
        component_sketches.extend(_items(lower_component.sketches))
    if upper_component is not None:
        component_sketches.extend(_items(upper_component.sketches))
    sketch_data = [
        {
            "name": sketch.name,
            "valid": sketch.isValid,
            "fully_constrained": sketch.isFullyConstrained,
            "visible": sketch.isVisible,
            "profiles": sketch.profiles.count,
        }
        for sketch in root_sketches + component_sketches
    ]
    check(
        "sketches.valid_and_constrained",
        len(root_sketches) == 1
        and root_sketches[0].name == "SK00_Assembly_Reference"
        and len(component_sketches) == 8
        and all(item["valid"] and item["fully_constrained"] for item in sketch_data),
        {"root_reference": 1, "component_sketches": 8, "fully_constrained": 9},
        sketch_data,
    )
    check(
        "sketches.consumed_component_sketches_hidden",
        len(component_sketches) == 8
        and all(not sketch.isVisible for sketch in component_sketches),
        {"hidden": 8},
        {"visible": [sketch.name for sketch in component_sketches if sketch.isVisible]},
    )

    features = []
    if lower_component is not None:
        features.extend(_items(lower_component.features))
    if upper_component is not None:
        features.extend(_items(upper_component.features))
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
        "EX01_Lower_Block",
        "EX02_Lower_Bore_Tool",
        "CB01_Lower_Bore_Cut",
        "EX03_Lower_Clamp_Tools",
        "RP01_Lower_Clamp_2x2",
        "CB02_Lower_Clamp_Cut",
        "EX04_Lower_Mounting_Tools",
        "RP02_Lower_Mounting_2x1",
        "CB03_Lower_Mounting_Cut",
        "EX05_Upper_Cap",
        "EX06_Upper_Bore_Tool",
        "CB04_Upper_Bore_Cut",
        "EX07_Upper_Clamp_Tools",
        "RP03_Upper_Clamp_2x2",
        "CB05_Upper_Clamp_Cut",
        "EX08_Upper_Counterbore_Tools",
        "RP04_Upper_Counterbore_2x2",
        "CB06_Upper_Counterbore_Cut",
    }
    check(
        "features.named_and_healthy",
        len(features) == 18
        and {item["name"] for item in feature_data} == expected_feature_names
        and all(item["valid"] and not item["message"] for item in feature_data),
        {"count": 18, "names": sorted(expected_feature_names), "errors": 0},
        feature_data,
    )

    if lower_body is not None and upper_body is not None:
        lower_bbox = _bbox_mm(lower_body)
        upper_bbox = _bbox_mm(upper_body)
        global_bbox = _global_bbox_mm([lower_body, upper_body])
        lower_bbox_ok = all(
            _close(lower_bbox["min"][index], expected, 0.1)
            for index, expected in enumerate([-45.0, -25.0, 0.0])
        ) and all(
            _close(lower_bbox["max"][index], expected, 0.1)
            for index, expected in enumerate([45.0, 25.0, 21.75])
        )
        upper_bbox_ok = all(
            _close(upper_bbox["min"][index], expected, 0.1)
            for index, expected in enumerate([-45.0, -25.0, 22.25])
        ) and all(
            _close(upper_bbox["max"][index], expected, 0.1)
            for index, expected in enumerate([45.0, 25.0, 44.0])
        )
        global_bbox_ok = all(
            _close(global_bbox["min"][index], expected, 0.1)
            for index, expected in enumerate([-45.0, -25.0, 0.0])
        ) and all(
            _close(global_bbox["max"][index], expected, 0.1)
            for index, expected in enumerate([45.0, 25.0, 44.0])
        )
        check(
            "geometry.global_and_body_bboxes",
            lower_bbox_ok and upper_bbox_ok and global_bbox_ok,
            {
                "global": {"min": [-45.0, -25.0, 0.0], "max": [45.0, 25.0, 44.0]},
                "lower_z": [0.0, 21.75],
                "upper_z": [22.25, 44.0],
            },
            {"global": global_bbox, "lower": lower_bbox, "upper": upper_bbox},
        )
        observed_gap = upper_bbox["min"][2] - lower_bbox["max"][2]
        gap_probes = {
            "lower": _point_state(lower_body, [0.0, 20.0, 22.0]),
            "upper": _point_state(upper_body, [0.0, 20.0, 22.0]),
        }
        check(
            "geometry.split_gap",
            _close(observed_gap, 0.5, 0.05)
            and gap_probes["lower"] == "outside"
            and gap_probes["upper"] == "outside",
            {"gap_mm": 0.5, "z_center_mm": 22.0},
            {"gap_mm": observed_gap, "center_probes": gap_probes},
        )

        lower_cylinders = _cylinders(lower_body)
        upper_cylinders = _cylinders(upper_body)
        lower_bore = [
            item
            for item in lower_cylinders
            if _close(item["radius_mm"], 12.0, 0.05)
            and math.fabs(item["axis"][0]) > 0.99
            and _close(item["origin_mm"][1], 0.0, 0.05)
            and _close(item["origin_mm"][2], 22.0, 0.05)
        ]
        upper_bore = [
            item
            for item in upper_cylinders
            if _close(item["radius_mm"], 12.0, 0.05)
            and math.fabs(item["axis"][0]) > 0.99
            and _close(item["origin_mm"][1], 0.0, 0.05)
            and _close(item["origin_mm"][2], 22.0, 0.05)
        ]
        bore_probes = {
            "lower_center": _point_state(lower_body, [0.0, 0.0, 15.0]),
            "lower_wall": _point_state(lower_body, [0.0, 13.0, 15.0]),
            "upper_center": _point_state(upper_body, [0.0, 0.0, 30.0]),
            "upper_wall": _point_state(upper_body, [0.0, 13.0, 30.0]),
        }
        check(
            "geometry.coaxial_bore",
            len(lower_bore) == 1
            and len(upper_bore) == 1
            and bore_probes
            == {
                "lower_center": "outside",
                "lower_wall": "inside",
                "upper_center": "outside",
                "upper_wall": "inside",
            },
            {"diameter_mm": 24.0, "axis": "X", "center": [0.0, 22.0]},
            {
                "lower_cylinders": lower_bore,
                "upper_cylinders": upper_bore,
                "probes": bore_probes,
            },
        )

        lower_clamps = [
            item
            for item in lower_cylinders
            if _close(item["radius_mm"], 2.75, 0.05)
            and math.fabs(item["axis"][2]) > 0.99
        ]
        upper_clamps = [
            item
            for item in upper_cylinders
            if _close(item["radius_mm"], 2.75, 0.05)
            and math.fabs(item["axis"][2]) > 0.99
        ]
        expected_clamp_centers = {
            (-28.0, -15.0),
            (28.0, -15.0),
            (-28.0, 15.0),
            (28.0, 15.0),
        }
        lower_clamp_centers = {
            (round(item["origin_mm"][0], 1), round(item["origin_mm"][1], 1))
            for item in lower_clamps
        }
        upper_clamp_centers = {
            (round(item["origin_mm"][0], 1), round(item["origin_mm"][1], 1))
            for item in upper_clamps
        }
        check(
            "holes.clamp_alignment",
            len(lower_clamps) == 4
            and len(upper_clamps) == 4
            and lower_clamp_centers == expected_clamp_centers
            and upper_clamp_centers == expected_clamp_centers,
            {"per_component": 4, "centers_mm": sorted(expected_clamp_centers)},
            {
                "lower": sorted(lower_clamp_centers),
                "upper": sorted(upper_clamp_centers),
            },
        )

        lower_counterbores = [
            item
            for item in lower_cylinders
            if _close(item["radius_mm"], 5.0, 0.05)
            and math.fabs(item["axis"][2]) > 0.99
        ]
        upper_counterbores = [
            item
            for item in upper_cylinders
            if _close(item["radius_mm"], 5.0, 0.05)
            and math.fabs(item["axis"][2]) > 0.99
        ]
        counterbore_centers = {
            (round(item["origin_mm"][0], 1), round(item["origin_mm"][1], 1))
            for item in upper_counterbores
        }
        counterbore_depth_ok = all(
            _close(item["bbox_min_mm"][2], 40.0, 0.1)
            and _close(item["bbox_max_mm"][2], 44.0, 0.1)
            for item in upper_counterbores
        )
        check(
            "holes.cap_only_counterbores",
            not lower_counterbores
            and len(upper_counterbores) == 4
            and counterbore_centers == expected_clamp_centers
            and counterbore_depth_ok,
            {"lower": 0, "upper": 4, "diameter_mm": 10.0, "depth_mm": 4.0},
            {
                "lower": len(lower_counterbores),
                "upper": len(upper_counterbores),
                "centers": sorted(counterbore_centers),
                "cylinders": upper_counterbores,
            },
        )

        lower_mounting = [
            item
            for item in lower_cylinders
            if _close(item["radius_mm"], 3.5, 0.05)
            and math.fabs(item["axis"][2]) > 0.99
        ]
        upper_mounting = [
            item
            for item in upper_cylinders
            if _close(item["radius_mm"], 3.5, 0.05)
            and math.fabs(item["axis"][2]) > 0.99
        ]
        expected_mounting_centers = {(0.0, -18.0), (0.0, 18.0)}
        mounting_centers = {
            (round(item["origin_mm"][0], 1), round(item["origin_mm"][1], 1))
            for item in lower_mounting
        }
        mounting_probes = {
            "lower_negative": _point_state(lower_body, [0.0, -18.0, 8.0]),
            "lower_positive": _point_state(lower_body, [0.0, 18.0, 8.0]),
            "upper_negative": _point_state(upper_body, [0.0, -18.0, 30.0]),
            "upper_positive": _point_state(upper_body, [0.0, 18.0, 30.0]),
        }
        check(
            "holes.lower_only_mounting",
            len(lower_mounting) == 2
            and not upper_mounting
            and mounting_centers == expected_mounting_centers
            and mounting_probes
            == {
                "lower_negative": "outside",
                "lower_positive": "outside",
                "upper_negative": "inside",
                "upper_positive": "inside",
            },
            {
                "lower": 2,
                "upper": 0,
                "diameter_mm": 7.0,
                "centers_mm": sorted(expected_mounting_centers),
            },
            {"centers": sorted(mounting_centers), "probes": mounting_probes},
        )
    else:
        for check_id in (
            "geometry.global_and_body_bboxes",
            "geometry.split_gap",
            "geometry.coaxial_bore",
            "holes.clamp_alignment",
            "holes.cap_only_counterbores",
            "holes.lower_only_mounting",
        ):
            check(check_id, False, "two valid bodies", None)

    failed = [item["id"] for item in checks if item["status"] != "pass"]
    result = {
        "ok": True,
        "schema_version": "fusion_parametric_oracle.v2",
        "oracle_id": "b03_split_pillow_block_geometry",
        "case_id": "b03_split_pillow_block",
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
            "parameter_expressions": {
                name: value["expression"] for name, value in parameter_values.items()
            },
            "lower_volume_mm3": None
            if lower_body is None
            else lower_body.volume * 1000.0,
            "upper_volume_mm3": None
            if upper_body is None
            else upper_body.volume * 1000.0,
            "lower_faces": None if lower_body is None else lower_body.faces.count,
            "upper_faces": None if upper_body is None else upper_body.faces.count,
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
