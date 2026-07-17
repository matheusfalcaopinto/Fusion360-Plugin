from __future__ import annotations

import json
import os
from importlib.metadata import version as package_version
from pathlib import Path

import pytest
from pydantic import ValidationError

from benchmark.codex_driver import (
    EXECUTION_PATH_ENV,
    ROUTE_LOCK_ENV,
    CodexE2EDriver,
    CodexInvocation,
    _token_count,
    discover_codex_executable,
)
from benchmark.artifacts import BenchmarkArtifactStore, collect_environment
from benchmark.fixtures import SCRIPT_REGISTRY
from benchmark.loader import BenchmarkSuiteError, load_benchmark_suite
from benchmark.models import BenchmarkRunConfig, ExecutionObservation
from benchmark.runner import (
    BenchmarkExecutionError,
    BenchmarkRunner,
    CanonicalMockExecutor,
    IndependentEvidence,
)
from benchmark.statistics import _rollout_status


SUITE = (
    Path(__file__).parents[1]
    / "packages"
    / "fusion_agent_assets"
    / "benchmarks"
    / "benchmark_suite_v2.json"
)


def test_strict_v2_loader_has_required_cases_and_no_fallback(tmp_path: Path) -> None:
    suite = load_benchmark_suite(SUITE)
    assert suite.schema_version == "benchmark_suite.v2"
    assert len(suite.cases) == 13
    assert {
        "persistent_cold_first_read",
        "api_documentation",
        "document_summary",
        "targeted_inspection_medium",
        "targeted_inspection_large",
        "bounded_global_inspection_large",
        "targeted_token_inspection_large",
        "additive_cube",
        "additive_plate_features",
        "scoped_parameter_update",
        "destructive_request_blocked",
        "mutation_timeout_no_replay",
        "reconnect_manifest_drift",
    } == {case.id for case in suite.cases}

    with pytest.raises(FileNotFoundError):
        load_benchmark_suite(tmp_path / "missing.json")

    invalid = json.loads(SUITE.read_text(encoding="utf-8"))
    invalid["cases"][0]["script"] = "print('suite code must never run')"
    invalid_path = tmp_path / "invalid.json"
    invalid_path.write_text(json.dumps(invalid), encoding="utf-8")
    with pytest.raises(BenchmarkSuiteError, match="embedded executable"):
        load_benchmark_suite(invalid_path)

    unknown = json.loads(SUITE.read_text(encoding="utf-8"))
    unknown["cases"][0]["oracle_id"] = "unregistered_oracle"
    unknown_path = tmp_path / "unknown.json"
    unknown_path.write_text(json.dumps(unknown), encoding="utf-8")
    with pytest.raises(ValueError, match="unregistered"):
        load_benchmark_suite(unknown_path)


def test_stability_cases_use_code_owned_independent_budget_evidence() -> None:
    cold = SCRIPT_REGISTRY["persistent_cold_first_read"].profiles
    assert cold["safe_harness"].observation["transport"]["cold_first_read_ms"] == 1250
    assert cold["native_fast"].observation["transport"]["cold_first_read_ms"] == 480

    bounded = SCRIPT_REGISTRY["inspect_large_bounded"].profiles
    for profile in bounded.values():
        evidence = profile.observation["inspection"]
        assert evidence["visited_entities"] == 1000
        assert evidence["response_bytes"] <= 1024 * 1024
        assert evidence["physical_properties_access_count"] == 0
        assert evidence["complete"] is False

    targeted = SCRIPT_REGISTRY["inspect_large_by_token"].profiles
    for profile in targeted.values():
        evidence = profile.observation["inspection"]
        assert evidence["lookup_strategy"] == "entity_token"
        assert evidence["global_scan_count"] == 0
        assert evidence["visited_entities"] == 1


