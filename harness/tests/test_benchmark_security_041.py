from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from benchmark.filesystem import (
    atomic_write_text,
    list_files,
    physical_artifact_name,
    read_text,
)
from benchmark.loader import load_benchmark_suite
from benchmark.models import BenchmarkResult, BenchmarkRunConfig
from benchmark.provenance import RevisionIdentity, source_manifest_digest
from benchmark.public import (
    AdapterExecution,
    AdapterPreflight,
    NormalizedPublicMetrics,
    PublicBenchmarkConfig,
    PublicBenchmarkRunner,
    load_public_manifest,
)
from benchmark.runner import BenchmarkRunner
from fusion_agent_assets import asset_root


MANIFEST = asset_root("benchmarks") / "public_competitors_v1.json"
SUITE = asset_root("benchmarks") / "benchmark_suite_v2.json"
COMMIT = "a" * 40
DIGEST = "b" * 64


def _exact_workspace_identity() -> RevisionIdentity:
    return RevisionIdentity(
        scheme="source-manifest-v1",
        expected_git_commit=COMMIT,
        observed_git_commit=COMMIT,
        expected_source_manifest_sha256=DIGEST,
        observed_source_manifest_sha256=DIGEST,
        tracked_state="clean",
    )


class _Adapter:
    def __init__(
        self,
        revision: str,
        *,
        identity: RevisionIdentity | None = None,
    ) -> None:
        self.revision = revision
        self.identity = identity
        self.execute_calls = 0

    async def preflight(self, _subject, _config) -> AdapterPreflight:  # noqa: ANN001
        return AdapterPreflight(
            ready=True,
            observed_revision=self.revision,
            revision_identity=self.identity,
        )

    async def execute(self, _subject, _task, _config) -> AdapterExecution:  # noqa: ANN001
        self.execute_calls += 1
        return AdapterExecution(
            state="completed",
            metrics=NormalizedPublicMetrics(task_success=True, oracle_passed=True),
            independent_oracle=True,
            evidence={"oracle": "independent"},
        )


@pytest.mark.asyncio
async def test_scoreable_requires_own_subject_and_same_task_comparator() -> None:
    own = _Adapter(COMMIT, identity=_exact_workspace_identity())
    comparator = _Adapter("b44b667e440da070081795cfcbfaf75de2a44251")
    runner = PublicBenchmarkRunner(
        {
            "fusion_agent_codex": own,
            "faust_fusion360_mcp": comparator,
        }
    )
    report = await runner.run(
        MANIFEST,
        config=PublicBenchmarkConfig(
            mode="mock",
            include_faults=False,
            subject_ids=["fusion_agent_codex", "faust_fusion360_mcp"],
        ),
    )

    assert report.summary["scoreable"] is True
    assert report.summary["scoreability"]["own_subject_complete"] is True
    assert report.summary["scoreability"]["eligible_comparators"] == [
        "faust_fusion360_mcp"
    ]


@pytest.mark.asyncio
async def test_completed_comparator_without_own_subject_is_not_scoreable() -> None:
    comparator = _Adapter("b44b667e440da070081795cfcbfaf75de2a44251")
    report = await PublicBenchmarkRunner({"faust_fusion360_mcp": comparator}).run(
        MANIFEST,
        config=PublicBenchmarkConfig(
            mode="mock",
            include_faults=False,
            subject_ids=["faust_fusion360_mcp"],
        ),
    )
    assert report.summary["states"]["completed"] == 6
    assert report.summary["scoreable"] is False


@pytest.mark.asyncio
async def test_explicit_workspace_revision_mismatch_blocks_before_execution() -> None:
    mismatch = _exact_workspace_identity().model_copy(
        update={"observed_source_manifest_sha256": "c" * 64}
    )
    own = _Adapter(COMMIT, identity=mismatch)
    report = await PublicBenchmarkRunner({"fusion_agent_codex": own}).run(
        MANIFEST,
        config=PublicBenchmarkConfig(
            mode="mock",
            include_faults=False,
            subject_ids=["fusion_agent_codex"],
        ),
    )

    assert own.execute_calls == 0
    assert {item.state for item in report.results} == {"not_run"}
    assert all(
        item.reason and item.reason.startswith("revision_mismatch:")
        for item in report.results
    )


