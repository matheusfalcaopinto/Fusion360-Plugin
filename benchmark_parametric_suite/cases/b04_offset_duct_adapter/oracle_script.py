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


def _bbox_mm(entity):
    box = (
        entity.preciseBoundingBox
        if hasattr(entity, "preciseBoundingBox")
        else entity.boundingBox
    )
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


def _named(collection, name):
    for item in _items(collection):
        if item.name == name:
            return item
    return None


def _line_extent_mm(sketch):
    values_x = []
    values_y = []
    for line in _items(sketch.sketchCurves.sketchLines):
        for sketch_point in (line.startSketchPoint, line.endSketchPoint):
            point = sketch_point.geometry
            values_x.append(point.x * 10.0)
            values_y.append(point.y * 10.0)
    if not values_x:
        return None
    return {
        "min": [min(values_x), min(values_y)],
        "max": [max(values_x), max(values_y)],
        "size": [max(values_x) - min(values_x), max(values_y) - min(values_y)],
    }


def _circle_signatures(sketch):
    result = []
    for circle in _items(sketch.sketchCurves.sketchCircles):
        center = circle.centerSketchPoint.geometry
        result.append(
            {
                "center_mm": [center.x * 10.0, center.y * 10.0],
                "diameter_mm": circle.radius * 20.0,
            }
        )
    return result


