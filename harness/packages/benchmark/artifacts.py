"""Immutable per-run benchmark artifacts and paginated report reads."""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import re
import sys
from collections.abc import Mapping
from uuid import uuid4
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path
from typing import Any, cast

from benchmark.models import BenchmarkReport, BenchmarkRun
from benchmark.filesystem import (
    atomic_write_text,
    list_files,
    mkdir,
    path_exists,
    path_is_dir,
    physical_artifact_name,
    read_text,
    replace,
    rmtree,
)
from telemetry.trace import redact_sensitive


_RUN_ID = re.compile(r"^bench_[A-Za-z0-9_-]{8,96}$")


class BenchmarkArtifactStore:
    """Write and read benchmark artifacts beneath ``outputs/benchmarks``."""

    def __init__(self, output_dir: Path | str = "outputs") -> None:
        self.output_dir = Path(output_dir)
        self.root = self.output_dir / "benchmarks"

    def write_run(
        self,
        report: BenchmarkReport,
        *,
        environment: dict[str, Any],
        traces: dict[str, dict[str, Any]],
        oracles: dict[str, dict[str, Any]],
    ) -> BenchmarkRun:
        run_dir = self._run_dir(report.run_id)
        mkdir(self.root)
        if path_exists(run_dir):
            raise FileExistsError(run_dir)
        staging = self.root / f".{report.run_id}.{uuid4().hex}.tmp"
        if path_exists(staging):  # pragma: no cover - UUID collision guard
            raise FileExistsError(staging)
        mkdir(staging)
        try:
            trace_dir = staging / "traces"
            oracle_dir = staging / "oracles"
            mkdir(trace_dir)
            mkdir(oracle_dir)

            trial_lines = "".join(
                json.dumps(
                    _project_trial(trial.model_dump(mode="json")),
                    sort_keys=True,
                    ensure_ascii=False,
                    allow_nan=False,
                )
                + "\n"
                for trial in report.trials
            )
            _atomic_write(staging / "trials.jsonl", trial_lines)
            _atomic_write(
                staging / "report.json",
                json.dumps(
                    _project_report(report.model_dump(mode="json")),
                    indent=2,
                    sort_keys=True,
                    ensure_ascii=False,
                    allow_nan=False,
                ),
            )
            _atomic_write(staging / "summary.md", _summary_markdown(report))
            _atomic_write(
                staging / "environment.json",
                json.dumps(
                    _public_projection(environment),
                    indent=2,
                    sort_keys=True,
                    ensure_ascii=False,
                    allow_nan=False,
                ),
            )
            for trial_id, trace in sorted(traces.items()):
                _atomic_write(
                    trace_dir / physical_artifact_name(_safe_trial_id(trial_id)),
                    json.dumps(
                        _sanitize_trace(trace),
                        indent=2,
                        sort_keys=True,
                        ensure_ascii=False,
                        allow_nan=False,
                    ),
                )
            for trial_id, oracle in sorted(oracles.items()):
                _atomic_write(
                    oracle_dir / physical_artifact_name(_safe_trial_id(trial_id)),
                    json.dumps(
                        _public_projection(oracle),
                        indent=2,
                        sort_keys=True,
                        ensure_ascii=False,
                        allow_nan=False,
                    ),
                )
            replace(staging, run_dir)
        except BaseException:
            rmtree(staging)
            raise
        report_path = run_dir / "report.json"
        summary_path = run_dir / "summary.md"
        trials_path = run_dir / "trials.jsonl"
        environment_path = run_dir / "environment.json"
        return BenchmarkRun(
            report=report,
            report_path=report_path,
            summary_path=summary_path,
            trials_path=trials_path,
            environment_path=environment_path,
        )

    def read(
        self,
        *,
        run_id: str | None = None,
        view: str = "report",
        offset: int = 0,
        limit: int = 100,
        legacy_path: Path | str | None = None,
    ) -> dict[str, Any]:
        """Read one bounded view; absent ``run_id`` explicitly selects legacy mode."""

        if offset < 0:
            raise ValueError("offset must be >= 0")
        if not 1 <= limit <= 1_000:
            raise ValueError("limit must be between 1 and 1000")
        if run_id is None:
            path = (
                Path(legacy_path)
                if legacy_path
                else self.output_dir / "benchmark_report.json"
            )
            if not path_exists(path):
                raise FileNotFoundError(path)
            payload = json.loads(read_text(path))
            if isinstance(payload, list):
                page = payload[offset : offset + limit]
                return {
                    "legacy": True,
                    "offset": offset,
                    "limit": limit,
                    "total": len(payload),
                    "items": [_public_projection(item) for item in page],
                }
            return {"legacy": True, "report": _public_projection(payload)}

        run_dir = self._run_dir(run_id)
        if not path_is_dir(run_dir):
            raise FileNotFoundError(run_dir)
        if view == "summary":
            path = run_dir / "summary.md"
            return {"run_id": run_id, "view": view, "text": read_text(path)}
        if view == "environment":
            path = run_dir / "environment.json"
            return {
                "run_id": run_id,
                "view": view,
                "environment": json.loads(read_text(path)),
            }
        if view == "report":
            path = run_dir / "report.json"
            report = json.loads(read_text(path))
            trials = report.pop("trials", [])
            return {
                "run_id": run_id,
                "view": view,
                "report": report,
                "offset": offset,
                "limit": limit,
                "total": len(trials),
                "trials": trials[offset : offset + limit],
            }
        if view == "trials":
            path = run_dir / "trials.jsonl"
            records = [
                json.loads(line) for line in read_text(path).splitlines() if line
            ]
            return {
                "run_id": run_id,
                "view": view,
                "offset": offset,
                "limit": limit,
                "total": len(records),
                "items": records[offset : offset + limit],
            }
        if view in {"traces", "oracles"}:
            files = list_files(run_dir / view, suffix=".json")
            page = files[offset : offset + limit]
            return {
                "run_id": run_id,
                "view": view,
                "offset": offset,
                "limit": limit,
                "total": len(files),
                "items": [json.loads(read_text(path)) for path in page],
            }
        raise ValueError(
            "view must be report, summary, trials, environment, traces, or oracles"
        )

    def _run_dir(self, run_id: str) -> Path:
        if not _RUN_ID.fullmatch(run_id):
            raise ValueError("invalid benchmark run_id")
        run_dir = (self.root / run_id).resolve()
        root = self.root.resolve()
        if root not in run_dir.parents:
            raise ValueError("benchmark run path escapes output root")
        return run_dir


