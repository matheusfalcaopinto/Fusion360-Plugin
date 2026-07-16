from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

from benchmark_parametric_ab.causal_benchmark import (
    CausalBenchmarkRunner,
    CausalExecutionError,
    CausalRunConfig,
    CausalSuiteError,
    ExecutionObservation,
    OracleObservation,
    freeze_planner_submission,
    load_causal_suite,
)
from benchmark_parametric_ab.causal_benchmark.runner import LAYERS, ROUTE_LOCK_ENV


ROOT = Path(__file__).parents[1]
EXAMPLE = ROOT / "benchmark_parametric_ab" / "causal_suite.example.json"


class RecordingExecutor:
    def __init__(self, *, wrong_route: bool = False) -> None:
        self.contexts = []
        self.route_env_values: list[str | None] = []
        self.wrong_route = wrong_route

    async def execute(self, context):
        self.contexts.append(context)
        route = os.environ.get(ROUTE_LOCK_ENV)
        self.route_env_values.append(route)
        duration = 10.0 if context.arm_id == "claude" else 20.0
        if context.layer == "native_e2e" and self.wrong_route:
            route = "wrong_route"
        return ExecutionObservation(
            status="succeeded",
            execution_success=True,
            duration_ms=duration,
            planning_ms=duration / 2,
            execution_ms=duration / 2,
            call_count=1 if context.arm_id == "claude" else 2,
            script_count=1,
            mutation_dispatch_count=0 if context.risk == "read_only" else 1,
            observed_runner_id=context.runner_id,
            observed_route_lock=route,
            consumed_artifacts=dict(context.artifacts),
        )


class SensitiveTraceExecutor(RecordingExecutor):
    async def execute(self, context):
        observation = await super().execute(context)
        return observation.model_copy(
            update={
                "trace": {
                    "script": "print('private')",
                    "nested": {"access_token": "top-secret", "duration_ms": 12},
                    "binary": b"private-bytes",
                }
            }
        )


class PassingOracle:
    def __init__(self) -> None:
        self.contexts = []

    async def observe(self, context):
        self.contexts.append(context)
        return OracleObservation(passed=True, checks={"independent": True})


def _runner(tmp_path: Path, executor: RecordingExecutor | None = None):
    executor = executor or RecordingExecutor()
    oracle = PassingOracle()
    return (
        CausalBenchmarkRunner(
            output_dir=tmp_path,
            executors={layer: executor for layer in LAYERS},
            oracles={"nema17_bracket_oracle": oracle},
            environment={"test": True},
        ),
        executor,
        oracle,
    )


def _copy_example(tmp_path: Path) -> Path:
    source = ROOT / "benchmark_parametric_ab"
    destination = tmp_path / "suite"
    destination.mkdir()
    shutil.copy2(source / "causal_suite.example.json", destination / "suite.json")
    shutil.copytree(source / "causal_artifacts", destination / "causal_artifacts")
    return destination / "suite.json"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_example_suite_is_strict_hash_frozen_and_records_explicit_models() -> None:
    suite = load_causal_suite(EXAMPLE)
    assert suite.schema_version == "fusion_causal_suite.v1"
    assert [arm.id for arm in suite.arms] == ["claude", "codex"]
    assert suite.arms[0].model == "Fable 5 Alto"
    assert suite.arms[1].model == "gpt-5.6-sol"
    assert suite.arms[1].reasoning_profile == "ultra"
    assert suite.cases[0].planner_isolated.runner_id == "common_fusion_runner_v1"


@pytest.mark.parametrize(
    "mutation", ["unknown_property", "hash_mismatch", "planner_schema"]
)
def test_invalid_suite_or_artifact_fails_closed(tmp_path: Path, mutation: str) -> None:
    suite_path = _copy_example(tmp_path)
    payload = json.loads(suite_path.read_text(encoding="utf-8"))
    if mutation == "unknown_property":
        payload["unexpected"] = True
        suite_path.write_text(json.dumps(payload), encoding="utf-8")
    elif mutation == "hash_mismatch":
        artifact = suite_path.parent / "causal_artifacts" / "shared_reference_script.py"
        artifact.write_text(
            artifact.read_text(encoding="utf-8") + "# changed\n", encoding="utf-8"
        )
    else:
        plan = suite_path.parent / "causal_artifacts" / "arm_a_plan.json"
        plan_payload = json.loads(plan.read_text(encoding="utf-8"))
        plan_payload["unexpected"] = True
        plan.write_text(json.dumps(plan_payload), encoding="utf-8")
        payload["cases"][0]["planner_isolated"]["artifacts"][0]["plan"]["sha256"] = (
            _sha(plan)
        )
        suite_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(CausalSuiteError):
        load_causal_suite(suite_path)


