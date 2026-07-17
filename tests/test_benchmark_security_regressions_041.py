from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from benchmark.fixtures import SCRIPT_REGISTRY
from benchmark.filesystem import atomic_write_bytes, read_bytes, read_text
from benchmark.models import ExecutionObservation
from benchmark.public import (
    AdapterPreflight,
    PublicBenchmarkConfig,
    PublicBenchmarkRunner,
)
from benchmark.runner import BenchmarkExecutionError, BenchmarkRunner
from benchmark_parametric_ab.causal_benchmark import runner as causal_runner
from benchmark_parametric_ab.causal_benchmark.models import (
    CausalRunConfig,
    ExecutionObservation as CausalExecutionObservation,
)
from benchmark_parametric_ab.causal_benchmark.runner import CausalBenchmarkRunner
from fusion_agent_assets import asset_root


ROOT = Path(__file__).resolve().parents[1]
CAUSAL_SUITE = ROOT / "benchmark_parametric_ab" / "causal_suite.example.json"
BENCHMARK_SUITE = asset_root("benchmarks") / "benchmark_suite_v2.json"
PUBLIC_MANIFEST = asset_root("benchmarks") / "public_competitors_v1.json"
PRIVATE_CANARY = "PRIVATE_TOKEN=/Users/alice/project argv=--bearer-secret"


class _CausalExecutor:
    async def execute(self, context):  # noqa: ANN001
        active = causal_runner.current_trial_context()
        return CausalExecutionObservation(
            status="ok",
            execution_success=True,
            duration_ms=1.0,
            observed_runner_id=context.runner_id,
            observed_route_lock=(active.route_lock if active is not None else None),
            consumed_artifacts=dict(context.artifacts),
        )


class _CausalOracle:
    async def observe(self, _context):  # noqa: ANN001
        return {"passed": True, "checks": {"bound": True}}


def _causal_adapters():
    executor = _CausalExecutor()
    return (
        {layer: executor for layer in causal_runner.LAYERS},
        {"nema17_bracket_oracle": _CausalOracle()},
    )


