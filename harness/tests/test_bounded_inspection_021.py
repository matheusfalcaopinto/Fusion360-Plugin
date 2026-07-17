from __future__ import annotations

import asyncio
import contextlib
import io
import json
import math
import sys
import types
from pathlib import Path

import pytest

from agent_core.fusion_scripts import (
    compact_snapshot_script,
    hub_inventory_script,
    safe_delete_apply_script,
    safe_visibility_apply_script,
)
from agent_core.guardrails import (
    bind_safe_change_targets,
    canonical_snapshot_fingerprint,
    classify_safe_change,
    compact_mock_snapshot,
)
from agent_core.session_controller import (
    SessionController,
    SessionOptions,
    _safe_change_transport_fields,
    _safe_change_verification,
    _safe_change_preview_digest,
    _snapshot_is_complete,
)
from agent_core.targeted_inspection import (
    build_targeted_inspection_script,
    validate_inspection_payload,
)


def _query_payload(**overrides):
    payload = {
        "queries": [
            {
                "id": "body",
                "entity_type": "body",
                "selector": {"component_path": "root", "name": "Body1"},
                "fields": ["exists", "bounding_box_mm"],
            }
        ]
    }
    payload.update(overrides)
    return payload


def test_targeted_inspection_budget_defaults_and_limits() -> None:
    normalized = validate_inspection_payload(_query_payload())
    assert normalized["max_entities_visited"] == 1000
    assert normalized["deadline_ms"] == 1500
    assert normalized["max_response_bytes"] == 1024 * 1024

    bounded = validate_inspection_payload(
        _query_payload(
            max_entities_visited=5000, deadline_ms=5000, max_response_bytes=4096
        )
    )
    assert bounded["max_entities_visited"] == 5000
    assert bounded["deadline_ms"] == 5000
    assert bounded["max_response_bytes"] == 4096

    for field, value in (
        ("max_entities_visited", 5001),
        ("deadline_ms", 49),
        ("max_response_bytes", 1024 * 1024 + 1),
    ):
        with pytest.raises(ValueError, match=field):
            validate_inspection_payload(_query_payload(**{field: value}))


def test_targeted_script_is_budgeted_and_uses_shared_precedence_paths() -> None:
    script = build_targeted_inspection_script(
        {
            "queries": [
                {"id": "doc", "entity_type": "document"},
                {
                    "id": "token",
                    "entity_type": "body",
                    "selector": {"entity_token": "abc"},
                },
                {
                    "id": "path",
                    "entity_type": "body",
                    "selector": {"path": "Arm:1/Body1"},
                },
                {"id": "name-a", "entity_type": "body", "selector": {"name": "Body1"}},
                {"id": "name-b", "entity_type": "body", "selector": {"name": "Body2"}},
            ]
        }
    )
    compile(script, "<bounded-targeted-inspection>", "exec")
    assert "_MAX_ENTITIES" in script
    assert "_DEADLINE_MS" in script
    assert "_MAX_RESPONSE_BYTES" in script
    assert "_navigate_occurrence_path" in script
    assert "childOccurrences" in script
    assert "allOccurrences" not in script
    assert "for entity_type in sorted(set(" in script
    assert '"complete"' in script
    assert '"visited_entities"' in script
    assert '"counts_exact"' in script
    assert '"stop_reason"' in script
    assert "physicalProperties" not in script