@pytest.mark.asyncio
async def test_internal_mock_is_deterministic_counterbalanced_and_writes_all_artifacts(
    tmp_path: Path,
) -> None:
    prior_lock = os.environ.get(ROUTE_LOCK_ENV)
    prior_path = os.environ.get(EXECUTION_PATH_ENV)
    config = BenchmarkRunConfig(repetitions=2, warmups=1, seed=9)

    first_runner = BenchmarkRunner(output_dir=tmp_path / "first")
    first = await first_runner.run_suite(
        SUITE, config=config, run_id="bench_deterministic01"
    )
    second_runner = BenchmarkRunner(output_dir=tmp_path / "second")
    second = await second_runner.run_suite(
        SUITE, config=config, run_id="bench_deterministic02"
    )

    assert len(first.report.trials) == 13 * 2 * 3

    def signature(run):
        return [
            (
                trial.case_id,
                trial.warmup,
                trial.repetition,
                trial.execution_path,
                trial.status,
                trial.metrics,
            )
            for trial in run.report.trials
        ]

    assert signature(first) == signature(second)
    assert first.report.summary == second.report.summary
    assert first.report.summary["measured_trial_count"] == 52
    assert first.report.summary["warmup_trial_count"] == 26
    assert first.report.summary["paired"]["pair_count"] == 26
    assert first.report.summary["paired"]["duration_delta_ms"]["p50"] < 0
    assert first.report.summary["paired"]["call_count_delta"]["p50"] < 0
    assert (
        first.report.summary["paired"]["duration_delta_ms"]["bootstrap_95"]["samples"]
        == 2000
    )
    assert first.report.summary["gates"]["all_required"] is True
    assert (
        first.report.summary["gates"]["one_initialize_per_session_generation"] is True
    )
    assert sum(trial.metrics["initialize_count"] for trial in first.report.trials) == 2
    assert first.report.summary["rollout"]["native_read"]["verified_trials"] == 0
    assert (
        first.report.summary["rollout"]["additive_fast_execute"]["verified_mutations"]
        == 0
    )
    assert first.report.summary["rollout"]["scoped_update"]["verified_mutations"] == 0
    assert all(trial.metrics["expectations_met"] for trial in first.report.trials)

    cube = [trial for trial in first.report.trials if trial.case_id == "additive_cube"]
    pair_orders: dict[str, list[str]] = {}
    for trial in cube:
        pair_orders.setdefault(trial.pair_id, []).append(trial.execution_path)
    assert len({tuple(order) for order in pair_orders.values()}) == 2

    assert first.report_path.exists()
    assert first.summary_path.exists()
    assert first.trials_path.exists()
    assert first.environment_path.exists()
    run_dir = first.report_path.parent
    assert len(list((run_dir / "traces").glob("*.json"))) == len(first.report.trials)
    assert len(list((run_dir / "oracles").glob("*.json"))) == len(first.report.trials)
    assert not list((run_dir.parent).glob(".*.tmp"))
    trace_text = next((run_dir / "traces").glob("*.json")).read_text(encoding="utf-8")
    assert '"observation"' not in trace_text
    assert "observation_redacted" in trace_text

    assert os.environ.get(ROUTE_LOCK_ENV) == prior_lock
    assert os.environ.get(EXECUTION_PATH_ENV) == prior_path


