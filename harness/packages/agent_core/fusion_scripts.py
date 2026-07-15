"""Fusion script builders for safe read-first workflows."""

from __future__ import annotations

import json
from typing import Any


def compact_snapshot_script(payload: dict[str, Any]) -> str:
    """Return a Fusion script that emits a capped, component-scoped snapshot."""

    return _script(
        payload,
        r'''    design = _design()
    max_occurrences = int(PAYLOAD.get("max_occurrences") or 500)
    max_bodies = int(PAYLOAD.get("max_bodies") or 500)
    include_transforms = bool(PAYLOAD.get("include_transforms"))
    max_entities_visited = min(5000, max(1, int(PAYLOAD.get("max_entities_visited") or 1000)))
    deadline_ms = min(5000, max(50, int(PAYLOAD.get("deadline_ms") or 1500)))
    max_response_bytes = min(1048576, max(4096, int(PAYLOAD.get("max_response_bytes") or 1048576)))
    started = time.perf_counter()
    visited_entities = 0
    estimated_response_bytes = 1024
    stop_reason = None

    def elapsed_ms():
        return int((time.perf_counter() - started) * 1000.0)

    def stop(reason):
        nonlocal stop_reason
        if stop_reason is None:
            stop_reason = reason

    def visit():
        nonlocal visited_entities
        if stop_reason is not None:
            return False
        if elapsed_ms() >= deadline_ms:
            stop("deadline_ms")
            return False
        if visited_entities >= max_entities_visited:
            stop("max_entities_visited")
            return False
        visited_entities += 1
        return True

    def reserve(value):
        nonlocal estimated_response_bytes
        size = len(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")) + 16
        if estimated_response_bytes + size > max_response_bytes:
            stop("max_response_bytes")
            return False
        estimated_response_bytes += size
        return True

    snapshot = {
        "schema_version": "compact_snapshot.v2",
        "schema_compatibility": ["compact_snapshot.v1"],
        "source": "real",
        "document": _document_payload(),
        "payload_capped": False,
        "counts": {
            "components_total": 0,
            "occurrences_total": 0,
            "bodies_total": 0,
            "visible_occurrences": 0,
            "visible_bodies": 0,
            "visible_components": 0,
        },
        "occurrences": [],
        "bodies": [],
        "visible_occurrence_paths": [],
        "visible_body_keys": [],
        "visible_component_keys": [],
        "visible_body_bbox_mm": None,
        "duplicate_body_names": {},
        "duplicate_name_warnings": [],
    }
    visible_components = set()

    def visit_occurrences(occurrences, prefix):
        if not occurrences or stop_reason is not None:
            return
        for index in range(occurrences.count):
            if not visit():
                return
            occurrence = occurrences.item(index)
            if not occurrence:
                continue
            component = occurrence.component
            name = occurrence.name or (component.name if component else "occurrence_%d" % (index + 1))
            path = ("%s/%s" % (prefix, name)) if prefix else name
            visible = _visible(occurrence)
            snapshot["counts"]["occurrences_total"] += 1
            item = {
                "path": path,
                "name": name,
                "component": component.name if component else "",
                "component_key": _component_key(component) if component else "",
                "entity_token": _entity_token(occurrence),
                "visible": visible,
            }
            if include_transforms:
                item["transform"] = _transform_payload(occurrence)
            try:
                item["bbox_mm"] = _bbox_payload_mm(occurrence.boundingBox)
            except Exception as exc:
                item["bbox_error"] = str(exc)
            if not reserve(item):
                return
            if visible:
                snapshot["counts"]["visible_occurrences"] += 1
                snapshot["visible_occurrence_paths"].append(path)
                if component:
                    visible_components.add(_component_key(component))
            if len(snapshot["occurrences"]) < max_occurrences:
                snapshot["occurrences"].append(item)
            else:
                snapshot["payload_capped"] = True
            try:
                visit_occurrences(occurrence.childOccurrences, path)
            except Exception:
                pass

    visit_occurrences(design.rootComponent.occurrences, "")
    body_name_counts = {}
    body_bbox_union = None
    for component_index in range(design.allComponents.count):
        if not visit():
            break
        component = design.allComponents.item(component_index)
        if not component:
            continue
        snapshot["counts"]["components_total"] += 1
        component_key = _component_key(component)
        try:
            bodies = component.bRepBodies
        except Exception:
            continue
        for body_index in range(bodies.count):
            if not visit():
                break
            body = bodies.item(body_index)
            if not body:
                continue
            snapshot["counts"]["bodies_total"] += 1
            name = body.name or "body_%d" % (body_index + 1)
            body_name_counts[name] = body_name_counts.get(name, 0) + 1
            key = "%s/%s#%d" % (component.name or component_key, name, body_index + 1)
            visible = _visible(body) and _component_visible(component)
            bbox = None
            try:
                bbox = _bbox_payload_mm(body.boundingBox)
            except Exception as exc:
                bbox = {"error": str(exc)}
            body_item = {
                "key": key,
                "name": name,
                "component": component.name or "",
                "component_key": component_key,
                "entity_token": _entity_token(body),
                "visible": visible,
                "bbox_mm": bbox,
            }
            if not reserve(body_item):
                break
            if visible:
                snapshot["counts"]["visible_bodies"] += 1
                snapshot["visible_body_keys"].append(key)
                visible_components.add(component_key)
                if isinstance(bbox, dict) and "min_mm" in bbox and "max_mm" in bbox:
                    body_bbox_union = _merge_bbox(body_bbox_union, bbox)
            if len(snapshot["bodies"]) < max_bodies:
                snapshot["bodies"].append(body_item)
            else:
                snapshot["payload_capped"] = True
        if stop_reason is not None:
            break

    duplicate_body_names = {}
    for name, count in body_name_counts.items():
        if count > 1:
            duplicate_body_names[name] = count
    snapshot["duplicate_body_names"] = duplicate_body_names
    snapshot["duplicate_name_warnings"] = [
        "Body name '%s' appears %d times; target by component/body key." % (name, count)
        for name, count in sorted(duplicate_body_names.items())
    ]
    snapshot["visible_component_keys"] = sorted(visible_components)
    snapshot["counts"]["visible_components"] = len(visible_components)
    snapshot["visible_body_bbox_mm"] = body_bbox_union
    snapshot["complete"] = stop_reason is None
    snapshot["truncated"] = stop_reason is not None or snapshot["payload_capped"]
    snapshot["visited_entities"] = visited_entities
    snapshot["elapsed_ms"] = elapsed_ms()
    snapshot["response_bytes"] = 0
    snapshot["counts_exact"] = stop_reason is None
    snapshot["stop_reason"] = stop_reason
    snapshot["snapshot_hash"] = _hash_payload(
        {
            "visible_occurrence_paths": snapshot["visible_occurrence_paths"],
            "visible_body_keys": snapshot["visible_body_keys"],
            "visible_component_keys": snapshot["visible_component_keys"],
        }
    )
    wrapper = {"success": True, "snapshot": snapshot}
    while True:
        for _iteration in range(8):
            encoded = json.dumps(wrapper, sort_keys=True, separators=(",", ":"))
            response_bytes = len(encoded.encode("utf-8"))
            if snapshot["response_bytes"] == response_bytes:
                break
            snapshot["response_bytes"] = response_bytes
        encoded = json.dumps(wrapper, sort_keys=True, separators=(",", ":"))
        response_bytes = len(encoded.encode("utf-8"))
        if response_bytes <= max_response_bytes:
            print(encoded)
            break
        trimmed = True
        if snapshot["bodies"]:
            snapshot["bodies"].pop()
        elif snapshot["occurrences"]:
            snapshot["occurrences"].pop()
        elif snapshot["visible_body_keys"]:
            snapshot["visible_body_keys"].pop()
        elif snapshot["visible_occurrence_paths"]:
            snapshot["visible_occurrence_paths"].pop()
        elif snapshot["visible_component_keys"]:
            snapshot["visible_component_keys"].pop()
        elif snapshot["duplicate_name_warnings"]:
            snapshot["duplicate_name_warnings"].pop()
        else:
            trimmed = False
        stop("max_response_bytes")
        snapshot["complete"] = False
        snapshot["truncated"] = True
        snapshot["counts_exact"] = False
        snapshot["stop_reason"] = "max_response_bytes"
        if not trimmed:
            snapshot["document"] = {
                "name": str((snapshot.get("document") or {}).get("name") or "")[:256],
                "truncated": True,
            }
            for _iteration in range(8):
                encoded = json.dumps(wrapper, sort_keys=True, separators=(",", ":"))
                measured = len(encoded.encode("utf-8"))
                if snapshot["response_bytes"] == measured:
                    break
                snapshot["response_bytes"] = measured
            print(json.dumps(wrapper, sort_keys=True, separators=(",", ":")))
            break
''',
    )


