"""Deterministic aggregation, paired deltas, and bootstrap confidence intervals."""

from __future__ import annotations

import math
import random
from collections import defaultdict
from typing import Any, Iterable

from benchmark.models import BenchmarkTrial


def percentile(values: Iterable[float], percentile_value: float) -> float | None:
    """Linear-interpolated percentile without removing outliers."""

    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile_value
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def bootstrap_mean_ci(
    values: list[float],
    *,
    seed: int,
    samples: int = 2_000,
) -> dict[str, float | int | None]:
    """Return a deterministic percentile-bootstrap 95% interval for the mean."""

    if not values:
        return {"low": None, "high": None, "samples": 0}
    if len(values) == 1:
        value = float(values[0])
        return {"low": value, "high": value, "samples": 1}
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(samples):
        sample = [values[rng.randrange(len(values))] for _ in values]
        means.append(sum(sample) / len(sample))
    return {
        "low": percentile(means, 0.025),
        "high": percentile(means, 0.975),
        "samples": samples,
    }


def aggregate_trials(
    trials: list[BenchmarkTrial],
    *,
    seed: int,
    safe_harness_baseline_p90_ms: float | None = None,
    allow_current_safe_baseline: bool = False,
) -> dict[str, Any]:
    """Aggregate measured trials by route and compute paired A/B statistics."""

    measured = [trial for trial in trials if not trial.warmup]
    by_path: dict[str, list[BenchmarkTrial]] = defaultdict(list)
    for trial in measured:
        by_path[trial.execution_path].append(trial)

    route_summary: dict[str, Any] = {}
    for path, path_trials in sorted(by_path.items()):
        durations = [_metric(trial, "duration_ms") for trial in path_trials]
        calls = [_metric(trial, "call_count") for trial in path_trials]
        route_summary[path] = {
            "trial_count": len(path_trials),
            "oracle_pass_rate": _rate(trial.oracle.passed for trial in path_trials),
            "final_success_rate": _rate(trial.final_success for trial in path_trials),
            "duration_ms": {
                "p50": percentile(durations, 0.5),
                "p90": percentile(durations, 0.9),
            },
            "call_count": {
                "p50": percentile(calls, 0.5),
                "p90": percentile(calls, 0.9),
            },
            "safety": {
                "unexpected_diffs": sum(
                    int(_metric(trial, "unexpected_diff_count"))
                    for trial in path_trials
                ),
                "duplicates": sum(
                    int(_metric(trial, "duplicate_count")) for trial in path_trials
                ),
                "saves": sum(
                    int(_metric(trial, "save_count")) for trial in path_trials
                ),
                "restoration_rate": _rate(
                    bool(trial.metrics.get("restored", True)) for trial in path_trials
                ),
            },
        }

    paired = _paired_statistics(measured, seed=seed)
    segments = {
        "read": _route_segment(
            [trial for trial in measured if trial.risk == "read_only"]
        ),
        "write": _route_segment(
            [trial for trial in measured if trial.risk in {"additive", "scoped_update"}]
        ),
    }
    if allow_current_safe_baseline and safe_harness_baseline_p90_ms is None:
        current_safe_p90 = (
            route_summary.get("safe_harness", {}).get("duration_ms", {}).get("p90")
        )
        if isinstance(current_safe_p90, (int, float)) and current_safe_p90 > 0:
            safe_harness_baseline_p90_ms = float(current_safe_p90)
    gates, gate_details = _evaluate_gates(
        measured,
        route_summary,
        paired,
        segments,
        session_trials=trials,
        safe_harness_baseline_p90_ms=safe_harness_baseline_p90_ms,
        baseline_kind="code_owned_mock" if allow_current_safe_baseline else "prior_run",
    )
    return {
        "measured_trial_count": len(measured),
        "warmup_trial_count": len(trials) - len(measured),
        "routes": route_summary,
        "segments": segments,
        "paired": paired,
        "gates": gates,
        "gate_details": gate_details,
        "rollout": _rollout_status(measured, gates),
    }


