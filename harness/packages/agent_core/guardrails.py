"""Guardrails for planner routing and safe Fusion changes."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from typing import Any


PLANNER_UNSUPPORTED_KEYWORDS = {
    "audit": "audit",
    "auditoria": "audit",
    "inspect": "inspection",
    "inspection": "inspection",
    "inspec": "inspection",
    "investigue": "inspection",
    "investigar": "inspection",
    "diagnost": "diagnostic",
    "read-only": "read_only",
    "read only": "read_only",
    "somente leitura": "read_only",
    "apenas leitura": "read_only",
    "nao modificar": "read_only",
    "não modificar": "read_only",
    "do not modify": "read_only",
    "sem modificar": "read_only",
    "hub": "hub_inventory",
    "personal library": "hub_inventory",
    "data panel": "hub_inventory",
    "inventario": "hub_inventory",
    "inventário": "hub_inventory",
    "inventory": "hub_inventory",
    "reorg": "reorg",
    "reorgan": "reorg",
    "move folder": "reorg",
    "mover pasta": "reorg",
    "cleanup": "cleanup",
    "limpeza": "cleanup",
    "clean up": "cleanup",
    "delete": "delete",
    "deletar": "delete",
    "delet": "delete",
    "remove": "delete",
    "remover": "delete",
    "apagar": "delete",
    "apague": "delete",
    "trash": "delete",
    "lixeira": "delete",
    "hidden": "hidden_cleanup",
    "oculto": "hidden_cleanup",
    "oculta": "hidden_cleanup",
    "shared definition": "shared_definition",
    "definicao compartilhada": "shared_definition",
    "definição compartilhada": "shared_definition",
}

GENERIC_PLANNER_INTENTS = {
    "create_open_box",
    "create_box",
    "create_plate",
    "create_cube",
    "edit_parameter",
    "parameter_edit",
}

GENERIC_FEATURE_TYPES = {
    "open_box",
    "box",
    "plate",
    "cube",
    "parameter_edit",
}


class PlannerUnsupportedError(ValueError):
    """Raised when a prompt should be routed away from CadSpec planning."""

    code = "unsupported_for_planner"

    def __init__(self, prompt: str, reason: str, matched_terms: list[str]) -> None:
        self.prompt = prompt
        self.reason = reason
        self.matched_terms = matched_terms
        super().__init__(reason)

    def payload(self) -> dict[str, Any]:
        """Return a JSON-safe unsupported response."""

        categories = sorted(
            {
                PLANNER_UNSUPPORTED_KEYWORDS[term]
                for term in self.matched_terms
                if term in PLANNER_UNSUPPORTED_KEYWORDS
            }
        )
        safe_only = bool(
            set(categories)
            & {
                "reorg",
                "cleanup",
                "delete",
                "hidden_cleanup",
                "shared_definition",
                "hub_inventory",
            }
        )
        return {
            "supported": False,
            "code": self.code,
            "reason": self.reason,
            "matched_terms": self.matched_terms,
            "categories": categories,
            "recommended_path": "safe_harness"
            if safe_only
            else "native_read_then_targeted_inspect",
            "recommended_tools": (
                [
                    "fusion_agent_compact_snapshot",
                    "fusion_agent_hub_inventory",
                    "fusion_agent_safe_change_preview",
                ]
                if safe_only
                else ["fusion_agent_native_read", "fusion_agent_targeted_inspect"]
            ),
        }


def planner_intent_guard(prompt: str) -> dict[str, Any] | None:
    """Return an unsupported-planner payload when a prompt is not CAD creation."""

    lowered = prompt.lower()
    matched = [term for term in PLANNER_UNSUPPORTED_KEYWORDS if term in lowered]
    if not matched:
        return None
    categories = sorted({PLANNER_UNSUPPORTED_KEYWORDS[term] for term in matched})
    return {
        "supported": False,
        "code": PlannerUnsupportedError.code,
        "matched_terms": sorted(matched),
        "categories": categories,
        "reason": (
            "This request is an audit, inventory, reorganization, read-only, or destructive-change workflow. "
            "CadSpec planning is only for known CAD creation/modeling intents."
        ),
    }


def raise_if_unsupported_for_planner(prompt: str) -> None:
    """Raise a normalized exception when the prompt belongs to a safer workflow."""

    guard = planner_intent_guard(prompt)
    if guard:
        raise PlannerUnsupportedError(prompt, guard["reason"], guard["matched_terms"])


def validate_planned_spec(prompt: str, spec: Any) -> None:
    """Reject generic fallback specs for prompts that were not specific CAD creation."""

    guard = planner_intent_guard(prompt)
    if not guard:
        return
    intent = str(getattr(spec, "intent", "")).lower()
    feature_types: set[str] = set()
    for component in getattr(spec, "components", []) or []:
        for feature in getattr(component, "features", []) or []:
            feature_types.add(str(getattr(feature, "type", "")).lower())
            feature_types.add(str(getattr(feature, "name", "")).lower())
    if intent in GENERIC_PLANNER_INTENTS or feature_types & GENERIC_FEATURE_TYPES:
        raise PlannerUnsupportedError(prompt, guard["reason"], guard["matched_terms"])


def compact_mock_snapshot(
    state: dict[str, Any],
    *,
    max_occurrences: int,
    max_bodies: int,
    max_entities_visited: int = 1000,
    max_response_bytes: int = 1024 * 1024,
) -> dict[str, Any]:
    """Build a compact snapshot from the in-memory mock inspection payload."""

    state = state.get("state", state)
    components = state.get("components", {}) if isinstance(state, dict) else {}
    bodies = state.get("bodies", {}) if isinstance(state, dict) else {}
    occurrences: list[dict[str, Any]] = []
    component_keys: set[str] = set()
    visited_entities = 0
    stop_reason: str | None = None
    for index, (name, component) in enumerate(sorted(components.items())):
        if visited_entities >= max_entities_visited:
            stop_reason = "max_entities_visited"
            break
        visited_entities += 1
        if index >= max_occurrences:
            break
        component_name = (
            component.get("name", name) if isinstance(component, dict) else name
        )
        component_keys.add(component_name)
        occurrences.append(
            _with_binding_fingerprint(
                {
                    "path": component_name,
                    "name": component_name,
                    "component": component_name,
                    "entity_token": (
                        component.get("entity_token")
                        or component.get("token")
                        or f"mock:occurrence:{component_name}"
                        if isinstance(component, dict)
                        else f"mock:occurrence:{component_name}"
                    ),
                    "visible": bool(
                        component.get("visible", True)
                        if isinstance(component, dict)
                        else True
                    ),
                    "is_root": bool(
                        component.get("is_root", False)
                        if isinstance(component, dict)
                        else False
                    ),
                    "is_referenced": bool(
                        component.get("is_referenced", False)
                        if isinstance(component, dict)
                        else False
                    ),
                    "is_imported": bool(
                        component.get("is_imported", False)
                        if isinstance(component, dict)
                        else False
                    ),
                    "shared_definition": bool(
                        component.get("shared_definition", False)
                        if isinstance(component, dict)
                        else False
                    ),
                }
            )
        )

    body_payloads: list[dict[str, Any]] = []
    body_name_counts = Counter()
    visible_body_keys: list[str] = []
    for index, (name, body) in enumerate(sorted(bodies.items())):
        if visited_entities >= max_entities_visited:
            stop_reason = "max_entities_visited"
            break
        visited_entities += 1
        if index >= max_bodies:
            break
        component_name = body.get("component", "") if isinstance(body, dict) else ""
        key = f"{component_name}/{name}" if component_name else name
        body_name_counts[name] += 1
        visible_body_keys.append(key)
        body_payloads.append(
            _with_binding_fingerprint(
                {
                    "key": key,
                    "name": name,
                    "component": component_name,
                    "entity_token": (
                        body.get("entity_token")
                        or body.get("token")
                        or f"mock:body:{key}"
                        if isinstance(body, dict)
                        else f"mock:body:{key}"
                    ),
                    "visible": bool(
                        body.get("visible", True) if isinstance(body, dict) else True
                    ),
                    "is_root": bool(
                        body.get("is_root", False) if isinstance(body, dict) else False
                    ),
                    "is_referenced": bool(
                        body.get("is_referenced", False)
                        if isinstance(body, dict)
                        else False
                    ),
                    "is_imported": bool(
                        body.get("is_imported", False)
                        if isinstance(body, dict)
                        else False
                    ),
                    "shared_definition": bool(
                        body.get("shared_definition", False)
                        if isinstance(body, dict)
                        else False
                    ),
                    "bbox_mm": body.get("bounding_box_mm", [])
                    if isinstance(body, dict)
                    else [],
                }
            )
        )
    if stop_reason is None and len(components) > max_occurrences:
        stop_reason = "max_occurrences"
    if stop_reason is None and len(bodies) > max_bodies:
        stop_reason = "max_bodies"
    duplicate_names = {
        name: count for name, count in body_name_counts.items() if count > 1
    }
    visible_occurrence_paths = [
        item["path"] for item in occurrences if item.get("visible")
    ]
    visible_component_keys = sorted(component_keys)
    snapshot = {
        "schema_version": "compact_snapshot.v2",
        "schema_compatibility": ["compact_snapshot.v1"],
        "source": "mock",
        "document": {
            "identity_kind": "mock_session",
            "stable_id": "mock:active-design",
        },
        "payload_capped": stop_reason is not None
        or len(components) > max_occurrences
        or len(bodies) > max_bodies,
        "counts": {
            "components_total": len(components),
            "occurrences_total": len(components),
            "bodies_total": len(bodies),
            "visible_occurrences": len(visible_occurrence_paths),
            "visible_bodies": len(visible_body_keys),
            "visible_components": len(visible_component_keys),
        },
        "occurrences": occurrences,
        "bodies": body_payloads,
        "visible_occurrence_paths": visible_occurrence_paths,
        "visible_body_keys": visible_body_keys,
        "visible_component_keys": visible_component_keys,
        "visible_body_bbox_mm": _union_body_bbox(body_payloads),
        "duplicate_body_names": duplicate_names,
        "duplicate_name_warnings": _duplicate_name_warnings(duplicate_names),
        "complete": stop_reason is None,
        "truncated": stop_reason is not None
        or len(components) > max_occurrences
        or len(bodies) > max_bodies,
        "visited_entities": visited_entities,
        "elapsed_ms": 0,
        "response_bytes": 0,
        "counts_exact": stop_reason is None,
        "stop_reason": stop_reason,
        "snapshot_hash": snapshot_hash(
            {
                "visible_occurrence_paths": visible_occurrence_paths,
                "visible_body_keys": visible_body_keys,
                "visible_component_keys": visible_component_keys,
            }
        ),
    }
    snapshot["response_bytes"] = len(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    if snapshot["response_bytes"] > max_response_bytes:
        snapshot["complete"] = False
        snapshot["truncated"] = True
        snapshot["counts_exact"] = False
        snapshot["stop_reason"] = "max_response_bytes"
        snapshot["payload_capped"] = True
        while snapshot["response_bytes"] > max_response_bytes:
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
                break
            snapshot["response_bytes"] = len(
                json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode(
                    "utf-8"
                )
            )
    return snapshot


def snapshot_document_identity(snapshot: dict[str, Any]) -> dict[str, str]:
    """Return a stable document identity without falling back to its display name."""

    document = snapshot.get("document") if isinstance(snapshot, dict) else None
    if not isinstance(document, dict):
        return {}
    data_file_id = str(document.get("id") or "").strip()
    if data_file_id:
        return {"kind": "data_file", "stable_id": data_file_id}
    runtime_id = str(
        document.get("stable_id")
        or document.get("runtime_id")
        or document.get("unsaved_session_id")
        or ""
    ).strip()
    if runtime_id:
        return {
            "kind": str(document.get("identity_kind") or "unsaved_session"),
            "stable_id": runtime_id,
        }
    return {}


def canonical_snapshot_fingerprint(snapshot: dict[str, Any]) -> str | None:
    """Hash mutation-relevant snapshot state while excluding volatile diagnostics."""

    identity = snapshot_document_identity(snapshot)
    if not identity:
        return None

    def occurrence(item: Any) -> dict[str, Any]:
        value = item if isinstance(item, dict) else {}
        return {
            "path": value.get("path"),
            "name": value.get("name"),
            "component": value.get("component"),
            "entity_token": value.get("entity_token") or value.get("token"),
            "visible": value.get("visible"),
            "bbox_mm": value.get("bbox_mm"),
            "transform": value.get("transform"),
            "is_root": value.get("is_root"),
            "is_referenced": value.get("is_referenced"),
            "is_imported": value.get("is_imported"),
            "shared_definition": value.get("shared_definition"),
            "binding_fingerprint": value.get("binding_fingerprint"),
        }

    def body(item: Any) -> dict[str, Any]:
        value = item if isinstance(item, dict) else {}
        return {
            "key": value.get("key"),
            "name": value.get("name"),
            "component": value.get("component"),
            "component_key": value.get("component_key"),
            "entity_token": value.get("entity_token") or value.get("token"),
            "visible": value.get("visible"),
            "bbox_mm": value.get("bbox_mm"),
            "is_root": value.get("is_root"),
            "is_referenced": value.get("is_referenced"),
            "is_imported": value.get("is_imported"),
            "shared_definition": value.get("shared_definition"),
            "binding_fingerprint": value.get("binding_fingerprint"),
        }

    payload = {
        "document": identity,
        "counts": snapshot.get("counts") or {},
        "occurrences": sorted(
            (occurrence(item) for item in snapshot.get("occurrences") or []),
            key=lambda item: (
                str(item.get("path") or ""),
                str(item.get("entity_token") or ""),
            ),
        ),
        "bodies": sorted(
            (body(item) for item in snapshot.get("bodies") or []),
            key=lambda item: (
                str(item.get("key") or ""),
                str(item.get("entity_token") or ""),
            ),
        ),
        "duplicate_body_names": snapshot.get("duplicate_body_names") or {},
        "visible_occurrence_paths": sorted(
            snapshot.get("visible_occurrence_paths") or []
        ),
        "visible_body_keys": sorted(snapshot.get("visible_body_keys") or []),
        "visible_component_keys": sorted(snapshot.get("visible_component_keys") or []),
        "visible_body_bbox_mm": snapshot.get("visible_body_bbox_mm"),
        "complete": snapshot.get("complete"),
        "counts_exact": snapshot.get("counts_exact"),
        "truncated": snapshot.get("truncated"),
    }
    return snapshot_hash(payload)


def bind_safe_change_targets(
    targets: list[dict[str, Any]],
    snapshot: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Resolve every target to one stable snapshot entity before mutation."""

    bodies = [item for item in snapshot.get("bodies") or [] if isinstance(item, dict)]
    occurrences = [
        item for item in snapshot.get("occurrences") or [] if isinstance(item, dict)
    ]
    bindings: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    seen_entities: set[tuple[str, str]] = set()

    for index, target in enumerate(targets):
        kind = str(target.get("kind") or target.get("type") or "body").lower()
        if kind in {"body", "brepbody"}:
            target_token = str(target.get("entity_token") or target.get("token") or "")
            target_key = str(target.get("body_key") or target.get("key") or "")
            target_name = str(
                target.get("name") or target.get("body") or target.get("target") or ""
            )
            target_component = str(
                target.get("component") or target.get("component_path") or ""
            )
            matches = [
                item
                for item in bodies
                if (
                    target_token
                    and target_token
                    == str(item.get("entity_token") or item.get("token") or "")
                )
                or (target_key and target_key == str(item.get("key") or ""))
                or (
                    not target_token
                    and not target_key
                    and target_name
                    and target_name == str(item.get("name") or "")
                    and (
                        not target_component
                        or target_component == str(item.get("component") or "")
                    )
                )
            ]
            normalized_kind = "body"
            identifier = (
                str(
                    matches[0].get("entity_token")
                    or matches[0].get("token")
                    or matches[0].get("key")
                    or ""
                )
                if len(matches) == 1
                else target_token or target_key or f"{target_component}/{target_name}"
            )
        elif kind == "occurrence":
            target_token = str(target.get("entity_token") or target.get("token") or "")
            target_path = str(target.get("path") or target.get("occurrence_path") or "")
            matches = [
                item
                for item in occurrences
                if (
                    target_token
                    and target_token
                    == str(item.get("entity_token") or item.get("token") or "")
                )
                or (target_path and target_path == str(item.get("path") or ""))
            ]
            normalized_kind = "occurrence"
            identifier = (
                str(
                    matches[0].get("entity_token")
                    or matches[0].get("token")
                    or matches[0].get("path")
                    or ""
                )
                if len(matches) == 1
                else target_token or target_path
            )
        else:
            matches = []
            normalized_kind = kind
            identifier = ""

        if len(matches) != 1:
            errors.append(
                {
                    "target_index": index,
                    "kind": normalized_kind,
                    "match_count": len(matches),
                    "reason": "target_must_match_exactly_one_entity",
                }
            )
            continue
        entity_key = (normalized_kind, identifier)
        if entity_key in seen_entities:
            errors.append(
                {
                    "target_index": index,
                    "kind": normalized_kind,
                    "match_count": 1,
                    "reason": "duplicate_target_binding",
                }
            )
            continue
        seen_entities.add(entity_key)
        match = matches[0]
        bindings.append(
            {
                "target_index": index,
                "kind": normalized_kind,
                "identifier": identifier,
                "entity_token": match.get("entity_token") or match.get("token"),
                "path": match.get("path"),
                "key": match.get("key"),
                "component": match.get("component"),
                "name": match.get("name"),
                "visible": match.get("visible"),
                "is_root": match.get("is_root"),
                "is_referenced": match.get("is_referenced"),
                "is_imported": match.get("is_imported"),
                "shared_definition": match.get("shared_definition"),
                "binding_fingerprint": match.get("binding_fingerprint"),
            }
        )
    return bindings, errors