def test_response_trimming_marks_payload_incomplete_and_invalidates_fingerprint(
    monkeypatch,
) -> None:
    class EmptyCollection:
        count = 0

        def item(self, _index):
            raise AssertionError("empty collection must not be indexed")

        def itemByName(self, _name):
            return None

    empty = EmptyCollection()

    class Attributes:
        @staticmethod
        def itemByName(_group, _name):
            return None

    class Root:
        attributes = Attributes()
        occurrences = empty
        bRepBodies = empty
        sketches = empty
        features = empty

    root = Root()

    class Design:
        rootComponent = root
        allComponents = empty
        allParameters = empty

    design = Design()

    class Products:
        @staticmethod
        def itemByProductType(_product_type):
            return design

    class Product:
        productType = "DesignProductType"

    class Document:
        name = "D" * 20_000
        dataFile = None
        isModified = False
        products = Products()
        product = Product()

    class ApplicationInstance:
        activeDocument = Document()
        activeProduct = design

    adsk = types.ModuleType("adsk")
    adsk.__path__ = []  # type: ignore[attr-defined]
    core = types.ModuleType("adsk.core")
    fusion = types.ModuleType("adsk.fusion")
    core.Application = type(
        "Application", (), {"get": staticmethod(lambda: ApplicationInstance())}
    )
    fusion.Design = type(
        "FusionDesign", (), {"cast": staticmethod(lambda value: value)}
    )
    adsk.core = core
    adsk.fusion = fusion
    monkeypatch.setitem(sys.modules, "adsk", adsk)
    monkeypatch.setitem(sys.modules, "adsk.core", core)
    monkeypatch.setitem(sys.modules, "adsk.fusion", fusion)

    script = build_targeted_inspection_script(
        {
            "queries": [{"id": "document", "entity_type": "document"}],
            "include_state_fingerprint": True,
            "deadline_ms": 50,
            "max_response_bytes": 4096,
        }
    )
    namespace: dict[str, object] = {}
    exec(compile(script, "<trimmed-targeted-inspection>", "exec"), namespace)
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        namespace["run"]("")  # type: ignore[index,operator]
    encoded = output.getvalue().strip()
    payload = json.loads(encoded)

    assert len(encoded.encode("utf-8")) <= 4096
    assert payload["complete"] is False
    assert payload["truncated"] is True
    assert payload["counts_exact"] is False
    assert payload["stop_reason"] == "response_limit"
    assert payload["summary"]["state_fingerprint"] is None
    assert payload["summary"]["state_fingerprint_truncated"] is True
    assert payload["results"][0]["match_count"] == len(payload["results"][0]["matches"])
    assert payload["results"][0]["match_count_exact"] is False


def test_compact_snapshot_v2_preserves_v1_keys_and_hard_budgets() -> None:
    script = compact_snapshot_script(
        {
            "max_entities_visited": 1000,
            "deadline_ms": 1500,
            "max_response_bytes": 1024 * 1024,
        }
    )
    compile(script, "<compact-snapshot-v2>", "exec")
    assert '"schema_version": "compact_snapshot.v2"' in script
    for legacy_key in (
        "payload_capped",
        "counts",
        "occurrences",
        "bodies",
        "visible_occurrence_paths",
        "visible_body_keys",
        "visible_component_keys",
        "visible_body_bbox_mm",
        "snapshot_hash",
    ):
        assert f'"{legacy_key}"' in script
    for metadata_key in (
        "complete",
        "truncated",
        "visited_entities",
        "elapsed_ms",
        "response_bytes",
        "counts_exact",
        "stop_reason",
    ):
        assert f'"{metadata_key}"' in script
    assert "str(exc)" not in script
    assert "BOUNDING_BOX_UNAVAILABLE" in script


def test_compact_snapshot_visibility_read_failure_is_incomplete_not_visible() -> None:
    script = compact_snapshot_script({})

    assert 'stop("visibility_unavailable")' in script
    assert "def _visible(entity):" in script
    assert "def _component_visible(component):" in script
    assert "except Exception:\n        return True" not in script


def test_successful_read_scripts_expose_codes_not_raw_exception_text() -> None:
    script = hub_inventory_script({"query": "", "max_results": 5})
    compile(script, "<hub-inventory>", "exec")
    assert "str(exc)" not in script
    assert "PROJECT_METADATA_UNAVAILABLE" in script
    assert "OPEN_DOCUMENTS_UNAVAILABLE" in script


def test_mock_snapshot_stops_at_entity_budget_and_is_not_a_safe_baseline() -> None:
    state = {
        "state": {
            "components": {
                f"Component{i}": {"name": f"Component{i}"} for i in range(20)
            },
            "bodies": {f"Body{i}": {"component": "root"} for i in range(20)},
        }
    }
    snapshot = compact_mock_snapshot(
        state,
        max_occurrences=100,
        max_bodies=100,
        max_entities_visited=5,
        max_response_bytes=1024 * 1024,
    )
    assert snapshot["schema_version"] == "compact_snapshot.v2"
    assert snapshot["visited_entities"] == 5
    assert snapshot["complete"] is False
    assert snapshot["counts_exact"] is False
    assert snapshot["stop_reason"] == "max_entities_visited"
    assert _snapshot_is_complete(snapshot) is False