def hub_inventory_script(payload: dict[str, Any]) -> str:
    """Return a Fusion script for metadata-first hub inventory."""

    return _script(
        payload,
        r'''    app = adsk.core.Application.get()
    query = str(PAYLOAD.get("query") or "").lower()
    max_results = int(PAYLOAD.get("max_results") or 50)
    enrich = bool(PAYLOAD.get("enrich"))
    results = []
    strategy = {
        "primary": "metadata_search",
        "enrichment": "findFileById" if enrich else "disabled",
        "direct_datafolder_traversal": False,
    }
    data = getattr(app, "data", None)
    projects_seen = 0
    if data:
        try:
            projects = data.dataProjects
            for index in range(projects.count):
                project = projects.item(index)
                if not project:
                    continue
                projects_seen += 1
                name = getattr(project, "name", "") or ""
                project_id = getattr(project, "id", "") or ""
                haystack = ("%s %s" % (name, project_id)).lower()
                if query and query not in haystack:
                    continue
                results.append({"kind": "project", "name": name, "id": project_id})
                if len(results) >= max_results:
                    break
        except Exception as exc:
            strategy["project_metadata_error"] = str(exc)
    try:
        for index in range(app.documents.count):
            document = app.documents.item(index)
            if not document:
                continue
            name = getattr(document, "name", "") or ""
            haystack = name.lower()
            if query and query not in haystack:
                continue
            item = {"kind": "open_document", "name": name}
            data_file = getattr(document, "dataFile", None)
            if data_file:
                item["id"] = getattr(data_file, "id", "") or ""
                item["version"] = getattr(data_file, "versionNumber", None)
                if enrich and item.get("id") and data and hasattr(data, "findFileById"):
                    try:
                        found = data.findFileById(item["id"])
                        if found:
                            item["enriched_name"] = getattr(found, "name", "") or ""
                    except Exception as exc:
                        item["enrich_error"] = str(exc)
            results.append(item)
            if len(results) >= max_results:
                break
    except Exception as exc:
        strategy["open_documents_error"] = str(exc)
    print(json.dumps({"success": True, "strategy": strategy, "projects_seen": projects_seen, "results": results[:max_results]}, sort_keys=True))
''',
    )