def _paired_statistics(trials: list[BenchmarkTrial], *, seed: int) -> dict[str, Any]:
    pairs: dict[str, dict[str, BenchmarkTrial]] = defaultdict(dict)
    for trial in trials:
        pairs[trial.pair_id][trial.execution_path] = trial
    duration_deltas: list[float] = []
    duration_percent: list[float] = []
    call_deltas: list[float] = []
    records: list[dict[str, Any]] = []
    for pair_id, arms in sorted(pairs.items()):
        if "safe_harness" not in arms or "native_fast" not in arms:
            continue
        safe = arms["safe_harness"]
        fast = arms["native_fast"]
        safe_duration = _metric(safe, "duration_ms")
        fast_duration = _metric(fast, "duration_ms")
        delta = fast_duration - safe_duration
        percent = (delta / safe_duration * 100.0) if safe_duration else 0.0
        call_delta = _metric(fast, "call_count") - _metric(safe, "call_count")
        duration_deltas.append(delta)
        duration_percent.append(percent)
        call_deltas.append(call_delta)
        records.append(
            {
                "pair_id": pair_id,
                "case_id": safe.case_id,
                "duration_delta_ms": delta,
                "duration_delta_percent": percent,
                "call_count_delta": call_delta,
            }
        )
    return {
        "pair_count": len(records),
        "duration_delta_ms": {
            "mean": _mean(duration_deltas),
            "p50": percentile(duration_deltas, 0.5),
            "p90": percentile(duration_deltas, 0.9),
            "bootstrap_95": bootstrap_mean_ci(duration_deltas, seed=seed),
        },
        "duration_delta_percent": {
            "mean": _mean(duration_percent),
            "p50": percentile(duration_percent, 0.5),
            "p90": percentile(duration_percent, 0.9),
            "bootstrap_95": bootstrap_mean_ci(duration_percent, seed=seed + 1),
        },
        "call_count_delta": {
            "mean": _mean(call_deltas),
            "p50": percentile(call_deltas, 0.5),
            "p90": percentile(call_deltas, 0.9),
            "bootstrap_95": bootstrap_mean_ci(call_deltas, seed=seed + 2),
        },
        "records": records,
    }


def _evaluate_gates(
    trials: list[BenchmarkTrial],
    routes: dict[str, Any],
    paired: dict[str, Any],
    segments: dict[str, Any],
    *,
    session_trials: list[BenchmarkTrial],
    safe_harness_baseline_p90_ms: float | None,
    baseline_kind: str,
) -> tuple[dict[str, bool], dict[str, Any]]:
    all_oracles = bool(trials) and all(trial.oracle.passed for trial in trials)
    safety = all(
        int(_metric(trial, "save_count")) == 0
        and int(_metric(trial, "hub_sync_count")) == 0
        and int(_metric(trial, "personal_project_access_count")) == 0
        and int(_metric(trial, "parallel_overlap_count")) == 0
        and int(_metric(trial, "duplicate_count")) == 0
        and int(_metric(trial, "unexpected_diff_count")) == 0
        and bool(trial.metrics.get("restored", True))
        for trial in trials
    )
    safe_success = routes.get("safe_harness", {}).get("final_success_rate")
    fast_success = routes.get("native_fast", {}).get("final_success_rate")
    success_delta_ok = (
        True
        if safe_success is None or fast_success is None
        else float(fast_success) >= float(safe_success) - 0.02
    )
    duration_delta = paired.get("duration_delta_percent", {}).get("p50")
    calls_delta = paired.get("call_count_delta", {}).get("p50")
    final_success_95 = all(
        route.get("final_success_rate") is not None
        and float(route["final_success_rate"]) >= 0.95
        for route in routes.values()
    )
    single_initialize = _one_initialize_per_session_generation(session_trials)
    mutation_never_replayed = all(_mutation_dispatch_is_safe(trial) for trial in trials)
    expectations_met = bool(trials) and all(
        bool(trial.metrics.get("expectations_met")) for trial in trials
    )
    independent_codex_metrics = all(
        _codex_critical_metrics_are_independent(trial) for trial in trials
    )
    destructive_blocked = all(
        bool(trial.metrics.get("blocked_destructive"))
        for trial in trials
        if trial.risk == "destructive"
    )
    read_reductions = _route_reductions(segments.get("read", {}))
    write_reductions = _route_reductions(segments.get("write", {}))
    safe_trials = [trial for trial in trials if trial.execution_path == "safe_harness"]
    safe_correct = bool(safe_trials) and all(
        trial.oracle.passed for trial in safe_trials
    )
    current_safe_p90 = routes.get("safe_harness", {}).get("duration_ms", {}).get("p90")
    safe_p90_regression = None
    if (
        isinstance(current_safe_p90, (int, float))
        and isinstance(safe_harness_baseline_p90_ms, (int, float))
        and safe_harness_baseline_p90_ms > 0
    ):
        safe_p90_regression = (
            float(current_safe_p90) - safe_harness_baseline_p90_ms
        ) / safe_harness_baseline_p90_ms
    gates = {
        "oracle_100_percent": all_oracles,
        "final_success_at_least_95_percent": final_success_95,
        "zero_safety_regressions": safety,
        "fast_success_within_2pp": success_delta_ok,
        "one_initialize_per_session_generation": single_initialize,
        "mutation_never_replayed": mutation_never_replayed,
        "expectations_met": expectations_met,
        "codex_critical_metrics_independently_observed": independent_codex_metrics,
        "destructive_requests_blocked": destructive_blocked,
        "fast_read_p50_reduction_at_least_50_percent": read_reductions["duration_p50"]
        >= 0.50,
        "fast_read_p90_reduction_at_least_30_percent": read_reductions["duration_p90"]
        >= 0.30,
        "fast_write_p50_reduction_at_least_30_percent": write_reductions["duration_p50"]
        >= 0.30,
        "fast_write_p90_reduction_at_least_20_percent": write_reductions["duration_p90"]
        >= 0.20,
        "fast_write_call_reduction_at_least_50_percent": write_reductions["call_p50"]
        >= 0.50,
        "safe_harness_no_correctness_loss": safe_correct,
        "safe_harness_p90_regression_at_most_10_percent": (
            safe_p90_regression is not None and safe_p90_regression <= 0.10
        ),
        "paired_fast_duration_improved": duration_delta is not None
        and duration_delta < 0,
        "paired_fast_calls_reduced": calls_delta is not None and calls_delta < 0,
    }
    gates["all_required"] = all(gates.values())
    details = {
        "read_reductions": read_reductions,
        "write_reductions": write_reductions,
        "safe_harness_p90_regression": {
            "status": "measured"
            if safe_p90_regression is not None
            else "baseline_required",
            "threshold": 0.10,
            "baseline_kind": baseline_kind,
            "baseline_p90_ms": safe_harness_baseline_p90_ms,
            "current_p90_ms": current_safe_p90,
            "regression": safe_p90_regression,
            "message": (
                "Safe Harness p90 is within the release regression threshold."
                if safe_p90_regression is not None and safe_p90_regression <= 0.10
                else "Select a comparable prior run, or resolve the Safe Harness p90 regression, before promotion."
            ),
        },
    }
    return gates, details