def collect_environment(
    extra: dict[str, Any] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Capture non-secret provenance once from explicit startup inputs."""

    values = os.environ if environment is None else environment
    payload: dict[str, Any] = {
        "python": sys.version,
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "plugin_version": values.get("FUSION_AGENT_PLUGIN_VERSION"),
        "wheel_version": values.get("FUSION_AGENT_WHEEL_VERSION")
        or _installed_wheel_version(),
        "fusion_version": values.get("FUSION_VERSION"),
        "mcp_fingerprint": values.get("FUSION_MCP_MANIFEST_FINGERPRINT"),
        "git_commit": values.get("GIT_COMMIT"),
    }
    if extra:
        payload.update(extra)
    return payload


def _installed_wheel_version() -> str | None:
    try:
        return package_version("fusion-agent-harness")
    except PackageNotFoundError:
        return None


def _summary_markdown(report: BenchmarkReport) -> str:
    summary = report.summary
    routes = summary.get("routes", {})
    lines = [
        f"# Benchmark {report.run_id}",
        "",
        f"Suite: `{report.suite_id}`",
        f"Driver/mode: `{report.config.driver}` / `{report.config.mode}`",
        f"Status: `{report.status}`",
        f"Measured trials: {summary.get('measured_trial_count', 0)}",
        "",
        "| Route | Trials | Oracle pass | p50 ms | p90 ms |",
        "|---|---:|---:|---:|---:|",
    ]
    for path, route in sorted(routes.items()):
        durations = route.get("duration_ms", {})
        lines.append(
            f"| {path} | {route.get('trial_count', 0)} | "
            f"{_percent(route.get('oracle_pass_rate'))} | "
            f"{_number(durations.get('p50'))} | {_number(durations.get('p90'))} |"
        )
    lines.extend(["", "## Gates", ""])
    for name, passed in sorted(summary.get("gates", {}).items()):
        lines.append(f"- {'PASS' if passed else 'FAIL'} - `{name}`")
    lines.append("")
    if report.error:
        lines.extend(
            [
                "## Abort",
                "",
                f"- Code: `{report.error.get('code', 'BENCHMARK_EXECUTION_FAILED')}`",
                f"- Message: {report.error.get('generic_message', 'The benchmark run failed.')}",
                "",
            ]
        )
    return "\n".join(lines)


def _sanitize_trace(trace: dict[str, Any]) -> dict[str, Any]:
    trace = dict(trace)
    for key in list(trace):
        normalized = key.lower()
        if any(
            part in normalized
            for part in (
                "prompt",
                "stdout",
                "stderr",
                "observation",
                "script",
                "content",
                "error",
                "exception",
                "message",
                "argv",
                "command",
                "path",
            )
        ):
            value = trace.pop(key)
            serialized = json.dumps(
                value, sort_keys=True, default=str, ensure_ascii=False
            ).encode("utf-8")
            trace[f"{key}_redacted"] = {
                "sha256": hashlib.sha256(serialized).hexdigest(),
                "type": type(value).__name__,
                "size": len(serialized),
            }
    return cast(dict[str, Any], _public_projection(redact_sensitive(trace)))


def _public_projection(value: Any, *, depth: int = 0) -> Any:
    """Bound public artifacts and remove provider-controlled error/identity data."""

    if depth > 6:
        return {"truncated": True}
    if isinstance(value, dict):
        projected: dict[str, Any] = {}
        for key, child in list(sorted(value.items(), key=lambda item: str(item[0])))[
            :128
        ]:
            normalized = str(key).lower().replace("-", "_")
            if _is_sensitive_public_key(normalized):
                projected[str(key)] = {"redacted": True}
            else:
                projected[str(key)] = _public_projection(child, depth=depth + 1)
        return projected
    if isinstance(value, (list, tuple)):
        return [
            _public_projection(child, depth=depth + 1) for child in list(value)[:256]
        ]
    if isinstance(value, str):
        if _looks_sensitive_string(value):
            return {"redacted": True}
        return value[:2_048]
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("public benchmark artifact evidence must be finite")
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return type(value).__name__


def _project_trial(value: dict[str, Any]) -> dict[str, Any]:
    projected = dict(value)
    prompt = projected.pop("prompt", None)
    if isinstance(prompt, str):
        projected["prompt_sha256"] = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    status = projected.get("status")
    if not isinstance(status, str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]{1,120}", status
    ):
        projected["status"] = "benchmark_status_redacted"
    return cast(dict[str, Any], _public_projection(projected))


def _project_report(value: dict[str, Any]) -> dict[str, Any]:
    projected = dict(value)
    trials = projected.pop("trials", [])
    public_error = _project_public_error(projected.pop("error", None))
    projected["trials"] = [
        _project_trial(item) for item in trials[:10_000] if isinstance(item, dict)
    ]
    result = cast(dict[str, Any], _public_projection(projected))
    if public_error is not None:
        result["error"] = public_error
    return result


def _project_public_error(value: Any) -> dict[str, Any] | None:
    """Preserve only the fixed public error contract, never provider text."""

    if not isinstance(value, dict):
        return None
    code = value.get("code")
    message = value.get("generic_message")
    correlation_id = value.get("correlation_id")
    retryable = value.get("retryable")
    if (
        not isinstance(code, str)
        or not re.fullmatch(r"[A-Z][A-Z0-9_]{2,79}", code)
        or not isinstance(message, str)
        or not re.fullmatch(r"[A-Za-z0-9 .,;:'()_-]{1,200}", message)
        or not isinstance(correlation_id, str)
        or not re.fullmatch(r"[0-9a-f]{16}", correlation_id)
        or type(retryable) is not bool
    ):
        return None
    return {
        "code": code,
        "generic_message": message,
        "correlation_id": correlation_id,
        "retryable": retryable,
    }


def _is_sensitive_public_key(normalized: str) -> bool:
    """Classify secret/raw-data keys without confusing route names with paths."""

    if normalized == "execution_paths":
        return False
    if normalized in {
        "error",
        "exception",
        "message",
        "token",
        "secret",
        "authorization",
        "credential",
        "document_id",
        "entity_token",
        "path",
        "argv",
        "command",
        "script",
        "content",
        "stdout",
        "stderr",
    }:
        return True
    return normalized.endswith(
        (
            "_error",
            "_exception",
            "_message",
            "_token",
            "_secret",
            "_authorization",
            "_credential",
            "_document_id",
            "_entity_token",
            "_path",
            "_argv",
            "_command",
            "_script",
            "_content",
            "_stdout",
            "_stderr",
        )
    )


def _looks_sensitive_string(value: str) -> bool:
    lowered = value.lower()
    return bool(
        re.search(r"(?:[a-z]:\\|/users/|/home/|data:urn:adsk|--[a-z])", lowered)
        or any(
            marker in lowered
            for marker in (
                "bearer ",
                "token=",
                "secret=",
                "password=",
                "authorization=",
                "argv=",
            )
        )
    )


def _safe_trial_id(trial_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,180}", trial_id):
        raise ValueError("invalid benchmark trial_id")
    return trial_id


def _atomic_write(path: Path, text: str) -> None:
    atomic_write_text(path, text)


def _percent(value: Any) -> str:
    return "n/a" if value is None else f"{float(value) * 100:.1f}%"


def _number(value: Any) -> str:
    return "n/a" if value is None else f"{float(value):.1f}"