@pytest.mark.asyncio
async def test_causal_report_is_bound_to_revision_and_environment_cannot_override_core(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = causal_runner.RevisionIdentity(
        expected_git_commit="a" * 40,
        observed_git_commit="a" * 40,
        expected_source_manifest_sha256="b" * 64,
        observed_source_manifest_sha256="b" * 64,
        tracked_state="clean",
    )
    monkeypatch.setattr(
        causal_runner, "collect_workspace_revision", lambda *_a, **_k: identity
    )
    executors, oracles = _causal_adapters()
    runner = CausalBenchmarkRunner(
        output_dir=tmp_path,
        executors=executors,
        oracles=oracles,
        environment={
            "python": "attacker",
            "suite_id": "wrong",
            "custom": "ok",
            "benign_key": PRIVATE_CANARY,
        },
    )

    result = await runner.run_suite(
        CAUSAL_SUITE,
        config=CausalRunConfig(
            repetitions=1,
            warmups=0,
            expected_git_commit="a" * 40,
            expected_source_manifest_sha256="b" * 64,
        ),
        run_id="causal_revisionbound01",
    )

    assert result.report.revision_identity.exact is True
    assert result.report.summary["scoreable"] is True
    environment = json.loads(result.environment_path.read_text(encoding="utf-8"))
    assert environment["python"] != "attacker"
    assert (
        environment["suite_id"]
        == json.loads(CAUSAL_SUITE.read_text(encoding="utf-8"))["suite_id"]
    )
    assert environment["extra"]["custom"] == "ok"
    assert environment["extra"]["benign_key"]["redacted"] is True
    assert PRIVATE_CANARY not in json.dumps(environment)


@pytest.mark.asyncio
async def test_causal_revision_mismatch_is_zero_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = causal_runner.RevisionIdentity(
        expected_git_commit="a" * 40,
        observed_git_commit="c" * 40,
        expected_source_manifest_sha256="b" * 64,
        observed_source_manifest_sha256="b" * 64,
        tracked_state="clean",
    )
    monkeypatch.setattr(
        causal_runner, "collect_workspace_revision", lambda *_a, **_k: identity
    )

    class NeverDispatch(_CausalExecutor):
        calls = 0

        async def execute(self, context):  # noqa: ANN001
            self.calls += 1
            return await super().execute(context)

    executor = NeverDispatch()
    runner = CausalBenchmarkRunner(
        output_dir=tmp_path,
        executors={layer: executor for layer in causal_runner.LAYERS},
        oracles={"nema17_bracket_oracle": _CausalOracle()},
    )
    with pytest.raises(
        causal_runner.CausalExecutionError,
        match="causal benchmark execution failed",
    ):
        await runner.run_suite(
            CAUSAL_SUITE,
            config=CausalRunConfig(
                repetitions=1,
                warmups=0,
                expected_git_commit="a" * 40,
                expected_source_manifest_sha256="b" * 64,
            ),
            run_id="causal_revisionmismatch01",
        )
    assert executor.calls == 0


@pytest.mark.asyncio
async def test_causal_context_is_request_local_under_concurrency() -> None:
    seen: dict[str, tuple[str | None, str | None]] = {}

    async def sample(route: str, trial: str) -> None:
        context = causal_runner.TrialContext(
            run_id="causal_context01",
            pair_id=f"pair_{trial}",
            trial_id=trial,
            suite_id="suite",
            case_id="case",
            layer="native_e2e",
            arm_id=trial,
            prompt="prompt",
            category="category",
            risk="additive",
            fixture_id="fixture",
            oracle_id="oracle",
            timeout_seconds=1.0,
            repetition=0,
            warmup=False,
            order_index=0,
            seed=1,
            route_lock=route,
        )
        with causal_runner.route_context(context):
            await causal_runner.asyncio.sleep(0)
            active = causal_runner.current_trial_context()
            seen[trial] = (
                active.route_lock if active else None,
                active.trial_id if active else None,
            )

    await causal_runner.asyncio.gather(
        sample("route_a", "arm_a"), sample("route_b", "arm_b")
    )
    assert seen == {"arm_a": ("route_a", "arm_a"), "arm_b": ("route_b", "arm_b")}
    assert causal_runner.current_trial_context() is None


@pytest.mark.asyncio
async def test_causal_context_resets_after_nesting_exception_and_cancellation() -> None:
    def context(trial_id: str, route: str) -> causal_runner.TrialContext:
        return causal_runner.TrialContext(
            run_id="causal_context_reset01",
            pair_id=f"pair_{trial_id}",
            trial_id=trial_id,
            suite_id="suite",
            case_id="case",
            layer="native_e2e",
            arm_id=trial_id,
            prompt="prompt",
            category="category",
            risk="additive",
            fixture_id="fixture",
            oracle_id="oracle",
            timeout_seconds=1.0,
            repetition=0,
            warmup=False,
            order_index=0,
            seed=1,
            route_lock=route,
        )

    outer = context("outer_arm", "route_outer")
    other = context("other_arm", "route_other")
    with causal_runner.route_context(outer):
        with causal_runner.route_context(outer):
            assert causal_runner.current_trial_context() == outer
        with pytest.raises(causal_runner.CausalExecutionError, match="nested"):
            with causal_runner.route_context(other):
                pass
        assert causal_runner.current_trial_context() == outer
    assert causal_runner.current_trial_context() is None

    with pytest.raises(RuntimeError, match="control"):
        with causal_runner.route_context(outer):
            raise RuntimeError("control")
    assert causal_runner.current_trial_context() is None

    started = causal_runner.asyncio.Event()
    after_cancel: list[causal_runner.TrialContext | None] = []

    async def cancelled_worker() -> None:
        try:
            with causal_runner.route_context(outer):
                started.set()
                await causal_runner.asyncio.Future()
        finally:
            after_cancel.append(causal_runner.current_trial_context())

    task = causal_runner.asyncio.create_task(cancelled_worker())
    await started.wait()
    task.cancel()
    with pytest.raises(causal_runner.asyncio.CancelledError):
        await task
    assert after_cancel == [None]
    assert causal_runner.current_trial_context() is None


def test_causal_arm_metadata_projector_never_emits_machine_local_canary() -> None:
    suite = causal_runner.load_causal_suite(CAUSAL_SUITE)
    arm = suite.arms[1].model_copy(
        update={"metadata": {"codex_bin": PRIVATE_CANARY, "channel": "stable"}}
    )

    projected = causal_runner._public_arm(arm)

    assert PRIVATE_CANARY not in json.dumps(projected)
    assert projected["metadata"]["codex_bin"]["redacted"] is True
    assert projected["metadata"]["channel"] == "stable"


@pytest.mark.asyncio
async def test_causal_adapter_error_canary_is_not_persisted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = causal_runner.RevisionIdentity(
        expected_git_commit="a" * 40,
        observed_git_commit="a" * 40,
        expected_source_manifest_sha256="b" * 64,
        observed_source_manifest_sha256="b" * 64,
        tracked_state="clean",
    )
    monkeypatch.setattr(
        causal_runner, "collect_workspace_revision", lambda *_a, **_k: identity
    )

    class FailingExecutor:
        async def execute(self, _context):  # noqa: ANN001
            raise RuntimeError(PRIVATE_CANARY)

    runner = CausalBenchmarkRunner(
        output_dir=tmp_path,
        executors={layer: FailingExecutor() for layer in causal_runner.LAYERS},
        oracles={"nema17_bracket_oracle": _CausalOracle()},
    )
    with pytest.raises(causal_runner.CausalExecutionError):
        await runner.run_suite(
            CAUSAL_SUITE,
            config=CausalRunConfig(
                repetitions=1,
                warmups=0,
                expected_git_commit="a" * 40,
                expected_source_manifest_sha256="b" * 64,
            ),
            run_id="causal_errorprojection01",
        )

    serialized = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (tmp_path / "causal_errorprojection01").rglob("*")
        if path.is_file()
    )
    assert PRIVATE_CANARY not in serialized
    error = json.loads(
        (tmp_path / "causal_errorprojection01" / "report.json").read_text(
            encoding="utf-8"
        )
    )["error"]
    assert set(error) == {"code", "generic_message", "correlation_id", "retryable"}


