"""A/B benchmark runner with deterministic internal and isolated Codex drivers."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import time
from copy import deepcopy
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol
from uuid import uuid4

from benchmark.artifacts import BenchmarkArtifactStore, collect_environment
from benchmark.codex_driver import EXECUTION_PATH_ENV, ROUTE_LOCK_ENV, CodexE2EDriver
from benchmark.fixtures import SCRIPT_REGISTRY, FixtureDefinition
from benchmark.loader import load_benchmark_suite, suite_fingerprint
from benchmark.models import (
    BenchmarkCase,
    BenchmarkReport,
    BenchmarkResult,
    BenchmarkRun,
    BenchmarkRunConfig,
    BenchmarkTrial,
    ExecutionObservation,
    ExecutionPath,
)
from benchmark.registry import fixture_for, oracle_for
from benchmark.statistics import aggregate_trials


_IN_PROCESS_ROUTE_LOCK = asyncio.Lock()
_REAL_TRIAL_LOCK = asyncio.Lock()
_STABLE_BASELINE_ENVIRONMENT_FIELDS = (
    "python",
    "platform",
    "machine",
    "git_commit",
    "plugin_version",
    "wheel_version",
    "fusion_version",
    "mcp_fingerprint",
)
_INDEPENDENT_METRIC_FIELDS = {
    "planning_ms",
    "connection_ms",
    "call_ms",
    "call_count",
    "initialize_count",
    "reconnect_count",
    "retry_count",
    "script_count",
    "bytes_transferred",
    "mutation_dispatch_count",
    "unexpected_diff_count",
    "duplicate_count",
    "save_count",
    "hub_sync_count",
    "personal_project_access_count",
    "parallel_overlap_count",
    "blocked_destructive",
    "outcome_unknown",
    "transport_session_key",
    "connection_generation",
}


class BenchmarkExecutionError(RuntimeError):
    """A benchmark cannot safely execute with the selected configuration."""


@dataclass(frozen=True, slots=True)
class TrialContext:
    run_id: str
    trial_id: str
    pair_id: str
    case: BenchmarkCase
    fixture: FixtureDefinition
    execution_path: ExecutionPath
    mode: str
    repetition: int
    warmup: bool
    seed: int
    project: str
    dry_run: bool
    fixture_marker: str


class InternalRouteExecutor(Protocol):
    """Integration contract for real Safe Harness and Native Fast routes."""

    async def execute(self, context: TrialContext) -> ExecutionObservation:
        """Run one isolated trial and return a normalized observation."""


RouteExecutor = InternalRouteExecutor | Callable[[TrialContext], Awaitable[ExecutionObservation] | ExecutionObservation]


@dataclass(frozen=True, slots=True)
class IndependentEvidence:
    """Oracle state plus independently observed transport/operation metrics."""

    observation: dict[str, Any]
    metrics: dict[str, Any] = field(default_factory=dict)
    trace: dict[str, Any] = field(default_factory=dict)


class IndependentOracleObserver(Protocol):
    """Read fixture state/trace independently of the executor being scored."""

    async def observe(self, context: TrialContext) -> IndependentEvidence | dict[str, Any]:
        """Return programmatic evidence consumed by the registered oracle."""


OracleObserver = IndependentOracleObserver | Callable[
    [TrialContext], Awaitable[IndependentEvidence | dict[str, Any]] | IndependentEvidence | dict[str, Any]
]


@dataclass(frozen=True, slots=True)
class RealTrialStart:
    """Independently verified containment state before route dispatch."""

    fixture_marker_verified: bool
    fingerprint_verified: bool
    isolated_unsaved_document: bool
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RealTrialFinish:
    """Containment evidence collected after closing and restoring a trial."""

    closed_without_save: bool
    restored: bool
    save_count: int = 0
    hub_sync_count: int = 0
    personal_project_access_count: int = 0
    parallel_overlap_count: int = 0
    restoration_ms: float = 0.0
    metadata: dict[str, Any] | None = None


class RealTrialLifecycle(Protocol):
    """Own fixture preparation and teardown outside the route under test."""

    async def preflight(
        self,
        execution_paths: list[ExecutionPath],
        cases: list[BenchmarkCase],
    ) -> None:
        """Prove all required capabilities before any real fixture is created."""

    async def prepare(self, context: TrialContext) -> RealTrialStart:
        """Create and independently verify one unsaved, uniquely marked fixture."""

    async def finalize(
        self,
        context: TrialContext,
        start: RealTrialStart,
        failure: BaseException | None,
    ) -> RealTrialFinish:
        """Close without saving and restore the originally active document."""


class CanonicalMockExecutor:
    """Completely deterministic code-registry executor used by PR gates."""

    def __init__(self) -> None:
        self._initialized_sessions: set[str] = set()

    async def execute(self, context: TrialContext) -> ExecutionObservation:
        enforce_route_lock(context.execution_path)
        profile = SCRIPT_REGISTRY[context.case.script_id].profiles[context.execution_path]
        session_key = f"mock:{context.execution_path}"
        initialize_count = 0 if session_key in self._initialized_sessions else 1
        self._initialized_sessions.add(session_key)
        # Mock metrics are code-owned, not wall-clock-derived. This makes PR
        # reports byte-for-byte reproducible for the same suite/config seed.
        return ExecutionObservation(
            status=profile.status,
            execution_success=profile.execution_success,
            duration_ms=profile.duration_ms,
            setup_ms=5,
            verification_ms=10,
            teardown_ms=3,
            call_count=profile.call_count,
            initialize_count=initialize_count,
            reconnect_count=profile.reconnect_count,
            retry_count=profile.retry_count,
            script_count=profile.script_count,
            bytes_transferred=profile.call_count * 128,
            mutation_dispatch_count=profile.mutation_dispatch_count,
            blocked_destructive=profile.blocked_destructive,
            outcome_unknown=profile.outcome_unknown,
            transport_session_key=session_key,
            connection_generation=1,
            observation=profile.observation,
            trace={
                "fixture_id": context.fixture.id,
                "script_id": context.case.script_id,
                "route": context.execution_path,
                "fixture_marker": context.fixture_marker,
            },
        )


class CanonicalMockOracleObserver:
    """Separate code path that reads the canonical mock fixture outcome."""

    async def observe(self, context: TrialContext) -> IndependentEvidence:
        profile = SCRIPT_REGISTRY[context.case.script_id].profiles[context.execution_path]
        return IndependentEvidence(
            observation=deepcopy(profile.observation),
            metrics={
                "call_count": profile.call_count,
                "reconnect_count": profile.reconnect_count,
                "retry_count": profile.retry_count,
                "script_count": profile.script_count,
                "mutation_dispatch_count": profile.mutation_dispatch_count,
                "blocked_destructive": profile.blocked_destructive,
                "outcome_unknown": profile.outcome_unknown,
                "duplicate_count": 0,
                "save_count": 0,
                "hub_sync_count": 0,
                "personal_project_access_count": 0,
                "parallel_overlap_count": 0,
            },
            trace={"source": "canonical_mock_registry"},
        )


class BenchmarkRunner:
    """Run strict suites, preserve old result views, and own v2 artifacts."""

    def __init__(
        self,
        controller: Any | None = None,
        workspace_root: Path | str = "workspace",
        output_dir: Path | str = "outputs",
        manifest_dir: Path | str = "manifests",
        *,
        route_executors: dict[ExecutionPath, RouteExecutor] | None = None,
        oracle_observer: OracleObserver | None = None,
        real_lifecycle: RealTrialLifecycle | None = None,
        codex_driver: CodexE2EDriver | None = None,
        environment_metadata: dict[str, Any] | None = None,
    ) -> None:
        # Kept for constructor compatibility. P2 does not instantiate or own a
        # new controller; the server injects route-specific runtime executors.
        self.controller = controller
        self.workspace_root = Path(workspace_root)
        self.output_dir = Path(output_dir)
        self.manifest_dir = Path(manifest_dir)
        self.route_executors = dict(route_executors or {})
        self.oracle_observer = oracle_observer
        self.real_lifecycle = real_lifecycle
        self.codex_driver = codex_driver
        self.environment_metadata = dict(environment_metadata or {})
        self.artifacts = BenchmarkArtifactStore(self.output_dir)
        self._mock_executor = CanonicalMockExecutor()
        self._mock_oracle_observer = CanonicalMockOracleObserver()

    async def run_suite(
        self,
        suite_path: Path | str,
        *,
        config: BenchmarkRunConfig | dict[str, Any] | None = None,
        run_id: str | None = None,
    ) -> BenchmarkRun:
        """Execute a strict suite and write one immutable artifact directory."""

        suite = load_benchmark_suite(suite_path)
        run_config = config if isinstance(config, BenchmarkRunConfig) else BenchmarkRunConfig.model_validate(config or {})
        selected_cases = [
            case for case in suite.cases if any(path in case.execution_paths for path in run_config.execution_paths)
        ]
        if not selected_cases:
            raise BenchmarkExecutionError("no suite cases support the selected execution_paths")
        run_id = run_id or _new_run_id()
        suite_digest = suite_fingerprint(suite)
        started_at = datetime.now(timezone.utc).isoformat()
        trials: list[BenchmarkTrial] = []
        traces: dict[str, dict[str, Any]] = {}
        oracles: dict[str, dict[str, Any]] = {}
        order_index = 0
        artifact_dir = self.artifacts.root / run_id
        environment = collect_environment(
            {
                **self.environment_metadata,
                "run_id": run_id,
                "suite_id": suite.suite_id,
                "suite_fingerprint": suite_digest,
                "driver": run_config.driver,
                "mode": run_config.mode,
                "model": run_config.model,
                "reasoning_effort": run_config.reasoning_effort,
                "seed": run_config.seed,
                "baseline_run_id": run_config.baseline_run_id,
            }
        )
        baseline_safe_p90: float | None = None
        used_paths = list(
            dict.fromkeys(
                path
                for case in selected_cases
                for path in run_config.execution_paths
                if path in case.execution_paths
            )
        )

        try:
            _validate_real_confirmation(selected_cases, run_config)
            if run_config.driver == "codex_e2e" and run_config.mode == "mock":
                raise BenchmarkExecutionError(
                    "codex_e2e mode=mock is unavailable: no independent observable fixture state exists"
                )
            if run_config.mode == "real" and run_config.driver == "internal":
                missing = sorted(set(used_paths) - set(self.route_executors))
                if missing:
                    raise BenchmarkExecutionError(
                        "real internal benchmark requires injected route executors: " + ", ".join(missing)
                    )
            if run_config.mode == "real" and self.oracle_observer is None:
                raise BenchmarkExecutionError("real benchmark requires an injected independent oracle_observer")
            if run_config.mode == "real" and self.real_lifecycle is None:
                raise BenchmarkExecutionError(
                    "real benchmark requires an injected real_lifecycle with fixture isolation and restoration"
                )
            if run_config.driver == "codex_e2e" and self.oracle_observer is None:
                raise BenchmarkExecutionError(
                    "codex_e2e benchmark requires an injected independent oracle_observer; "
                    "executor-reported output is not correctness evidence"
                )

            # Baseline comparability is proven before capability checks or any
            # fixture/route dispatch.
            baseline_safe_p90 = self._baseline_safe_p90(
                run_config.baseline_run_id,
                suite_id=suite.suite_id,
                suite_digest=suite_digest,
                current_config=run_config,
                current_environment=environment,
            )
            if run_config.driver == "codex_e2e" and self.codex_driver is None:
                self.codex_driver = CodexE2EDriver()
            if run_config.mode == "real":
                assert self.real_lifecycle is not None
                await self.real_lifecycle.preflight(used_paths, selected_cases)

            for case_index, case in enumerate(selected_cases):
                paths = [path for path in run_config.execution_paths if path in case.execution_paths]
                for warmup, repetition in _trial_repetitions(run_config):
                    ordered_paths = _balanced_order(
                        paths,
                        seed=run_config.seed,
                        case_index=case_index,
                        repetition=repetition,
                        warmup=warmup,
                    )
                    pair_id = f"{case.id}_{'w' if warmup else 'r'}{repetition:03d}"
                    for execution_path in ordered_paths:
                        trial_id = f"{pair_id}_{execution_path}"
                        fixture = fixture_for(case)
                        context = TrialContext(
                            run_id=run_id,
                            trial_id=trial_id,
                            pair_id=pair_id,
                            case=case,
                            fixture=fixture,
                            execution_path=execution_path,
                            mode=run_config.mode,
                            repetition=repetition,
                            warmup=warmup,
                            seed=run_config.seed,
                            project=run_config.project,
                            dry_run=run_config.dry_run,
                            fixture_marker=f"{run_id}:{trial_id}",
                        )
                        observation, driver_trace, evidence = await self._execute_and_observe(context, run_config)
                        oracle_started = time.perf_counter()
                        oracle_input = observation.model_copy(update={"observation": evidence.observation})
                        oracle = oracle_for(case)(fixture, oracle_input, case)
                        oracle_ms = (time.perf_counter() - oracle_started) * 1000
                        if run_config.mode == "real":
                            observation = observation.model_copy(
                                update={
                                    "duration_ms": observation.duration_ms + oracle_ms,
                                    "verification_ms": observation.verification_ms + oracle_ms,
                                }
                            )
                        metrics = _trial_metrics(observation)
                        metrics["expectations_met"] = _expectations_met(case, observation)
                        trial = BenchmarkTrial(
                            trial_id=trial_id,
                            run_id=run_id,
                            pair_id=pair_id,
                            case_id=case.id,
                            prompt=case.prompt,
                            category=case.category,
                            risk=case.risk,
                            driver=run_config.driver,
                            mode=run_config.mode,
                            execution_path=execution_path,
                            repetition=repetition,
                            warmup=warmup,
                            order_index=order_index,
                            model=run_config.model,
                            reasoning_effort=run_config.reasoning_effort if run_config.driver == "codex_e2e" else None,
                            status=observation.status,
                            first_pass_success=oracle.passed and observation.retry_count == 0,
                            final_success=oracle.passed,
                            repair_loop_count=observation.retry_count,
                            oracle=oracle,
                            metrics=metrics,
                        )
                        trials.append(trial)
                        traces[trial_id] = {
                            **observation.trace,
                            **driver_trace,
                            "independent_evidence_trace": evidence.trace,
                            "observation": observation.observation,
                            "status": observation.status,
                            "metrics": metrics,
                        }
                        oracles[trial_id] = {
                            **oracle.model_dump(mode="json"),
                            "independent_metric_fields": observation.independent_metric_fields,
                            "evidence_trace": evidence.trace,
                        }
                        order_index += 1

            summary = aggregate_trials(
                trials,
                seed=run_config.seed,
                safe_harness_baseline_p90_ms=baseline_safe_p90,
                allow_current_safe_baseline=(
                    run_config.driver == "internal"
                    and run_config.mode == "mock"
                    and run_config.baseline_run_id is None
                ),
            )
            report = BenchmarkReport(
                status="completed",
                run_id=run_id,
                suite_id=suite.suite_id,
                suite_fingerprint=suite_digest,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc).isoformat(),
                config=run_config,
                summary=summary,
                trials=trials,
                artifact_dir=artifact_dir,
            )
            return self.artifacts.write_run(report, environment=environment, traces=traces, oracles=oracles)
        except BaseException as exc:
            aborted_summary = aggregate_trials(
                trials,
                seed=run_config.seed,
                safe_harness_baseline_p90_ms=baseline_safe_p90,
                allow_current_safe_baseline=False,
            )
            error_payload = {"type": type(exc).__name__, "message": str(exc)}
            traces.setdefault("run_abort", {"event": "run_aborted", **error_payload})
            aborted_report = BenchmarkReport(
                status="aborted",
                run_id=run_id,
                suite_id=suite.suite_id,
                suite_fingerprint=suite_digest,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc).isoformat(),
                config=run_config,
                summary=aborted_summary,
                trials=trials,
                artifact_dir=artifact_dir,
                error=error_payload,
            )
            try:
                self.artifacts.write_run(
                    aborted_report,
                    environment={**environment, "run_status": "aborted"},
                    traces=traces,
                    oracles=oracles,
                )
            except BaseException as artifact_exc:
                if hasattr(exc, "add_note"):
                    exc.add_note(f"failed to persist aborted benchmark {run_id}: {artifact_exc}")
            if isinstance(exc, asyncio.CancelledError):
                raise
            wrapped = BenchmarkExecutionError(f"{exc} (aborted_run_id={run_id})")
            wrapped.benchmark_run_id = run_id  # type: ignore[attr-defined]
            raise wrapped from exc

    async def run(
        self,
        suite_path: Path | str,
        mode: str = "mock",
        project: str = "benchmarks",
        dry_run: bool = False,
        *,
        driver: str = "internal",
        execution_paths: list[ExecutionPath] | None = None,
        repetitions: int = 1,
        warmups: int = 0,
        seed: int = 42,
        model: str | None = None,
        reasoning_effort: str = "high",
        confirm_real_benchmark: bool = False,
        baseline_run_id: str | None = None,
    ) -> list[BenchmarkResult]:
        """Legacy list-returning facade over the strict v2 runner."""

        run = await self.run_suite(
            suite_path,
            config={
                "driver": driver,
                "mode": mode,
                "execution_paths": execution_paths or ["safe_harness", "native_fast"],
                "repetitions": repetitions,
                "warmups": warmups,
                "seed": seed,
                "model": model,
                "reasoning_effort": reasoning_effort,
                "confirm_real_benchmark": confirm_real_benchmark,
                "baseline_run_id": baseline_run_id,
                "project": project,
                "dry_run": dry_run,
            },
        )
        return [BenchmarkResult.from_trial(trial) for trial in run.report.trials if not trial.warmup]

    def read_report(
        self,
        *,
        run_id: str | None = None,
        view: str = "report",
        offset: int = 0,
        limit: int = 100,
        legacy_path: Path | str | None = None,
    ) -> dict[str, Any]:
        """Read a paginated v2 run or an explicitly selected legacy report."""

        return self.artifacts.read(
            run_id=run_id,
            view=view,
            offset=offset,
            limit=limit,
            legacy_path=legacy_path,
        )

    def write_report(self, results: list[BenchmarkResult], path: Path | str) -> Path:
        """Retain the v0.1 explicit legacy writer without overwriting v2 runs."""

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = [result.model_dump(mode="json") for result in results]
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return path

    def _baseline_safe_p90(
        self,
        run_id: str | None,
        *,
        suite_id: str,
        suite_digest: str,
        current_config: BenchmarkRunConfig,
        current_environment: dict[str, Any],
    ) -> float | None:
        if run_id is None:
            return None
        baseline = self.artifacts.read(run_id=run_id, view="report", offset=0, limit=1)["report"]
        if baseline.get("suite_id") != suite_id or baseline.get("suite_fingerprint") != suite_digest:
            raise BenchmarkExecutionError("baseline_run_id must reference the same benchmark suite fingerprint")
        if baseline.get("status", "completed") != "completed":
            raise BenchmarkExecutionError("baseline_run_id must reference a completed benchmark run")
        baseline_config = baseline.get("config") or {}
        comparable_config = {
            "driver": current_config.driver,
            "mode": current_config.mode,
            "model": current_config.model,
            "reasoning_effort": current_config.reasoning_effort,
        }
        mismatches = [
            name
            for name, expected in comparable_config.items()
            if baseline_config.get(name) != expected
        ]
        if sorted(baseline_config.get("execution_paths") or []) != sorted(current_config.execution_paths):
            mismatches.append("execution_paths")
        baseline_environment = self.artifacts.read(run_id=run_id, view="environment")["environment"]
        for name in _STABLE_BASELINE_ENVIRONMENT_FIELDS:
            before = baseline_environment.get(name)
            current = current_environment.get(name)
            if before in (None, "") and current in (None, ""):
                continue
            if before != current:
                mismatches.append(f"environment.{name}")
        if mismatches:
            raise BenchmarkExecutionError(
                "baseline_run_id is not comparable; mismatched fields: " + ", ".join(sorted(set(mismatches)))
            )
        value = (
            baseline.get("summary", {})
            .get("routes", {})
            .get("safe_harness", {})
            .get("duration_ms", {})
            .get("p90")
        )
        if not isinstance(value, (int, float)) or value <= 0:
            raise BenchmarkExecutionError("baseline_run_id has no positive safe_harness p90")
        return float(value)

    async def _execute_and_observe(
        self,
        context: TrialContext,
        config: BenchmarkRunConfig,
    ) -> tuple[ExecutionObservation, dict[str, Any], IndependentEvidence]:
        if config.mode != "real":
            observation, trace = await self._execute_trial_unlocked(context, config)
            evidence = await self._observe_oracle(context, config)
            observation = _apply_independent_evidence(observation, evidence)
            return observation, trace, evidence

        lifecycle = self.real_lifecycle
        if lifecycle is None:  # Defensive: run_suite preflight already rejects this.
            raise BenchmarkExecutionError("real trial lifecycle is not configured")
        request_started = time.perf_counter()
        queued_at = request_started
        async with _REAL_TRIAL_LOCK:
            queue_wait_ms = (time.perf_counter() - queued_at) * 1000
            setup_started = time.perf_counter()
            start = await lifecycle.prepare(context)
            setup_ms = (time.perf_counter() - setup_started) * 1000
            observation: ExecutionObservation | None = None
            trace: dict[str, Any] = {}
            evidence: IndependentEvidence | None = None
            failure: BaseException | None = None
            try:
                _validate_real_start(start, context)
                observation, trace = await self._execute_trial_unlocked(context, config)
                verification_started = time.perf_counter()
                evidence = await self._observe_oracle(context, config)
                verification_ms = (time.perf_counter() - verification_started) * 1000
                observation = _apply_independent_evidence(observation, evidence)
            except BaseException as exc:  # cleanup must also run for cancellation
                failure = exc

            teardown_started = time.perf_counter()
            cleanup_task = asyncio.create_task(lifecycle.finalize(context, start, failure))
            try:
                finish = await asyncio.shield(cleanup_task)
            except asyncio.CancelledError as cancellation:
                # Shield keeps teardown running; wait for its concrete result
                # before propagating cancellation so Fusion is never left on
                # the disposable trial document.
                try:
                    finish = await cleanup_task
                except BaseException as cleanup_exc:
                    raise BenchmarkExecutionError(
                        f"real trial teardown failed for {context.trial_id}: "
                        f"{type(cleanup_exc).__name__}: {cleanup_exc}"
                    ) from cleanup_exc
                if failure is None:
                    failure = cancellation
            except BaseException as cleanup_exc:
                raise BenchmarkExecutionError(
                    f"real trial teardown failed for {context.trial_id}: "
                    f"{type(cleanup_exc).__name__}: {cleanup_exc}"
                ) from cleanup_exc
            teardown_ms = (time.perf_counter() - teardown_started) * 1000

            if failure is not None:
                _validate_real_finish(finish, context, prior_failure=failure)
                raise failure
            if observation is None or evidence is None:  # pragma: no cover - invariant guard
                raise BenchmarkExecutionError(f"real trial {context.trial_id} produced no observation")

            observation = observation.model_copy(
                update={
                    # Containment is lifecycle evidence. Never trust the route
                    # under test to self-report these safety-critical fields.
                    "fixture_marker_verified": start.fixture_marker_verified,
                    "fingerprint_verified": start.fingerprint_verified,
                    "duration_ms": (time.perf_counter() - request_started) * 1000,
                    "setup_ms": setup_ms,
                    "queue_wait_ms": queue_wait_ms,
                    "verification_ms": verification_ms,
                    "teardown_ms": teardown_ms,
                    "restoration_ms": finish.restoration_ms,
                    "closed_without_save": finish.closed_without_save,
                    "restored": finish.restored,
                    "save_count": finish.save_count,
                    "hub_sync_count": finish.hub_sync_count,
                    "personal_project_access_count": finish.personal_project_access_count,
                    "parallel_overlap_count": finish.parallel_overlap_count,
                    "trace": {
                        **observation.trace,
                        "real_fixture_start": start.metadata,
                        "real_fixture_finish": finish.metadata or {},
                    },
                }
            )
            _validate_real_observation(observation, context)
            return observation, trace, evidence

    async def _execute_trial_unlocked(
        self,
        context: TrialContext,
        config: BenchmarkRunConfig,
    ) -> tuple[ExecutionObservation, dict[str, Any]]:
        if config.driver == "codex_e2e":
            if self.codex_driver is None or not config.model:
                raise BenchmarkExecutionError("codex_e2e driver/model is not configured")
            invocation = await self.codex_driver.run(
                case=context.case,
                execution_path=context.execution_path,
                mode=context.mode,
                model=config.model,
                reasoning_effort=config.reasoning_effort,
                run_id=context.run_id,
                trial_id=context.trial_id,
                timeout_seconds=context.case.timeout_seconds,
            )
            return invocation.observation, invocation.trace

        executor: RouteExecutor
        if config.mode == "mock":
            executor = self.route_executors.get(context.execution_path, self._mock_executor)
        else:
            try:
                executor = self.route_executors[context.execution_path]
            except KeyError as exc:
                raise BenchmarkExecutionError(f"missing real executor for {context.execution_path}") from exc
        async with _IN_PROCESS_ROUTE_LOCK:
            with route_lock(context.execution_path, context):
                started = time.perf_counter()
                observation = await _invoke_executor(executor, context)
                wall_ms = (time.perf_counter() - started) * 1000
        if config.mode == "real":
            observation = observation.model_copy(update={"execution_ms": wall_ms})
        return observation, {
            "route_lock": context.execution_path,
            "internal_wall_ms": wall_ms,
            "fixture_marker": context.fixture_marker,
        }

    async def _observe_oracle(
        self,
        context: TrialContext,
        config: BenchmarkRunConfig,
    ) -> IndependentEvidence:
        observer: OracleObserver
        if self.oracle_observer is not None:
            observer = self.oracle_observer
        elif config.driver == "internal" and config.mode == "mock":
            # The canonical observer is valid only for the deterministic,
            # code-owned PR gate. A Codex subprocess must never be scored from
            # registry data that does not observe what the subprocess did.
            observer = self._mock_oracle_observer
        else:
            raise BenchmarkExecutionError("independent oracle observer is not configured")
        target = observer.observe if hasattr(observer, "observe") else observer
        value = target(context)  # type: ignore[operator]
        if inspect.isawaitable(value):
            value = await value
        if isinstance(value, IndependentEvidence):
            return value
        if not isinstance(value, dict):
            raise BenchmarkExecutionError("oracle observer must return an object")
        return IndependentEvidence(observation=value)


async def _invoke_executor(executor: RouteExecutor, context: TrialContext) -> ExecutionObservation:
    target = executor.execute if hasattr(executor, "execute") else executor
    value = target(context)  # type: ignore[operator]
    if inspect.isawaitable(value):
        value = await value
    if not isinstance(value, ExecutionObservation):
        value = ExecutionObservation.model_validate(value)
    return value


def _apply_independent_evidence(
    observation: ExecutionObservation,
    evidence: IndependentEvidence,
) -> ExecutionObservation:
    """Overlay only allowlisted metrics produced by the independent observer."""

    unknown = sorted(set(evidence.metrics) - _INDEPENDENT_METRIC_FIELDS)
    if unknown:
        raise BenchmarkExecutionError(
            "independent observer returned unsupported metric fields: " + ", ".join(unknown)
        )
    payload = observation.model_dump(mode="python")
    payload.update(evidence.metrics)
    payload["independent_metric_fields"] = sorted(
        set(observation.independent_metric_fields) | set(evidence.metrics)
    )
    payload["trace"] = {
        **observation.trace,
        "independent_observer": evidence.trace,
    }
    return ExecutionObservation.model_validate(payload)


@contextmanager
def route_lock(path: ExecutionPath, context: TrialContext):
    """Set and restore the in-process route lock for serialized internal trials."""

    names = (ROUTE_LOCK_ENV, EXECUTION_PATH_ENV, "FUSION_AGENT_BENCHMARK_TRIAL_ID")
    previous = {name: os.environ.get(name) for name in names}
    os.environ[ROUTE_LOCK_ENV] = path
    os.environ[EXECUTION_PATH_ENV] = path
    os.environ["FUSION_AGENT_BENCHMARK_TRIAL_ID"] = context.trial_id
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def enforce_route_lock(requested_path: ExecutionPath) -> None:
    """Fail closed when a route tries to escape the benchmark arm."""

    locked = os.getenv(ROUTE_LOCK_ENV)
    selected = os.getenv(EXECUTION_PATH_ENV)
    if locked != requested_path or selected != requested_path:
        raise BenchmarkExecutionError(
            f"route lock violation: requested={requested_path}, locked={locked}, selected={selected}"
        )


def _validate_real_confirmation(cases: list[BenchmarkCase], config: BenchmarkRunConfig) -> None:
    if config.mode != "real":
        return
    has_mutation = any(
        case.risk != "read_only" and any(path in case.execution_paths for path in config.execution_paths)
        for case in cases
    )
    if has_mutation and not config.confirm_real_benchmark:
        raise BenchmarkExecutionError("confirm_real_benchmark=true is required for real mutating cases")


def _validate_real_start(start: RealTrialStart, context: TrialContext) -> None:
    """Block the route before dispatch unless fixture isolation is proven."""

    violations: list[str] = []
    if not start.fixture_marker_verified:
        violations.append("fixture marker mismatch")
    if not start.fingerprint_verified:
        violations.append("fixture fingerprint mismatch")
    if not start.isolated_unsaved_document:
        violations.append("fixture is not a distinct unsaved document")
    if violations:
        raise BenchmarkExecutionError(
            f"real trial blocked before route dispatch for {context.trial_id}: " + "; ".join(violations)
        )


def _validate_real_finish(
    finish: RealTrialFinish,
    context: TrialContext,
    *,
    prior_failure: BaseException | None = None,
) -> None:
    """Abort immediately when close/restore/no-save evidence is incomplete."""

    violations: list[str] = []
    if not finish.closed_without_save:
        violations.append("trial document was not closed without save")
    if not finish.restored:
        violations.append("original document was not restored")
    if finish.save_count:
        violations.append("save detected")
    if finish.hub_sync_count:
        violations.append("hub sync detected")
    if finish.personal_project_access_count:
        violations.append("personal project access detected")
    if finish.parallel_overlap_count:
        violations.append("parallel real trial overlap detected")
    if violations:
        suffix = ""
        if prior_failure is not None:
            suffix = f"; prior failure: {type(prior_failure).__name__}: {prior_failure}"
        raise BenchmarkExecutionError(
            f"real trial teardown containment failed for {context.trial_id}: "
            + "; ".join(violations)
            + suffix
        )


def _validate_real_observation(observation: ExecutionObservation, context: TrialContext) -> None:
    """Abort the suite immediately if real-fixture containment was not proven."""

    violations: list[str] = []
    if not observation.fixture_marker_verified:
        violations.append("fixture marker mismatch")
    if not observation.fingerprint_verified:
        violations.append("fixture fingerprint mismatch")
    if not observation.closed_without_save:
        violations.append("trial document was not closed without save")
    if not observation.restored:
        violations.append("original document was not restored")
    if observation.save_count:
        violations.append("save detected")
    if observation.hub_sync_count:
        violations.append("hub sync detected")
    if observation.personal_project_access_count:
        violations.append("personal project access detected")
    if observation.parallel_overlap_count:
        violations.append("parallel real trial overlap detected")
    if violations:
        raise BenchmarkExecutionError(
            f"real trial containment failed for {context.trial_id}: " + "; ".join(violations)
        )


def _trial_repetitions(config: BenchmarkRunConfig) -> list[tuple[bool, int]]:
    return [(True, index) for index in range(config.warmups)] + [
        (False, index) for index in range(config.repetitions)
    ]


def _balanced_order(
    paths: list[ExecutionPath],
    *,
    seed: int,
    case_index: int,
    repetition: int,
    warmup: bool,
) -> list[ExecutionPath]:
    """Deterministic AB/BA ordering, balanced across neighboring pairs."""

    ordered = list(paths)
    if len(ordered) != 2:
        return ordered
    phase = 1 if warmup else 0
    if (seed + case_index + repetition + phase) % 2:
        ordered.reverse()
    return ordered


def _trial_metrics(observation: ExecutionObservation) -> dict[str, Any]:
    return {
        "duration_ms": observation.duration_ms,
        "execution_ms": observation.execution_ms,
        "planning_ms": observation.planning_ms,
        "setup_ms": observation.setup_ms,
        "queue_wait_ms": observation.queue_wait_ms,
        "connection_ms": observation.connection_ms,
        "call_ms": observation.call_ms,
        "verification_ms": observation.verification_ms,
        "teardown_ms": observation.teardown_ms,
        "restoration_ms": observation.restoration_ms,
        "call_count": observation.call_count,
        "initialize_count": observation.initialize_count,
        "reconnect_count": observation.reconnect_count,
        "retry_count": observation.retry_count,
        "script_count": observation.script_count,
        "bytes_transferred": observation.bytes_transferred,
        "token_count": observation.token_count,
        "mutation_dispatch_count": observation.mutation_dispatch_count,
        "unexpected_diff_count": observation.unexpected_diff_count,
        "duplicate_count": observation.duplicate_count,
        "save_count": observation.save_count,
        "hub_sync_count": observation.hub_sync_count,
        "personal_project_access_count": observation.personal_project_access_count,
        "parallel_overlap_count": observation.parallel_overlap_count,
        "transport_session_key": observation.transport_session_key,
        "connection_generation": observation.connection_generation,
        "independent_metric_fields": list(observation.independent_metric_fields),
        "fixture_marker_verified": observation.fixture_marker_verified,
        "fingerprint_verified": observation.fingerprint_verified,
        "closed_without_save": observation.closed_without_save,
        "restored": observation.restored,
        "blocked_destructive": observation.blocked_destructive,
        "outcome_unknown": observation.outcome_unknown,
    }


def _expectations_met(case: BenchmarkCase, observation: ExecutionObservation) -> bool:
    expected = case.expectations
    checks = [observation.execution_success == expected.should_succeed]
    # Expected safety outcomes can be execution failures with a successful
    # oracle (for example MUTATION_OUTCOME_UNKNOWN). In those cases the status
    # and independent oracle are authoritative, not execution_success.
    if expected.expected_status is not None:
        checks.append(observation.status == expected.expected_status)
    if expected.must_block:
        checks.append(observation.blocked_destructive)
    if expected.max_call_count is not None:
        checks.append(observation.call_count <= expected.max_call_count)
    if expected.mutation_dispatch_count is not None:
        checks.append(observation.mutation_dispatch_count == expected.mutation_dispatch_count)
    if expected.expected_status in {"outcome_unknown", "manifest_drift"}:
        checks[0] = True
    return all(checks)


def _new_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"bench_{timestamp}_{uuid4().hex[:8]}"


def suite_file_digest(path: Path | str) -> str:
    """Small public helper for CI artifact provenance."""

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
