from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sys
import types
from typing import Any

from agent_core.fusion_scripts import (
    safe_delete_apply_script,
    safe_visibility_apply_script,
)
from agent_core.session_controller import _snapshot_is_complete


class _Collection:
    def __init__(self, *items: object) -> None:
        self._items = items

    @property
    def count(self) -> int:
        return len(self._items)

    def item(self, index: int) -> object:
        return self._items[index]


class _Attributes:
    @staticmethod
    def itemByName(_group: str, _name: str) -> None:
        return None


class _Body:
    def __init__(self, name: str = "Body", token: str = "body-token") -> None:
        self.name = name
        self.entityToken = token
        self.isVisible = True
        self._visible = True
        self.visibility_sets = 0

    @property
    def isLightBulbOn(self) -> bool:
        return self._visible

    @isLightBulbOn.setter
    def isLightBulbOn(self, value: bool) -> None:
        self.visibility_sets += 1
        self._visible = value


class _Component:
    def __init__(self, name: str, *bodies: _Body) -> None:
        self.name = name
        self.entityToken = f"component:{name}"
        self.bRepBodies = _Collection(*bodies)
        self.occurrences = _Collection()
        self.attributes = _Attributes()
        self.isReferencedComponent = False
        self.isLightBulbOn = True


class _Occurrence:
    def __init__(self, name: str, component: _Component, token: str) -> None:
        self.name = name
        self.component = component
        self.entityToken = token
        self.childOccurrences = _Collection()
        self.isLightBulbOn = True
        self.isVisible = True
        self.delete_calls = 0

    def deleteMe(self) -> None:
        self.delete_calls += 1


def _install_adsk(
    monkeypatch, design: object, document_id: str = "doc-current"
) -> None:
    data_file = type("DataFile", (), {"id": document_id, "versionNumber": 1})()
    document = type("Document", (), {"name": "fixture", "dataFile": data_file})()
    application = type(
        "ApplicationInstance",
        (),
        {"activeDocument": document, "activeProduct": design},
    )()
    adsk = types.ModuleType("adsk")
    adsk.__path__ = []  # type: ignore[attr-defined]
    core = types.ModuleType("adsk.core")
    fusion = types.ModuleType("adsk.fusion")
    core.Application = type(
        "Application", (), {"get": staticmethod(lambda: application)}
    )
    fusion.Design = type("Design", (), {"cast": staticmethod(lambda value: value)})
    adsk.core = core
    adsk.fusion = fusion
    monkeypatch.setitem(sys.modules, "adsk", adsk)
    monkeypatch.setitem(sys.modules, "adsk.core", core)
    monkeypatch.setitem(sys.modules, "adsk.fusion", fusion)


def _run_script(script: str) -> dict[str, Any]:
    namespace: dict[str, object] = {}
    exec(compile(script, "<safe-change-sink>", "exec"), namespace)
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        namespace["run"]("")  # type: ignore[index,operator]
    return json.loads(output.getvalue().strip())