@pytest.mark.asyncio
async def test_harness_benchmark_error_canary_is_not_persisted(tmp_path: Path) -> None:
    class FailingExecutor:
        async def execute(self, _context):  # noqa: ANN001
            raise RuntimeError(PRIVATE_CANARY)

    runner = BenchmarkRunner(
        output_dir=tmp_path,
        route_executors={"safe_harness": FailingExecutor()},
    )
    with pytest.raises(BenchmarkExecutionError):
        await runner.run_suite(
            BENCHMARK_SUITE,
            config={"execution_paths": ["safe_harness"]},
            run_id="bench_errorprojection01",
        )
    serialized = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (tmp_path / "benchmarks" / "bench_errorprojection01").rglob("*")
        if path.is_file()
    )
    assert PRIVATE_CANARY not in serialized


class _ThrowingPublicAdapter:
    async def preflight(self, _subject, _config):  # noqa: ANN001
        return AdapterPreflight(
            ready=True,
            observed_revision="b44b667e440da070081795cfcbfaf75de2a44251",
            environment={"benign_key": PRIVATE_CANARY},
        )

    async def execute(self, _subject, _task, _config):  # noqa: ANN001
        raise RuntimeError(PRIVATE_CANARY)


@pytest.mark.asyncio
async def test_public_benchmark_adapter_error_canary_is_not_in_report() -> None:
    report = await PublicBenchmarkRunner(
        {"faust_fusion360_mcp": _ThrowingPublicAdapter()}
    ).run(
        PUBLIC_MANIFEST,
        config=PublicBenchmarkConfig(
            mode="mock",
            include_faults=False,
            subject_ids=["faust_fusion360_mcp"],
        ),
    )
    assert PRIVATE_CANARY not in report.model_dump_json()


@pytest.mark.parametrize("value", [math.inf, -math.inf, math.nan])
def test_benchmark_observations_reject_non_finite_numbers(value: float) -> None:
    with pytest.raises(ValueError):
        ExecutionObservation(
            status="invalid",
            execution_success=False,
            duration_ms=value,
        )


def test_canonical_mock_registry_remains_a_legitimate_finite_control() -> None:
    for definition in SCRIPT_REGISTRY.values():
        for profile in definition.profiles.values():
            assert math.isfinite(profile.duration_ms)


@pytest.mark.asyncio
async def test_causal_suite_inputs_and_outputs_work_beyond_320_characters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout = tmp_path
    output = tmp_path
    for index in range(5):
        checkout /= f"onedrive-causal-checkout-{index}-" + ("c" * 52)
        output /= f"onedrive-causal-output-{index}-" + ("o" * 54)
    suite_path = checkout / "causal_suite.example.json"
    assert len(str(suite_path.resolve())) > 320
    atomic_write_bytes(suite_path, read_bytes(CAUSAL_SUITE))
    source_artifacts = CAUSAL_SUITE.parent / "causal_artifacts"
    for source in source_artifacts.iterdir():
        if source.is_file():
            atomic_write_bytes(
                checkout / "causal_artifacts" / source.name,
                read_bytes(source),
            )

    identity = causal_runner.RevisionIdentity(
        expected_git_commit="a" * 40,
        observed_git_commit="a" * 40,
        expected_source_manifest_sha256="b" * 64,
        observed_source_manifest_sha256="b" * 64,
        tracked_state="clean",
    )
    monkeypatch.setattr(
        causal_runner, "collect_workspace_revision", lambda *_a, **_k: identity
    )
    executors, oracles = _causal_adapters()

    result = await CausalBenchmarkRunner(
        output_dir=output,
        executors=executors,
        oracles=oracles,
    ).run_suite(
        suite_path,
        config=CausalRunConfig(
            repetitions=1,
            warmups=0,
            expected_git_commit="a" * 40,
            expected_source_manifest_sha256="b" * 64,
        ),
        run_id="causal_longpath041",
    )

    assert len(str(result.report_path.resolve())) > 320
    assert json.loads(read_text(result.report_path))["status"] == "completed"
