import adsk.core
import adsk.fusion
import json
import math


def _safe(callable_object, default=None):
    try:
        return callable_object()
    except (RuntimeError, TypeError, AttributeError):
        return default


def _items(collection):
    if collection is None:
        return []
    count = int(_safe(lambda: collection.count, 0) or 0)
    result = []
    for index in range(count):
        item = _safe(lambda i=index: collection.item(i))
        if item is not None:
            result.append(item)
    return result


def _bbox(bounding_box):
    if bounding_box is None:
        return None
    minimum = _safe(lambda: bounding_box.minPoint)
    maximum = _safe(lambda: bounding_box.maxPoint)
    if minimum is None or maximum is None:
        return None
    values = [
        float(minimum.x),
        float(minimum.y),
        float(minimum.z),
        float(maximum.x),
        float(maximum.y),
        float(maximum.z),
    ]
    if not all(math.isfinite(value) for value in values):
        return None
    minimum_mm = [value * 10.0 for value in values[:3]]
    maximum_mm = [value * 10.0 for value in values[3:]]
    return {
        "min_mm": minimum_mm,
        "max_mm": maximum_mm,
        "size_mm": [
            maximum_mm[index] - minimum_mm[index]
            for index in range(3)
        ],
    }


def run(_context: str):
    app = adsk.core.Application.get()
    document = app.activeDocument
    design = adsk.fusion.Design.cast(app.activeProduct)
    if document is None or design is None:
        payload = json.dumps({
            "schema_version": "fusion_parametric_oracle.v1",
            "success": False,
            "error": "NO_ACTIVE_FUSION_DESIGN",
        }, sort_keys=True)
        print(payload)
        return payload

    root = design.rootComponent
    components = [root]
    for component in _items(design.allComponents):
        if component != root:
            components.append(component)

    parameters = []
    for parameter in _items(design.userParameters):
        parameters.append({
            "name": str(_safe(lambda p=parameter: p.name, "") or ""),
            "expression": str(_safe(lambda p=parameter: p.expression, "") or ""),
            "unit": str(_safe(lambda p=parameter: p.unit, "") or ""),
            "value_internal": float(_safe(lambda p=parameter: p.value, 0.0) or 0.0),
        })

    sketches = []
    features = []
    bodies = []
    for component in components:
        component_name = str(_safe(lambda c=component: c.name, "") or "")
        for sketch in _items(_safe(lambda c=component: c.sketches)):
            sketches.append({
                "component": component_name,
                "name": str(_safe(lambda s=sketch: s.name, "") or ""),
                "valid": bool(_safe(lambda s=sketch: s.isValid, False)),
                "fully_constrained": bool(_safe(lambda s=sketch: s.isFullyConstrained, False)),
                "visible": bool(_safe(lambda s=sketch: s.isVisible, False)),
                "curves": len(_items(_safe(lambda s=sketch: s.sketchCurves))),
                "constraints": len(_items(_safe(lambda s=sketch: s.geometricConstraints))),
                "dimensions": len(_items(_safe(lambda s=sketch: s.sketchDimensions))),
                "profiles": len(_items(_safe(lambda s=sketch: s.profiles))),
            })
        for feature in _items(_safe(lambda c=component: c.features)):
            features.append({
                "component": component_name,
                "name": str(_safe(lambda f=feature: f.name, "") or ""),
                "type": str(_safe(lambda f=feature: f.objectType, "") or ""),
                "valid": bool(_safe(lambda f=feature: f.isValid, False)),
                "health": str(_safe(lambda f=feature: f.healthState, "") or ""),
                "message": str(_safe(lambda f=feature: f.errorOrWarningMessage, "") or ""),
                "suppressed": bool(_safe(lambda f=feature: f.isSuppressed, False)),
            })
        for body in _items(_safe(lambda c=component: c.bRepBodies)):
            volume = _safe(lambda b=body: b.volume)
            area = _safe(lambda b=body: b.area)
            cylinders = []
            for face in _items(_safe(lambda b=body: b.faces)):
                geometry = _safe(lambda f=face: f.geometry)
                if geometry is None or str(_safe(lambda g=geometry: g.objectType, "") or "") != adsk.core.Cylinder.classType():
                    continue
                origin = _safe(lambda g=geometry: g.origin)
                axis = _safe(lambda g=geometry: g.axis)
                radius = _safe(lambda g=geometry: g.radius)
                cylinders.append({
                    "origin_mm": [float(origin.x) * 10.0, float(origin.y) * 10.0, float(origin.z) * 10.0] if origin is not None else None,
                    "axis": [float(axis.x), float(axis.y), float(axis.z)] if axis is not None else None,
                    "radius_mm": float(radius) * 10.0 if radius is not None else None,
                    "area_mm2": float(_safe(lambda f=face: f.area, 0.0) or 0.0) * 100.0,
                })
            bodies.append({
                "component": component_name,
                "name": str(_safe(lambda b=body: b.name, "") or ""),
                "valid": bool(_safe(lambda b=body: b.isValid, False)),
                "solid": bool(_safe(lambda b=body: b.isSolid, False)),
                "visible": bool(_safe(lambda b=body: b.isVisible, False)),
                "volume_mm3": float(volume) * 1000.0 if volume is not None else None,
                "area_mm2": float(area) * 100.0 if area is not None else None,
                "faces": len(_items(_safe(lambda b=body: b.faces))),
                "edges": len(_items(_safe(lambda b=body: b.edges))),
                "lumps": len(_items(_safe(lambda b=body: b.lumps))),
                "bbox_mm": _bbox(_safe(lambda b=body: b.boundingBox)),
                "cylindrical_faces": sorted(cylinders, key=lambda item: (item["radius_mm"] or 0.0, item["origin_mm"] or [])),
            })

    data_file = _safe(lambda: document.dataFile)
    result = {
        "schema_version": "fusion_parametric_oracle.v1",
        "success": True,
        "document": {
            "name": str(_safe(lambda: document.name, "") or ""),
            "saved": data_file is not None,
            "modified": bool(_safe(lambda: document.isModified, False)),
            "root_component": str(_safe(lambda: root.name, "") or ""),
        },
        "summary": {
            "components": len(components),
            "occurrences": len(_items(root.allOccurrences)),
            "user_parameters": len(parameters),
            "sketches": len(sketches),
            "fully_constrained_sketches": sum(1 for item in sketches if item["fully_constrained"]),
            "visible_sketches": sum(1 for item in sketches if item["visible"]),
            "features": len(features),
            "unhealthy_features": sum(1 for item in features if (not item["valid"]) or item["message"]),
            "bodies": len(bodies),
            "solid_bodies": sum(1 for item in bodies if item["solid"]),
            "visible_bodies": sum(1 for item in bodies if item["visible"]),
            "body_lumps": sum(item["lumps"] for item in bodies),
        },
        "parameters": sorted(parameters, key=lambda item: item["name"]),
        "sketches": sorted(sketches, key=lambda item: (item["component"], item["name"])),
        "features": sorted(features, key=lambda item: (item["component"], item["name"])),
        "bodies": sorted(bodies, key=lambda item: (item["component"], item["name"])),
    }
    payload = json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    print(payload)
    return payload
