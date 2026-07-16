from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-coverage-gate.py"
SPEC = importlib.util.spec_from_file_location("check_coverage_gate", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _payload(*, global_percentage: float = 73.93, critical_percentage: float = 90.0):
    return {
        "totals": {"percent_covered": global_percentage},
        "files": {
            path: {"summary": {"percent_covered": critical_percentage}}
            for path in MODULE.DEFAULT_CRITICAL_FILES
        },
    }


def _evaluate(payload):
    return MODULE.evaluate_coverage(
        payload,
        global_minimum=70.0,
        baseline_minimum=65.34,
        critical_minimum=90.0,
    )


def test_gate_accepts_global_and_every_critical_boundary() -> None:
    measured = _evaluate(_payload())
    assert measured["global"] == pytest.approx(73.93)
    assert set(MODULE.DEFAULT_CRITICAL_FILES).issubset(measured)


@pytest.mark.parametrize("percentage", [69.99, 65.34])
def test_gate_rejects_global_below_release_minimum(percentage: float) -> None:
    with pytest.raises(MODULE.CoverageGateError, match="global coverage"):
        _evaluate(_payload(global_percentage=percentage))


def test_gate_rejects_one_critical_boundary_below_ninety() -> None:
    payload = _payload()
    path = MODULE.DEFAULT_CRITICAL_FILES[0]
    payload["files"][path]["summary"]["percent_covered"] = 89.99
    with pytest.raises(MODULE.CoverageGateError, match="critical coverage"):
        _evaluate(payload)


def test_gate_rejects_missing_and_invalid_measurements() -> None:
    missing = _payload()
    missing["files"].pop(MODULE.DEFAULT_CRITICAL_FILES[-1])
    with pytest.raises(MODULE.CoverageGateError, match="entry is missing"):
        _evaluate(missing)

    invalid = _payload()
    invalid["totals"]["percent_covered"] = float("nan")
    with pytest.raises(MODULE.CoverageGateError, match="invalid"):
        _evaluate(invalid)