@pytest.mark.asyncio
async def test_baseline_is_validated_before_dispatch_and_requires_comparable_config_and_environment(
    tmp_path: Path,
) -> None:
    suite_output = tmp_path / "outputs"
    baseline_runner = BenchmarkRunner(
        output_dir=suite_output, environment_metadata={"git_commit": "abc"}
    )
    await baseline_runner.run_suite(
        SUITE,
        config=BenchmarkRunConfig(),
        run_id="bench_comparablebase01",
    )

    class CountingExecutor:
        def __init__(self) -> None:
            self.calls = 0
            self.inner = CanonicalMockExecutor()

        async def execute(self, context):
            self.calls += 1
            return await self.inner.execute(context)

    for run_id, config, metadata, expected in (
        (
            "bench_missingbefore01",
            BenchmarkRunConfig(baseline_run_id="bench_doesnotexist01"),
            {"git_commit": "abc"},
            "doesnotexist",
        ),
        (
            "bench_pathmismatch01",
            BenchmarkRunConfig(
                execution_paths=["native_fast"],
                baseline_run_id="bench_comparablebase01",
            ),
            {"git_commit": "abc"},
            "execution_paths",
        ),
        (
            "bench_modelmismatch1",
            BenchmarkRunConfig(
                model="different", baseline_run_id="bench_comparablebase01"
            ),
            {"git_commit": "abc"},
            "model",
        ),
        (
            "bench_reasonmismatch1",
            BenchmarkRunConfig(
                reasoning_effort="medium", baseline_run_id="bench_comparablebase01"
            ),
            {"git_commit": "abc"},
            "reasoning_effort",
        ),
        (
            "bench_envmismatch001",
            BenchmarkRunConfig(baseline_run_id="bench_comparablebase01"),
            {"git_commit": "different"},
            "environment.git_commit",
        ),
    ):
        counting = CountingExecutor()
        runner = BenchmarkRunner(
            output_dir=suite_output,
            route_executors={"safe_harness": counting, "native_fast": counting},
            environment_metadata=metadata,
        )
        with pytest.raises(BenchmarkExecutionError, match="benchmark execution failed"):
            await runner.run_suite(SUITE, config=config, run_id=run_id)
        assert counting.calls == 0
        aborted = json.loads(
            (suite_output / "benchmarks" / run_id / "report.json").read_text(
                encoding="utf-8"
            )
        )
        assert aborted["status"] == "aborted"
        assert aborted["trials"] == []

    comparable = await BenchmarkRunner(
        output_dir=suite_output,
        environment_metadata={"git_commit": "abc"},
    ).run_suite(
        SUITE,
        config=BenchmarkRunConfig(baseline_run_id="bench_comparablebase01"),
        run_id="bench_comparablecur01",
    )
    assert (
        comparable.report.summary["gate_details"]["safe_harness_p90_regression"][
            "status"
        ]
        == "measured"
    )

    baseline_environment = baseline_runner.artifacts.read(
        run_id="bench_comparablebase01", view="environment"
    )["environment"]
    baseline_digest = baseline_runner.artifacts.read(
        run_id="bench_comparablebase01", view="report", offset=0, limit=1
    )["report"]["suite_fingerprint"]
    for name, config in (
        ("driver", BenchmarkRunConfig(driver="codex_e2e", model="gpt-test")),
        ("mode", BenchmarkRunConfig(mode="real", confirm_real_benchmark=True)),
    ):
        with pytest.raises(BenchmarkExecutionError, match=name):
            baseline_runner._baseline_safe_p90(
                "bench_comparablebase01",
                suite_id="fusion_agent_core_v2",
                suite_digest=baseline_digest,
                current_config=config,
                current_environment=baseline_environment,
            )


@pytest.mark.asyncio
async def test_rollout_requires_real_independent_expectation_and_dispatch_evidence(
    tmp_path: Path,
) -> None:
    source = await BenchmarkRunner(output_dir=tmp_path).run_suite(
        SUITE,
        config=BenchmarkRunConfig(),
        run_id="bench_rolloutsource01",
    )
    native = [
        trial for trial in source.report.trials if trial.execution_path == "native_fast"
    ]
    read = next(trial for trial in native if trial.risk == "read_only")
    additive = next(trial for trial in native if trial.risk == "additive")
    scoped = next(trial for trial in native if trial.risk == "scoped_update")

    def copies(trial, count: int, label: str):
        return [
            trial.model_copy(
                update={
                    "trial_id": f"{label}_{index:03d}",
                    "mode": "real",
                    "metrics": {**trial.metrics, "expectations_met": True},
                }
            )
            for index in range(count)
        ]

    trials = (
        copies(read, 50, "read")
        + copies(additive, 30, "add")
        + copies(scoped, 20, "scope")
    )
    gates = {
        "oracle_100_percent": True,
        "zero_safety_regressions": True,
        "fast_success_within_2pp": True,
        "mutation_never_replayed": True,
        "expectations_met": True,
        "codex_critical_metrics_independently_observed": True,
        "fast_read_p50_reduction_at_least_50_percent": True,
        "fast_read_p90_reduction_at_least_30_percent": True,
    }
    eligible = _rollout_status(trials, gates)
    assert eligible["native_read"]["eligible"] is True
    assert eligible["additive_fast_execute"]["eligible"] is True
    assert eligible["scoped_update"]["eligible"] is True

    replayed = _rollout_status(trials, {**gates, "mutation_never_replayed": False})
    assert replayed["native_read"]["eligible"] is True
    assert replayed["additive_fast_execute"]["eligible"] is False
    assert replayed["scoped_update"]["eligible"] is False

    unmet = _rollout_status(trials, {**gates, "expectations_met": False})
    assert all(
        not value["eligible"]
        for key, value in unmet.items()
        if key != "always_safe_harness"
    )

    unobserved_codex = [
        trial.model_copy(
            update={
                "driver": "codex_e2e",
                "metrics": {**trial.metrics, "independent_metric_fields": []},
            }
        )
        for trial in trials
    ]
    unobserved = _rollout_status(unobserved_codex, gates)
    assert unobserved["native_read"]["verified_trials"] == 0
    assert unobserved["additive_fast_execute"]["verified_mutations"] == 0
    assert unobserved["scoped_update"]["verified_mutations"] == 0


