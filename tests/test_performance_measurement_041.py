from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "measure-performance.py"
SPEC = importlib.util.spec_from_file_location("measure_performance_041", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_p95_uses_nearest_rank_and_rejects_invalid_samples() -> None:
    samples = [float(value) for value in range(1, 101)]
    assert MODULE._p95(samples) == 95.0
    with pytest.raises(MODULE.PerformanceMeasurementError):
        MODULE._p95([1.0, float("nan")])


def test_workload_digest_binds_iteration_contract() -> None:
    first = MODULE._workload_digest(iterations=2000, warmup=200)
    assert first == MODULE._workload_digest(iterations=2000, warmup=200)
    assert first != MODULE._workload_digest(iterations=2001, warmup=200)
    assert len(first) == 64


@pytest.mark.skipif(os.name != "nt", reason="Windows API regression")
def test_windows_peak_rss_uses_a_non_truncated_process_handle() -> None:
    assert MODULE._windows_peak_rss_bytes() > 0