def safe_visibility_apply_script(payload: dict[str, Any]) -> str:
    """Return a Fusion script that applies reversible visibility changes."""

    return _script(
        payload,
        r'''    design = _design()
    targets = list(PAYLOAD.get("targets") or [])
    changed = []
    resolved = []
    preflight_errors = []

    def target_visible(target):
        if "visible" in target:
            return bool(target.get("visible"))
        if "value" in target:
            return bool(target.get("value"))
        return False

    def target_matches_body(target, component, body, body_index):
        name = body.name or ""
        component_name = component.name or ""
        key = "%s/%s#%d" % (component_name, name, body_index + 1)
        target_token = target.get("entity_token") or target.get("token")
        if target_token:
            return target_token == _entity_token(body)
        if target.get("body_key") == key or target.get("key") == key:
            return True
        target_name = target.get("name") or target.get("body") or target.get("target")
        if target_name and target_name == name:
            target_component = target.get("component") or target.get("component_path")
            return not target_component or target_component == component_name
        return False

    for target_index, target in enumerate(targets):
        kind = str(target.get("kind") or target.get("type") or "body").lower()
        desired_visible = target_visible(target)
        if kind not in ("body", "brepbody"):
            preflight_errors.append({"target_index": target_index, "reason": "unsupported_visibility_kind"})
            continue
        matches = []
        for component_index in range(design.allComponents.count):
            component = design.allComponents.item(component_index)
            if not component:
                continue
            bodies = component.bRepBodies
            for body_index in range(bodies.count):
                body = bodies.item(body_index)
                if body and target_matches_body(target, component, body, body_index):
                    matches.append((component, body))
        if len(matches) != 1:
            preflight_errors.append({"target_index": target_index, "reason": "target_must_match_exactly_one_entity", "match_count": len(matches)})
        else:
            resolved.append((matches[0][0], matches[0][1], desired_visible))
    if preflight_errors:
        print(json.dumps({"success": False, "error_code": "TARGET_PREFLIGHT_FAILED", "changed": [], "changed_count": 0, "preflight_errors": preflight_errors}, sort_keys=True))
        return
    for component, body, desired_visible in resolved:
        body.isLightBulbOn = desired_visible
        changed.append(
            {
                "kind": "body",
                "component": component.name or "",
                "name": body.name or "",
                "visible": desired_visible,
            }
        )
    print(json.dumps({"success": True, "changed": changed, "changed_count": len(changed)}, sort_keys=True))
''',
    )


