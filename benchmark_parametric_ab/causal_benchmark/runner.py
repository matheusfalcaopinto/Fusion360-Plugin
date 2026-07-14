"""Serialized, adapter-driven runner for the three causal benchmark layers."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import platform
import random
import re
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol

from .loader import load_causal_suite, suite_fingerprint
from .models import (
    CausalLayer,
    CausalReport,
    CausalRunConfig,
    CausalSuite,
    ExecutionObservation,
    OracleObservation,
    TrialContext,
    TrialRecord,
)


ROUTE_LOCK_ENV = "FUSION_CAUSAL_ROUTE_LOCK"
ARM_ENV = "FUSION_CAUSAL_ARM"
TRIAL_ENV = "FUSION_CAUSAL_TRIAL_ID"
LAYERS: tuple[CausalLayer, ...] = (
    "transport_replay",
    "planner_isolated",
    "native_e2e",
)
_RUN_ID = re.compile(r"^causal_[A-Za-z0-9_-]{8,96}$")
_SENSITIVE_TRACE_KEY_PARTS = (
    "script",
    "content",
    "token",
    "secret",
    "password",
    "passwd",
    "authorization",
    "credential",
    "api_key",
    "apikey",
    "mcp_session",
    "session_header",
)


class CausalExecutionError(RuntimeError):
    """The run stopped fail-closed after a dispatch or contract violation."""


class LayerExecutor(Protocol):
    """Injected adapter. The framework itself never invokes a model or Fusion."""

    async def execute(self, context: TrialContext) -> ExecutionObservation:
        ...


class IndependentOracle(Protocol):
    """Independent observer; deliberately receives no executor observation."""

    async def observe(self, context: TrialContext) -> OracleObservation:
        ...


@dataclass(frozen=True)
class CausalRunResult:
    report: CausalReport
    run_dir: Path
    report_path: Path
    trials_path: Path
    environment_path: Path


class CausalBenchmarkRunner:
    """Run all three layers with the same two arms and immutable inputs."""

    def __init__(
        self,
        *,
        output_dir: Path | str,
        executors: Mapping[CausalLayer, LayerExecutor],
        oracles: Mapping[str, IndependentOracle],
        environment: Mapping[str, Any] | None = None,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.executors = dict(executors)
        self.oracles = dict(oracles)
        self.environment = dict(environment or {})

    async def run_suite(
        self,
        suite_path: Path | str,
        *,
        config: CausalRunConfig | None = None,
        run_id: str | None = None,
    ) -> CausalRunResult:
        # Suite/schema/hash validation happens before any executor is inspected or called.
        suite = load_causal_suite(suite_path)
        run_config = config or CausalRunConfig()
        run_id = run_id or _fresh_run_id()
        if not _RUN_ID.fullmatch(run_id):
            raise ValueError("run_id must match causal_[A-Za-z0-9_-]{8,96}")
        run_dir = self.output_dir / run_id
        if run_dir.exists():
            raise CausalExecutionError(f"immutable run directory already exists: {run_dir}")
        run_dir.mkdir(parents=True)

        started_at = _utc_now()
        trials: list[TrialRecord] = []
        report_path = run_dir / "report.json"
        trials_path = run_dir / "trials.jsonl"
        environment_path = run_dir / "environment.json"
        fingerprint = suite_fingerprint(suite)

        try:
            self._preflight(suite)
            arm_ids = [arm.id for arm in suite.arms]
            for case in suite.cases:
                for layer in LAYERS:
                    for warmup, repetition in _repetition_schedule(run_config):
                        order = _balanced_order(
                            arm_ids,
                            seed=run_config.seed,
                            suite_id=suite.suite_id,
                            case_id=case.id,
                            layer=layer,
                            repetition=repetition,
                            warmup=warmup,
                        )
                        phase = "w" if warmup else "r"
                        pair_id = f"{run_id}_{case.id}_{layer}_{phase}{repetition:03d}"
                        for order_index, arm_id in enumerate(order):
                            context = _trial_context(
                                suite=suite,
                                case=case,
                                layer=layer,
                                arm_id=arm_id,
                                run_id=run_id,
                                pair_id=pair_id,
                                repetition=repetition,
                                warmup=warmup,
                                order_index=order_index,
                                seed=run_config.seed,
                            )
                            trial = await self._run_trial(context)
                            trials.append(trial)
                            _atomic_write_json(
                                run_dir / "trials" / f"{trial.trial_id}.json",
                                trial.model_dump(mode="json"),
                            )

            report = CausalReport(
                status="completed",
                run_id=run_id,
                suite_id=suite.suite_id,
                suite_fingerprint=fingerprint,
                started_at=started_at,
                finished_at=_utc_now(),
                config=run_config,
                summary=_aggregate(trials, suite=suite, seed=run_config.seed),
                trials=trials,
            )
            environment = _environment_payload(self.environment, suite=suite, config=run_config)
            _atomic_write_text(trials_path, _trials_jsonl(trials))
            _atomic_write_json(environment_path, environment)
            _atomic_write_json(report_path, report.model_dump(mode="json"))
            _atomic_write_text(run_dir / "summary.md", _summary_markdown(report, suite))
            return CausalRunResult(report, run_dir, report_path, trials_path, environment_path)
        except Exception as exc:
            aborted = CausalReport(
                status="aborted",
                run_id=run_id,
                suite_id=suite.suite_id,
                suite_fingerprint=fingerprint,
                started_at=started_at,
                finished_at=_utc_now(),
                config=run_config,
                summary=_aggregate(trials, suite=suite, seed=run_config.seed),
                trials=trials,
                error={"type": type(exc).__name__, "message": str(exc)},
            )
            _atomic_write_json(
                environment_path,
                _environment_payload(self.environment, suite=suite, config=run_config),
            )
            _atomic_write_json(report_path, aborted.model_dump(mode="json"))
            if trials:
                _atomic_write_text(trials_path, _trials_jsonl(trials))
            if isinstance(exc, CausalExecutionError):
                raise
            raise CausalExecutionError(str(exc)) from exc

    def _preflight(self, suite: CausalSuite) -> None:
        missing_layers = [layer for layer in LAYERS if layer not in self.executors]
        if missing_layers:
            raise CausalExecutionError(
                "missing injected executors before dispatch: " + ", ".join(missing_layers)
            )
        missing_oracles = sorted(
            {case.oracle_id for case in suite.cases if case.oracle_id not in self.oracles}
        )
        if missing_oracles:
            raise CausalExecutionError(
                "missing independent oracles before dispatch: " + ", ".join(missing_oracles)
            )

    async def _run_trial(self, context: TrialContext) -> TrialRecord:
        executor = self.executors[context.layer]
        observer = self.oracles[context.oracle_id]
        wall_start = time.perf_counter()
        try:
            with _route_lock(context):
                raw_execution = await asyncio.wait_for(
                    executor.execute(context), timeout=context.timeout_seconds
                )
            execution = _as_execution(raw_execution)
            _validate_execution_contract(context, execution)
            raw_oracle = await asyncio.wait_for(
                observer.observe(context), timeout=context.timeout_seconds
            )
            oracle = _as_oracle(raw_oracle)
        except asyncio.TimeoutError as exc:
            raise CausalExecutionError(
                f"trial {context.trial_id} timed out; it must not be replayed automatically"
            ) from exc
        wall_ms = (time.perf_counter() - wall_start) * 1_000.0
        return TrialRecord(
            trial_id=context.trial_id,
            run_id=context.run_id,
            pair_id=context.pair_id,
            case_id=context.case_id,
            layer=context.layer,
            arm_id=context.arm_id,
            repetition=context.repetition,
            warmup=context.warmup,
            order_index=context.order_index,
            runner_id=context.runner_id,
            route_lock=context.route_lock,
            artifacts=context.artifacts,
            wall_duration_ms=wall_ms,
            execution=execution,
            oracle=oracle,
            final_success=execution.execution_success and oracle.passed,
        )


def _trial_context(
    *,
    suite: CausalSuite,
    case: Any,
    layer: CausalLayer,
    arm_id: str,
    run_id: str,
    pair_id: str,
    repetition: int,
    warmup: bool,
    order_index: int,
    seed: int,
) -> TrialContext:
    runner_id: str | None = None
    route_lock: str | None = None
    artifacts: dict[str, str] = {}
    if layer == "transport_replay":
        runner_id = case.transport_replay.runner_id
        ref = case.transport_replay.script
        artifacts = {ref.path: ref.sha256}
    elif layer == "planner_isolated":
        runner_id = case.planner_isolated.runner_id
        arm_artifacts = next(
            item for item in case.planner_isolated.artifacts if item.arm_id == arm_id
        )
        artifacts = {
            arm_artifacts.plan.path: arm_artifacts.plan.sha256,
            arm_artifacts.script.path: arm_artifacts.script.sha256,
        }
    else:
        route_lock = next(
            item.route_lock for item in case.native_e2e.routes if item.arm_id == arm_id
        )
    return TrialContext(
        run_id=run_id,
        pair_id=pair_id,
        trial_id=f"trial_{uuid.uuid4().hex}",
        suite_id=suite.suite_id,
        case_id=case.id,
        layer=layer,
        arm_id=arm_id,
        prompt=case.prompt,
        category=case.category,
        risk=case.risk,
        fixture_id=case.fixture_id,
        oracle_id=case.oracle_id,
        timeout_seconds=case.timeout_seconds,
        repetition=repetition,
        warmup=warmup,
        order_index=order_index,
        seed=seed,
        runner_id=runner_id,
        route_lock=route_lock,
        artifacts=artifacts,
    )


def _validate_execution_contract(
    context: TrialContext, observation: ExecutionObservation
) -> None:
    if context.layer in {"transport_replay", "planner_isolated"}:
        if observation.observed_runner_id != context.runner_id:
            raise CausalExecutionError(
                f"trial {context.trial_id}: common runner mismatch: "
                f"expected {context.runner_id!r}, observed {observation.observed_runner_id!r}"
            )
        if observation.consumed_artifacts != context.artifacts:
            raise CausalExecutionError(
                f"trial {context.trial_id}: frozen artifact acknowledgement mismatch"
            )
    if context.layer == "native_e2e":
        if observation.observed_route_lock != context.route_lock:
            raise CausalExecutionError(
                f"trial {context.trial_id}: native route-lock mismatch: "
                f"expected {context.route_lock!r}, observed {observation.observed_route_lock!r}"
            )
        if observation.consumed_artifacts:
            raise CausalExecutionError(
                f"trial {context.trial_id}: native_e2e must not claim replay/planner artifacts"
            )


@contextmanager
def _route_lock(context: TrialContext):
    if context.layer != "native_e2e":
        yield
        return
    keys = (ROUTE_LOCK_ENV, ARM_ENV, TRIAL_ENV)
    prior = {key: os.environ.get(key) for key in keys}
    existing = prior[ROUTE_LOCK_ENV]
    if existing is not None and existing != context.route_lock:
        raise CausalExecutionError(
            f"pre-existing route lock {existing!r} conflicts with {context.route_lock!r}"
        )
    os.environ[ROUTE_LOCK_ENV] = str(context.route_lock)
    os.environ[ARM_ENV] = context.arm_id
    os.environ[TRIAL_ENV] = context.trial_id
    try:
        yield
    finally:
        for key, value in prior.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _repetition_schedule(config: CausalRunConfig) -> list[tuple[bool, int]]:
    return [(True, index) for index in range(config.warmups)] + [
        (False, index) for index in range(config.repetitions)
    ]


def _balanced_order(
    arms: list[str],
    *,
    seed: int,
    suite_id: str,
    case_id: str,
    layer: CausalLayer,
    repetition: int,
    warmup: bool,
) -> list[str]:
    """Seeded AB/BA start with alternating neighboring repetitions."""

    material = f"{seed}|{suite_id}|{case_id}|{layer}|{int(warmup)}".encode("utf-8")
    seeded_flip = hashlib.sha256(material).digest()[0] & 1
    reverse = bool(seeded_flip ^ (repetition & 1))
    return list(reversed(arms)) if reverse else list(arms)


def _aggregate(
    trials: list[TrialRecord], *, suite: CausalSuite, seed: int
) -> dict[str, Any]:
    measured = [trial for trial in trials if not trial.warmup]
    layer_summary: dict[str, Any] = {}
    for layer in LAYERS:
        layer_trials = [trial for trial in measured if trial.layer == layer]
        arms: dict[str, Any] = {}
        for arm in suite.arms:
            arm_trials = [trial for trial in layer_trials if trial.arm_id == arm.id]
            durations = [trial.execution.duration_ms for trial in arm_trials]
            planning = [trial.execution.planning_ms for trial in arm_trials]
            calls = [float(trial.execution.call_count) for trial in arm_trials]
            tokens = [
                float(trial.execution.token_count)
                for trial in arm_trials
                if trial.execution.token_count is not None
            ]
            arms[arm.id] = {
                "trial_count": len(arm_trials),
                "final_success_rate": _rate(trial.final_success for trial in arm_trials),
                "oracle_pass_rate": _rate(trial.oracle.passed for trial in arm_trials),
                "duration_ms": _distribution(durations),
                "planning_ms": _distribution(planning),
                "call_count": _distribution(calls),
                "token_count": _distribution(tokens),
            }
        layer_summary[layer] = {
            "arms": arms,
            "paired": _paired(layer_trials, suite=suite, seed=seed),
        }

    order_counts = {"AB": 0, "BA": 0}
    pairs: dict[str, list[TrialRecord]] = {}
    for trial in trials:
        pairs.setdefault(trial.pair_id, []).append(trial)
    nominal_a = suite.arms[0].id
    for pair in pairs.values():
        if len(pair) == 2:
            first = min(pair, key=lambda item: item.order_index).arm_id
            order_counts["AB" if first == nominal_a else "BA"] += 1
    gates = {
        "all_oracles_passed": bool(measured) and all(trial.oracle.passed for trial in measured),
        "all_trials_succeeded": bool(measured) and all(trial.final_success for trial in measured),
        "zero_duplicates": all(trial.execution.duplicate_count == 0 for trial in measured),
        "zero_saves": all(trial.execution.save_count == 0 for trial in measured),
        "zero_outcome_unknown": all(not trial.execution.outcome_unknown for trial in measured),
        "complete_pairs": bool(measured) and all(len(pair) == 2 for pair in pairs.values()),
    }
    gates["all_required"] = all(gates.values())
    return {
        "measured_trial_count": len(measured),
        "warmup_trial_count": len(trials) - len(measured),
        "arm_configuration": [arm.model_dump(mode="json") for arm in suite.arms],
        "pair_order_counts": order_counts,
        "layers": layer_summary,
        "gates": gates,
    }


def _paired(
    trials: list[TrialRecord], *, suite: CausalSuite, seed: int
) -> dict[str, Any]:
    arm_a, arm_b = suite.arms[0].id, suite.arms[1].id
    pairs: dict[str, dict[str, TrialRecord]] = {}
    for trial in trials:
        pairs.setdefault(trial.pair_id, {})[trial.arm_id] = trial
    duration: list[float] = []
    planning: list[float] = []
    calls: list[float] = []
    records: list[dict[str, Any]] = []
    for pair_id, pair in sorted(pairs.items()):
        if set(pair) != {arm_a, arm_b}:
            continue
        a, b = pair[arm_a], pair[arm_b]
        duration_delta = b.execution.duration_ms - a.execution.duration_ms
        planning_delta = b.execution.planning_ms - a.execution.planning_ms
        call_delta = float(b.execution.call_count - a.execution.call_count)
        duration.append(duration_delta)
        planning.append(planning_delta)
        calls.append(call_delta)
        records.append(
            {
                "pair_id": pair_id,
                "case_id": a.case_id,
                "arm_b_minus_arm_a_duration_ms": duration_delta,
                "arm_b_minus_arm_a_planning_ms": planning_delta,
                "arm_b_minus_arm_a_call_count": call_delta,
            }
        )
    return {
        "arm_a": arm_a,
        "arm_b": arm_b,
        "pair_count": len(records),
        "duration_ms_b_minus_a": _delta_summary(duration, seed=seed),
        "planning_ms_b_minus_a": _delta_summary(planning, seed=seed + 1),
        "call_count_b_minus_a": _delta_summary(calls, seed=seed + 2),
        "records": records,
    }


def _distribution(values: list[float]) -> dict[str, float | int | None]:
    return {
        "count": len(values),
        "mean": sum(values) / len(values) if values else None,
        "p50": _percentile(values, 0.5),
        "p90": _percentile(values, 0.9),
    }


def _delta_summary(values: list[float], *, seed: int) -> dict[str, Any]:
    return {
        **_distribution(values),
        "bootstrap_95": _bootstrap_ci(values, seed=seed),
    }


def _percentile(values: list[float], quantile: float) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _bootstrap_ci(values: list[float], *, seed: int, samples: int = 2_000) -> dict[str, Any]:
    if not values:
        return {"low": None, "high": None, "samples": 0}
    if len(values) == 1:
        return {"low": values[0], "high": values[0], "samples": 1}
    rng = random.Random(seed)
    means = [
        sum(values[rng.randrange(len(values))] for _ in values) / len(values)
        for _ in range(samples)
    ]
    return {
        "low": _percentile(means, 0.025),
        "high": _percentile(means, 0.975),
        "samples": samples,
    }


def _rate(values: Any) -> float | None:
    collected = list(values)
    return sum(bool(value) for value in collected) / len(collected) if collected else None


def _as_execution(value: Any) -> ExecutionObservation:
    observation = (
        value if isinstance(value, ExecutionObservation) else ExecutionObservation.model_validate(value)
    )
    return observation.model_copy(update={"trace": _redact_trace(observation.trace)})


def _redact_trace(value: Any, *, key: str | None = None) -> Any:
    """Keep causal reports useful without persisting scripts or credentials."""

    if (
        isinstance(value, dict)
        and value.get("redacted") is True
        and {"redacted", "sha256", "type", "size"}.issubset(value)
    ):
        return value
    if key is not None and _is_sensitive_trace_key(key):
        return _redacted_descriptor(value)
    if hasattr(value, "model_dump"):
        value = value.model_dump(by_alias=True, mode="json")
    if isinstance(value, dict):
        return {
            str(child_key): _redact_trace(child_value, key=str(child_key))
            for child_key, child_value in value.items()
            if not str(child_key).startswith("_")
        }
    if isinstance(value, (list, tuple)):
        return [_redact_trace(child) for child in value]
    if isinstance(value, bytes):
        return _redacted_descriptor(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _is_sensitive_trace_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_").replace(" ", "_")
    return any(part in normalized for part in _SENSITIVE_TRACE_KEY_PARTS)


def _redacted_descriptor(value: Any) -> dict[str, Any]:
    if isinstance(value, bytes):
        serialized = value
    else:
        try:
            serialized = json.dumps(
                value, sort_keys=True, default=str, ensure_ascii=False
            ).encode("utf-8")
        except (TypeError, ValueError):
            serialized = str(value).encode("utf-8")
    return {
        "redacted": True,
        "sha256": hashlib.sha256(serialized).hexdigest(),
        "type": type(value).__name__,
        "size": len(serialized),
    }


def _as_oracle(value: Any) -> OracleObservation:
    if isinstance(value, OracleObservation):
        return value
    return OracleObservation.model_validate(value)


def _fresh_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"causal_{stamp}_{uuid.uuid4().hex[:8]}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _environment_payload(
    supplied: Mapping[str, Any], *, suite: CausalSuite, config: CausalRunConfig
) -> dict[str, Any]:
    return {
        "python": sys.version,
        "platform": platform.platform(),
        "suite_id": suite.suite_id,
        "arms": [arm.model_dump(mode="json") for arm in suite.arms],
        "seed": config.seed,
        "repetitions": config.repetitions,
        "warmups": config.warmups,
        **dict(supplied),
    }


def _summary_markdown(report: CausalReport, suite: CausalSuite) -> str:
    gates = report.summary.get("gates", {})
    lines = [
        f"# Causal benchmark `{report.run_id}`",
        "",
        f"Suite: `{suite.suite_id}`",
        f"Status: **{report.status.upper()}**",
        f"Measured trials: `{report.summary.get('measured_trial_count', 0)}`",
        f"Warmup trials: `{report.summary.get('warmup_trial_count', 0)}`",
        f"All required gates: `{gates.get('all_required', False)}`",
        "",
        "| Layer | Arm | Trials | Oracle pass | Duration p50 (ms) | Duration p90 (ms) |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for layer, layer_data in report.summary.get("layers", {}).items():
        for arm_id, arm in layer_data.get("arms", {}).items():
            duration = arm.get("duration_ms", {})
            lines.append(
                f"| {layer} | {arm_id} | {arm.get('trial_count', 0)} | "
                f"{arm.get('oracle_pass_rate')} | {duration.get('p50')} | {duration.get('p90')} |"
            )
    return "\n".join(lines) + "\n"


def _atomic_write_json(path: Path, payload: Any) -> None:
    _atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _trials_jsonl(trials: list[TrialRecord]) -> str:
    return "".join(
        json.dumps(item.model_dump(mode="json"), sort_keys=True) + "\n" for item in trials
    )
