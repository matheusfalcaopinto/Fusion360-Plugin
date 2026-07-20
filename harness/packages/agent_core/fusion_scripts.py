"""Fusion script builders for safe read-first workflows."""

from __future__ import annotations

import json
from typing import Any


def compact_snapshot_script(payload: dict[str, Any]) -> str:
    """Return a Fusion script that emits a capped, component-scoped snapshot."""

    return _script(
        payload,
        r"""    design = _design()
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
    component_instance_counts = {}

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
            if visible is None:
                stop("visibility_unavailable")
                return
            snapshot["counts"]["occurrences_total"] += 1
            component_key = _component_key(component) if component else ""
            if component:
                component_instance_counts[component_key] = (
                    component_instance_counts.get(component_key, 0) + 1
                )
            if visible:
                snapshot["counts"]["visible_occurrences"] += 1
                snapshot["visible_occurrence_paths"].append(path)
                if component:
                    visible_components.add(component_key)
            if len(snapshot["occurrences"]) < max_occurrences:
                item = {
                    "path": path,
                    "name": name,
                    "component": component.name if component else "",
                    "component_key": component_key,
                    "entity_token": _entity_token(occurrence),
                    "visible": visible,
                    "is_root": False,
                    "is_referenced": _component_reference_fact(component),
                    "is_imported": _component_imported_fact(component),
                    "shared_definition": None,
                }
                if include_transforms:
                    item["transform"] = _transform_payload(occurrence)
                try:
                    item["bbox_mm"] = _bbox_payload_mm(occurrence.boundingBox)
                except Exception:
                    item["bbox_error_code"] = "BOUNDING_BOX_UNAVAILABLE"
                if not reserve(item):
                    return
                snapshot["occurrences"].append(item)
            else:
                snapshot["payload_capped"] = True
            try:
                child_occurrences = occurrence.childOccurrences
            except Exception:
                stop("downstream_unavailable")
                return
            visit_occurrences(child_occurrences, path)

    visit_occurrences(design.rootComponent.occurrences, "")
    for item in snapshot["occurrences"]:
        item["shared_definition"] = bool(
            component_instance_counts.get(item.get("component_key") or "", 0) > 1
        )
        item["binding_fingerprint"] = _binding_fingerprint(item)
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
            stop("downstream_unavailable")
            break
        for body_index in range(bodies.count):
            if not visit():
                break
            body = bodies.item(body_index)
            if not body:
                continue
            body_visible = _visible(body)
            component_visible = _component_visible(component)
            if body_visible is None or component_visible is None:
                stop("visibility_unavailable")
                break
            snapshot["counts"]["bodies_total"] += 1
            name = body.name or "body_%d" % (body_index + 1)
            body_name_counts[name] = body_name_counts.get(name, 0) + 1
            key = "%s/%s#%d" % (component.name or component_key, name, body_index + 1)
            visible = body_visible and component_visible
            bbox = None
            try:
                bbox = _bbox_payload_mm(body.boundingBox)
            except Exception:
                bbox = {"error_code": "BOUNDING_BOX_UNAVAILABLE"}
            if visible:
                snapshot["counts"]["visible_bodies"] += 1
                snapshot["visible_body_keys"].append(key)
                visible_components.add(component_key)
                if isinstance(bbox, dict) and "min_mm" in bbox and "max_mm" in bbox:
                    body_bbox_union = _merge_bbox(body_bbox_union, bbox)
            if len(snapshot["bodies"]) < max_bodies:
                body_item = {
                    "key": key,
                    "name": name,
                    "component": component.name or "",
                    "component_key": component_key,
                    "entity_token": _entity_token(body),
                    "visible": visible,
                    "is_root": component == design.rootComponent,
                    "is_referenced": _component_reference_fact(component),
                    "is_imported": _component_imported_fact(component),
                    "shared_definition": bool(
                        component_instance_counts.get(component_key, 0) > 1
                    ),
                    "bbox_mm": bbox,
                }
                body_item["binding_fingerprint"] = _binding_fingerprint(body_item)
                if not reserve(body_item):
                    break
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

    def encode_wrapper():
        snapshot["elapsed_ms"] = elapsed_ms()
        if snapshot["elapsed_ms"] >= deadline_ms and stop_reason is None:
            stop("deadline_ms")
        if stop_reason is not None:
            snapshot["complete"] = False
            snapshot["truncated"] = True
            snapshot["counts_exact"] = False
            snapshot["stop_reason"] = stop_reason
        snapshot["snapshot_hash"] = _hash_payload(
            {
                "visible_occurrence_paths": snapshot.get("visible_occurrence_paths", []),
                "visible_body_keys": snapshot.get("visible_body_keys", []),
                "visible_component_keys": snapshot.get("visible_component_keys", []),
            }
        )
        for _iteration in range(8):
            encoded = json.dumps(wrapper, sort_keys=True, separators=(",", ":"))
            response_bytes = len(encoded.encode("utf-8"))
            if snapshot["response_bytes"] == response_bytes:
                break
            snapshot["response_bytes"] = response_bytes
        encoded = json.dumps(wrapper, sort_keys=True, separators=(",", ":"))
        return encoded, len(encoded.encode("utf-8"))

    def shrink_sequence(values, response_bytes):
        if not values:
            return False
        ratio = min(0.9, (max_response_bytes / float(max(response_bytes, 1))) * 0.9)
        keep = max(0, min(len(values) - 1, int(len(values) * ratio)))
        del values[keep:]
        return True

    def shrink_mapping(values, response_bytes):
        if not values:
            return False
        keys = sorted(values)
        ratio = min(0.9, (max_response_bytes / float(max(response_bytes, 1))) * 0.9)
        keep = max(0, min(len(keys) - 1, int(len(keys) * ratio)))
        for key in keys[keep:]:
            values.pop(key, None)
        return True

    for trim_pass in range(64):
        encoded, response_bytes = encode_wrapper()
        if response_bytes <= max_response_bytes:
            print(encoded)
            break
        stop("max_response_bytes")
        snapshot["complete"] = False
        snapshot["truncated"] = True
        snapshot["counts_exact"] = False
        snapshot["stop_reason"] = stop_reason
        trimmed = False
        for key in (
            "bodies",
            "occurrences",
            "visible_body_keys",
            "visible_occurrence_paths",
            "visible_component_keys",
            "duplicate_name_warnings",
        ):
            if shrink_sequence(snapshot[key], response_bytes):
                trimmed = True
                break
        if not trimmed:
            trimmed = shrink_mapping(snapshot["duplicate_body_names"], response_bytes)
        if not trimmed:
            document = snapshot.get("document") or {}
            if document.get("truncated") is not True:
                snapshot["document"] = {
                    "name": str(document.get("name") or "")[:256],
                    "truncated": True,
                }
            else:
                snapshot.clear()
                snapshot.update(
                    {
                        "schema_version": "compact_snapshot.v2",
                        "schema_compatibility": ["compact_snapshot.v1"],
                        "source": "real",
                        "document": {"name": "", "truncated": True},
                        "payload_capped": True,
                        "counts": {},
                        "occurrences": [],
                        "bodies": [],
                        "visible_occurrence_paths": [],
                        "visible_body_keys": [],
                        "visible_component_keys": [],
                        "complete": False,
                        "truncated": True,
                        "visited_entities": visited_entities,
                        "elapsed_ms": elapsed_ms(),
                        "response_bytes": 0,
                        "counts_exact": False,
                        "stop_reason": stop_reason or "max_response_bytes",
                        "snapshot_hash": "",
                    }
                )
        if trim_pass == 63:
            snapshot.clear()
            snapshot.update(
                {
                    "schema_version": "compact_snapshot.v2",
                    "source": "real",
                    "payload_capped": True,
                    "complete": False,
                    "truncated": True,
                    "visited_entities": visited_entities,
                    "elapsed_ms": elapsed_ms(),
                    "response_bytes": 0,
                    "counts_exact": False,
                    "stop_reason": stop_reason or "max_response_bytes",
                    "snapshot_hash": "",
                }
            )
            encoded, _response_bytes = encode_wrapper()
            print(encoded)
            break
""",
    )


