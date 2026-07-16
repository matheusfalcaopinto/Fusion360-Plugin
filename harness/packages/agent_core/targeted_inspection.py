"""Bounded, targeted Fusion inspection used by the native fast path."""

from __future__ import annotations

import json
from typing import Any


SUPPORTED_ENTITY_TYPES = {
    "document",
    "component",
    "occurrence",
    "body",
    "sketch",
    "feature",
    "parameter",
}

MAX_ENTITIES_VISITED = 5000
MAX_DEADLINE_MS = 5000
MIN_MAX_RESPONSE_BYTES = 4096
DEFAULT_MAX_ENTITIES_VISITED = 1000
DEFAULT_DEADLINE_MS = 1500
DEFAULT_MAX_RESPONSE_BYTES = 1024 * 1024


def validate_inspection_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize a targeted-inspection request."""

    queries = payload.get("queries")
    if not isinstance(queries, list) or not queries:
        raise ValueError("queries must be a non-empty array")
    if len(queries) > 50:
        raise ValueError("queries may contain at most 50 entries")
    limit = int(payload.get("limit_per_query", 20))
    if limit < 1 or limit > 100:
        raise ValueError("limit_per_query must be between 1 and 100")
    include_state_fingerprint = bool(payload.get("include_state_fingerprint", False))
    state_fingerprint_limit = int(payload.get("state_fingerprint_limit", 5000))
    if state_fingerprint_limit < 100 or state_fingerprint_limit > 20_000:
        raise ValueError("state_fingerprint_limit must be between 100 and 20000")
    max_entities_visited = int(
        payload.get("max_entities_visited", DEFAULT_MAX_ENTITIES_VISITED)
    )
    if max_entities_visited < 1 or max_entities_visited > MAX_ENTITIES_VISITED:
        raise ValueError("max_entities_visited must be between 1 and 5000")
    deadline_ms = int(payload.get("deadline_ms", DEFAULT_DEADLINE_MS))
    if deadline_ms < 50 or deadline_ms > MAX_DEADLINE_MS:
        raise ValueError("deadline_ms must be between 50 and 5000")
    max_response_bytes = int(
        payload.get("max_response_bytes", DEFAULT_MAX_RESPONSE_BYTES)
    )
    if (
        max_response_bytes < MIN_MAX_RESPONSE_BYTES
        or max_response_bytes > DEFAULT_MAX_RESPONSE_BYTES
    ):
        raise ValueError("max_response_bytes must be between 4096 and 1048576")

    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(queries):
        if not isinstance(raw, dict):
            raise ValueError(f"queries[{index}] must be an object")
        query_id = str(raw.get("id") or "").strip()
        if not query_id or query_id in seen_ids:
            raise ValueError(f"queries[{index}].id must be unique and non-empty")
        seen_ids.add(query_id)
        entity_type = str(raw.get("entity_type") or "").strip().lower()
        if entity_type not in SUPPORTED_ENTITY_TYPES:
            raise ValueError(
                f"queries[{index}].entity_type is unsupported: {entity_type}"
            )
        selector = raw.get("selector") or {}
        if not isinstance(selector, dict):
            raise ValueError(f"queries[{index}].selector must be an object")
        if selector.get("component_path") not in (None, "") and selector.get(
            "name"
        ) in (None, ""):
            raise ValueError(
                f"queries[{index}].selector.component_path requires selector.name"
            )
        fields = raw.get("fields") or ["exists"]
        if not isinstance(fields, list) or not all(
            isinstance(field, str) and field for field in fields
        ):
            raise ValueError(f"queries[{index}].fields must be an array of strings")
        normalized.append(
            {
                "id": query_id,
                "entity_type": entity_type,
                "selector": {
                    key: selector.get(key)
                    for key in ("entity_token", "path", "component_path", "name")
                    if selector.get(key) not in (None, "")
                },
                "fields": list(dict.fromkeys(fields)),
            }
        )
    return {
        "queries": normalized,
        "limit_per_query": limit,
        "include_state_fingerprint": include_state_fingerprint,
        "state_fingerprint_limit": min(state_fingerprint_limit, max_entities_visited),
        "max_entities_visited": max_entities_visited,
        "deadline_ms": deadline_ms,
        "max_response_bytes": max_response_bytes,
    }


def build_targeted_inspection_script(payload: dict[str, Any]) -> str:
    """Return one trusted Fusion script that evaluates all requested selectors."""

    normalized = validate_inspection_payload(payload)
    request_json = json.dumps(normalized, ensure_ascii=True, separators=(",", ":"))
    return TARGETED_INSPECTION_SCRIPT.replace("__REQUEST_JSON__", repr(request_json))


TARGETED_INSPECTION_SCRIPT = r'''
import adsk.core
import adsk.fusion
import hashlib
import json
import time

_REQUEST = json.loads(__REQUEST_JSON__)
_STARTED = time.perf_counter()
_MAX_ENTITIES = int(_REQUEST.get("max_entities_visited", 1000))
_DEADLINE_MS = int(_REQUEST.get("deadline_ms", 1500))
_MAX_RESPONSE_BYTES = int(_REQUEST.get("max_response_bytes", 1048576))
_VISITED = 0
_ESTIMATED_BYTES = 512
_STOP_REASON = ""
_COUNTS_EXACT = True
_WARNINGS = []


def _safe(callable_obj, default=None):
    try:
        return callable_obj()
    except BaseException:
        return default


def _elapsed_ms():
    return int((time.perf_counter() - _STARTED) * 1000.0)


def _stop(reason):
    global _STOP_REASON, _COUNTS_EXACT
    if not _STOP_REASON:
        _STOP_REASON = reason
    _COUNTS_EXACT = False


def _consume_entity():
    global _VISITED
    if _STOP_REASON:
        return False
    if _elapsed_ms() >= _DEADLINE_MS:
        _stop("deadline_ms")
        return False
    if _VISITED >= _MAX_ENTITIES:
        _stop("max_entities_visited")
        return False
    _VISITED += 1
    return True


def _reserve(value):
    global _ESTIMATED_BYTES
    size = len(json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("utf-8")) + 16
    if _ESTIMATED_BYTES + size > _MAX_RESPONSE_BYTES:
        _stop("response_limit")
        return False
    _ESTIMATED_BYTES += size
    return True


def _iter_collection(collection):
    if _STOP_REASON:
        return
    count = int(_safe(lambda: collection.count, 0) or 0)
    for index in range(count):
        if not _consume_entity():
            return
        entity = _safe(lambda i=index: collection.item(i))
        if entity is not None:
            yield entity


def _document_identity(doc):
    data_file = _safe(lambda: doc.dataFile)
    data_id = _safe(lambda: data_file.id, "") if data_file else ""
    design = adsk.fusion.Design.cast(_safe(lambda: doc.products.itemByProductType("DesignProductType")))
    root = _safe(lambda: design.rootComponent) if design else None
    marker_attribute = _safe(lambda: root.attributes.itemByName("fusion_agent_benchmark", "trial_marker")) if root else None
    marker = _safe(lambda: marker_attribute.value, "") if marker_attribute else ""
    stable_runtime_id = "data:" + data_id if data_id else ("marker:" + marker if marker else "")
    return {
        "name": _safe(lambda: doc.name, ""),
        "runtime_id": stable_runtime_id,
        "id": data_id,
        "version_id": _safe(lambda: data_file.versionId, "") if data_file else "",
        "is_modified": bool(_safe(lambda: doc.isModified, False)),
        "product_type": _safe(lambda: doc.product.productType, ""),
    }


def _bbox_mm(entity):
    bbox = _safe(lambda: entity.boundingBox)
    minimum = _safe(lambda: bbox.minPoint) if bbox else None
    maximum = _safe(lambda: bbox.maxPoint) if bbox else None
    if minimum is None or maximum is None:
        return None
    return {
        "min_mm": [minimum.x * 10.0, minimum.y * 10.0, minimum.z * 10.0],
        "max_mm": [maximum.x * 10.0, maximum.y * 10.0, maximum.z * 10.0],
        "size_mm": [(maximum.x - minimum.x) * 10.0, (maximum.y - minimum.y) * 10.0, (maximum.z - minimum.z) * 10.0],
    }


def _matches_entity_type(entity, entity_type):
    object_type = str(_safe(lambda: entity.objectType, "")).lower()
    marker = {
        "component": "component",
        "occurrence": "occurrence",
        "body": "brepbody",
        "sketch": "sketch",
        "feature": "feature",
        "parameter": "parameter",
    }.get(entity_type, "")
    return bool(marker and marker in object_type)


def _component_and_path(entity, entity_type, path_hint=""):
    if entity_type == "occurrence":
        component = _safe(lambda: entity.component)
        return component, path_hint or _safe(lambda: entity.fullPathName, _safe(lambda: entity.name, ""))
    component = entity if entity_type == "component" else _safe(lambda: entity.parentComponent)
    if entity_type == "parameter" and component is None:
        created_by = _safe(lambda: entity.createdBy)
        component = _safe(lambda: created_by.parentComponent) if created_by else None
    name = _safe(lambda: entity.name, "")
    if path_hint:
        return component, path_hint
    context = _safe(lambda: entity.assemblyContext)
    context_path = _safe(lambda: context.fullPathName, "") if context else ""
    if entity_type == "component":
        fallback_paths = _fallback_paths(entity, entity_type, component)
        return component, context_path or (fallback_paths[0] if fallback_paths else name)
    component_name = _safe(lambda: component.name, "") if component else ""
    fallback_paths = _fallback_paths(entity, entity_type, component)
    return component, context_path + "/" + name if context_path and name else (fallback_paths[0] if fallback_paths else component_name)


def _fallback_paths(entity, entity_type, component):
    name = _safe(lambda: entity.name, "")
    root = _safe(lambda: component.parentDesign.rootComponent) if component else None
    if component and component == root:
        if entity_type == "component":
            return ["root"]
        return ["/".join(part for part in ("root", name) if part)]
    component_name = _safe(lambda: component.name, "") if component else ""
    return ["/".join(part for part in (component_name, name if entity_type != "component" else "") if part)]


def _record(entity, entity_type, fields, path_hint=""):
    component, path = _component_and_path(entity, entity_type, path_hint)
    paths = [path] if path else []
    name = _safe(lambda: entity.name, "")
    component_path = path if entity_type == "component" else (path[:-len(name) - 1] if name and path.endswith("/" + name) else _safe(lambda: component.name, ""))
    occurrence = entity if entity_type == "occurrence" else _safe(lambda: entity.assemblyContext)
    record = {
        "entity_type": entity_type,
        "name": name,
        "path": path,
        "paths": paths,
        "entity_token": _safe(lambda: entity.entityToken, ""),
        "exists": True,
        "component_path": component_path,
        "component_paths": [component_path] if component_path else [],
        "is_referenced_component": bool(_safe(lambda: occurrence.isReferencedComponent, False)) if occurrence else False,
        "visible": bool(_safe(lambda: entity.isVisible, _safe(lambda: entity.isLightBulbOn, True))),
    }
    if "valid" in fields:
        record["valid"] = bool(_safe(lambda: entity.isValid, True))
    if "bounding_box_mm" in fields:
        record["bounding_box_mm"] = _bbox_mm(entity)
    if entity_type == "parameter":
        if "expression" in fields:
            record["expression"] = _safe(lambda: entity.expression, "")
        if "value" in fields:
            record["value"] = _safe(lambda: entity.value)
        record["unit"] = _safe(lambda: entity.unit, "")
    if entity_type == "feature" or "health" in fields:
        record["health"] = str(_safe(lambda: entity.healthState, ""))
        record["error_or_warning"] = _safe(lambda: entity.errorOrWarningMessage, "")
    return record


def _local_entities(component, entity_type):
    if component is None:
        return
    if entity_type == "component":
        if _consume_entity():
            yield component
        return
    collection_name = {"body": "bRepBodies", "sketch": "sketches", "feature": "features"}.get(entity_type)
    if collection_name:
        collection = _safe(lambda: getattr(component, collection_name))
        for entity in _iter_collection(collection):
            yield entity


def _global_entities(design, entity_type):
    root = design.rootComponent
    if entity_type == "component":
        for component in _iter_collection(design.allComponents):
            yield component
        return
    if entity_type == "occurrence":
        for occurrence in _walk_occurrences(root.occurrences):
            yield occurrence
        return
    if entity_type == "parameter":
        for parameter in _iter_collection(design.allParameters):
            yield parameter
        return
    collection_name = {"body": "bRepBodies", "sketch": "sketches", "feature": "features"}.get(entity_type)
    for component in _iter_collection(design.allComponents):
        collection = _safe(lambda c=component: getattr(c, collection_name))
        for entity in _iter_collection(collection):
            yield entity
        if _STOP_REASON:
            return


def _token_lookup_failed(exception=None, reason=None):
    warning = {"code": "ENTITY_TOKEN_LOOKUP_FAILED"}
    if exception is not None:
        warning["exception_type"] = type(exception).__name__
    if reason:
        warning["reason"] = reason
    _WARNINGS.append(warning)
    _stop("entity_token_lookup_failed")
    return [], False


def _token_entities(design, token, entity_type, cap):
    if not _consume_entity():
        return [], False
    try:
        found = design.findEntityByToken(token)
    except BaseException as exc:
        return _token_lookup_failed(exception=exc)
    if _elapsed_ms() >= _DEADLINE_MS:
        _stop("deadline_ms")
        return [], False
    if isinstance(found, tuple) and len(found) == 2 and isinstance(found[1], bool):
        if not found[1]:
            return _token_lookup_failed(reason="lookup_status_false")
        found = found[0]
    if found is None:
        return _token_lookup_failed(reason="invalid_result_shape")
    iterator = None
    if isinstance(found, (list, tuple)):
        count = len(found)

        def item_at(index):
            return found[index]
    else:
        count = _safe(lambda: found.count, None)
        item_method = _safe(lambda: found.item, None)
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0 and callable(item_method):

            def item_at(index):
                return item_method(index)
        else:
            if isinstance(found, (str, bytes, bytearray, dict)):
                return _token_lookup_failed(reason="invalid_result_shape")
            try:
                iterator = iter(found)
            except BaseException:
                return _token_lookup_failed(reason="invalid_result_shape")
            count = None
    if _elapsed_ms() >= _DEADLINE_MS:
        _stop("deadline_ms")
        return [], False

    matches = []
    exact = True
    index = 0
    while count is None or index < count:
        if index > 0 and not _consume_entity():
            exact = False
            break
        try:
            if iterator is None:
                item = item_at(index)
            else:
                item = next(iterator)
        except StopIteration as exc:
            if iterator is None:
                return _token_lookup_failed(exception=exc, reason="item_access_failed")
            if _elapsed_ms() >= _DEADLINE_MS:
                _stop("deadline_ms")
                return matches, False
            break
        except BaseException as exc:
            return _token_lookup_failed(exception=exc, reason="item_access_failed")
        if _elapsed_ms() >= _DEADLINE_MS:
            _stop("deadline_ms")
            return matches, False
        if item is None:
            return _token_lookup_failed(reason="invalid_result_item")
        object_type = _safe(lambda i=item: i.objectType, "")
        if not isinstance(object_type, str) or not object_type:
            return _token_lookup_failed(reason="invalid_result_item")
        if _matches_entity_type(item, entity_type):
            matches.append(item)
            if len(matches) >= cap:
                if count is None or index + 1 < count:
                    exact = False
                break
        index += 1
    return matches, exact


def _walk_occurrences(collection):
    for occurrence in _iter_collection(collection):
        yield occurrence
        child_occurrences = _safe(lambda o=occurrence: o.childOccurrences)
        for child in _walk_occurrences(child_occurrences):
            yield child
        if _STOP_REASON:
            return


def _local_item_by_name(collection, name):
    if collection is None or not name or not _consume_entity():
        return None
    direct = _safe(lambda: collection.itemByName(name))
    if direct is not None:
        return direct
    count = int(_safe(lambda: collection.count, 0) or 0)
    for index in range(count):
        if not _consume_entity():
            return None
        candidate = _safe(lambda i=index: collection.item(i))
        if candidate is not None and _safe(lambda c=candidate: c.name, "") == name:
            return candidate
    return None


def _path_segments(path):
    normalized = str(path or "").replace("+", "/")
    return [segment for segment in normalized.split("/") if segment and segment != "root"]


def _navigate_occurrence_path(root, path, cache):
    segments = _path_segments(path)
    if not segments:
        return None
    collection = root.occurrences
    prefix = []
    occurrence = None
    for segment in segments:
        prefix.append(segment)
        cache_key = "/".join(prefix)
        occurrence = cache.get(cache_key)
        if occurrence is None:
            occurrence = _local_item_by_name(collection, segment)
            if occurrence is None:
                return None
            cache[cache_key] = occurrence
        collection = _safe(lambda o=occurrence: o.childOccurrences)
    return occurrence


def _same_component_path(candidate_path, component_path):
    return candidate_path == component_path


def _append_match(matches, entity, entity_type, fields, path_hint, cap):
    if len(matches) >= cap:
        return False
    record = _record(entity, entity_type, fields, path_hint)
    if not _reserve(record):
        return False
    matches.append(record)
    return True


def _merge_bbox(existing, bbox):
    if not bbox:
        return existing
    if not existing:
        return dict(bbox)
    minimum = [min(existing["min_mm"][axis], bbox["min_mm"][axis]) for axis in range(3)]
    maximum = [max(existing["max_mm"][axis], bbox["max_mm"][axis]) for axis in range(3)]
    return {"min_mm": minimum, "max_mm": maximum, "size_mm": [maximum[axis] - minimum[axis] for axis in range(3)]}


def _global_state_fingerprint(document, records, truncated):
    """Fingerprint only the explicitly selected state, never an unbounded global walk."""
    if not bool(_REQUEST.get("include_state_fingerprint", False)):
        return None, False, 0
    if truncated:
        return None, True, len(records)
    encoded = json.dumps([document, records], ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest(), False, len(records)


def _finalize_payload(payload):
    global _STOP_REASON, _COUNTS_EXACT
    payload["elapsed_ms"] = _elapsed_ms()
    payload["visited_entities"] = _VISITED
    payload["complete"] = not bool(_STOP_REASON)
    payload["truncated"] = bool(_STOP_REASON)
    payload["counts_exact"] = bool(_COUNTS_EXACT and not _STOP_REASON)
    payload["stop_reason"] = _STOP_REASON or None
    payload["response_bytes"] = 0
    while True:
        encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        size = len(encoded.encode("utf-8"))
        if size <= _MAX_RESPONSE_BYTES:
            for _index in range(4):
                encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
                stable_size = len(encoded.encode("utf-8"))
                if payload.get("response_bytes") == stable_size:
                    return encoded
                payload["response_bytes"] = stable_size
            return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        _STOP_REASON = "response_limit"
        _COUNTS_EXACT = False
        payload["stop_reason"] = "response_limit"
        payload["complete"] = False
        payload["truncated"] = True
        payload["counts_exact"] = False
        summary = payload.get("summary") or {}
        summary["state_fingerprint"] = None
        summary["state_fingerprint_truncated"] = True
        summary["state_fingerprint_items"] = 0
        for result in payload.get("results") or []:
            result["match_count"] = len(result.get("matches") or [])
            result["match_count_exact"] = False
            result["truncated"] = True
        removed = False
        for result in reversed(payload.get("results") or []):
            if result.get("matches"):
                result["matches"].pop()
                result["truncated"] = True
                result["match_count"] = len(result["matches"])
                result["match_count_exact"] = False
                result["ambiguity_unknown"] = True
                result["ambiguous"] = True
                removed = True
                break
        if not removed:
            document = payload.get("document") or {}
            for key, value in list(document.items()):
                if isinstance(value, str) and len(value) > 128:
                    document[key] = value[:96] + "..." + hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
                    removed = True
        if not removed and payload.get("warnings"):
            payload["warnings"].pop()
            removed = True
        if not removed and payload.get("results"):
            payload["results"].pop()
            removed = True
        if not removed:
            payload["summary"] = {
                "state_fingerprint": None,
                "state_fingerprint_truncated": True,
                "state_fingerprint_items": 0,
            }


def run(_context: str):
    app = adsk.core.Application.get()
    doc = app.activeDocument
    if doc is None:
        raise RuntimeError("No active Fusion document")
    design = adsk.fusion.Design.cast(app.activeProduct)
    if design is None:
        raise RuntimeError("Active product is not a Fusion design")

    document = _document_identity(doc)
    root = design.rootComponent
    initial_counts = {
        "components": int(_safe(lambda: design.allComponents.count, 0) or 0),
        "occurrences": int(_safe(lambda: root.occurrences.count, 0) or 0),
        "bodies": int(_safe(lambda: root.bRepBodies.count, 0) or 0),
        "sketches": int(_safe(lambda: root.sketches.count, 0) or 0),
        "features": int(_safe(lambda: root.features.count, 0) or 0),
        "parameters": int(_safe(lambda: design.allParameters.count, 0) or 0),
    }
    limit = int(_REQUEST.get("limit_per_query", 20))
    queries = list(_REQUEST["queries"])
    query_matches = {query["id"]: [] for query in queries}
    query_exact = {query["id"]: True for query in queries}

    for query in queries:
        if query["entity_type"] == "document":
            query_matches[query["id"]] = [{"entity_type": "document", "exists": True, **document}]
            continue
        selector = query.get("selector") or {}
        token = selector.get("entity_token")
        if token:
            entities, exact = _token_entities(design, token, query["entity_type"], limit + 1)
            query_exact[query["id"]] = exact
            for entity in entities:
                if not _append_match(query_matches[query["id"]], entity, query["entity_type"], query.get("fields") or [], "", limit + 1):
                    query_exact[query["id"]] = False
                    break

    direct_queries = [
        query for query in queries
        if query["entity_type"] != "document"
        and not (query.get("selector") or {}).get("entity_token")
        and ((query.get("selector") or {}).get("path") or (query.get("selector") or {}).get("component_path"))
    ]
    occurrence_cache = {}

    for query in direct_queries:
        selector = query.get("selector") or {}
        entity_type = query["entity_type"]
        path = selector.get("path") or ""
        component_path = selector.get("component_path") or ""
        name = selector.get("name") or ""
        if entity_type == "occurrence" and path:
            occurrence = _navigate_occurrence_path(root, path, occurrence_cache)
            if occurrence:
                _append_match(query_matches[query["id"]], occurrence, entity_type, query.get("fields") or [], path, limit + 1)
            continue
        if path and not component_path:
            component_path, _, inferred_name = path.rpartition("/")
            name = name or inferred_name
        component_occurrence = None if component_path in ("", "root") else _navigate_occurrence_path(root, component_path, occurrence_cache)
        component = root if _same_component_path("root", component_path) or component_path == "" else _safe(lambda: component_occurrence.component)
        if entity_type == "component" and component and (not name or _safe(lambda: component.name, "") == name):
            _append_match(query_matches[query["id"]], component, entity_type, query.get("fields") or [], component_path or "root", limit + 1)
            continue
        for entity in _local_entities(component, entity_type):
            if name and _safe(lambda e=entity: e.name, "") != name:
                continue
            hint = "/".join(part for part in (component_path or "root", _safe(lambda e=entity: e.name, "")) if part)
            if not _append_match(query_matches[query["id"]], entity, entity_type, query.get("fields") or [], hint, limit + 1):
                break
            if len(query_matches[query["id"]]) >= limit + 1:
                query_exact[query["id"]] = False
                break

    scan_queries = [
        query for query in queries
        if query["entity_type"] != "document"
        and not (query.get("selector") or {}).get("entity_token")
        and not (query.get("selector") or {}).get("path")
        and not (query.get("selector") or {}).get("component_path")
    ]
    for entity_type in sorted(set(query["entity_type"] for query in scan_queries)):
        grouped = [query for query in scan_queries if query["entity_type"] == entity_type]
        for entity in _global_entities(design, entity_type):
            entity_name = _safe(lambda e=entity: e.name, "")
            all_satisfied = True
            for query in grouped:
                selector_name = (query.get("selector") or {}).get("name")
                cap = 2 if selector_name else limit + 1
                matches = query_matches[query["id"]]
                if len(matches) < cap:
                    all_satisfied = False
                    if not selector_name or selector_name == entity_name:
                        _append_match(matches, entity, entity_type, query.get("fields") or [], "", cap)
            if all_satisfied or all(len(query_matches[query["id"]]) >= (2 if (query.get("selector") or {}).get("name") else limit + 1) for query in grouped):
                for query in grouped:
                    query_exact[query["id"]] = False
                break
            if _STOP_REASON:
                break

    results = []
    selected_bbox = None
    selected_visible_bodies = 0
    fingerprint_items = []
    for query in queries:
        matches = query_matches[query["id"]]
        if _STOP_REASON and query["entity_type"] != "document":
            query_exact[query["id"]] = False
        ambiguous_unknown = bool(not query_exact[query["id"]] and len(matches) == 1)
        ambiguous = len(matches) > 1 or ambiguous_unknown
        result = {
            "query_id": query["id"],
            "matches": matches[:limit],
            "ambiguous": ambiguous,
            "ambiguity_unknown": ambiguous_unknown,
            "truncated": len(matches) > limit or not query_exact[query["id"]],
            "match_count": len(matches),
            "match_count_exact": query_exact[query["id"]],
        }
        results.append(result)
        for record in result["matches"]:
            fingerprint_items.append(record)
            if record.get("entity_type") == "body" and record.get("visible"):
                selected_visible_bodies += 1
                selected_bbox = _merge_bbox(selected_bbox, record.get("bounding_box_mm"))

    include_fingerprint = bool(_REQUEST.get("include_state_fingerprint", False))
    fingerprint_truncated = bool(_STOP_REASON or any(not value for value in query_exact.values()))
    state_fingerprint, fingerprint_truncated, fingerprint_count = _global_state_fingerprint(
        document,
        fingerprint_items,
        fingerprint_truncated,
    )
    visible_bbox = selected_bbox if include_fingerprint else None
    summary = {
        **initial_counts,
        "counts_scope": "root_plus_direct_collections",
        "visible_body_count": selected_visible_bodies,
        "visible_body_bbox_mm": visible_bbox,
        "state_fingerprint": state_fingerprint,
        "state_fingerprint_truncated": fingerprint_truncated if include_fingerprint else False,
        "state_fingerprint_items": fingerprint_count,
    }
    payload = {"success": True, "document": document, "summary": summary, "results": results, "warnings": _WARNINGS}
    print(_finalize_payload(payload))
'''