def test_safe_change_preview_blocks_an_incomplete_baseline(tmp_path: Path) -> None:
    controller = SessionController(real_client=object())
    snapshot_path = tmp_path / "before.json"
    snapshot = {
        "schema_version": "compact_snapshot.v2",
        "complete": False,
        "truncated": True,
        "counts_exact": False,
        "stop_reason": "max_entities_visited",
        "payload_capped": True,
    }
    snapshot_path.write_text(json.dumps({"snapshot": snapshot}), encoding="utf-8")

    async def incomplete_snapshot(**_kwargs):
        return {
            "snapshot_id": "before_test",
            "snapshot_path": str(snapshot_path),
            "snapshot": snapshot,
        }

    controller.compact_snapshot = incomplete_snapshot  # type: ignore[method-assign]
    options = SessionOptions(mode="mock", project="bounded", output_dir=tmp_path)
    result = asyncio.run(
        controller.safe_change_preview(
            project="bounded",
            mode="mock",
            operation="visibility",
            targets=[{"kind": "body", "name": "Body1", "visible": False}],
            options=options,
        )
    )
    assert result["blocked"] is True
    assert result["baseline_complete"] is False
    assert result["classification"]["classification"] == "incomplete_baseline"


def test_safe_change_incomplete_readback_is_applied_unverified(tmp_path: Path) -> None:
    controller = SessionController(real_client=object())
    preview_dir = tmp_path / "safe_change_previews"
    preview_dir.mkdir()
    before_path = tmp_path / "before.json"
    before_snapshot = {
        "schema_version": "compact_snapshot.v2",
        "complete": True,
        "truncated": False,
        "counts_exact": True,
        "stop_reason": None,
        "payload_capped": False,
        "counts": {"bodies_total": 1, "visible_bodies": 1},
        "document": {"identity_kind": "mock_session", "stable_id": "mock:test"},
        "bodies": [
            {
                "key": "root/Body1",
                "name": "Body1",
                "component": "root",
                "entity_token": "body-1",
                "visible": True,
                "is_root": True,
                "is_referenced": False,
                "is_imported": False,
                "shared_definition": False,
                "binding_fingerprint": "bounded-body-proof",
            }
        ],
        "occurrences": [],
        "visible_occurrence_paths": [],
        "visible_body_keys": ["root/Body1"],
        "visible_component_keys": ["root"],
        "duplicate_body_names": {},
    }
    before_path.write_text(json.dumps({"snapshot": before_snapshot}), encoding="utf-8")
    preview_id = "preview_bounded"
    targets = [{"kind": "body", "name": "Body1", "component": "root", "visible": False}]
    bindings, errors = bind_safe_change_targets(targets, before_snapshot)
    assert not errors
    preview_payload = {
        "schema_version": "safe_change_preview.v2",
        "preview_id": preview_id,
        "preview_status": "ready",
        "project": "bounded",
        "mode": "mock",
        "operation": "visibility",
        "targets": targets,
        "policy": {},
        "classification": {"blocked": False},
        "before_snapshot_path": str(before_path),
        "document_identity": {"kind": "mock_session", "stable_id": "mock:test"},
        "state_fingerprint": canonical_snapshot_fingerprint(before_snapshot),
        "bound_targets": bindings,
        "inspection_budget": {},
        "requirements": [],
    }
    preview_payload["preview_digest"] = _safe_change_preview_digest(preview_payload)
    (preview_dir / f"{preview_id}.json").write_text(
        json.dumps(preview_payload),
        encoding="utf-8",
    )
    after_path = tmp_path / "after.json"
    incomplete_after = {
        **before_snapshot,
        "complete": False,
        "truncated": True,
        "counts_exact": False,
        "stop_reason": "deadline_ms",
    }

    calls = 0

    async def incomplete_readback(**_kwargs):
        nonlocal calls
        calls += 1
        snapshot = before_snapshot if calls == 1 else incomplete_after
        return {
            "snapshot_id": f"snapshot_{calls}",
            "snapshot_path": str(after_path),
            "snapshot": snapshot,
        }

    controller.compact_snapshot = incomplete_readback  # type: ignore[method-assign]
    result = asyncio.run(
        controller.safe_change_apply(
            project="bounded",
            mode="mock",
            preview_id=preview_id,
            batch_size=1,
            confirm_destructive=False,
            options=SessionOptions(mode="mock", project="bounded", output_dir=tmp_path),
        )
    )
    assert result["status"] == "applied_unverified"
    assert result["abort_reason"] == "incomplete_readback"
    assert result["verification_complete"] is False
    assert "Do not save" in result["recovery_instructions"]