def hub_inventory_script(payload: dict[str, Any]) -> str:
    """Return a Fusion script for metadata-first hub inventory."""

    return _script(
        payload,
        r"""    app = adsk.core.Application.get()
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
        except Exception:
            strategy["project_metadata_error_code"] = "PROJECT_METADATA_UNAVAILABLE"
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
                    except Exception:
                        item["enrich_error_code"] = "FILE_ENRICHMENT_UNAVAILABLE"
            results.append(item)
            if len(results) >= max_results:
                break
    except Exception:
        strategy["open_documents_error_code"] = "OPEN_DOCUMENTS_UNAVAILABLE"
    print(json.dumps({"success": True, "strategy": strategy, "projects_seen": projects_seen, "results": results[:max_results]}, sort_keys=True))
""",
    )


def safe_visibility_apply_script(payload: dict[str, Any]) -> str:
    """Return a Fusion script that applies reversible visibility changes."""

    return _script(
        payload,
        r"""    design = _design()
    targets = list(PAYLOAD.get("targets") or [])
    changed = []
    resolved = []
    preflight_errors = []

    envelope_error = _mutation_envelope_error(PAYLOAD)
    if envelope_error:
        print(json.dumps({"success": False, "error_code": envelope_error, "changed": [], "changed_count": 0}, sort_keys=True))
        return

    def target_visible(target):
        if "desired_visible" in target:
            return bool(target.get("desired_visible"))
        if "visible" in target:
            return bool(target.get("visible"))
        if "value" in target:
            return bool(target.get("value"))
        return False

    def target_matches_body(target, body):
        target_token = target.get("entity_token") or target.get("token")
        return bool(target_token) and target_token == _entity_token(body)

    component_instance_counts = {}

    def count_occurrences(occurrences):
        if not occurrences:
            return
        for index in range(occurrences.count):
            occurrence = occurrences.item(index)
            if not occurrence:
                continue
            component = occurrence.component
            component_key = _component_key(component)
            component_instance_counts[component_key] = component_instance_counts.get(component_key, 0) + 1
            try:
                count_occurrences(occurrence.childOccurrences)
            except Exception:
                pass

    count_occurrences(design.rootComponent.occurrences)

    def body_binding(component, body, body_index):
        name = body.name or ""
        component_name = component.name or ""
        component_key = _component_key(component)
        token = _entity_token(body)
        item = {
            "kind": "body",
            "identifier": token,
            "entity_token": token,
            "path": None,
            "key": "%s/%s#%d" % (component_name, name, body_index + 1),
            "component": component_name,
            "name": name,
            "visible": _visible(body) and _component_visible(component),
            "is_root": component == design.rootComponent,
            "is_referenced": _component_reference_fact(component),
            "is_imported": _component_imported_fact(component),
            "shared_definition": bool(component_instance_counts.get(component_key, 0) > 1),
        }
        item["binding_fingerprint"] = _binding_fingerprint(item)
        return item

    def expected_binding_matches(target, actual):
        token = target.get("entity_token") or target.get("token")
        required = (
            "identifier",
            "binding_fingerprint",
            "visible",
            "is_root",
            "is_referenced",
            "is_imported",
            "shared_definition",
        )
        if any(key not in target for key in required):
            return False
        if not isinstance(token, str) or not token or target.get("identifier") != token:
            return False
        if not all(
            isinstance(target.get(key), bool)
            for key in (
                "visible",
                "is_root",
                "is_referenced",
                "is_imported",
                "shared_definition",
            )
        ):
            return False
        return all(
            target.get(key) == actual.get(key)
            for key in (
                "kind",
                "identifier",
                "entity_token",
                "path",
                "key",
                "component",
                "name",
                "visible",
                "is_root",
                "is_referenced",
                "is_imported",
                "shared_definition",
                "binding_fingerprint",
            )
        )

    for target_index, target in enumerate(targets):
        if not isinstance(target, dict):
            preflight_errors.append({"target_index": target_index, "reason": "target_must_be_an_object"})
            continue
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
                if body and target_matches_body(target, body):
                    matches.append((component, body, body_index))
        if len(matches) != 1:
            preflight_errors.append({"target_index": target_index, "reason": "target_must_match_exactly_one_entity", "match_count": len(matches)})
        else:
            component, body, body_index = matches[0]
            actual = body_binding(component, body, body_index)
            if expected_binding_matches(target, actual):
                resolved.append((component, body, desired_visible, actual))
            else:
                preflight_errors.append({"target_index": target_index, "reason": "binding_fingerprint_mismatch"})
    if preflight_errors:
        print(json.dumps({"success": False, "error_code": "TARGET_PREFLIGHT_FAILED", "changed": [], "changed_count": 0, "preflight_errors": preflight_errors}, sort_keys=True))
        return
    for component, body, desired_visible, actual in resolved:
        body.isLightBulbOn = desired_visible
        changed.append(
            {
                "kind": "body",
                "component": component.name or "",
                "name": body.name or "",
                "visible": desired_visible,
                "entity_identity_digest": _identity_digest(actual.get("entity_token")),
                "binding_fingerprint": actual.get("binding_fingerprint"),
            }
        )
    print(json.dumps({"success": True, "changed": changed, "changed_count": len(changed), "operation_binding": PAYLOAD.get("operation_binding")}, sort_keys=True))
""",
    )


