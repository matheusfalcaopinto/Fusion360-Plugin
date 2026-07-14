from __future__ import annotations

import asyncio
import contextlib
import io
import json
import sys
import types
from pathlib import Path

import pytest

from agent_core.fusion_scripts import compact_snapshot_script
from agent_core.guardrails import compact_mock_snapshot
from agent_core.session_controller import SessionController, SessionOptions, _snapshot_is_complete
from agent_core.targeted_inspection import build_targeted_inspection_script, validate_inspection_payload


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
        _query_payload(max_entities_visited=5000, deadline_ms=5000, max_response_bytes=4096)
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
                {"id": "token", "entity_type": "body", "selector": {"entity_token": "abc"}},
                {"id": "path", "entity_type": "body", "selector": {"path": "Arm:1/Body1"}},
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


def test_response_trimming_marks_payload_incomplete_and_invalidates_fingerprint(monkeypatch) -> None:
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
    core.Application = type("Application", (), {"get": staticmethod(lambda: ApplicationInstance())})
    fusion.Design = type("FusionDesign", (), {"cast": staticmethod(lambda value: value)})
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
        {"max_entities_visited": 1000, "deadline_ms": 1500, "max_response_bytes": 1024 * 1024}
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


def test_mock_snapshot_stops_at_entity_budget_and_is_not_a_safe_baseline() -> None:
    state = {
        "state": {
            "components": {f"Component{i}": {"name": f"Component{i}"} for i in range(20)},
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
        return {"snapshot_id": "before_test", "snapshot_path": str(snapshot_path), "snapshot": snapshot}

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
        "counts": {},
    }
    before_path.write_text(json.dumps({"snapshot": before_snapshot}), encoding="utf-8")
    preview_id = "preview_bounded"
    (preview_dir / f"{preview_id}.json").write_text(
        json.dumps(
            {
                "preview_id": preview_id,
                "project": "bounded",
                "mode": "mock",
                "operation": "visibility",
                "targets": [{"kind": "body", "name": "Body1", "visible": False}],
                "policy": {},
                "classification": {"blocked": False},
                "before_snapshot_path": str(before_path),
            }
        ),
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

    async def incomplete_readback(**_kwargs):
        return {"snapshot_id": "after_test", "snapshot_path": str(after_path), "snapshot": incomplete_after}

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