def _complete_safe_snapshot(*, visible: bool = True) -> dict:
    return {
        "schema_version": "compact_snapshot.v2",
        "complete": True,
        "truncated": False,
        "counts_exact": True,
        "stop_reason": None,
        "payload_capped": False,
        "counts": {"bodies_total": 1, "visible_bodies": int(visible)},
        "document": {"identity_kind": "mock_session", "stable_id": "mock:safe-change"},
        "bodies": [
            {
                "key": "root/Body1",
                "name": "Body1",
                "component": "root",
                "entity_token": "body-token",
                "visible": visible,
                "is_root": True,
                "is_referenced": False,
                "is_imported": False,
                "shared_definition": False,
                "binding_fingerprint": "safe-body-proof",
            }
        ],
        "occurrences": [],
        "visible_occurrence_paths": [],
        "visible_body_keys": ["root/Body1"] if visible else [],
        "visible_component_keys": ["root"] if visible else [],
        "duplicate_body_names": {},
    }


@pytest.mark.asyncio
async def test_safe_change_preview_v2_binds_identity_state_and_targets(
    tmp_path: Path,
) -> None:
    controller = SessionController(real_client=object())
    snapshot = _complete_safe_snapshot()
    snapshot_path = tmp_path / "baseline.json"
    snapshot_path.write_text(json.dumps({"snapshot": snapshot}), encoding="utf-8")

    async def baseline(**_kwargs):
        return {
            "snapshot_id": "before",
            "snapshot_path": str(snapshot_path),
            "snapshot": snapshot,
        }

    controller.compact_snapshot = baseline  # type: ignore[method-assign]
    result = await controller.safe_change_preview(
        project="safe",
        mode="mock",
        operation="visibility",
        targets=[
            {"kind": "body", "component": "root", "name": "Body1", "visible": False}
        ],
        options=SessionOptions(mode="mock", project="safe", output_dir=tmp_path),
    )

    assert result["schema_version"] == "safe_change_preview.v2"
    assert result["preview_status"] == "ready"
    assert result["document_identity"]["stable_id"] == "mock:safe-change"
    assert result["state_fingerprint"] == canonical_snapshot_fingerprint(snapshot)
    assert result["bound_targets"][0]["entity_token"] == "body-token"


@pytest.mark.asyncio
async def test_safe_change_apply_marks_preview_stale_before_dispatch_on_drift(
    tmp_path: Path,
) -> None:
    controller = SessionController(real_client=object())
    baseline_snapshot = _complete_safe_snapshot()
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(
        json.dumps({"snapshot": baseline_snapshot}), encoding="utf-8"
    )

    async def baseline(**_kwargs):
        return {
            "snapshot_id": "before",
            "snapshot_path": str(baseline_path),
            "snapshot": baseline_snapshot,
        }

    controller.compact_snapshot = baseline  # type: ignore[method-assign]
    preview = await controller.safe_change_preview(
        project="safe",
        mode="mock",
        operation="visibility",
        targets=[
            {"kind": "body", "component": "root", "name": "Body1", "visible": False}
        ],
        options=SessionOptions(mode="mock", project="safe", output_dir=tmp_path),
    )
    drifted = {
        **baseline_snapshot,
        "counts": {**baseline_snapshot["counts"], "features": 1},
    }

    async def preapply(**_kwargs):
        return {
            "snapshot_id": "preapply",
            "snapshot_path": str(tmp_path / "preapply.json"),
            "snapshot": drifted,
        }

    controller.compact_snapshot = preapply  # type: ignore[method-assign]
    result = await controller.safe_change_apply(
        project="safe",
        mode="mock",
        preview_id=preview["preview_id"],
        batch_size=1,
        confirm_destructive=False,
        options=SessionOptions(mode="mock", project="safe", output_dir=tmp_path),
    )

    assert result["status"] == "aborted_before_apply"
    assert result["abort_reason"] == "preview_state_drift"
    assert result["preview_status"] == "stale"
    assert result["dispatched"] is False
    assert not (
        tmp_path / "safe_change_previews" / f"{preview['preview_id']}.claim"
    ).exists()