def safe_delete_apply_script(payload: dict[str, Any]) -> str:
    """Return a Fusion script that deletes explicitly targeted bodies/occurrences."""

    return _script(
        payload,
        r"""    design = _design()
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

    def expected_binding_matches(target, actual):
        token = target.get("entity_token") or target.get("token")
        required = (
            "identifier",
            "binding_fingerprint",
            "visible",
            "is_root",
            "is_referenced",
            "is_imported",
            "shared_definition",
        )
        if any(key not in target for key in required):
            return False
        if not isinstance(token, str) or not token or target.get("identifier") != token:
            return False
        if not all(
            isinstance(target.get(key), bool)
            for key in (
                "visible",
                "is_root",
                "is_referenced",
                "is_imported",
                "shared_definition",
            )
        ):
            return False
        return all(
            target.get(key) == actual.get(key)
            for key in (
                "kind",
                "identifier",
                "entity_token",
                "path",
                "key",
                "component",
                "name",
                "visible",
                "is_root",
                "is_referenced",
                "is_imported",
                "shared_definition",
                "binding_fingerprint",
            )
        )

    occurrences_by_path = {}
    occurrence_path_map(design.rootComponent.occurrences, "", occurrences_by_path)
    occurrences_by_token = {}
    occurrence_paths_by_token = {}
    component_instance_counts = {}
    for current_path, occurrence in occurrences_by_path.items():
        token = _entity_token(occurrence)
        if token:
            occurrences_by_token.setdefault(token, []).append(occurrence)
            occurrence_paths_by_token.setdefault(token, []).append(current_path)
        component = occurrence.component
        component_key = _component_key(component)
        component_instance_counts[component_key] = component_instance_counts.get(component_key, 0) + 1

    def body_binding(component, body, body_index):
        name = body.name or ""
        component_name = component.name or ""
        component_key = _component_key(component)
        token = _entity_token(body)
        item = {
            "kind": "body",
            "identifier": token,
            "entity_token": token,
            "path": None,
            "key": "%s/%s#%d" % (component_name, name, body_index + 1),
            "component": component_name,
            "name": name,
            "visible": _visible(body) and _component_visible(component),
            "is_root": component == design.rootComponent,
            "is_referenced": _component_reference_fact(component),
            "is_imported": _component_imported_fact(component),
            "shared_definition": bool(component_instance_counts.get(component_key, 0) > 1),
        }
        item["binding_fingerprint"] = _binding_fingerprint(item)
        return item

    def occurrence_binding(occurrence, path):
        component = occurrence.component
        component_key = _component_key(component)
        token = _entity_token(occurrence)
        item = {
            "kind": "occurrence",
            "identifier": token,
            "entity_token": token,
            "path": path,
            "key": None,
            "component": component.name if component else "",
            "name": occurrence.name or (component.name if component else ""),
            "visible": _visible(occurrence),
            "is_root": False,
            "is_referenced": _component_reference_fact(component),
            "is_imported": _component_imported_fact(component),
            "shared_definition": bool(component_instance_counts.get(component_key, 0) > 1),
        }
        item["binding_fingerprint"] = _binding_fingerprint(item)
        return item
    envelope_error = _mutation_envelope_error(PAYLOAD)
    if envelope_error:
        print(json.dumps({"success": False, "error_code": envelope_error, "deleted": [], "deleted_count": 0, "skipped": []}, sort_keys=True))
        return

    for target_index, target in enumerate(targets):
        if not isinstance(target, dict):
            skipped.append({"target_index": target_index, "reason": "target_must_be_an_object"})
            continue
        kind = str(target.get("kind") or target.get("type") or "body").lower()
        if kind in ("body", "brepbody"):
            target_token = target.get("entity_token") or target.get("token")
            if not isinstance(target_token, str) or not target_token:
                skipped.append({"target_index": target_index, "reason": "stable_entity_identity_required"})
                continue
            matches = []
            for component_index in range(design.allComponents.count):
                component = design.allComponents.item(component_index)
                if not component:
                    continue
                bodies = component.bRepBodies
                for body_index in range(bodies.count):
                    body = bodies.item(body_index)
                    if body and target_token == _entity_token(body):
                        matches.append((component, body, body_index))
            if len(matches) == 1:
                component, body, body_index = matches[0]
                actual = body_binding(component, body, body_index)
                if expected_binding_matches(target, actual):
                    resolved.append(("body", component, body, None, actual))
                else:
                    skipped.append({"target_index": target_index, "reason": "binding_fingerprint_mismatch"})
            else:
                skipped.append({"target_index": target_index, "target": target, "reason": "target_must_match_exactly_one_entity", "match_count": len(matches)})
        elif kind == "occurrence":
            token = target.get("entity_token") or target.get("token")
            matches = occurrences_by_token.get(token, []) if isinstance(token, str) and token else []
            current_paths = occurrence_paths_by_token.get(token, []) if isinstance(token, str) and token else []
            if len(matches) == 1 and len(current_paths) == 1:
                occurrence = matches[0]
                current_path = current_paths[0]
                actual = occurrence_binding(occurrence, current_path)
                if expected_binding_matches(target, actual):
                    resolved.append(("occurrence", None, occurrence, current_path, actual))
                else:
                    skipped.append({"target_index": target_index, "reason": "binding_fingerprint_mismatch"})
            else:
                skipped.append({"target_index": target_index, "reason": "occurrence_must_match_exactly_one_stable_identity", "match_count": len(matches)})
        else:
            skipped.append({"target_index": target_index, "target": target, "reason": "unsupported_delete_kind"})
    if skipped:
        print(json.dumps({"success": False, "error_code": "TARGET_PREFLIGHT_FAILED", "deleted": [], "deleted_count": 0, "skipped": skipped}, sort_keys=True))
        return
    for kind, component, entity, path, actual in resolved:
        if kind == "body":
            deleted.append({"kind": "body", "component": component.name or "", "name": entity.name or "", "entity_identity_digest": _identity_digest(actual.get("entity_token")), "binding_fingerprint": actual.get("binding_fingerprint")})
        else:
            deleted.append({"kind": "occurrence", "path": path, "entity_identity_digest": _identity_digest(actual.get("entity_token")), "binding_fingerprint": actual.get("binding_fingerprint")})
        entity.deleteMe()
    print(json.dumps({"success": True, "deleted": deleted, "deleted_count": len(deleted), "skipped": skipped, "operation_binding": PAYLOAD.get("operation_binding")}, sort_keys=True))
""",
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
    data_id = ""
    version_id = ""
    try:
        data_file = document.dataFile if document else None
        if data_file:
            data_id = str(getattr(data_file, "id", "") or "")
            version_id = str(getattr(data_file, "versionId", "") or "")
            payload["id"] = data_id
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
    if document:
        root_token = ""
        try:
            root_token = _entity_token(_design().rootComponent)
        except Exception:
            pass
        if data_id or root_token:
            payload["binding_identity"] = hashlib.sha256(json.dumps({{
                "data_id": data_id,
                "version_id": version_id,
                "root_token": root_token,
            }}, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return payload


def _is_sha256(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _mutation_envelope_error(payload):
    expected = payload.get("document_identity")
    if not isinstance(expected, dict) or set(expected) != {{"kind", "stable_id"}}:
        return "DOCUMENT_BINDING_INVALID"
    if not all(isinstance(expected.get(key), str) and expected.get(key) for key in ("kind", "stable_id")):
        return "DOCUMENT_BINDING_INVALID"
    current = _document_payload()
    actual = {{
        "kind": current.get("identity_kind") or "",
        "stable_id": current.get("stable_id") or "",
    }}
    if actual != expected:
        return "DOCUMENT_BINDING_MISMATCH"
    if not all(
        _is_sha256(payload.get(key))
        for key in ("state_fingerprint", "preview_digest", "operation_binding")
    ):
        return "OPERATION_BINDING_INVALID"
    return None


def _identity_digest(value):
    if not isinstance(value, str) or not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _entity_token(entity):
    try:
        return getattr(entity, "entityToken", "") or ""
    except Exception:
        return ""


def _visible(entity):
    try:
        light_bulb_on = bool(entity.isLightBulbOn)
    except Exception:
        return None
    try:
        api_visible = bool(entity.isVisible)
    except Exception:
        return None
    return light_bulb_on and api_visible


def _component_visible(component):
    try:
        return bool(component.isLightBulbOn)
    except Exception:
        return None


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


def _component_reference_fact(component):
    if not component:
        return None
    try:
        return bool(component.isReferencedComponent)
    except Exception:
        return None


def _component_imported_fact(component):
    referenced = _component_reference_fact(component)
    if referenced is True:
        return True
    if referenced is None:
        return None
    try:
        attributes = component.attributes
        marker = attributes.itemByName("fusion_agent", "origin") if attributes else None
        return bool(marker and str(marker.value).lower() == "imported")
    except Exception:
        return None


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


def _binding_fingerprint(item):
    return _hash_payload({{
        "key": item.get("key"),
        "path": item.get("path"),
        "component": item.get("component"),
        "name": item.get("name"),
        "entity_token": item.get("entity_token") or item.get("token"),
        "visible": item.get("visible"),
        "is_root": item.get("is_root"),
        "is_referenced": item.get("is_referenced"),
        "is_imported": item.get("is_imported"),
        "shared_definition": item.get("shared_definition"),
    }})


def run(_context: str):
{body}
"""