@pytest.mark.asyncio
async def test_independent_metrics_override_executor_and_gate_dispatch_and_expectations(
    tmp_path: Path,
) -> None:
    async def observer(context) -> IndependentEvidence:
        profile = SCRIPT_REGISTRY[context.case.script_id].profiles[
            context.execution_path
        ]
        return IndependentEvidence(
            observation=profile.observation,
            metrics={
                "call_count": 999,
                "mutation_dispatch_count": 2
                if context.execution_path == "native_fast"
                else profile.mutation_dispatch_count,
                "duplicate_count": 0,
            },
            trace={"source": "independent-test"},
        )

    run = await BenchmarkRunner(
        output_dir=tmp_path, oracle_observer=observer
    ).run_suite(
        SUITE,
        config=BenchmarkRunConfig(),
        run_id="bench_independent01",
    )

    assert all(trial.metrics["call_count"] == 999 for trial in run.report.trials)
    assert all(
        "call_count" in trial.metrics["independent_metric_fields"]
        for trial in run.report.trials
    )
    assert run.report.summary["gates"]["mutation_never_replayed"] is False
    assert run.report.summary["gates"]["expectations_met"] is False
    assert run.report.summary["gates"]["all_required"] is False


@pytest.mark.asyncio
async def test_aborted_trial_preserves_completed_evidence(tmp_path: Path) -> None:
    class FailSecondExecutor:
        def __init__(self) -> None:
            self.calls = 0
            self.inner = CanonicalMockExecutor()

        async def execute(self, context):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("intentional second-trial failure")
            return await self.inner.execute(context)

    executor = FailSecondExecutor()
    runner = BenchmarkRunner(
        output_dir=tmp_path,
        route_executors={"safe_harness": executor},
    )
    with pytest.raises(BenchmarkExecutionError, match="benchmark execution failed"):
        await runner.run_suite(
            SUITE,
            config=BenchmarkRunConfig(execution_paths=["safe_harness"]),
            run_id="bench_abortpersist01",
        )

    run_dir = tmp_path / "benchmarks" / "bench_abortpersist01"
    report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    assert report["status"] == "aborted"
    assert "intentional second-trial failure" not in json.dumps(report)
    assert len(report["trials"]) == 1
    assert len(list((run_dir / "traces").glob("*.json"))) == 2
    assert not list((run_dir.parent).glob(".*.tmp"))


def test_environment_uses_installed_distribution_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FUSION_AGENT_WHEEL_VERSION", raising=False)
    assert collect_environment()["wheel_version"] == package_version(
        "fusion-agent-harness"
    )


@pytest.mark.asyncio
async def test_runner_uses_constructor_environment_snapshot_after_process_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = BenchmarkRunner(
        output_dir=tmp_path,
        process_environment={
            "GIT_COMMIT": "startup-commit",
            "FUSION_VERSION": "startup-fusion",
        },
    )
    monkeypatch.setenv("GIT_COMMIT", "drifted-commit")
    monkeypatch.setenv("FUSION_VERSION", "drifted-fusion")

    run = await runner.run_suite(
        SUITE,
        config=BenchmarkRunConfig(execution_paths=["safe_harness"]),
        run_id="bench_environment_snapshot01",
    )
    environment = runner.artifacts.read(run_id=run.report.run_id, view="environment")[
        "environment"
    ]

    assert environment["git_commit"] == "startup-commit"
    assert environment["fusion_version"] == "startup-fusion"