def classify_safe_change(
    operation: str,
    targets: list[dict[str, Any]],
    policy: dict[str, Any] | None = None,
    snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify intended changes before any real Fusion mutation."""

    policy = policy or {}
    operation = operation.lower()
    duplicate_warnings = ambiguous_target_warnings(targets, snapshot or {})
    delete_risks = [_bound_delete_risk(target) for target in targets]
    missing_delete_facts = [
        risk for risk in delete_risks if risk["facts_complete"] is False
    ]
    has_shared_or_hidden = any(bool(risk["dangerous"]) for risk in delete_risks)
    result: dict[str, Any] = {
        "operation": operation,
        "target_count": len(targets),
        "allow_apply": False,
        "blocked": False,
        "risk_level": "unknown",
        "classification": "unknown",
        "requires_confirm_destructive": False,
        "requires_baseline": True,
        "blocked_by_default": False,
        "ambiguous_target_warnings": duplicate_warnings,
        "reasons": [],
    }
    if not targets:
        result["blocked"] = True
        result["classification"] = "empty_targets"
        result["risk_level"] = "high"
        result["reasons"].append("At least one explicitly scoped target is required.")
        return result
    if duplicate_warnings:
        result["blocked"] = True
        result["classification"] = "ambiguous_targets"
        result["risk_level"] = "high"
        result["reasons"].append(
            "Duplicate or unscoped target names are ambiguous; provide component-scoped paths."
        )
        return result
    if operation == "move":
        result.update(
            {
                "allow_apply": False,
                "blocked": False,
                "risk_level": "medium",
                "classification": "reversible_move",
                "reasons": [
                    "Move operations are reversible but still require post-change snapshot verification."
                ],
            }
        )
        return result
    if operation == "visibility":
        result.update(
            {
                "allow_apply": True,
                "blocked": False,
                "risk_level": "low",
                "classification": "reversible_visibility",
                "reasons": [
                    "Visibility changes are reversible and can be checked with visible-path diffs."
                ],
            }
        )
        return result
    if operation == "componentize":
        result.update(
            {
                "allow_apply": False,
                "blocked": True,
                "risk_level": "high",
                "classification": "destructive/shared-definition risk",
                "reasons": [
                    "Componentization can alter shared definitions and must be implemented as a specialized workflow."
                ],
            }
        )
        return result
    if operation == "delete":
        result["requires_confirm_destructive"] = True
        result["classification"] = "destructive/shared-definition risk"
        result["risk_level"] = "critical"
        if not policy.get("allow_delete", False):
            result["blocked"] = True
            result["blocked_by_default"] = True
            result["reasons"].append(
                "Deletes are blocked by default; set policy.allow_delete=true after preview review."
            )
        if missing_delete_facts:
            result["blocked"] = True
            result["blocked_by_default"] = True
            result["reasons"].append(
                "Delete target binding facts are incomplete; bind exact visibility, root, reference, import, shared-definition, and fingerprint facts."
            )
        if has_shared_or_hidden:
            result["blocked"] = True
            result["blocked_by_default"] = True
            result["reasons"].append(
                "Hidden/imported/shared-definition targets are blocked by default."
            )
        if not result["blocked"]:
            result["allow_apply"] = True
            result["reasons"].append(
                "Delete preview is high risk and requires confirm_destructive=true with batch_size<=5."
            )
        return result
    result["blocked"] = True
    result["classification"] = "unsupported_operation"
    result["risk_level"] = "high"
    result["reasons"].append(f"Unsupported safe-change operation: {operation}")
    return result


def diff_snapshots(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Compare compact snapshots and flag visible loss."""

    before_view = _snapshot_view(before)
    after_view = _snapshot_view(after)
    missing_occurrences = sorted(
        before_view["visible_occurrence_paths"] - after_view["visible_occurrence_paths"]
    )
    missing_bodies = sorted(
        before_view["visible_body_keys"] - after_view["visible_body_keys"]
    )
    missing_components = sorted(
        before_view["visible_component_keys"] - after_view["visible_component_keys"]
    )
    before_counts = before_view["counts"]
    after_counts = after_view["counts"]
    count_regressions = {
        key: {"before": before_counts.get(key, 0), "after": after_counts.get(key, 0)}
        for key in ("visible_occurrences", "visible_bodies", "visible_components")
        if int(after_counts.get(key, 0)) < int(before_counts.get(key, 0))
    }
    bbox_shrank = _bbox_shrank(
        before_view.get("visible_body_bbox_mm"), after_view.get("visible_body_bbox_mm")
    )
    negative_impact = bool(
        missing_occurrences
        or missing_bodies
        or missing_components
        or count_regressions
        or bbox_shrank
    )
    globally_complete = _complete_global_snapshot(before) and _complete_global_snapshot(
        after
    )
    drift_conclusion = (
        "drift_detected"
        if negative_impact
        else "no_drift_in_complete_global_fingerprint"
        if globally_complete
        else "no_drift_in_observed_scope"
    )
    return {
        "negative_impact": negative_impact,
        "drift_conclusion": drift_conclusion,
        "global_fingerprint_complete": globally_complete,
        "visible_occurrences_missing": missing_occurrences,
        "visible_bodies_missing": missing_bodies,
        "visible_component_keys_missing": missing_components,
        "visible_count_regressions": count_regressions,
        "visible_body_bbox_before": before_view.get("visible_body_bbox_mm"),
        "visible_body_bbox_after": after_view.get("visible_body_bbox_mm"),
        "visible_body_bbox_shrank": bbox_shrank,
    }


def _complete_global_snapshot(snapshot: dict[str, Any]) -> bool:
    return bool(
        snapshot.get("complete") is True
        and snapshot.get("counts_exact") is True
        and not snapshot.get("truncated", False)
        and not snapshot.get("payload_capped", False)
        and not snapshot.get("stop_reason")
    )


def ambiguous_target_warnings(
    targets: list[dict[str, Any]], snapshot: dict[str, Any]
) -> list[dict[str, Any]]:
    """Return warnings for duplicate body names used without component scoping."""

    duplicates = (
        snapshot.get("duplicate_body_names") if isinstance(snapshot, dict) else {}
    )
    if not isinstance(duplicates, dict) or not duplicates:
        return []
    warnings: list[dict[str, Any]] = []
    for target in targets:
        name = str(
            target.get("name") or target.get("body") or target.get("target") or ""
        )
        scoped = any(
            target.get(key)
            for key in (
                "path",
                "component",
                "component_path",
                "occurrence_path",
                "body_key",
            )
        )
        if name and name in duplicates and not scoped:
            warnings.append(
                {
                    "target": name,
                    "duplicate_count": duplicates[name],
                    "reason": "target name is not component-scoped",
                }
            )
    return warnings


def snapshot_hash(payload: dict[str, Any]) -> str:
    """Return a stable short hash for visible snapshot identity."""

    encoded = repr(_sorted_jsonish(payload)).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _snapshot_view(snapshot: dict[str, Any]) -> dict[str, Any]:
    snapshot = snapshot.get("snapshot", snapshot) if isinstance(snapshot, dict) else {}
    visible_occurrence_paths = set(snapshot.get("visible_occurrence_paths") or [])
    visible_body_keys = set(snapshot.get("visible_body_keys") or [])
    visible_component_keys = set(snapshot.get("visible_component_keys") or [])
    if not visible_occurrence_paths:
        visible_occurrence_paths = {
            str(item.get("path") or item.get("name"))
            for item in snapshot.get("occurrences", [])
            if isinstance(item, dict) and item.get("visible", True)
        }
    if not visible_body_keys:
        visible_body_keys = {
            str(
                item.get("key")
                or f"{item.get('component', '')}/{item.get('name', '')}".strip("/")
            )
            for item in snapshot.get("bodies", [])
            if isinstance(item, dict) and item.get("visible", True)
        }
    if not visible_component_keys:
        visible_component_keys = {
            str(item.get("component") or item.get("name"))
            for item in snapshot.get("occurrences", [])
            if isinstance(item, dict) and item.get("visible", True)
        }
    counts = dict(snapshot.get("counts") or {})
    counts.setdefault("visible_occurrences", len(visible_occurrence_paths))
    counts.setdefault("visible_bodies", len(visible_body_keys))
    counts.setdefault("visible_components", len(visible_component_keys))
    return {
        "visible_occurrence_paths": visible_occurrence_paths,
        "visible_body_keys": visible_body_keys,
        "visible_component_keys": visible_component_keys,
        "counts": counts,
        "visible_body_bbox_mm": snapshot.get("visible_body_bbox_mm"),
    }


def _bbox_shrank(before: Any, after: Any) -> bool:
    if not isinstance(before, dict) or not isinstance(after, dict):
        return False
    before_size = before.get("size_mm")
    after_size = after.get("size_mm")
    if (
        not isinstance(before_size, list)
        or not isinstance(after_size, list)
        or len(before_size) != len(after_size)
    ):
        return False
    return any(
        float(after_value) + 0.01 < float(before_value)
        for before_value, after_value in zip(before_size, after_size, strict=False)
    )


def _bound_delete_risk(target: dict[str, Any]) -> dict[str, Any]:
    facts = (
        target.get("visible"),
        target.get("is_root"),
        target.get("is_referenced"),
        target.get("is_imported"),
        target.get("shared_definition"),
    )
    facts_complete = bool(
        target.get("identifier")
        and target.get("binding_fingerprint")
        and all(isinstance(value, bool) for value in facts)
    )
    dangerous = bool(
        facts_complete
        and (
            target.get("visible") is False
            or target.get("is_root") is True
            or target.get("is_referenced") is True
            or target.get("is_imported") is True
            or target.get("shared_definition") is True
        )
    )
    return {"facts_complete": facts_complete, "dangerous": dangerous}


def _with_binding_fingerprint(item: dict[str, Any]) -> dict[str, Any]:
    binding_payload = {
        key: item.get(key)
        for key in (
            "key",
            "path",
            "component",
            "name",
            "entity_token",
            "visible",
            "is_root",
            "is_referenced",
            "is_imported",
            "shared_definition",
        )
    }
    return {
        **item,
        "binding_fingerprint": snapshot_hash(binding_payload),
    }


def _union_body_bbox(bodies: list[dict[str, Any]]) -> dict[str, Any] | None:
    min_point: list[float] | None = None
    max_point: list[float] | None = None
    for body in bodies:
        bbox = body.get("bbox_mm")
        if isinstance(bbox, dict):
            body_min = bbox.get("min_mm")
            body_max = bbox.get("max_mm")
        elif isinstance(bbox, list) and len(bbox) == 3:
            body_min = [0.0, 0.0, 0.0]
            body_max = [float(value) for value in bbox]
        else:
            continue
        if not isinstance(body_min, list) or not isinstance(body_max, list):
            continue
        min_point = [
            min(a, float(b))
            for a, b in zip(min_point or body_min, body_min, strict=False)
        ]
        max_point = [
            max(a, float(b))
            for a, b in zip(max_point or body_max, body_max, strict=False)
        ]
    if min_point is None or max_point is None:
        return None
    size = [round(abs(a - b), 6) for a, b in zip(max_point, min_point, strict=False)]
    center = [
        round((a + b) / 2.0, 6) for a, b in zip(max_point, min_point, strict=False)
    ]
    return {
        "min_mm": min_point,
        "max_mm": max_point,
        "size_mm": size,
        "center_mm": center,
    }


def _duplicate_name_warnings(duplicate_names: dict[str, int]) -> list[str]:
    return [
        f"Body name '{name}' appears {count} times; target by component/body key."
        for name, count in sorted(duplicate_names.items())
    ]


def _sorted_jsonish(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _sorted_jsonish(value[key]) for key in sorted(value)}
    if isinstance(value, set):
        return sorted(_sorted_jsonish(item) for item in value)
    if isinstance(value, list | tuple):
        return [_sorted_jsonish(item) for item in value]
    return value


def normalize_operation(value: str) -> str:
    """Normalize the public operation enum."""

    value = value.strip().lower()
    if not re.fullmatch(r"[a-z_]+", value):
        raise ValueError(
            "operation must be one of move, delete, visibility, componentize"
        )
    if value not in {"move", "delete", "visibility", "componentize"}:
        raise ValueError(
            "operation must be one of move, delete, visibility, componentize"
        )
    return value