def test_source_manifest_digest_is_ordered_and_content_exact(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("print('a')\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b\n", encoding="utf-8")
    first = source_manifest_digest(tmp_path, ["b.txt", "a.py"])
    second = source_manifest_digest(tmp_path, ["a.py", "b.txt"])
    assert first == second
    (tmp_path / "a.py").write_text("print('changed')\n", encoding="utf-8")
    assert source_manifest_digest(tmp_path, ["a.py", "b.txt"]) != first


def test_atomic_write_handles_logical_path_longer_than_320_characters(
    tmp_path: Path,
) -> None:
    path = tmp_path
    for index in range(5):
        path /= f"segment-{index}-" + ("x" * 60)
    target = path / "logical-benchmark-artifact.json"
    assert len(str(target.resolve())) > 320
    atomic_write_text(target, '{"ok": true}\n')
    assert read_text(target) == '{"ok": true}\n'


def test_physical_artifact_names_are_short_stable_and_collision_resistant() -> None:
    logical = "trial." + ("very-long-segment." * 30)
    first = physical_artifact_name(logical, suffix=".json")
    second = physical_artifact_name(logical, suffix=".json")
    other = physical_artifact_name(logical + "other", suffix=".json")
    assert first == second
    assert first != other
    assert len(first) <= 64


@pytest.mark.asyncio
async def test_benchmark_store_publishes_complete_run_beyond_320_characters(
    tmp_path: Path,
) -> None:
    output = tmp_path
    for index in range(5):
        output /= f"benchmark-root-{index}-" + ("y" * 55)
    assert len(str(output.resolve())) > 320

    runner = BenchmarkRunner(output_dir=output)
    run = await runner.run_suite(
        SUITE,
        config=BenchmarkRunConfig(execution_paths=["safe_harness"]),
        run_id="bench_longpath041",
    )

    assert '"status": "completed"' in read_text(run.report_path)
    trace_root = run.report_path.parent / "traces"
    trace_names = [path.name for path in list_files(trace_root, suffix=".json")]
    assert len(trace_names) == 14
    assert all(len(name) <= 64 for name in trace_names)
    assert runner.read_report(run_id="bench_longpath041", view="traces")["total"] == 14


@pytest.mark.asyncio
async def test_public_reports_use_short_physical_names_beyond_320_characters(
    tmp_path: Path,
) -> None:
    output = tmp_path
    for index in range(5):
        output /= f"public-report-root-{index}-" + ("z" * 55)
    comparator = _Adapter("b44b667e440da070081795cfcbfaf75de2a44251")
    report = await PublicBenchmarkRunner({"faust_fusion360_mcp": comparator}).run(
        MANIFEST,
        config=PublicBenchmarkConfig(
            mode="mock",
            include_faults=False,
            subject_ids=["faust_fusion360_mcp"],
        ),
    )

    json_path, markdown_path = PublicBenchmarkRunner.write(report, output)

    assert len(str(json_path.resolve())) > 320
    assert len(json_path.name) <= 64
    assert len(markdown_path.name) <= 64
    assert json.loads(read_text(json_path))["run_id"] == report.run_id
    assert report.run_id in read_text(markdown_path)
    with pytest.raises(FileExistsError):
        PublicBenchmarkRunner.write(report, output)


def test_long_checkout_inputs_cover_suite_manifest_and_provenance(
    tmp_path: Path,
) -> None:
    checkout = tmp_path
    for index in range(5):
        checkout /= f"onedrive-checkout-root-{index}-" + ("i" * 55)
    suite_path = checkout / "benchmark-suite.json"
    manifest_path = checkout / "public-manifest.json"
    tracked_path = checkout / "tracked-source.py"
    assert len(str(tracked_path.resolve())) > 320
    atomic_write_text(suite_path, read_text(SUITE))
    atomic_write_text(manifest_path, read_text(MANIFEST))
    atomic_write_text(tracked_path, "VALUE = 1\n")

    suite = load_benchmark_suite(suite_path)
    manifest, manifest_digest = load_public_manifest(manifest_path)
    first_source_digest = source_manifest_digest(checkout, ["tracked-source.py"])
    atomic_write_text(tracked_path, "VALUE = 2\n")

    assert suite.schema_version == "benchmark_suite.v2"
    assert manifest.schema_version == "public_benchmark.v1"
    assert len(manifest_digest) == 64
    assert (
        source_manifest_digest(checkout, ["tracked-source.py"]) != first_source_digest
    )


def test_legacy_report_writer_is_atomic_beyond_320_characters(tmp_path: Path) -> None:
    output = tmp_path
    for index in range(5):
        output /= f"legacy-report-root-{index}-" + ("l" * 55)
    target = output / "legacy-results.json"
    assert len(str(target.resolve())) > 320
    result = BenchmarkResult(
        id="legacy-case",
        prompt="legacy long-path control",
        status="completed",
        first_pass_success=True,
        final_success=True,
        repair_loop_count=0,
    )

    BenchmarkRunner(output_dir=tmp_path).write_report([result], target)

    payload = json.loads(read_text(target))
    assert payload[0]["id"] == "legacy-case"


@pytest.mark.asyncio
async def test_public_report_concurrent_writers_never_overwrite_winner(
    tmp_path: Path,
) -> None:
    comparator = _Adapter("b44b667e440da070081795cfcbfaf75de2a44251")
    report = await PublicBenchmarkRunner({"faust_fusion360_mcp": comparator}).run(
        MANIFEST,
        config=PublicBenchmarkConfig(
            mode="mock",
            include_faults=False,
            subject_ids=["faust_fusion360_mcp"],
        ),
    )
    output = tmp_path / "concurrent-public"
    contender_count = 8
    barrier = Barrier(contender_count)

    def write_once() -> str:
        barrier.wait(timeout=10)
        try:
            PublicBenchmarkRunner.write(report, output)
        except FileExistsError:
            return "exists"
        return "created"

    with ThreadPoolExecutor(max_workers=contender_count) as executor:
        outcomes = list(executor.map(lambda _: write_once(), range(contender_count)))

    assert outcomes.count("created") == 1
    assert outcomes.count("exists") == contender_count - 1
    files = list_files(output)
    assert len(files) == 2
    json_path = next(path for path in files if path.suffix == ".json")
    assert json.loads(read_text(json_path))["run_id"] == report.run_id