def test_artifact_reference_must_remain_relative_even_when_absolute_path_is_inside_suite(
    tmp_path: Path,
) -> None:
    suite_path = _copy_example(tmp_path)
    payload = json.loads(suite_path.read_text(encoding="utf-8"))
    artifact = (
        suite_path.parent / "causal_artifacts" / "shared_reference_script.py"
    ).resolve()
    payload["cases"][0]["transport_replay"]["script"]["path"] = str(artifact)
    suite_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(CausalSuiteError, match="must be relative"):
        load_causal_suite(suite_path)


@pytest.mark.asyncio
async def test_three_layers_are_counterbalanced_reproducible_and_use_fresh_trial_ids(
    tmp_path: Path,
) -> None:
    config = CausalRunConfig(repetitions=2, warmups=1, seed=9)
    first_runner, first_executor, first_oracle = _runner(tmp_path / "first")
    first = await first_runner.run_suite(
        EXAMPLE, config=config, run_id="causal_reproducible01"
    )
    second_runner, second_executor, _ = _runner(tmp_path / "second")
    second = await second_runner.run_suite(
        EXAMPLE, config=config, run_id="causal_reproducible02"
    )

    assert len(first.report.trials) == 18
    assert first.report.summary["measured_trial_count"] == 12
    assert first.report.summary["warmup_trial_count"] == 6
    measured = [trial for trial in first.report.trials if not trial.warmup]
    measured_first_arms = [trial.arm_id for trial in measured if trial.order_index == 0]
    assert measured_first_arms.count("claude") == 3
    assert measured_first_arms.count("codex") == 3

    def signature(contexts):
        return [
            (
                item.case_id,
                item.layer,
                item.warmup,
                item.repetition,
                item.order_index,
                item.arm_id,
            )
            for item in contexts
        ]

    assert signature(first_executor.contexts) == signature(second_executor.contexts)
    first_ids = {trial.trial_id for trial in first.report.trials}
    second_ids = {trial.trial_id for trial in second.report.trials}
    assert len(first_ids) == 18
    assert first_ids.isdisjoint(second_ids)
    assert len(first_oracle.contexts) == 18
    assert first.report.summary["gates"]["all_required"] is True
    environment = json.loads(first.environment_path.read_text(encoding="utf-8"))
    assert environment["arms"][1]["model"] == "gpt-5.6-sol"
    assert environment["arms"][1]["reasoning_profile"] == "ultra"
    for layer in LAYERS:
        paired = first.report.summary["layers"][layer]["paired"]
        assert paired["pair_count"] == 2
        assert paired["duration_ms_b_minus_a"]["p50"] == 10.0
        assert paired["call_count_b_minus_a"]["p50"] == 1.0
    assert first.report_path.exists()
    assert first.trials_path.exists()
    assert first.environment_path.exists()
    assert len(list((first.run_dir / "trials").glob("*.json"))) == 18


@pytest.mark.asyncio
async def test_layer_contracts_share_replay_and_runner_but_lock_native_routes(
    tmp_path: Path,
) -> None:
    runner, executor, _ = _runner(tmp_path)
    prior = os.environ.get(ROUTE_LOCK_ENV)
    await runner.run_suite(
        EXAMPLE,
        config=CausalRunConfig(repetitions=1, warmups=0, seed=4),
        run_id="causal_layercontracts01",
    )
    replay = [item for item in executor.contexts if item.layer == "transport_replay"]
    planner = [item for item in executor.contexts if item.layer == "planner_isolated"]
    native = [item for item in executor.contexts if item.layer == "native_e2e"]
    assert len(replay) == len(planner) == len(native) == 2
    assert replay[0].artifacts == replay[1].artifacts
    assert {item.runner_id for item in replay} == {"common_fusion_runner_v1"}
    assert planner[0].artifacts != planner[1].artifacts
    assert {item.runner_id for item in planner} == {"common_fusion_runner_v1"}
    assert {item.route_lock for item in native} == {
        "claude_fusion_connector",
        "codex_fusion_agent",
    }
    native_env = [
        value
        for context, value in zip(executor.contexts, executor.route_env_values)
        if context.layer == "native_e2e"
    ]
    assert set(native_env) == {"claude_fusion_connector", "codex_fusion_agent"}
    assert os.environ.get(ROUTE_LOCK_ENV) == prior


@pytest.mark.asyncio
async def test_invalid_suite_does_not_call_any_executor(tmp_path: Path) -> None:
    suite_path = _copy_example(tmp_path)
    payload = json.loads(suite_path.read_text(encoding="utf-8"))
    payload["schema_version"] = "wrong"
    suite_path.write_text(json.dumps(payload), encoding="utf-8")
    runner, executor, oracle = _runner(tmp_path / "outputs")
    with pytest.raises(CausalSuiteError):
        await runner.run_suite(suite_path)
    assert executor.contexts == []
    assert oracle.contexts == []
    assert not (tmp_path / "outputs").exists()