def safe_delete_apply_script(payload: dict[str, Any]) -> str:
    """Return a Fusion script that deletes explicitly targeted bodies/occurrences."""

    return _script(
        payload,
        r'''    design = _design()
    targets = list(PAYLOAD.get("targets") or [])
    deleted = []
    skipped = []
    resolved = []

    def _target_component(target):
        return target.get("component") or target.get("component_path") or ""

    def target_matches_body(target, component, body, body_index):
        name = body.name or ""
        component_name = component.name or ""
        key = "%s/%s#%d" % (component_name, name, body_index + 1)
        target_token = target.get("entity_token") or target.get("token")
        if target_token:
            return target_token == _entity_token(body)
        if target.get("body_key") == key or target.get("key") == key:
            return True
        target_name = target.get("name") or target.get("body") or target.get("target")
        target_component = _target_component(target)
        if target_name and target_name == name:
            return bool(target_component) and target_component == component_name
        return False

    def occurrence_path_map(occurrences, prefix, out):
        if not occurrences:
            return
        for index in range(occurrences.count):
            occurrence = occurrences.item(index)
            if not occurrence:
                continue
            component = occurrence.component
            name = occurrence.name or (component.name if component else "occurrence_%d" % (index + 1))
            path = ("%s/%s" % (prefix, name)) if prefix else name
            out[path] = occurrence
            try:
                occurrence_path_map(occurrence.childOccurrences, path, out)
            except Exception:
                pass

    occurrences_by_path = {}
    occurrence_path_map(design.rootComponent.occurrences, "", occurrences_by_path)
    occurrences_by_token = {}
    for occurrence in occurrences_by_path.values():
        token = _entity_token(occurrence)
        if token:
            occurrences_by_token[token] = occurrence
    for target_index, target in enumerate(targets):
        kind = str(target.get("kind") or target.get("type") or "body").lower()
        if kind in ("body", "brepbody"):
            matches = []
            for component_index in range(design.allComponents.count):
                component = design.allComponents.item(component_index)
                if not component:
                    continue
                bodies = component.bRepBodies
                for body_index in range(bodies.count):
                    body = bodies.item(body_index)
                    if body and target_matches_body(target, component, body, body_index):
                        matches.append((component, body))
            if len(matches) == 1:
                resolved.append(("body", matches[0][0], matches[0][1], None))
            else:
                skipped.append({"target_index": target_index, "target": target, "reason": "target_must_match_exactly_one_entity", "match_count": len(matches)})
        elif kind == "occurrence":
            path = target.get("path") or target.get("occurrence_path")
            token = target.get("entity_token") or target.get("token")
            occurrence = occurrences_by_token.get(token) if token else occurrences_by_path.get(path)
            if occurrence:
                resolved.append(("occurrence", None, occurrence, path))
            else:
                skipped.append({"target_index": target_index, "target": target, "reason": "occurrence_not_found"})
        else:
            skipped.append({"target_index": target_index, "target": target, "reason": "unsupported_delete_kind"})
    if skipped:
        print(json.dumps({"success": False, "error_code": "TARGET_PREFLIGHT_FAILED", "deleted": [], "deleted_count": 0, "skipped": skipped}, sort_keys=True))
        return
    for kind, component, entity, path in resolved:
        if kind == "body":
            deleted.append({"kind": "body", "component": component.name or "", "name": entity.name or ""})
        else:
            deleted.append({"kind": "occurrence", "path": path})
        entity.deleteMe()
    print(json.dumps({"success": True, "deleted": deleted, "deleted_count": len(deleted), "skipped": skipped}, sort_keys=True))
''',
    )


