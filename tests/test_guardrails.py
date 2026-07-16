from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE_PACKAGES = ROOT / "harness" / "packages"
SOURCE_APPS = ROOT / "harness" / "apps"
sys.path[:0] = [str(SOURCE_PACKAGES), str(SOURCE_APPS)]

from agent_core.guardrails import (  # noqa: E402
    PlannerUnsupportedError,
    classify_safe_change,
    diff_snapshots,
)
from agent_core.planner import PlanningRequest, RuleBasedPlanner  # noqa: E402
from fusion_mcp_adapter.manifest_store import ManifestStore  # noqa: E402
from fusion_mcp_adapter.tool_result import ToolDefinition, ToolManifest  # noqa: E402
from fusion_tool_facade.vendor_facade import VendorFusionFacade  # noqa: E402


def test_planner_guard_rejects_audit_reorg_delete_hub_prompts() -> None:
    async def run() -> None:
        planner = RuleBasedPlanner()
        prompts = [
            "audite o hub sem modificar nada",
            "reorganize a Personal Library em levas",
            "delete hidden imported roots",
            "inspect read-only before cleanup",
        ]
        for prompt in prompts:
            with pytest.raises(PlannerUnsupportedError):
                await planner.plan(PlanningRequest(user_prompt=prompt))

    asyncio.run(run())


def test_manifest_store_keeps_real_and_mock_latest_separate(tmp_path: Path) -> None:
    store = ManifestStore(tmp_path)
    real = ToolManifest(
        source="fusion_real", tools=[ToolDefinition(name="fusion_mcp_execute")]
    )
    mock = ToolManifest(source="mock", tools=[ToolDefinition(name="inspect_design")])

    store.save(real)
    store.save(mock)

    assert store.load_latest("real").source == "fusion_real"
    assert store.load_latest("mock").source == "mock"
    assert (tmp_path / "fusion_mcp_tools_latest_real.json").exists()
    assert (tmp_path / "fusion_mcp_tools_latest_mock.json").exists()
    assert store.load_latest().source == "fusion_real"


def test_safe_change_diff_detects_visible_losses() -> None:
    before = {
        "visible_occurrence_paths": ["root/A", "root/B"],
        "visible_body_keys": ["A/body#1", "B/body#1"],
        "visible_component_keys": ["A", "B"],
        "visible_body_bbox_mm": {"size_mm": [100.0, 50.0, 20.0]},
    }
    after = {
        "visible_occurrence_paths": ["root/A"],
        "visible_body_keys": ["A/body#1"],
        "visible_component_keys": ["A"],
        "visible_body_bbox_mm": {"size_mm": [90.0, 50.0, 20.0]},
    }

    diff = diff_snapshots(before, after)

    assert diff["negative_impact"] is True
    assert diff["visible_occurrences_missing"] == ["root/B"]
    assert diff["visible_bodies_missing"] == ["B/body#1"]
    assert diff["visible_component_keys_missing"] == ["B"]
    assert diff["visible_body_bbox_shrank"] is True
    assert diff["drift_conclusion"] == "drift_detected"


def test_safe_change_diff_limits_no_drift_claim_to_observed_scope() -> None:
    partial = {
        "complete": False,
        "counts_exact": False,
        "truncated": True,
        "visible_occurrence_paths": ["root/A"],
        "visible_body_keys": ["A/body#1"],
        "visible_component_keys": ["A"],
    }

    diff = diff_snapshots(partial, partial)

    assert diff["negative_impact"] is False
    assert diff["global_fingerprint_complete"] is False
    assert diff["drift_conclusion"] == "no_drift_in_observed_scope"


def test_duplicate_body_names_are_ambiguous_without_component_scope() -> None:
    snapshot = {"duplicate_body_names": {"Bolt": 3}}

    result = classify_safe_change(
        "delete", [{"name": "Bolt"}], {"allow_delete": True}, snapshot
    )

    assert result["blocked"] is True
    assert result["classification"] == "ambiguous_targets"
    assert result["ambiguous_target_warnings"]


def test_vendor_capture_fails_when_file_missing(tmp_path: Path) -> None:
    async def run() -> None:
        facade = object.__new__(VendorFusionFacade)
        facade._last_scene = {}
        facade._uses_crud_profile = lambda: True

        async def fake_execute_script_json(_script: str) -> dict:
            return {
                "success": True,
                "screenshot": {"path": str(tmp_path / "missing.png"), "bytes": 0},
            }

        facade._execute_script_json = fake_execute_script_json
        with pytest.raises(RuntimeError, match="local file does not exist"):
            await facade.capture_viewport(
                name="missing",
                path=tmp_path / "missing.png",
                view="isometric",
            )

    asyncio.run(run())