@pytest.mark.asyncio
async def test_native_route_mismatch_aborts_without_retry_and_restores_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ROUTE_LOCK_ENV, raising=False)
    executor = RecordingExecutor(wrong_route=True)
    runner, _, _ = _runner(tmp_path, executor)
    with pytest.raises(CausalExecutionError, match="route-lock mismatch"):
        await runner.run_suite(
            EXAMPLE,
            config=CausalRunConfig(repetitions=1, warmups=0, seed=1),
            run_id="causal_routemismatch01",
        )
    assert os.environ.get(ROUTE_LOCK_ENV) is None
    assert len([item for item in executor.contexts if item.layer == "native_e2e"]) == 1
    report = json.loads(
        (tmp_path / "causal_routemismatch01" / "report.json").read_text(
            encoding="utf-8"
        )
    )
    assert report["status"] == "aborted"
    assert len(report["trials"]) == 4
    assert "route-lock mismatch" in report["error"]["message"]


@pytest.mark.asyncio
async def test_missing_oracle_fails_before_any_dispatch_and_writes_aborted_report(
    tmp_path: Path,
) -> None:
    executor = RecordingExecutor()
    runner = CausalBenchmarkRunner(
        output_dir=tmp_path,
        executors={layer: executor for layer in LAYERS},
        oracles={},
    )
    with pytest.raises(CausalExecutionError, match="missing independent oracles"):
        await runner.run_suite(EXAMPLE, run_id="causal_missingoracle01")
    assert executor.contexts == []
    report = json.loads(
        (tmp_path / "causal_missingoracle01" / "report.json").read_text(
            encoding="utf-8"
        )
    )
    assert report["status"] == "aborted"
    assert report["trials"] == []


@pytest.mark.asyncio
async def test_execution_traces_are_recursively_redacted_before_persistence(
    tmp_path: Path,
) -> None:
    executor = SensitiveTraceExecutor()
    runner, _, _ = _runner(tmp_path, executor)
    result = await runner.run_suite(
        EXAMPLE,
        config=CausalRunConfig(repetitions=1, warmups=0, seed=3),
        run_id="causal_trace_redaction01",
    )
    raw_report = result.report_path.read_text(encoding="utf-8")
    assert "print('private')" not in raw_report
    assert "top-secret" not in raw_report
    assert "private-bytes" not in raw_report
    trace = result.report.trials[0].execution.trace
    assert trace["script"]["redacted"] is True
    assert trace["nested"]["access_token"]["redacted"] is True
    assert trace["nested"]["duration_ms"] == 12
    assert trace["binary"]["redacted"] is True


def test_structured_submission_freezes_plan_and_script_without_execution(
    tmp_path: Path,
) -> None:
    submission = {
        "schema_version": "fusion_planner_submission.v1",
        "arm_id": "codex",
        "case_id": "nema17_bracket",
        "planner": {
            "provider": "OpenAI",
            "model": "gpt-5.6-sol",
            "reasoning_profile": "ultra",
        },
        "intent": "Build one connected bracket.",
        "assumptions": [],
        "parameters": [{"name": "Width", "expression": "90 mm"}],
        "build_graph": [
            {
                "id": "base_plate",
                "operation": "extrude",
                "depends_on": [],
                "target_component": "root",
                "inputs": {"width": "Width"},
            }
        ],
        "verification_assertions": [
            {
                "id": "single_body",
                "target": "summary.visible_body_count",
                "operator": "eq",
                "expected": 1,
            }
        ],
        "script": {
            "language": "python",
            "entrypoint": "run",
            "content": "def run(_context: str):\n    return None\n",
        },
    }
    source = tmp_path / "submission.json"
    source.write_text(json.dumps(submission), encoding="utf-8")
    frozen = freeze_planner_submission(source, tmp_path / "frozen")
    plan_path = Path(frozen["plan_path"])
    script_path = Path(frozen["script_path"])
    assert plan_path.exists() and script_path.exists()
    assert frozen["script_sha256"] == _sha(script_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan["script_sha256"] == frozen["script_sha256"]
    assert plan["planner"]["model"] == "gpt-5.6-sol"
    with pytest.raises(CausalSuiteError, match="already exists"):
        freeze_planner_submission(source, tmp_path / "frozen")


def test_structured_submission_rejects_cycle_before_writing(tmp_path: Path) -> None:
    source = ROOT / "benchmark_parametric_ab" / "causal_artifacts" / "arm_b_plan.json"
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["schema_version"] = "fusion_planner_submission.v1"
    payload.pop("script_sha256")
    payload["build_graph"] = [
        {
            "id": "node_a",
            "operation": "extrude",
            "depends_on": ["node_b"],
            "target_component": "root",
            "inputs": {},
        },
        {
            "id": "node_b",
            "operation": "fillet",
            "depends_on": ["node_a"],
            "target_component": "root",
            "inputs": {},
        },
    ]
    payload["script"] = {
        "language": "python",
        "entrypoint": "run",
        "content": "def run(_context: str):\n    return None\n",
    }
    submission = tmp_path / "cyclic.json"
    submission.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(CausalSuiteError, match="cycle"):
        freeze_planner_submission(submission, tmp_path / "frozen")
    assert not (tmp_path / "frozen").exists()
