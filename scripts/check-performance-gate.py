"""Evaluate the 0.4.1 no-I/O p95 and peak-RSS release gate."""

from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
from typing import Any


SCHEMA = "fusion_agent.performance_evidence.v1"
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class PerformanceEvidenceError(ValueError):
    """Performance evidence is incomplete, invalid, or incomparable."""


def evaluate_performance_gate(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    *,
    rss_growth_justified: bool = False,
) -> dict[str, Any]:
    baseline_values = _validated_evidence(baseline, "baseline")
    candidate_values = _validated_evidence(candidate, "candidate")
    if baseline_values["workload_digest"] != candidate_values["workload_digest"]:
        raise PerformanceEvidenceError("baseline and candidate workloads differ")

    baseline_p95 = baseline_values["no_io_p95_ms"]
    candidate_p95 = candidate_values["no_io_p95_ms"]
    absolute_delta_ms = candidate_p95 - baseline_p95
    relative_delta = absolute_delta_ms / baseline_p95
    latency_passed = not (relative_delta > 0.10 and absolute_delta_ms > 25.0)

    baseline_rss = baseline_values["peak_rss_bytes"]
    candidate_rss = candidate_values["peak_rss_bytes"]
    rss_growth = (candidate_rss - baseline_rss) / baseline_rss
    rss_passed = rss_growth <= 0.10 or rss_growth_justified
    return {
        "schema_version": "fusion_agent.performance_gate.v1",
        "passed": latency_passed and rss_passed,
        "latency": {
            "passed": latency_passed,
            "baseline_p95_ms": baseline_p95,
            "candidate_p95_ms": candidate_p95,
            "absolute_delta_ms": absolute_delta_ms,
            "relative_delta": relative_delta,
            "reject_only_when_relative_over": 0.10,
            "and_absolute_delta_over_ms": 25.0,
        },
        "rss": {
            "passed": rss_passed,
            "baseline_peak_bytes": baseline_rss,
            "candidate_peak_bytes": candidate_rss,
            "relative_growth": rss_growth,
            "threshold": 0.10,
            "approved_justification": rss_growth_justified,
        },
        "candidate_git_commit": candidate_values["git_commit"],
        "workload_digest": candidate_values["workload_digest"],
    }


def _validated_evidence(payload: dict[str, Any], label: str) -> dict[str, Any]:
    if payload.get("schema_version") != SCHEMA:
        raise PerformanceEvidenceError(f"{label} schema is invalid")
    commit = payload.get("git_commit")
    workload = payload.get("workload_digest")
    if not isinstance(commit, str) or not SHA_PATTERN.fullmatch(commit):
        raise PerformanceEvidenceError(f"{label} git_commit is invalid")
    if not isinstance(workload, str) or not DIGEST_PATTERN.fullmatch(workload):
        raise PerformanceEvidenceError(f"{label} workload_digest is invalid")
    p95 = _finite_number(payload.get("no_io_p95_ms"), f"{label}.no_io_p95_ms")
    rss = _finite_number(payload.get("peak_rss_bytes"), f"{label}.peak_rss_bytes")
    if p95 <= 0 or rss <= 0:
        raise PerformanceEvidenceError(f"{label} metrics must be positive")
    return {
        "git_commit": commit,
        "workload_digest": workload,
        "no_io_p95_ms": p95,
        "peak_rss_bytes": rss,
    }


def _finite_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise PerformanceEvidenceError(f"{name} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise PerformanceEvidenceError(f"{name} must be finite")
    return number


def _read(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise PerformanceEvidenceError(f"{path.name} must contain an object")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--candidate-commit")
    parser.add_argument("--rss-growth-justified", action="store_true")
    args = parser.parse_args()
    try:
        candidate = _read(args.candidate)
        if (
            args.candidate_commit
            and candidate.get("git_commit") != args.candidate_commit
        ):
            raise PerformanceEvidenceError("candidate commit does not match frozen SHA")
        report = evaluate_performance_gate(
            _read(args.baseline),
            candidate,
            rss_growth_justified=args.rss_growth_justified,
        )
    except (OSError, json.JSONDecodeError, PerformanceEvidenceError) as exc:
        print(
            json.dumps(
                {
                    "passed": False,
                    "error_code": "PERFORMANCE_EVIDENCE_INVALID",
                    "message": str(exc),
                },
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