@pytest.mark.asyncio
async def test_safe_change_preview_is_consumed_once_under_concurrent_apply(
    tmp_path: Path,
) -> None:
    controller = SessionController(real_client=object())
    snapshot = _complete_safe_snapshot()
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(json.dumps({"snapshot": snapshot}), encoding="utf-8")

    async def snapshots(**kwargs):
        return {
            "snapshot_id": str(kwargs.get("label") or "snapshot"),
            "snapshot_path": str(snapshot_path),
            "snapshot": snapshot,
        }

    controller.compact_snapshot = snapshots  # type: ignore[method-assign]
    preview = await controller.safe_change_preview(
        project="safe",
        mode="mock",
        operation="visibility",
        targets=[
            {"kind": "body", "component": "root", "name": "Body1", "visible": False}
        ],
        options=SessionOptions(mode="mock", project="safe", output_dir=tmp_path),
    )

    async def apply_once():
        return await controller.safe_change_apply(
            project="safe",
            mode="mock",
            preview_id=preview["preview_id"],
            batch_size=1,
            confirm_destructive=False,
            options=SessionOptions(mode="mock", project="safe", output_dir=tmp_path),
        )

    results = await asyncio.gather(apply_once(), apply_once())
    assert sum(result["preview_status"] == "consumed" for result in results) == 2
    assert sum(result["status"] != "aborted_before_apply" for result in results) == 1
    assert all(result["dispatched"] is False for result in results)


def test_safe_change_transport_requires_correlated_dispatch_event() -> None:
    stale = _safe_change_transport_fields(
        mode="real",
        diagnostics={
            "last_call_outcome": {
                "operation_id": "older-operation",
                "semantics": "mutating",
                "dispatched": True,
                "mutation_outcome": "known",
            }
        },
        invoked=True,
        error=None,
        expected_operation_id="safe-change:preview_1",
    )
    assert stale["dispatched"] is False
    assert stale["dispatch_event_correlated"] is False
    assert stale["mutation_outcome"] == "unknown"
    assert stale["may_have_applied"] is True

    current = _safe_change_transport_fields(
        mode="real",
        diagnostics={
            "last_call_outcome": {
                "operation_id": "safe-change:preview_1",
                "semantics": "mutating",
                "dispatched": True,
                "mutation_outcome": "unknown",
                "post_dispatch_replay_suppressed": True,
            }
        },
        invoked=True,
        error="MUTATION_OUTCOME_UNKNOWN",
        expected_operation_id="safe-change:preview_1",
    )
    assert current["dispatched"] is True
    assert current["dispatch_event_correlated"] is True
    assert current["may_have_applied"] is True
    assert current["post_dispatch_replay_suppressed"] is True


def test_safe_change_mixed_requirements_cannot_self_satisfy_independent_oracle() -> (
    None
):
    preview = {
        "requirements": [
            {
                "id": "target_count",
                "required": True,
                "assertion_ids": ["expected_target_count"],
                "oracle": "contract",
            },
            {
                "id": "external_shape",
                "required": True,
                "assertion_ids": ["readback_complete"],
                "oracle": "independent_oracle",
            },
        ]
    }
    result = _safe_change_verification(
        preview,
        {"changed_count": 1},
        {"negative_impact": False, "numeric_evidence_valid": True},
        1,
    )

    by_id = {item["id"]: item for item in result["verification"]["requirements"]}
    assert result["assertion_status"] == "passed"
    assert result["intent_coverage"] == "partial"
    assert result["verification_level"] == "independent_oracle"
    assert result["contract_verified"] is False
    assert by_id["target_count"]["passed"] is True
    assert by_id["external_shape"]["covered"] is False
    assert by_id["external_shape"]["oracle_evidence"] == "not_available"


@pytest.mark.parametrize("invalid", [True, 1.0, "1", None, math.nan, math.inf])
def test_safe_change_verification_rejects_untyped_mutation_counts(
    invalid: object,
) -> None:
    result = _safe_change_verification(
        {},
        {"changed_count": invalid},
        {"negative_impact": False, "numeric_evidence_valid": True},
        1,
    )

    assert result["assertion_status"] == "incomplete"
    assert result["contract_verified"] is False
    assert result["invalid_numeric_evidence"] is True


def test_safe_change_verification_rejects_invalid_snapshot_numeric_evidence() -> None:
    result = _safe_change_verification(
        {},
        {"changed_count": 1},
        {"negative_impact": False, "numeric_evidence_valid": False},
        1,
    )

    assert result["assertion_status"] == "incomplete"
    assert result["contract_verified"] is False
    assert result["verification"]["readback_complete"] is False


