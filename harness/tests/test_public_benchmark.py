from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from benchmark.public import (
    AdapterExecution,
    AdapterPreflight,
    NormalizedPublicMetrics,
    PublicBenchmarkConfig,
    PublicBenchmarkRunner,
    load_public_manifest,
)
from fusion_agent_assets import asset_root


MANIFEST = asset_root("benchmarks") / "public_competitors_v1.json"


class FakeAdapter:
    def __init__(self, revision: str, *, fail: bool = False) -> None:
        self.revision = revision
        self.fail = fail
        self.preflight_calls = 0
        self.execute_calls = 0

    async def preflight(self, subject, config) -> AdapterPreflight:  # noqa: ANN001
        self.preflight_calls += 1
        return AdapterPreflight(
            ready=True,
            observed_revision=self.revision,
            environment={"fixture": "disposable"},
        )

    async def execute(self, subject, task, config) -> AdapterExecution:  # noqa: ANN001
        self.execute_calls += 1
        if self.fail:
            raise RuntimeError("external adapter crashed")
        return AdapterExecution(
            state="completed",
            metrics=NormalizedPublicMetrics(
                task_success=True,
                oracle_passed=True,
                contract_coverage=1.0,
                latency_ms=10,
                tool_calls=2,
                mutation_dispatch_count=1,
                replay_count=0,
                recovery_status="not_needed",
                payload_bytes=128,
                install_status="pinned",
            ),
            evidence={"oracle": "independent", "authorization": "Bearer top-secret"},
        )


def test_manifest_pins_public_subjects_and_fault_matrix() -> None:
    manifest, fingerprint = load_public_manifest(MANIFEST)
    assert len(fingerprint) == 64
    assert {subject.id for subject in manifest.subjects} == {
        "fusion_agent_codex",
        "autodesk_fusion_official",
        "faust_fusion360_mcp",
        "frank_autodesk_fusion_mcp",
        "ndoo_fusion360_bridge",
    }
    assert {case.id for case in manifest.cases} == {
        "b02_vented_enclosure",
        "b03_split_pillow_block",
        "b04_offset_duct_adapter",
        "b05_spherical_lattice_radome",
        "b06_robot_arm_assembly",
        "b07_packaging_machine",
    }
    assert len(manifest.faults) == 8


@pytest.mark.asyncio
async def test_missing_adapters_are_not_run_and_not_scoreable() -> None:
    report = await PublicBenchmarkRunner().run(MANIFEST)
    assert report.summary["states"] == {"completed": 0, "failed": 0, "not_run": 70}
    assert report.summary["scoreable"] is False
    assert report.summary["oracle_pass_rate"] is None
    assert {result.reason for result in report.results} == {"adapter_not_installed"}


@pytest.mark.asyncio
async def test_public_report_uses_constructor_environment_snapshot_after_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = PublicBenchmarkRunner(
        environment_snapshot={
            "GIT_COMMIT": "startup-commit",
            "FUSION_VERSION": "startup-fusion",
        }
    )
    monkeypatch.setenv("GIT_COMMIT", "drifted-commit")
    monkeypatch.setenv("FUSION_VERSION", "drifted-fusion")

    report = await runner.run(MANIFEST)

    assert report.environment["git_commit"] == "startup-commit"
    assert report.environment["fusion_version"] == "startup-fusion"


@pytest.mark.asyncio
async def test_real_adapter_is_not_touched_without_both_confirmations() -> None:
    adapter = FakeAdapter("b44b667e440da070081795cfcbfaf75de2a44251")
    report = await PublicBenchmarkRunner({"faust_fusion360_mcp": adapter}).run(
        MANIFEST,
        config=PublicBenchmarkConfig(mode="real", subject_ids=["faust_fusion360_mcp"]),
    )
    assert adapter.preflight_calls == 0
    assert adapter.execute_calls == 0
    assert {item.reason for item in report.results} == {"real_execution_not_confirmed"}


@pytest.mark.asyncio
async def test_revision_mismatch_fails_closed_before_execution() -> None:
    adapter = FakeAdapter("wrong-commit")
    report = await PublicBenchmarkRunner({"faust_fusion360_mcp": adapter}).run(
        MANIFEST,
        config=PublicBenchmarkConfig(mode="mock", subject_ids=["faust_fusion360_mcp"]),
    )
    assert adapter.preflight_calls == 1
    assert adapter.execute_calls == 0
    assert all(item.state == "not_run" for item in report.results)
    assert all(
        item.reason and item.reason.startswith("revision_mismatch:")
        for item in report.results
    )


@pytest.mark.asyncio
async def test_pinned_comparator_alone_produces_normalized_but_not_scoreable_results(
    tmp_path: Path,
) -> None:
    adapter = FakeAdapter("b44b667e440da070081795cfcbfaf75de2a44251")
    runner = PublicBenchmarkRunner({"faust_fusion360_mcp": adapter})
    report = await runner.run(
        MANIFEST,
        config=PublicBenchmarkConfig(mode="mock", subject_ids=["faust_fusion360_mcp"]),
    )
    assert report.summary["states"] == {"completed": 14, "failed": 0, "not_run": 0}
    assert report.summary["oracle_pass_rate"] == 1.0
    assert report.summary["scoreable"] is False
    assert report.results[0].evidence_mode == "mock"
    assert report.results[0].evidence["authorization"]["redacted"] is True
    json_path, markdown_path = runner.write(report, tmp_path)
    assert (
        json.loads(json_path.read_text(encoding="utf-8"))["summary"]["scoreable"]
        is False
    )
    assert "never count as success" in markdown_path.read_text(encoding="utf-8")
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "Normalized task results" in markdown
    assert "Contract coverage" in markdown
    assert "Environment and provenance" in markdown
    assert "Mock, real Fusion, and not-run" in markdown
    with pytest.raises(FileExistsError):
        runner.write(report, tmp_path)


@pytest.mark.asyncio
async def test_started_adapter_exception_is_failed_not_not_run() -> None:
    adapter = FakeAdapter("b44b667e440da070081795cfcbfaf75de2a44251", fail=True)
    report = await PublicBenchmarkRunner({"faust_fusion360_mcp": adapter}).run(
        MANIFEST,
        config=PublicBenchmarkConfig(
            mode="real",
            confirm_real_benchmark=True,
            disposable_fixture_confirmed=True,
            include_faults=False,
            subject_ids=["faust_fusion360_mcp"],
        ),
    )
    assert report.summary["states"] == {"completed": 0, "failed": 6, "not_run": 0}


def test_manifest_cannot_smuggle_executable_commands(tmp_path: Path) -> None:
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    payload["subjects"][0]["command"] = "python exploit.py"
    path = tmp_path / "unsafe.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="executable"):
        load_public_manifest(path)


def test_completed_result_requires_real_correctness_evidence() -> None:
    with pytest.raises(ValidationError, match="task_success"):
        AdapterExecution(state="completed")