def run(_context: str):
    app = adsk.core.Application.get()
    document = app.activeDocument
    design = adsk.fusion.Design.cast(app.activeProduct)
    if document is None or design is None:
        raise RuntimeError("B04 oracle requires an active Fusion design")

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
    check("topology.body_count", len(bodies) == 1, 1, len(bodies))
    check(
        "topology.single_visible_solid_lump",
        body is not None
        and body.isValid
        and body.isSolid
        and body.isVisible
        and body.lumps.count == 1,
        {"valid": True, "solid": True, "visible": True, "lumps": 1},
        None
        if body is None
        else {
            "valid": body.isValid,
            "solid": body.isSolid,
            "visible": body.isVisible,
            "lumps": body.lumps.count,
            "name": body.name,
        },
    )

    parameter_values = {}
    for parameter in _items(design.userParameters):
        parameter_values[parameter.name] = {
            "expression": parameter.expression,
            "unit": parameter.unit,
            "value": parameter.value,
        }
    expected_parameters = {
        "BaseWidth": 10.0,
        "BaseDepth": 7.0,
        "BaseThickness": 0.5,
        "InletWidth": 8.0,
        "InletDepth": 5.0,
        "WallThickness": 0.3,
        "TransitionHeight": 9.0,
        "OutletInnerDiameter": 5.4,
        "OutletOuterDiameter": 6.0,
        "OutletOffsetX": 1.4,
        "OutletOffsetY": 0.8,
        "TopFlangeDiameter": 8.2,
        "TopFlangeThickness": 0.5,
        "BottomBoltPitchX": 8.4,
        "BottomBoltPitchY": 5.4,
        "BottomBoltDiameter": 0.5,
        "TopBoltCircleDiameter": 7.2,
        "TopBoltDiameter": 0.45,
        "TopBoltCount": 6.0,
    }
    parameter_failures = []
    for name, expected_value in expected_parameters.items():
        actual_value = parameter_values.get(name, {}).get("value")
        if not _close(actual_value, expected_value, 0.0001):
            parameter_failures.append(
                {
                    "name": name,
                    "expected": expected_value,
                    "actual": actual_value,
                }
            )
    check(
        "parameters.exact_values",
        len(parameter_values) == 19 and not parameter_failures,
        {"count": 19, "values_internal": expected_parameters},
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
        len(sketches) == 8
        and all(item["valid"] and item["fully_constrained"] for item in sketch_data),
        {"count": 8, "valid": 8, "fully_constrained": 8},
        sketch_data,
        {"visible_count": sum(1 for item in sketch_data if item["visible"])},
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
    required_feature_names = {
        "EX01_Base_Flange",
        "LF01_Outer_Transition",
        "CB01_Base_Outer_Join",
        "EX02_Top_Flange",
        "CB02_Top_Flange_Join",
        "LF02_Inner_Passage_Tool",
        "CB03_Inner_Passage_Cut",
        "EX03_Bottom_Bolt_Seed_Tool",
        "RP01_Bottom_Bolt_2x2",
        "CB04_Bottom_Bolts_Cut",
        "EX04_Top_Bolt_Seed_Tool",
        "CP01_Top_Bolt_6x",
        "CB05_Top_Bolts_Cut",
    }
    observed_feature_names = {item["name"] for item in feature_data}
    check(
        "features.named_valid_and_healthy",
        required_feature_names.issubset(observed_feature_names)
        and all(item["valid"] and not item["message"] for item in feature_data),
        {"required_names": sorted(required_feature_names), "errors": 0},
        feature_data,
    )

    loft_data = []
    for loft in _items(root.features.loftFeatures):
        loft_data.append(
            {
                "name": loft.name,
                "valid": loft.isValid,
                "solid": loft.isSolid,
                "sections": loft.loftSections.count,
                "message": loft.errorOrWarningMessage,
            }
        )
    loft_by_name = {item["name"]: item for item in loft_data}
    check(
        "features.outer_and_inner_lofts",
        set(loft_by_name) == {"LF01_Outer_Transition", "LF02_Inner_Passage_Tool"}
        and all(
            item["valid"]
            and item["solid"]
            and item["sections"] == 2
            and not item["message"]
            for item in loft_data
        ),
        {
            "names": ["LF01_Outer_Transition", "LF02_Inner_Passage_Tool"],
            "sections_each": 2,
        },
        loft_data,
    )

    lower_outer_sketch = _named(root.sketches, "SK02_Outer_Lower_86x56")
    lower_inner_sketch = _named(root.sketches, "SK05_Inner_Inlet_80x50")
    lower_outer_extent = (
        None if lower_outer_sketch is None else _line_extent_mm(lower_outer_sketch)
    )
    lower_inner_extent = (
        None if lower_inner_sketch is None else _line_extent_mm(lower_inner_sketch)
    )
    section_ok = (
        lower_outer_extent is not None
        and lower_inner_extent is not None
        and _close(lower_outer_extent["size"][0], 86.0)
        and _close(lower_outer_extent["size"][1], 56.0)
        and _close(lower_inner_extent["size"][0], 80.0)
        and _close(lower_inner_extent["size"][1], 50.0)
    )
    check(
        "ports.rectangular_section_signature",
        section_ok,
        {"outer_mm": [86.0, 56.0], "inner_mm": [80.0, 50.0]},
        {"outer": lower_outer_extent, "inner": lower_inner_extent},
    )

    outer_upper_sketch = _named(root.sketches, "SK03_Outer_Outlet_OD60")
    inner_upper_sketch = _named(root.sketches, "SK06_Inner_Outlet_ID54")
    outer_upper_circles = (
        [] if outer_upper_sketch is None else _circle_signatures(outer_upper_sketch)
    )
    inner_upper_circles = (
        [] if inner_upper_sketch is None else _circle_signatures(inner_upper_sketch)
    )
    circular_sections_ok = (
        len(outer_upper_circles) == 1
        and len(inner_upper_circles) == 1
        and _close(outer_upper_circles[0]["diameter_mm"], 60.0)
        and _close(inner_upper_circles[0]["diameter_mm"], 54.0)
        and all(
            _close(outer_upper_circles[0]["center_mm"][index], [14.0, 8.0][index])
            for index in range(2)
        )
        and all(
            _close(inner_upper_circles[0]["center_mm"][index], [14.0, 8.0][index])
            for index in range(2)
        )
    )
    check(
        "ports.circular_section_signature",
        circular_sections_ok,
        {
            "outer_diameter_mm": 60.0,
            "inner_diameter_mm": 54.0,
            "center_mm": [14.0, 8.0],
        },
        {"outer": outer_upper_circles, "inner": inner_upper_circles},
    )

    if body is not None:
        bbox = _bbox_mm(body)
        bbox_ok = all(
            _close(bbox["min"][index], [-50.0, -35.0, 0.0][index], 0.2)
            and _close(bbox["max"][index], [55.0, 49.0, 100.0][index], 0.2)
            for index in range(3)
        )
        check(
            "geometry.global_bbox",
            bbox_ok,
            {"min": [-50.0, -35.0, 0.0], "max": [55.0, 49.0, 100.0]},
            bbox,
        )

        probe_specs = {
            "inlet_open": ([0.0, 0.0, 2.5], "outside"),
            "transition_center_open": ([7.0, 4.0, 50.0], "outside"),
            "outlet_open": ([14.0, 8.0, 97.5], "outside"),
            "base_flange_material": ([48.0, 0.0, 2.5], "inside"),
            "lower_duct_wall": ([41.5, 0.0, 5.2], "inside"),
            "top_flange_material": ([46.0, 8.0, 97.5], "inside"),
            "outside_envelope": ([56.0, 8.0, 97.5], "outside"),
        }
        probe_observed = {
            name: _point_state(body, specification[0])
            for name, specification in probe_specs.items()
        }
        check(
            "geometry.open_continuous_passage",
            all(
                probe_observed[name] == specification[1]
                for name, specification in probe_specs.items()
            ),
            {name: specification[1] for name, specification in probe_specs.items()},
            probe_observed,
        )

        rectangular_port_faces = []
        cylinders = []
        for face in _items(body.faces):
            geometry = face.geometry
            face_box = face.boundingBox
            face_bbox = {
                "min": [
                    face_box.minPoint.x * 10.0,
                    face_box.minPoint.y * 10.0,
                    face_box.minPoint.z * 10.0,
                ],
                "max": [
                    face_box.maxPoint.x * 10.0,
                    face_box.maxPoint.y * 10.0,
                    face_box.maxPoint.z * 10.0,
                ],
                "size": [
                    (face_box.maxPoint.x - face_box.minPoint.x) * 10.0,
                    (face_box.maxPoint.y - face_box.minPoint.y) * 10.0,
                    (face_box.maxPoint.z - face_box.minPoint.z) * 10.0,
                ],
            }
            if (
                geometry is not None
                and geometry.objectType == adsk.core.Plane.classType()
            ):
                x_side = (
                    face_bbox["size"][0] < 0.1
                    and _close(math.fabs(face_bbox["min"][0]), 40.0)
                    and _close(face_bbox["size"][1], 50.0)
                    and _close(face_bbox["min"][2], 0.0)
                    and _close(face_bbox["max"][2], 5.0)
                )
                y_side = (
                    face_bbox["size"][1] < 0.1
                    and _close(math.fabs(face_bbox["min"][1]), 25.0)
                    and _close(face_bbox["size"][0], 80.0)
                    and _close(face_bbox["min"][2], 0.0)
                    and _close(face_bbox["max"][2], 5.0)
                )
                if x_side or y_side:
                    rectangular_port_faces.append(face_bbox)
            if (
                geometry is not None
                and geometry.objectType == adsk.core.Cylinder.classType()
            ):
                cylinders.append(
                    {
                        "radius_mm": geometry.radius * 10.0,
                        "origin_mm": [
                            geometry.origin.x * 10.0,
                            geometry.origin.y * 10.0,
                            geometry.origin.z * 10.0,
                        ],
                        "axis": [geometry.axis.x, geometry.axis.y, geometry.axis.z],
                        "bbox": face_bbox,
                    }
                )
        check(
            "ports.rectangular_opening_faces",
            len(rectangular_port_faces) == 4,
            {"vertical_inner_faces": 4, "opening_mm": [80.0, 50.0]},
            rectangular_port_faces,
        )

        outlet_inner_faces = [
            item
            for item in cylinders
            if _close(item["radius_mm"], 27.0, 0.05)
            and _close(item["origin_mm"][0], 14.0, 0.05)
            and _close(item["origin_mm"][1], 8.0, 0.05)
            and math.fabs(item["axis"][2]) > 0.99
        ]
        top_outer_faces = [
            item
            for item in cylinders
            if _close(item["radius_mm"], 41.0, 0.05)
            and _close(item["origin_mm"][0], 14.0, 0.05)
            and _close(item["origin_mm"][1], 8.0, 0.05)
            and math.fabs(item["axis"][2]) > 0.99
        ]
        check(
            "ports.outlet_offset_and_coaxiality",
            len(outlet_inner_faces) >= 1 and len(top_outer_faces) >= 1,
            {
                "axis_xy_mm": [14.0, 8.0],
                "inner_radius_mm": 27.0,
                "flange_radius_mm": 41.0,
            },
            {"inner": outlet_inner_faces, "outer": top_outer_faces},
        )

        bottom_holes = [
            item
            for item in cylinders
            if _close(item["radius_mm"], 2.5, 0.05)
            and math.fabs(item["axis"][2]) > 0.99
        ]
        expected_bottom_centers = [
            [-42.0, -27.0],
            [42.0, -27.0],
            [-42.0, 27.0],
            [42.0, 27.0],
        ]
        bottom_centers = [
            [item["origin_mm"][0], item["origin_mm"][1]] for item in bottom_holes
        ]
        bottom_positions_ok = len(bottom_centers) == 4 and all(
            any(
                _close(actual[0], expected[0], 0.1)
                and _close(actual[1], expected[1], 0.1)
                for actual in bottom_centers
            )
            for expected in expected_bottom_centers
        )
        check(
            "bolts.bottom_grid_2x2",
            bottom_positions_ok,
            {"count": 4, "diameter_mm": 5.0, "centers_mm": expected_bottom_centers},
            {"count": len(bottom_centers), "centers_mm": bottom_centers},
        )

        top_holes = [
            item
            for item in cylinders
            if _close(item["radius_mm"], 2.25, 0.05)
            and math.fabs(item["axis"][2]) > 0.99
        ]
        top_centers = [
            [item["origin_mm"][0], item["origin_mm"][1]] for item in top_holes
        ]
        top_radii = [
            math.hypot(center[0] - 14.0, center[1] - 8.0) for center in top_centers
        ]
        top_angles = sorted(
            (math.degrees(math.atan2(center[1] - 8.0, center[0] - 14.0)) + 360.0)
            % 360.0
            for center in top_centers
        )
        top_gaps = []
        if len(top_angles) == 6:
            for index in range(6):
                top_gaps.append(
                    (top_angles[(index + 1) % 6] - top_angles[index]) % 360.0
                )
        top_positions_ok = (
            len(top_centers) == 6
            and all(_close(radius, 36.0, 0.1) for radius in top_radii)
            and all(_close(gap, 60.0, 0.1) for gap in top_gaps)
        )
        check(
            "bolts.top_bolt_circle_6x",
            top_positions_ok,
            {
                "count": 6,
                "diameter_mm": 4.5,
                "circle_diameter_mm": 72.0,
                "angular_step_deg": 60.0,
            },
            {
                "count": len(top_centers),
                "centers_mm": top_centers,
                "radii_mm": top_radii,
                "gaps_deg": top_gaps,
            },
        )

        bottom_pattern = _named(
            root.features.rectangularPatternFeatures, "RP01_Bottom_Bolt_2x2"
        )
        bottom_pattern_data = None
        if bottom_pattern is not None:
            bottom_pattern_data = {
                "quantity_one": bottom_pattern.quantityOne.value,
                "quantity_two": bottom_pattern.quantityTwo.value,
                "elements": bottom_pattern.patternElements.count,
            }
        top_pattern = _named(root.features.circularPatternFeatures, "CP01_Top_Bolt_6x")
        top_pattern_data = None
        if top_pattern is not None:
            top_pattern_data = {
                "quantity": top_pattern.quantity.value,
                "elements": top_pattern.patternElements.count,
                "total_angle_rad": top_pattern.totalAngle.value,
            }
        patterns_ok = (
            bottom_pattern_data is not None
            and _close(bottom_pattern_data["quantity_one"], 2.0, 0.001)
            and _close(bottom_pattern_data["quantity_two"], 2.0, 0.001)
            and bottom_pattern_data["elements"] == 4
            and top_pattern_data is not None
            and _close(top_pattern_data["quantity"], 6.0, 0.001)
            and top_pattern_data["elements"] == 6
            and _close(top_pattern_data["total_angle_rad"], math.pi * 2.0, 0.001)
        )
        check(
            "features.pattern_definitions",
            patterns_ok,
            {"rectangular": [2, 2, 4], "circular": [6, 6, math.pi * 2.0]},
            {"rectangular": bottom_pattern_data, "circular": top_pattern_data},
        )
    else:
        for check_id in (
            "geometry.global_bbox",
            "geometry.open_continuous_passage",
            "ports.rectangular_opening_faces",
            "ports.outlet_offset_and_coaxiality",
            "bolts.bottom_grid_2x2",
            "bolts.top_bolt_circle_6x",
            "features.pattern_definitions",
        ):
            check(check_id, False, "one valid body", None)

    failed = [item["id"] for item in checks if item["status"] != "pass"]
    construction_visibility = {
        "planes": [
            {"name": plane.name, "visible": plane.isLightBulbOn}
            for plane in _items(root.constructionPlanes)
        ],
        "axes": [
            {"name": axis.name, "visible": axis.isLightBulbOn}
            for axis in _items(root.constructionAxes)
        ],
    }
    result = {
        "ok": True,
        "schema_version": "fusion_parametric_oracle.v2",
        "oracle_id": "b04_offset_duct_adapter_geometry",
        "case_id": "b04_offset_duct_adapter",
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
            "sketch_visibility": {
                item["name"]: item["visible"] for item in sketch_data
            },
            "construction_visibility": construction_visibility,
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