def _route_segment(trials: list[BenchmarkTrial]) -> dict[str, Any]:
    by_path: dict[str, list[BenchmarkTrial]] = defaultdict(list)
    for trial in trials:
        by_path[trial.execution_path].append(trial)
    summary: dict[str, Any] = {}
    for path, path_trials in sorted(by_path.items()):
        durations = [_metric(trial, "duration_ms") for trial in path_trials]
        calls = [_metric(trial, "call_count") for trial in path_trials]
        summary[path] = {
            "trial_count": len(path_trials),
            "duration_ms": {
                "p50": percentile(durations, 0.5),
                "p90": percentile(durations, 0.9),
            },
            "call_count": {
                "p50": percentile(calls, 0.5),
                "p90": percentile(calls, 0.9),
            },
        }
    return summary


def _route_reductions(segment: dict[str, Any]) -> dict[str, float]:
    safe = segment.get("safe_harness", {})
    fast = segment.get("native_fast", {})
    return {
        "duration_p50": _reduction(
            safe.get("duration_ms", {}).get("p50"),
            fast.get("duration_ms", {}).get("p50"),
        ),
        "duration_p90": _reduction(
            safe.get("duration_ms", {}).get("p90"),
            fast.get("duration_ms", {}).get("p90"),
        ),
        "call_p50": _reduction(
            safe.get("call_count", {}).get("p50"), fast.get("call_count", {}).get("p50")
        ),
    }


def _reduction(baseline: Any, candidate: Any) -> float:
    if (
        not isinstance(baseline, (int, float))
        or not isinstance(candidate, (int, float))
        or baseline <= 0
    ):
        return 0.0
    return (float(baseline) - float(candidate)) / float(baseline)


