from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from agent_core.session_controller import SessionController, SessionOptions
from fusion_mcp_adapter.semantics import CallSemantics, McpCallOptions
from fusion_mcp_adapter.tool_result import ToolResult
from fusion_tool_facade.vendor_facade import VendorFusionFacade


_DOCUMENT_IDENTITY = {
    "identity_kind": "mock_session",
    "stable_id": "mock:can-014",
}


def _binding_fingerprint(body: dict[str, Any]) -> str:
    payload = {
        key: body.get(key)
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
    encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _body(**overrides: Any) -> dict[str, Any]:
    name = str(overrides.get("name") or "neutral_current")
    body: dict[str, Any] = {
        "key": f"assembly/{name}#1",
        "name": name,
        "component": "assembly",
        "component_key": "component-token",
        "entity_token": "body-token",
        "visible": True,
        "is_root": False,
        "is_referenced": False,
        "is_imported": False,
        "shared_definition": False,
        "bbox_mm": {
            "min_mm": [0.0, 0.0, 0.0],
            "max_mm": [1.0, 1.0, 1.0],
            "size_mm": [1.0, 1.0, 1.0],
        },
    }
    body.update(overrides)
    body["binding_fingerprint"] = _binding_fingerprint(body)
    return body


def _snapshot(body: dict[str, Any] | None) -> dict[str, Any]:
    bodies = [] if body is None else [body]
    visible_bodies = [
        str(item["key"]) for item in bodies if item.get("visible") is True
    ]
    return {
        "schema_version": "compact_snapshot.v2",
        "complete": True,
        "truncated": False,
        "counts_exact": True,
        "stop_reason": None,
        "payload_capped": False,
        "counts": {
            "components_total": 1,
            "occurrences_total": 0,
            "bodies_total": len(bodies),
            "visible_occurrences": 0,
            "visible_bodies": len(visible_bodies),
            "visible_components": int(bool(visible_bodies)),
        },
        "document": dict(_DOCUMENT_IDENTITY),
        "bodies": bodies,
        "occurrences": [],
        "visible_occurrence_paths": [],
        "visible_body_keys": visible_bodies,
        "visible_component_keys": ["component-token"] if visible_bodies else [],
        "visible_body_bbox_mm": bodies[0].get("bbox_mm") if visible_bodies else None,
        "duplicate_body_names": {},
    }


def _install_snapshot_sequence(
    controller: SessionController,
    tmp_path: Path,
    snapshots: list[dict[str, Any]],
) -> None:
    remaining = iter(snapshots)
    calls = 0

    async def compact_snapshot(**kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        snapshot = next(remaining)
        label = str(kwargs.get("label") or f"snapshot-{calls}")
        path = tmp_path / f"{calls:02d}-{label}.json"
        path.write_text(json.dumps({"snapshot": snapshot}), encoding="utf-8")
        return {
            "snapshot_id": f"snapshot-{calls}",
            "snapshot_path": str(path),
            "snapshot": snapshot,
        }

    controller.compact_snapshot = compact_snapshot  # type: ignore[method-assign]


class _RealClientCanary:
    diagnostics: dict[str, Any] = {}


class _MutationAdapterCanary:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], McpCallOptions | None]] = []

    async def call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        options: McpCallOptions | None = None,
    ) -> ToolResult:
        self.calls.append((tool_name, arguments, options))
        script = str(arguments["object"]["script"])
        match = re.search(r'"operation_binding": "([0-9a-f]{64})"', script)
        assert match is not None
        return ToolResult.success(
            message=json.dumps(
                {
                    "success": True,
                    "deleted": [
                        {
                            "kind": "body",
                            "component": "assembly",
                            "name": "neutral_current",
                            "entity_identity_digest": hashlib.sha256(
                                b"body-token"
                            ).hexdigest(),
                            "binding_fingerprint": _body()["binding_fingerprint"],
                        }
                    ],
                    "deleted_count": 1,
                    "skipped": [],
                    "operation_binding": match.group(1),
                }
            ),
            meta={
                "fusion_agent_transport": {
                    "operation_id": options.operation_id if options else None,
                    "semantics": "mutating",
                    "dispatched": True,
                    "may_have_applied": False,
                    "post_dispatch_replay_suppressed": True,
                    "mutation_outcome": "known",
                }
            },
        )


