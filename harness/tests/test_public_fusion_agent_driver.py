from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmark import fusion_agent_driver as driver_module
from benchmark.fusion_agent_driver import FusionAgentCodexPublicDriver
from benchmark.provenance import RevisionIdentity
from cli import main as cli_main


def test_public_driver_binds_revision_expectations_to_startup_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    expected_commit = "a" * 40
    expected_manifest = "b" * 64
    captured: dict[str, object] = {}

    def collect(root, **kwargs):  # noqa: ANN001, ANN003
        captured["root"] = root
        captured.update(kwargs)
        return RevisionIdentity(
            expected_git_commit=expected_commit,
            observed_git_commit=expected_commit,
            expected_source_manifest_sha256=expected_manifest,
            observed_source_manifest_sha256=expected_manifest,
            tracked_state="clean",
        )

    monkeypatch.setattr(driver_module, "collect_workspace_revision", collect)
    driver = FusionAgentCodexPublicDriver(
        output_dir=tmp_path,
        environment_snapshot={
            "FUSION_AGENT_EXPECTED_GIT_COMMIT": expected_commit,
            "FUSION_AGENT_EXPECTED_SOURCE_MANIFEST_SHA256": expected_manifest,
        },
    )

    identity = driver._observed_revision()

    assert identity.exact is True
    assert captured["expected_git_commit"] == expected_commit
    assert captured["expected_source_manifest_sha256"] == expected_manifest


@pytest.mark.asyncio
async def test_cli_executes_our_mock_b02_b07_and_faults_but_not_competitors(
    tmp_path: Path,
) -> None:
    result = await cli_main._benchmark_public(
        cli_main._default_public_manifest(),
        str(tmp_path / "public"),
        "mock",
        False,
        False,
        True,
    )
    report = json.loads(Path(result["report_path"]).read_text(encoding="utf-8"))

    assert result["summary"]["states"] == {
        "completed": 14,
        "failed": 0,
        "not_run": 56,
    }
    own = [
        item for item in report["results"] if item["subject_id"] == "fusion_agent_codex"
    ]
    competitors = [
        item for item in report["results"] if item["subject_id"] != "fusion_agent_codex"
    ]
    assert len(own) == 14
    assert all(item["state"] == "completed" for item in own)
    assert all(item["metrics"]["task_success"] is True for item in own)
    assert all(item["metrics"]["oracle_passed"] is True for item in own)
    assert all(item["metrics"]["contract_coverage"] == 1.0 for item in own)
    assert all(
        item["metrics"]["backend_id"] == "fusion_agent_internal_mock" for item in own
    )
    normal = [item for item in own if item["task"]["fault_id"] is None]
    assert len(normal) == 6
    assert all(item["metrics"]["geometry_valid"] is True for item in normal)
    unknown = next(
        item for item in own if item["task"]["fault_id"] == "timeout_after_dispatch"
    )
    assert unknown["metrics"]["geometry_valid"] is None
    assert unknown["metrics"]["mutation_dispatch_count"] == 1
    assert unknown["metrics"]["replay_count"] == 0
    assert unknown["metrics"]["recovery_status"] == "readback_required"
    assert len(competitors) == 56
    assert all(item["state"] == "not_run" for item in competitors)
    assert {item["reason"] for item in competitors} == {"prerequisites_not_injected"}
    assert list(
        (tmp_path / "public" / "fusion_agent_internal" / "benchmarks").glob("bench_*")
    )


@pytest.mark.asyncio
async def test_confirmed_real_public_run_fails_closed_at_runtime_capability_preflight(
    tmp_path: Path,
) -> None:
    result = await cli_main._benchmark_public(
        cli_main._default_public_manifest(),
        str(tmp_path / "public-real"),
        "real",
        True,
        True,
        False,
    )
    report = json.loads(Path(result["report_path"]).read_text(encoding="utf-8"))
    own = [
        item for item in report["results"] if item["subject_id"] == "fusion_agent_codex"
    ]

    assert result["summary"]["states"] == {
        "completed": 0,
        "failed": 0,
        "not_run": 30,
    }
    assert len(own) == 6
    assert all(item["state"] == "not_run" for item in own)
    assert all(
        item["reason"].startswith("real_public_capabilities_unavailable:")
        for item in own
    )
    assert all(
        "no real benchmark action was dispatched" in item["reason"] for item in own
    )
    assert not (tmp_path / "public-real" / "fusion_agent_internal").exists()