@pytest.mark.asyncio
async def test_artifact_publication_is_atomic_on_write_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import benchmark.artifacts as artifacts_module

    source = await BenchmarkRunner(output_dir=tmp_path / "source").run_suite(
        SUITE,
        config=BenchmarkRunConfig(execution_paths=["safe_harness"]),
        run_id="bench_atomsource001",
    )
    destination = tmp_path / "destination"
    report = source.report.model_copy(
        update={
            "run_id": "bench_atomicfailure01",
            "artifact_dir": destination / "benchmarks" / "bench_atomicfailure01",
        }
    )
    original_write = artifacts_module._atomic_write

    def fail_summary(path: Path, text: str) -> None:
        if path.name == "summary.md":
            raise OSError("intentional artifact failure")
        original_write(path, text)

    monkeypatch.setattr(artifacts_module, "_atomic_write", fail_summary)
    store = BenchmarkArtifactStore(destination)
    with pytest.raises(OSError, match="intentional artifact failure"):
        store.write_run(report, environment={}, traces={}, oracles={})

    assert not (destination / "benchmarks" / "bench_atomicfailure01").exists()
    assert not list((destination / "benchmarks").glob(".*.tmp"))


@pytest.mark.asyncio
async def test_paginated_v2_reads_and_explicit_legacy_report(tmp_path: Path) -> None:
    runner = BenchmarkRunner(output_dir=tmp_path)
    run = await runner.run_suite(
        SUITE,
        config=BenchmarkRunConfig(
            execution_paths=["native_fast"], repetitions=1, seed=1
        ),
        run_id="bench_pagination001",
    )
    page = runner.read_report(
        run_id=run.report.run_id, view="trials", offset=2, limit=3
    )
    assert page["total"] == 13
    assert len(page["items"]) == 3
    report_page = runner.read_report(
        run_id=run.report.run_id, view="report", offset=0, limit=2
    )
    assert len(report_page["trials"]) == 2
    assert "trials" not in report_page["report"]
    summary = runner.read_report(run_id=run.report.run_id, view="summary")["text"]
    assert "Benchmark" in summary
    assert "â€" not in summary
    assert "- PASS - `" in summary

    legacy_path = tmp_path / "benchmark_report.json"
    legacy_path.write_text(
        json.dumps([{"id": str(index)} for index in range(5)]), encoding="utf-8"
    )
    legacy = runner.read_report(offset=1, limit=2)
    assert legacy["legacy"] is True
    assert legacy["total"] == 5
    assert [item["id"] for item in legacy["items"]] == ["1", "2"]

    with pytest.raises(ValueError, match="run_id"):
        runner.read_report(run_id="../escape")


@pytest.mark.asyncio
async def test_real_mutation_confirmation_and_executor_requirements_fail_before_execution(
    tmp_path: Path,
) -> None:
    runner = BenchmarkRunner(output_dir=tmp_path)
    with pytest.raises(BenchmarkExecutionError, match="benchmark execution failed"):
        await runner.run_suite(
            SUITE,
            config=BenchmarkRunConfig(mode="real", repetitions=1),
            run_id="bench_realblocked01",
        )
    with pytest.raises(BenchmarkExecutionError, match="benchmark execution failed"):
        await runner.run_suite(
            SUITE,
            config=BenchmarkRunConfig(
                mode="real", repetitions=1, confirm_real_benchmark=True
            ),
            run_id="bench_realblocked02",
        )
    for run_id in ("bench_realblocked01", "bench_realblocked02"):
        report = json.loads(
            (tmp_path / "benchmarks" / run_id / "report.json").read_text(
                encoding="utf-8"
            )
        )
        assert report["status"] == "aborted"
        assert report["trials"] == []


