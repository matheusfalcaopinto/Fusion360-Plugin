"""Strict benchmark_suite.v2 JSON loader with registry validation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from benchmark.models import BenchmarkCase, BenchmarkSuite
from benchmark.registry import validate_case_registry


class BenchmarkSuiteError(ValueError):
    """A missing, malformed, or unsafe suite definition."""


def load_benchmark_suite(path: Path | str) -> BenchmarkSuite:
    """Load exactly one strict v2 JSON suite; never substitute fallback cases."""

    suite_path = Path(path)
    if suite_path.is_dir():
        suite_path = suite_path / "benchmark_suite_v2.json"
    if not suite_path.exists():
        raise FileNotFoundError(f"benchmark suite does not exist: {suite_path}")
    if suite_path.suffix.lower() != ".json":
        raise BenchmarkSuiteError("benchmark_suite.v2 must be a JSON file")
    try:
        payload = json.loads(suite_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BenchmarkSuiteError(f"invalid benchmark JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise BenchmarkSuiteError("benchmark suite root must be an object")
    if payload.get("schema_version") != "benchmark_suite.v2":
        raise BenchmarkSuiteError("schema_version must be exactly 'benchmark_suite.v2'")
    _reject_embedded_code(payload)
    try:
        suite = BenchmarkSuite.model_validate(payload)
    except ValidationError as exc:
        raise BenchmarkSuiteError(str(exc)) from exc
    for case in suite.cases:
        validate_case_registry(case)
    return suite


def load_benchmark_cases(path: Path | str) -> list[BenchmarkCase]:
    """Compatibility view backed only by the strict v2 loader."""

    return list(load_benchmark_suite(path).cases)


def suite_fingerprint(suite: BenchmarkSuite) -> str:
    """Return the canonical, formatting-independent suite hash."""

    payload = suite.model_dump(mode="json")
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _reject_embedded_code(value: Any, path: str = "$") -> None:
    """Reject fields that could smuggle executable code into a data suite."""

    forbidden = {"script", "python", "code", "command", "shell", "executable"}
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in forbidden or normalized.endswith("_script") or normalized.endswith("_code"):
                raise BenchmarkSuiteError(f"embedded executable field is forbidden at {path}.{key}")
            _reject_embedded_code(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_embedded_code(child, f"{path}[{index}]")