def _fingerprint(item: dict[str, Any]) -> str:
    fields = {
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
    return hashlib.sha256(
        json.dumps(fields, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]


def _envelope(document_id: str = "doc-current") -> dict[str, Any]:
    return {
        "document_identity": {"kind": "data_file", "stable_id": document_id},
        "state_fingerprint": "a" * 64,
        "preview_digest": "b" * 64,
        "operation_binding": "c" * 64,
    }


def _occurrence_target(path: str, *, token: str = "occ-token") -> dict[str, Any]:
    target = {
        "target_index": 0,
        "kind": "occurrence",
        "identifier": token,
        "entity_token": token,
        "path": path,
        "key": None,
        "component": "Part",
        "name": "Current:1",
        "visible": True,
        "is_root": False,
        "is_referenced": False,
        "is_imported": False,
        "shared_definition": False,
    }
    target["binding_fingerprint"] = _fingerprint(target)
    return target


def test_legacy_snapshot_without_explicit_completeness_is_not_a_mutation_oracle() -> (
    None
):
    legacy = {
        "schema_version": "compact_snapshot.v1",
        "payload_capped": False,
        "document": {"id": "legacy-document"},
        "bodies": [],
        "occurrences": [],
    }

    assert _snapshot_is_complete(legacy) is False


def test_delete_sink_derives_current_occurrence_path_before_mutation(
    monkeypatch,
) -> None:
    component = _Component("Part")
    occurrence = _Occurrence("Current:1", component, "occ-token")
    root = _Component("Root")
    root.occurrences = _Collection(occurrence)
    design = type(
        "Design",
        (),
        {"rootComponent": root, "allComponents": _Collection(root, component)},
    )()
    _install_adsk(monkeypatch, design)
    payload = {
        **_envelope(),
        # The token still resolves, but the previewed occurrence path is stale.
        "targets": [_occurrence_target("Previewed:1")],
    }

    result = _run_script(safe_delete_apply_script(payload))

    assert result["success"] is False
    assert result["deleted_count"] == 0
    assert occurrence.delete_calls == 0


def test_delete_sink_rejects_active_document_drift_with_zero_mutation(
    monkeypatch,
) -> None:
    component = _Component("Part")
    occurrence = _Occurrence("Current:1", component, "occ-token")
    root = _Component("Root")
    root.occurrences = _Collection(occurrence)
    design = type(
        "Design",
        (),
        {"rootComponent": root, "allComponents": _Collection(root, component)},
    )()
    _install_adsk(monkeypatch, design, document_id="doc-current")
    payload = {
        **_envelope(document_id="doc-previewed"),
        "targets": [_occurrence_target("Current:1")],
    }

    result = _run_script(safe_delete_apply_script(payload))

    assert result["success"] is False
    assert result["error_code"] == "DOCUMENT_BINDING_MISMATCH"
    assert result["deleted_count"] == 0
    assert occurrence.delete_calls == 0


def test_visibility_sink_requires_stable_entity_identity(monkeypatch) -> None:
    body = _Body()
    component = _Component("Part", body)
    root = _Component("Root")
    design = type(
        "Design",
        (),
        {"rootComponent": root, "allComponents": _Collection(root, component)},
    )()
    _install_adsk(monkeypatch, design)
    target = {
        "target_index": 0,
        "kind": "body",
        "identifier": "Part/Body#1",
        "entity_token": "",
        "key": "Part/Body#1",
        "path": None,
        "component": "Part",
        "name": "Body",
        "visible": True,
        "desired_visible": False,
        "is_root": False,
        "is_referenced": False,
        "is_imported": False,
        "shared_definition": False,
    }
    target["binding_fingerprint"] = _fingerprint(target)

    result = _run_script(
        safe_visibility_apply_script({**_envelope(), "targets": [target]})
    )

    assert result["success"] is False
    assert result["changed_count"] == 0
    assert body.visibility_sets == 0


def test_visibility_sink_positive_control_returns_bound_identity_proof(
    monkeypatch,
) -> None:
    body = _Body()
    component = _Component("Part", body)
    root = _Component("Root")
    design = type(
        "Design",
        (),
        {"rootComponent": root, "allComponents": _Collection(root, component)},
    )()
    _install_adsk(monkeypatch, design)
    target = {
        "target_index": 0,
        "kind": "body",
        "identifier": "body-token",
        "entity_token": "body-token",
        "key": "Part/Body#1",
        "path": None,
        "component": "Part",
        "name": "Body",
        "visible": True,
        "desired_visible": False,
        "is_root": False,
        "is_referenced": False,
        "is_imported": False,
        "shared_definition": False,
    }
    target["binding_fingerprint"] = _fingerprint(target)

    result = _run_script(
        safe_visibility_apply_script({**_envelope(), "targets": [target]})
    )

    assert result["success"] is True
    assert result["changed_count"] == 1
    assert result["operation_binding"] == "c" * 64
    assert result["changed"][0]["binding_fingerprint"] == target["binding_fingerprint"]
    assert (
        result["changed"][0]["entity_identity_digest"]
        == hashlib.sha256(b"body-token").hexdigest()
    )
    assert body.visibility_sets == 1
    assert body.isLightBulbOn is False