def _rollout_status(
    trials: list[BenchmarkTrial], gates: dict[str, bool]
) -> dict[str, Any]:
    fast = [
        trial
        for trial in trials
        if trial.execution_path == "native_fast" and _is_verified_real_trial(trial)
    ]
    read_count = sum(1 for trial in fast if trial.risk == "read_only")
    additive_count = sum(
        1 for trial in fast if trial.risk == "additive" and trial.oracle.passed
    )
    scoped_count = sum(
        1 for trial in fast if trial.risk == "scoped_update" and trial.oracle.passed
    )
    common_promotion_gates = bool(
        gates.get("zero_safety_regressions", False)
        and gates.get("expectations_met", False)
        and gates.get("codex_critical_metrics_independently_observed", False)
    )
    mutation_promotion_gates = bool(
        common_promotion_gates and gates.get("mutation_never_replayed", False)
    )
    return {
        "native_read": {
            "verified_trials": read_count,
            "required_trials": 50,
            "eligible": read_count >= 50
            and common_promotion_gates
            and gates.get("oracle_100_percent", False)
            and gates.get("fast_success_within_2pp", False)
            and gates.get("fast_read_p50_reduction_at_least_50_percent", False)
            and gates.get("fast_read_p90_reduction_at_least_30_percent", False),
        },
        "additive_fast_execute": {
            "verified_mutations": additive_count,
            "required_mutations": 30,
            "eligible": additive_count >= 30 and mutation_promotion_gates,
        },
        "scoped_update": {
            "verified_mutations": scoped_count,
            "required_mutations": 20,
            "eligible": scoped_count >= 20 and mutation_promotion_gates,
        },
        "always_safe_harness": [
            "destructive",
            "bulk",
            "hidden_shared",
            "reorganization",
            "ambiguous",
        ],
    }


def _one_initialize_per_session_generation(trials: list[BenchmarkTrial]) -> bool:
    groups: dict[tuple[str, int], int] = defaultdict(int)
    for trial in trials:
        session_key = trial.metrics.get("transport_session_key")
        generation = trial.metrics.get("connection_generation")
        if (
            not isinstance(session_key, str)
            or not session_key
            or not isinstance(generation, int)
        ):
            return False
        groups[(session_key, generation)] += int(_metric(trial, "initialize_count"))
    return bool(groups) and all(count == 1 for count in groups.values())


def _mutation_dispatch_is_safe(trial: BenchmarkTrial) -> bool:
    if int(_metric(trial, "duplicate_count")) != 0:
        return False
    dispatches = int(_metric(trial, "mutation_dispatch_count"))
    if trial.risk == "destructive":
        return dispatches == 0
    if (
        trial.risk in {"additive", "scoped_update"}
        and trial.execution_path == "native_fast"
    ):
        return dispatches == 1
    if bool(trial.metrics.get("outcome_unknown")):
        return dispatches == 1
    return True


def _codex_critical_metrics_are_independent(trial: BenchmarkTrial) -> bool:
    if trial.driver != "codex_e2e":
        return True
    observed = set(trial.metrics.get("independent_metric_fields") or [])
    required = {
        "call_count",
        "initialize_count",
        "reconnect_count",
        "retry_count",
        "mutation_dispatch_count",
        "duplicate_count",
        "save_count",
        "transport_session_key",
        "connection_generation",
    }
    return required.issubset(observed)


def _is_verified_real_trial(trial: BenchmarkTrial) -> bool:
    return bool(
        trial.mode == "real"
        and trial.oracle.passed
        and bool(trial.metrics.get("expectations_met"))
        and _codex_critical_metrics_are_independent(trial)
        and trial.metrics.get("fixture_marker_verified")
        and trial.metrics.get("fingerprint_verified")
        and trial.metrics.get("closed_without_save")
        and trial.metrics.get("restored")
        and int(_metric(trial, "save_count")) == 0
        and int(_metric(trial, "hub_sync_count")) == 0
        and int(_metric(trial, "unexpected_diff_count")) == 0
        and int(_metric(trial, "duplicate_count")) == 0
    )


def _metric(trial: BenchmarkTrial, name: str) -> float:
    value = trial.metrics.get(name, 0)
    return float(value) if isinstance(value, (int, float)) else 0.0


def _rate(values: Iterable[bool]) -> float | None:
    items = list(values)
    return (sum(1 for value in items if value) / len(items)) if items else None


def _mean(values: list[float]) -> float | None:
    return (sum(values) / len(values)) if values else None