def _forbid_facade_build(
    controller: SessionController,
) -> Callable[..., Awaitable[VendorFusionFacade]]:
    builds = 0

    async def forbidden(*_args: Any, **_kwargs: Any) -> VendorFusionFacade:
        nonlocal builds
        builds += 1
        raise AssertionError("a rejected delete must not reach the provider boundary")

    forbidden.build_count = lambda: builds  # type: ignore[attr-defined]
    controller._build_facade = forbidden  # type: ignore[assignment]
    return forbidden


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("risk_field", "risk_value"),
    [
        pytest.param("is_root", True, id="root"),
        pytest.param("visible", False, id="hidden"),
        pytest.param("is_referenced", True, id="referenced"),
        pytest.param("is_imported", True, id="imported"),
        pytest.param("shared_definition", True, id="shared-definition"),
    ],
)
async def test_delete_risk_matrix_uses_bound_facts_and_never_dispatches(
    tmp_path: Path,
    risk_field: str,
    risk_value: bool,
) -> None:
    body = _body(**{risk_field: risk_value})
    snapshot = _snapshot(body)
    controller = SessionController(real_client=_RealClientCanary())
    _install_snapshot_sequence(controller, tmp_path, [snapshot])
    forbidden = _forbid_facade_build(controller)

    preview = await controller.safe_change_preview(
        project="can-014",
        mode="real",
        operation="delete",
        targets=[
            {
                "kind": "body",
                "entity_token": "body-token",
                # These neutral, stale presentation labels must not influence
                # the safety decision made from the bound snapshot entity.
                "name": "neutral_requested",
                "component": "neutral_requested_component",
            }
        ],
        policy={"allow_delete": True},
        options=SessionOptions(mode="real", project="can-014", output_dir=tmp_path),
    )

    assert preview["blocked"] is True
    assert preview["classification"]["allow_apply"] is False
    assert preview["binding_errors"] == []
    assert preview["bound_targets"][0][risk_field] is risk_value
    assert preview["bound_targets"][0]["name"] == "neutral_current"

    result = await controller.safe_change_apply(
        project="can-014",
        mode="real",
        preview_id=preview["preview_id"],
        batch_size=1,
        confirm_destructive=True,
        options=SessionOptions(mode="real", project="can-014", output_dir=tmp_path),
    )

    assert result["status"] == "aborted_before_apply"
    assert result["abort_reason"] == "preview_blocked"
    assert result["dispatched"] is False
    assert result["may_have_applied"] is False
    assert forbidden.build_count() == 0  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_delete_rename_between_preview_and_apply_is_stale_zero_dispatch(
    tmp_path: Path,
) -> None:
    baseline = _snapshot(_body(name="neutral_before"))
    renamed = _snapshot(_body(name="neutral_after"))
    controller = SessionController(real_client=_RealClientCanary())
    _install_snapshot_sequence(controller, tmp_path, [baseline, renamed])
    forbidden = _forbid_facade_build(controller)

    preview = await controller.safe_change_preview(
        project="can-014",
        mode="real",
        operation="delete",
        targets=[
            {
                "kind": "body",
                "entity_token": "body-token",
                "name": "neutral_before",
            }
        ],
        policy={"allow_delete": True},
        options=SessionOptions(mode="real", project="can-014", output_dir=tmp_path),
    )
    assert preview["blocked"] is False
    assert preview["classification"]["allow_apply"] is True
    assert len(str(preview["preview_digest"])) == 64

    result = await controller.safe_change_apply(
        project="can-014",
        mode="real",
        preview_id=preview["preview_id"],
        batch_size=1,
        confirm_destructive=True,
        options=SessionOptions(mode="real", project="can-014", output_dir=tmp_path),
    )

    assert result["status"] == "aborted_before_apply"
    assert result["abort_reason"] == "preview_state_drift"
    assert result["preview_status"] == "stale"
    assert result["preapply_guard"]["fingerprint_matches"] is False
    assert result["preapply_guard"]["bindings_match"] is False
    assert result["dispatched"] is False
    assert result["may_have_applied"] is False
    assert forbidden.build_count() == 0  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_safe_bound_target_dispatches_exactly_once_end_to_end(
    tmp_path: Path,
) -> None:
    body = _body()
    baseline = _snapshot(body)
    after = _snapshot(None)
    controller = SessionController(real_client=_RealClientCanary())
    _install_snapshot_sequence(controller, tmp_path, [baseline, baseline, after])
    adapter = _MutationAdapterCanary()
    facade = VendorFusionFacade(
        adapter,  # type: ignore[arg-type]
        available_tools={"fusion_mcp_read", "fusion_mcp_execute"},
    )

    async def build_facade(*_args: Any, **_kwargs: Any) -> VendorFusionFacade:
        return facade

    controller._build_facade = build_facade  # type: ignore[assignment]
    preview = await controller.safe_change_preview(
        project="can-014",
        mode="real",
        operation="delete",
        targets=[
            {
                "kind": "body",
                "entity_token": "body-token",
                "name": "neutral_requested",
            }
        ],
        policy={"allow_delete": True},
        options=SessionOptions(mode="real", project="can-014", output_dir=tmp_path),
    )

    assert preview["blocked"] is False
    assert preview["classification"]["allow_apply"] is True
    assert preview["bound_targets"] == [
        {
            "target_index": 0,
            "kind": "body",
            "identifier": "body-token",
            "entity_token": "body-token",
            "path": None,
            "key": "assembly/neutral_current#1",
            "component": "assembly",
            "name": "neutral_current",
            "visible": True,
            "is_root": False,
            "is_referenced": False,
            "is_imported": False,
            "shared_definition": False,
            "binding_fingerprint": body["binding_fingerprint"],
        }
    ]
    assert len(str(preview["preview_digest"])) == 64

    result = await controller.safe_change_apply(
        project="can-014",
        mode="real",
        preview_id=preview["preview_id"],
        batch_size=1,
        confirm_destructive=True,
        options=SessionOptions(mode="real", project="can-014", output_dir=tmp_path),
    )

    assert len(adapter.calls) == 1
    tool_name, arguments, options = adapter.calls[0]
    assert tool_name == "fusion_mcp_execute"
    assert options is not None
    assert options.semantics is CallSemantics.MUTATING
    script = str(arguments["object"]["script"])
    assert "body-token" in script
    assert str(body["binding_fingerprint"]) in script
    assert str(preview["preview_digest"]) in script
    assert str(preview["state_fingerprint"]) in script
    assert str(preview["document_identity"]["stable_id"]) in script
    assert "neutral_current" in script
    assert "neutral_requested" not in script
    assert result["preview_status"] == "consumed"
    assert result["dispatched"] is True
    assert result["may_have_applied"] is False
    assert result["applied"]["deleted_count"] == 1
    assert result["status"] == "aborted_after_verification"