@pytest.mark.asyncio
async def test_safe_change_applying_preview_requires_readback_without_replay(
    tmp_path: Path,
) -> None:
    preview_dir = tmp_path / "safe_change_previews"
    preview_dir.mkdir()
    preview_id = "preview_interrupted"
    (preview_dir / f"{preview_id}.json").write_text(
        json.dumps(
            {
                "schema_version": "safe_change_preview.v2",
                "preview_id": preview_id,
                "preview_status": "applying",
                "project": "safe",
                "mode": "real",
                "dispatch_operation_id": f"safe-change:{preview_id}",
            }
        ),
        encoding="utf-8",
    )
    controller = SessionController(real_client=object())
    result = await controller.safe_change_apply(
        project="safe",
        mode="real",
        preview_id=preview_id,
        batch_size=1,
        confirm_destructive=False,
        options=SessionOptions(mode="real", project="safe", output_dir=tmp_path),
    )
    assert result["status"] == "mutation_outcome_unknown"
    assert result["error_code"] == "MUTATION_OUTCOME_UNKNOWN"
    assert result["may_have_applied"] is True
    assert "Do not replay" in result["recovery_instructions"]


@pytest.mark.asyncio
async def test_safe_change_claimed_crash_is_known_predispatch_and_staled(
    tmp_path: Path,
) -> None:
    preview_dir = tmp_path / "safe_change_previews"
    preview_dir.mkdir()
    preview_id = "preview_claimed_only"
    preview_path = preview_dir / f"{preview_id}.json"
    preview_path.write_text(
        json.dumps(
            {
                "schema_version": "safe_change_preview.v2",
                "preview_id": preview_id,
                "preview_status": "applying",
                "dispatch_phase": "claimed",
                "project": "safe",
                "mode": "real",
                "dispatch_operation_id": f"safe-change:{preview_id}",
            }
        ),
        encoding="utf-8",
    )
    claim_path = preview_path.with_suffix(".claim")
    claim_path.write_text("", encoding="utf-8")

    controller = SessionController(real_client=object())
    result = await controller.safe_change_apply(
        project="safe",
        mode="real",
        preview_id=preview_id,
        batch_size=1,
        confirm_destructive=False,
        options=SessionOptions(mode="real", project="safe", output_dir=tmp_path),
    )

    assert result["status"] == "aborted_before_apply"
    assert result["abort_reason"] == "interrupted_before_backend_invocation"
    assert result["preview_status"] == "stale"
    assert result["dispatched"] is False
    assert result["may_have_applied"] is False
    assert result["mutation_outcome"] == "known"
    assert not claim_path.exists()


def test_safe_change_scripts_preflight_all_targets_before_first_mutation() -> None:
    visibility = safe_visibility_apply_script(
        {"targets": [{"kind": "body", "name": "B"}]}
    )
    delete = safe_delete_apply_script(
        {"targets": [{"kind": "body", "name": "B", "component": "root"}]}
    )

    assert visibility.index("if preflight_errors:") < visibility.index(
        "body.isLightBulbOn = desired_visible"
    )
    assert delete.index("if skipped:") < delete.index("entity.deleteMe()")


def test_delete_classification_uses_bound_snapshot_facts_not_caller_labels() -> None:
    snapshot = {
        "bodies": [
            {
                "key": "part/Body#1",
                "name": "Body",
                "component": "part",
                "entity_token": "opaque-token",
                "visible": False,
                "is_root": False,
                "is_referenced": False,
                "is_imported": False,
                "shared_definition": False,
                "binding_fingerprint": "snapshot-proof",
            }
        ],
        "occurrences": [],
        "duplicate_body_names": {},
    }
    caller_target = {"kind": "body", "entity_token": "opaque-token"}
    bindings, errors = bind_safe_change_targets([caller_target], snapshot)
    assert errors == []
    classification = classify_safe_change(
        "delete", bindings, {"allow_delete": True}, snapshot
    )
    assert classification["blocked"] is True
    assert classification["allow_apply"] is False


def test_delete_fails_closed_when_bound_safety_facts_are_missing() -> None:
    classification = classify_safe_change(
        "delete",
        [{"kind": "body", "identifier": "body-token", "visible": True}],
        {"allow_delete": True},
        {},
    )
    assert classification["blocked"] is True
    assert "facts" in " ".join(classification["reasons"]).lower()