def _script(payload: dict[str, Any], body: str) -> str:
    payload_json = json.dumps(payload, sort_keys=True)
    return f"""
import hashlib
import json
import time
import adsk.core
import adsk.fusion

PAYLOAD = json.loads({payload_json!r})


def _design():
    app = adsk.core.Application.get()
    design = adsk.fusion.Design.cast(app.activeProduct)
    if not design:
        raise RuntimeError("active product is not a Fusion design")
    return design


def _document_payload():
    app = adsk.core.Application.get()
    document = app.activeDocument
    payload = {{"name": getattr(document, "name", "") if document else ""}}
    try:
        data_file = document.dataFile if document else None
        if data_file:
            payload["id"] = getattr(data_file, "id", "") or ""
            payload["version"] = getattr(data_file, "versionNumber", None)
            if payload["id"]:
                payload["identity_kind"] = "data_file"
                payload["stable_id"] = payload["id"]
    except Exception:
        pass
    if document and not payload.get("stable_id"):
        root_token = ""
        try:
            root_token = _entity_token(_design().rootComponent)
        except Exception:
            pass
        payload["identity_kind"] = "unsaved_session"
        payload["stable_id"] = (
            "unsaved-root:%s" % root_token
            if root_token
            else "unsaved-process:%s" % id(document)
        )
    return payload


def _entity_token(entity):
    try:
        return getattr(entity, "entityToken", "") or ""
    except Exception:
        return ""


def _visible(entity):
    visible = True
    try:
        visible = visible and bool(entity.isLightBulbOn)
    except Exception:
        pass
    try:
        visible = visible and bool(entity.isVisible)
    except Exception:
        pass
    return visible


def _component_visible(component):
    try:
        return bool(component.isLightBulbOn)
    except Exception:
        return True


def _component_key(component):
    if not component:
        return ""
    try:
        token = getattr(component, "entityToken", "") or ""
        if token:
            return token
    except Exception:
        pass
    return component.name or ""


def _point_mm(point):
    return [round(point.x * 10.0, 6), round(point.y * 10.0, 6), round(point.z * 10.0, 6)]


def _bbox_payload_mm(box):
    if not box:
        return None
    min_point = _point_mm(box.minPoint)
    max_point = _point_mm(box.maxPoint)
    size = [
        round(abs(max_point[0] - min_point[0]), 6),
        round(abs(max_point[1] - min_point[1]), 6),
        round(abs(max_point[2] - min_point[2]), 6),
    ]
    center = [
        round((min_point[0] + max_point[0]) / 2.0, 6),
        round((min_point[1] + max_point[1]) / 2.0, 6),
        round((min_point[2] + max_point[2]) / 2.0, 6),
    ]
    return {{"min_mm": min_point, "max_mm": max_point, "size_mm": size, "center_mm": center}}


def _transform_payload(occurrence):
    transform = None
    try:
        transform = occurrence.transform2
    except Exception:
        try:
            transform = occurrence.transform
        except Exception:
            transform = None
    if not transform:
        return {{}}
    payload = {{}}
    try:
        payload["translation_mm"] = _point_mm(transform.translation)
    except Exception:
        pass
    try:
        matrix = []
        for row in range(4):
            matrix_row = []
            for column in range(4):
                value = float(transform.getCell(row, column))
                if column == 3 and row < 3:
                    value *= 10.0
                matrix_row.append(round(value, 6))
            matrix.append(matrix_row)
        payload["matrix"] = matrix
    except Exception:
        pass
    return payload


def _merge_bbox(existing, bbox):
    if not existing:
        return dict(bbox)
    min_point = [min(float(a), float(b)) for a, b in zip(existing["min_mm"], bbox["min_mm"])]
    max_point = [max(float(a), float(b)) for a, b in zip(existing["max_mm"], bbox["max_mm"])]
    size = [round(abs(a - b), 6) for a, b in zip(max_point, min_point)]
    center = [round((a + b) / 2.0, 6) for a, b in zip(max_point, min_point)]
    return {{"min_mm": min_point, "max_mm": max_point, "size_mm": size, "center_mm": center}}


def _hash_payload(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def run(_context: str):
{body}
"""
