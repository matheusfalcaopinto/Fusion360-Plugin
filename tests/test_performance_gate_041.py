from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _module():
    path = ROOT / "scripts" / "check-performance-gate.py"
    spec = importlib.util.spec_from_file_location("performance_gate_041", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _evidence(*, p95: float, rss: int, commit: str = "a" * 40) -> dict:
    return {
        "schema_version": "fusion_agent.performance_evidence.v1",
        "git_commit": commit,
        "workload_digest": "b" * 64,
        "no_io_p95_ms": p95,
        "peak_rss_bytes": rss,
    }


def test_latency_rejects_only_when_relative_and_absolute_thresholds_are_exceeded() -> (
    None
):
    module = _module()
    baseline = _evidence(p95=100.0, rss=1000)

    small_absolute = module.evaluate_performance_gate(
        baseline, _evidence(p95=120.0, rss=1000)
    )
    assert small_absolute["latency"]["passed"] is True

    excessive = module.evaluate_performance_gate(
        baseline, _evidence(p95=130.0, rss=1000)
    )
    assert excessive["latency"]["passed"] is False
    assert excessive["passed"] is False


def test_rss_growth_requires_threshold_or_explicit_approved_justification() -> None:
    module = _module()
    baseline = _evidence(p95=100.0, rss=1000)
    candidate = _evidence(p95=100.0, rss=1101)

    rejected = module.evaluate_performance_gate(baseline, candidate)
    assert rejected["rss"]["passed"] is False

    justified = module.evaluate_performance_gate(
        baseline, candidate, rss_growth_justified=True
    )
    assert justified["rss"]["passed"] is True
    assert justified["passed"] is True


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), -1.0])
def test_performance_evidence_rejects_invalid_numeric_values(value: object) -> None:
    module = _module()
    candidate = _evidence(p95=100.0, rss=1000)
    candidate["no_io_p95_ms"] = value

    with pytest.raises(module.PerformanceEvidenceError):
        module.evaluate_performance_gate(_evidence(p95=100.0, rss=1000), candidate)
