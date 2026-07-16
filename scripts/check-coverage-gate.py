"""Enforce the 0.4.1 global and per-boundary coverage contract."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any


DEFAULT_CRITICAL_FILES = (
    "harness/packages/agent_core/authority.py",
    "harness/packages/agent_core/request_context.py",
    "harness/apps/fusion_agent_mcp/mcp_surface.py",
    "harness/packages/fusion_mcp_adapter/tool_result.py",
    "harness/packages/verifier/result_models.py",
    "harness/packages/benchmark/filesystem.py",
    "harness/packages/benchmark/provenance.py",
)


class CoverageGateError(RuntimeError):
    """The measured coverage does not satisfy the release contract."""


def _percentage(summary: Any, *, label: str) -> float:
    if not isinstance(summary, dict):
        raise CoverageGateError(f"{label} coverage summary is missing")
    value = summary.get("percent_covered")
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise CoverageGateError(f"{label} percent_covered must be numeric")
    percentage = float(value)
    if not math.isfinite(percentage) or percentage < 0 or percentage > 100:
        raise CoverageGateError(f"{label} percent_covered is invalid")
    return percentage


def evaluate_coverage(
    payload: dict[str, Any],
    *,
    global_minimum: float,
    baseline_minimum: float,
    critical_minimum: float,
    critical_files: tuple[str, ...] = DEFAULT_CRITICAL_FILES,
) -> dict[str, float]:
    """Return measured percentages or raise when any release gate fails."""

    global_percentage = _percentage(payload.get("totals"), label="global")
    required_global = max(global_minimum, baseline_minimum)
    if global_percentage + 1e-12 < required_global:
        raise CoverageGateError(
            f"global coverage {global_percentage:.2f}% is below {required_global:.2f}%"
        )

    files = payload.get("files")
    if not isinstance(files, dict):
        raise CoverageGateError("coverage file map is missing")
    normalized = {str(path).replace("\\", "/"): value for path, value in files.items()}
    measured: dict[str, float] = {"global": global_percentage}
    for expected in critical_files:
        key = expected.replace("\\", "/")
        entry = normalized.get(key)
        if entry is None:
            matches = [
                value for path, value in normalized.items() if path.endswith("/" + key)
            ]
            if len(matches) != 1:
                raise CoverageGateError(
                    f"critical coverage entry is missing: {expected}"
                )
            entry = matches[0]
        percentage = _percentage(
            entry.get("summary") if isinstance(entry, dict) else None, label=expected
        )
        measured[expected] = percentage
        if percentage + 1e-12 < critical_minimum:
            raise CoverageGateError(
                f"critical coverage {expected}={percentage:.2f}% is below {critical_minimum:.2f}%"
            )
    return measured


def _positive_percentage(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be numeric") from exc
    if not math.isfinite(number) or number < 0 or number > 100:
        raise argparse.ArgumentTypeError("must be between 0 and 100")
    return number


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--coverage-json", type=Path, default=Path("coverage.json"))
    parser.add_argument("--global-min", type=_positive_percentage, default=70.0)
    parser.add_argument("--baseline-min", type=_positive_percentage, default=65.34)
    parser.add_argument("--critical-min", type=_positive_percentage, default=90.0)
    parser.add_argument("--critical", action="append", dest="critical_files")
    args = parser.parse_args()

    try:
        payload = json.loads(args.coverage_json.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise CoverageGateError("coverage payload must be an object")
        measured = evaluate_coverage(
            payload,
            global_minimum=args.global_min,
            baseline_minimum=args.baseline_min,
            critical_minimum=args.critical_min,
            critical_files=tuple(args.critical_files or DEFAULT_CRITICAL_FILES),
        )
    except (OSError, json.JSONDecodeError, CoverageGateError) as exc:
        print(f"coverage_gate=failed reason={exc}")
        return 1

    print("coverage_gate=passed")
    for path, percentage in measured.items():
        print(f"{path}={percentage:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