def test_codex_driver_discovery_and_command_are_fixed_without_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex = tmp_path / "codex.exe"
    codex.write_bytes(b"not executed")
    env = {"CODEX_BIN": str(codex), "PATH": "", "LOCALAPPDATA": str(tmp_path / "none")}
    assert discover_codex_executable(env) == codex.resolve()

    base_environment = {"PATH": "fixed", "BENCHMARK_SENTINEL": "startup"}
    driver = CodexE2EDriver(
        codex_bin=codex,
        cwd=tmp_path,
        base_environment=base_environment,
    )
    case = load_benchmark_suite(SUITE).cases[0]
    command, child_env = driver.build_command(
        case=case,
        execution_path="native_fast",
        mode="mock",
        model="gpt-test",
        reasoning_effort="high",
        run_id="bench_command0001",
        trial_id="api_r000_native_fast",
    )
    assert command[:6] == [
        str(codex.resolve()),
        "exec",
        "--ephemeral",
        "--json",
        "--sandbox",
        "read-only",
    ]
    assert command[command.index("-m") + 1] == "gpt-test"
    assert 'model_reasoning_effort="high"' in command
    assert child_env == base_environment
    assert ROUTE_LOCK_ENV not in child_env
    assert EXECUTION_PATH_ENV not in child_env
    assert not any(key.startswith("FUSION_AGENT_BENCHMARK_") for key in child_env)
    assert "FUSION_AGENT_FAST_PATH_MODE" not in child_env

    with pytest.raises(ValidationError, match="model is required"):
        BenchmarkRunConfig(driver="codex_e2e")

    assert _token_count([{"usage": {"total_tokens": 123}}, {"tokens_used": 99}]) == 123


class _FakeCodexDriver:
    def __init__(self) -> None:
        self.calls = 0

    async def run(self, **_: object) -> CodexInvocation:
        self.calls += 1
        return CodexInvocation(
            observation=ExecutionObservation(
                status="read_succeeded",
                execution_success=True,
                duration_ms=1,
                observation={
                    "api_documentation": {"matches": 1, "class": "Application"}
                },
            ),
            trace={"fake_codex": True},
        )


def _single_case_suite(tmp_path: Path) -> Path:
    payload = json.loads(SUITE.read_text(encoding="utf-8"))
    payload["suite_id"] = "codex_e2e_observer_gate"
    payload["cases"] = [payload["cases"][0]]
    path = tmp_path / "codex_e2e_suite.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_codex_e2e_fails_closed_without_independent_observer(
    tmp_path: Path,
) -> None:
    driver = _FakeCodexDriver()
    runner = BenchmarkRunner(output_dir=tmp_path / "outputs", codex_driver=driver)

    with pytest.raises(BenchmarkExecutionError, match="benchmark execution failed"):
        await runner.run_suite(
            _single_case_suite(tmp_path),
            config=BenchmarkRunConfig(
                driver="codex_e2e",
                model="gpt-test",
                execution_paths=["native_fast"],
            ),
            run_id="bench_codexclosed01",
        )

    assert driver.calls == 0
    report = json.loads(
        (
            tmp_path / "outputs" / "benchmarks" / "bench_codexclosed01" / "report.json"
        ).read_text(encoding="utf-8")
    )
    assert report["status"] == "aborted"


@pytest.mark.asyncio
async def test_oracle_observer_contract_has_no_executor_argument(
    tmp_path: Path,
) -> None:
    calls = 0

    async def independent_observer(context: object) -> dict[str, object]:
        nonlocal calls
        del context
        calls += 1
        # Deliberately contradict the executor's perfect self-reported result.
        return {"api_documentation": {"matches": 0, "class": "WrongClass"}}

    runner = BenchmarkRunner(
        output_dir=tmp_path / "outputs",
        oracle_observer=independent_observer,
    )
    run = await runner.run_suite(
        _single_case_suite(tmp_path),
        config=BenchmarkRunConfig(
            driver="internal",
            execution_paths=["native_fast"],
        ),
        run_id="bench_codexevidence01",
    )

    assert calls == 1
    assert run.report.trials[0].oracle.passed is False
    assert run.report.trials[0].final_success is False
    assert run.report.summary["gates"]["all_required"] is False
