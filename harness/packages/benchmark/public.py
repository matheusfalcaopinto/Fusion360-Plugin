"""Clean-room public comparison contract for Fusion MCP providers.

The manifest contains identity, provenance, cases, and expected fault behavior;
it never contains executable commands.  Executable adapters must be injected by
trusted application code.  Missing prerequisites are represented as
``not_run`` and are never folded into a passing score.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator
from telemetry.trace import redact_sensitive


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class RevisionPin(_StrictModel):
    kind: Literal["git", "pypi", "runtime", "workspace"]
    value: str = Field(min_length=1, max_length=160)


class PublicBenchmarkSubject(_StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,79}$")
    display_name: str = Field(min_length=1, max_length=160)
    source_url: str = Field(pattern=r"^https://")
    license: str = Field(min_length=1, max_length=100)
    redistributable: bool
    pin: RevisionPin
    entitlement: str = Field(min_length=1, max_length=300)
    adapter_id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,79}$")


class PublicBenchmarkCase(_StrictModel):
    id: str = Field(pattern=r"^b0[2-7]_[a-z0-9_]+$")
    fixture_path: str = Field(pattern=r"^benchmark_parametric_suite/cases/b0[2-7]_[a-z0-9_]+$")
    risk: Literal["additive", "scoped_update"]
    oracle_required: bool = True


class FaultScenario(_StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,79}$")
    case_ids: list[str] = Field(min_length=1)
    expected_outcome: Literal[
        "blocked_before_dispatch",
        "outcome_unknown_no_replay",
        "recover_by_readback",
        "zero_dispatch",
        "at_most_one_dispatch",
    ]


class PublicBenchmarkManifest(_StrictModel):
    schema_version: Literal["public_benchmark.v1"]
    generated_at: str
    clean_room: bool
    subjects: list[PublicBenchmarkSubject] = Field(min_length=1)
    cases: list[PublicBenchmarkCase] = Field(min_length=1)
    faults: list[FaultScenario] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_unique_references(self) -> "PublicBenchmarkManifest":
        for label, values in (
            ("subject", [item.id for item in self.subjects]),
            ("case", [item.id for item in self.cases]),
            ("fault", [item.id for item in self.faults]),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"duplicate {label} id")
        case_ids = {item.id for item in self.cases}
        unknown = sorted({case_id for fault in self.faults for case_id in fault.case_ids} - case_ids)
        if unknown:
            raise ValueError(f"fault scenarios reference unknown cases: {unknown}")
        if not self.clean_room:
            raise ValueError("public benchmark manifest must be clean-room")
        return self


class PublicBenchmarkConfig(_StrictModel):
    mode: Literal["mock", "real"] = "real"
    confirm_real_benchmark: bool = False
    disposable_fixture_confirmed: bool = False
    include_faults: bool = True
    subject_ids: list[str] = Field(default_factory=list)


class AdapterPreflight(_StrictModel):
    ready: bool
    observed_revision: str | None = Field(default=None, min_length=1, max_length=200)
    environment: dict[str, str] = Field(default_factory=dict)
    reason: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def _reason_when_not_ready(self) -> "AdapterPreflight":
        if not self.ready and not self.reason:
            raise ValueError("not-ready adapter preflight requires a reason")
        if self.ready and not self.observed_revision:
            raise ValueError("ready adapter preflight requires observed_revision")
        return self


class PublicBenchmarkTask(_StrictModel):
    task_id: str
    case_id: str
    fixture_path: str
    risk: Literal["additive", "scoped_update"]
    fault_id: str | None = None
    expected_outcome: str | None = None


class NormalizedPublicMetrics(_StrictModel):
    task_success: bool | None = None
    oracle_passed: bool | None = None
    contract_coverage: float | None = Field(default=None, ge=0, le=1)
    geometry_valid: bool | None = None
    constraint_health: str | None = None
    backend_id: str | None = None
    backend_version: str | None = None
    latency_ms: float | None = Field(default=None, ge=0)
    tool_calls: int | None = Field(default=None, ge=0)
    mutation_dispatch_count: int | None = Field(default=None, ge=0)
    replay_count: int | None = Field(default=None, ge=0)
    recovery_status: str | None = None
    payload_bytes: int | None = Field(default=None, ge=0)
    install_status: str | None = None


class AdapterExecution(_StrictModel):
    state: Literal["completed", "failed", "not_run"]
    metrics: NormalizedPublicMetrics = Field(default_factory=NormalizedPublicMetrics)
    reason: str | None = Field(default=None, min_length=1, max_length=1_000)
    evidence: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _state_is_honest(self) -> "AdapterExecution":
        if self.state == "completed":
            if self.metrics.task_success is None or self.metrics.oracle_passed is None:
                raise ValueError("completed execution requires task_success and oracle_passed")
        elif not self.reason:
            raise ValueError("failed/not_run execution requires a reason")
        return self


class PublicBenchmarkResult(_StrictModel):
    subject_id: str
    adapter_id: str
    task: PublicBenchmarkTask
    state: Literal["completed", "failed", "not_run"]
    evidence_mode: Literal["mock", "real", "not_run"]
    reason: str | None = None
    observed_revision: str | None = None
    metrics: NormalizedPublicMetrics = Field(default_factory=NormalizedPublicMetrics)
    evidence: dict[str, Any] = Field(default_factory=dict)


class PublicBenchmarkReport(_StrictModel):
    schema_version: Literal["public_benchmark_report.v1"] = "public_benchmark_report.v1"
    run_id: str
    manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    started_at: str
    finished_at: str
    config: PublicBenchmarkConfig
    subjects: list[PublicBenchmarkSubject]
    environment: dict[str, Any]
    summary: dict[str, Any]
    results: list[PublicBenchmarkResult]


class PublicBenchmarkAdapter(Protocol):
    """Trusted, code-injected adapter; manifests cannot instantiate adapters."""

    async def preflight(
        self,
        subject: PublicBenchmarkSubject,
        config: PublicBenchmarkConfig,
    ) -> AdapterPreflight: ...

    async def execute(
        self,
        subject: PublicBenchmarkSubject,
        task: PublicBenchmarkTask,
        config: PublicBenchmarkConfig,
    ) -> AdapterExecution: ...


def load_public_manifest(path: Path | str) -> tuple[PublicBenchmarkManifest, str]:
    """Load and fingerprint a strict, non-executable comparison manifest."""

    manifest_path = Path(path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if _contains_executable_field(payload):
        raise ValueError("public benchmark manifest contains an executable field")
    manifest = PublicBenchmarkManifest.model_validate(payload)
    canonical = json.dumps(manifest.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return manifest, hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class PublicBenchmarkRunner:
    """Run comparable tasks while keeping missing evidence explicit."""

    def __init__(self, adapters: Mapping[str, PublicBenchmarkAdapter] | None = None) -> None:
        self.adapters = dict(adapters or {})

    async def run(
        self,
        manifest_path: Path | str,
        *,
        config: PublicBenchmarkConfig | None = None,
    ) -> PublicBenchmarkReport:
        started = datetime.now(timezone.utc)
        manifest, fingerprint = load_public_manifest(manifest_path)
        run_config = config or PublicBenchmarkConfig()
        subjects = manifest.subjects
        if run_config.subject_ids:
            requested = set(run_config.subject_ids)
            known = {subject.id for subject in subjects}
            unknown = requested - known
            if unknown:
                raise ValueError(f"unknown public benchmark subjects: {sorted(unknown)}")
            subjects = [subject for subject in subjects if subject.id in requested]
        tasks = _tasks(manifest, include_faults=run_config.include_faults)
        results: list[PublicBenchmarkResult] = []
        for subject in subjects:
            adapter = self.adapters.get(subject.adapter_id)
            if adapter is None:
                results.extend(_not_run(subject, tasks, "adapter_not_installed"))
                continue
            if run_config.mode == "real" and not run_config.confirm_real_benchmark:
                results.extend(_not_run(subject, tasks, "real_execution_not_confirmed"))
                continue
            if run_config.mode == "real" and not run_config.disposable_fixture_confirmed:
                results.extend(_not_run(subject, tasks, "disposable_fixture_not_confirmed"))
                continue
            try:
                preflight = await adapter.preflight(subject, run_config)
            except Exception as exc:  # noqa: BLE001 - external adapters normalize at boundary
                results.extend(_not_run(subject, tasks, f"preflight_error:{type(exc).__name__}:{exc}"))
                continue
            revision_error = _revision_error(subject.pin, preflight)
            if not preflight.ready or revision_error:
                results.extend(_not_run(subject, tasks, revision_error or preflight.reason or "preflight_not_ready"))
                continue
            for task in tasks:
                try:
                    execution = await adapter.execute(subject, task, run_config)
                except Exception as exc:  # noqa: BLE001 - a started external trial is a failure, not not_run
                    execution = AdapterExecution(state="failed", reason=f"adapter_error:{type(exc).__name__}:{exc}")
                results.append(
                    PublicBenchmarkResult(
                        subject_id=subject.id,
                        adapter_id=subject.adapter_id,
                        task=task,
                        state=execution.state,
                        evidence_mode=run_config.mode if execution.state != "not_run" else "not_run",
                        reason=execution.reason,
                        observed_revision=preflight.observed_revision,
                        metrics=execution.metrics,
                        evidence=redact_sensitive(
                            {"preflight_environment": preflight.environment, **execution.evidence}
                        ),
                    )
                )
        finished = datetime.now(timezone.utc)
        run_id = f"public_{started.strftime('%Y%m%dT%H%M%S%fZ')}_{fingerprint[:8]}"
        return PublicBenchmarkReport(
            run_id=run_id,
            manifest_sha256=fingerprint,
            started_at=started.isoformat(),
            finished_at=finished.isoformat(),
            config=run_config,
            subjects=subjects,
            environment={
                "python": sys.version,
                "platform": platform.platform(),
                "git_commit": os.getenv("GIT_COMMIT"),
                "fusion_version": os.getenv("FUSION_VERSION"),
            },
            summary=_summary(results),
            results=results,
        )

    @staticmethod
    def write(report: PublicBenchmarkReport, output_dir: Path | str) -> tuple[Path, Path]:
        """Write normalized JSON and Markdown reports."""

        root = Path(output_dir)
        root.mkdir(parents=True, exist_ok=True)
        json_path = root / f"{report.run_id}.json"
        markdown_path = root / f"{report.run_id}.md"
        if json_path.exists() or markdown_path.exists():
            raise FileExistsError(f"public benchmark report already exists: {report.run_id}")
        try:
            with json_path.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(report.model_dump_json(indent=2))
                handle.write("\n")
            with markdown_path.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(_markdown(report))
        except BaseException:
            json_path.unlink(missing_ok=True)
            markdown_path.unlink(missing_ok=True)
            raise
        return json_path, markdown_path


def _tasks(manifest: PublicBenchmarkManifest, *, include_faults: bool) -> list[PublicBenchmarkTask]:
    cases = {case.id: case for case in manifest.cases}
    tasks = [
        PublicBenchmarkTask(
            task_id=f"{case.id}:normal",
            case_id=case.id,
            fixture_path=case.fixture_path,
            risk=case.risk,
        )
        for case in manifest.cases
    ]
    if include_faults:
        for fault in manifest.faults:
            for case_id in fault.case_ids:
                case = cases[case_id]
                tasks.append(
                    PublicBenchmarkTask(
                        task_id=f"{case_id}:fault:{fault.id}",
                        case_id=case_id,
                        fixture_path=case.fixture_path,
                        risk=case.risk,
                        fault_id=fault.id,
                        expected_outcome=fault.expected_outcome,
                    )
                )
    return tasks


def _not_run(
    subject: PublicBenchmarkSubject,
    tasks: list[PublicBenchmarkTask],
    reason: str,
) -> list[PublicBenchmarkResult]:
    return [
        PublicBenchmarkResult(
            subject_id=subject.id,
            adapter_id=subject.adapter_id,
            task=task,
            state="not_run",
            evidence_mode="not_run",
            reason=reason[:1_000],
        )
        for task in tasks
    ]


def _revision_error(pin: RevisionPin, preflight: AdapterPreflight) -> str | None:
    if not preflight.ready:
        return None
    observed = preflight.observed_revision or ""
    if pin.kind in {"git", "pypi"} and observed != pin.value:
        return f"revision_mismatch:expected={pin.value}:observed={observed}"
    if pin.kind in {"runtime", "workspace"} and not observed:
        return "runtime_revision_missing"
    return None


def _summary(results: list[PublicBenchmarkResult]) -> dict[str, Any]:
    states = {state: sum(item.state == state for item in results) for state in ("completed", "failed", "not_run")}
    by_subject: dict[str, dict[str, int]] = {}
    for result in results:
        counts = by_subject.setdefault(result.subject_id, {"completed": 0, "failed": 0, "not_run": 0})
        counts[result.state] += 1
    completed = [item for item in results if item.state == "completed"]
    return {
        "task_count": len(results),
        "states": states,
        "by_subject": by_subject,
        "oracle_pass_rate": (
            sum(item.metrics.oracle_passed is True for item in completed) / len(completed) if completed else None
        ),
        "scoreable": bool(completed),
    }


def _contains_executable_field(value: Any) -> bool:
    forbidden = {"command", "script", "python", "code", "shell", "executable", "args", "env"}
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = re.sub(r"[^a-z]", "", str(key).lower())
            if normalized in forbidden:
                return True
            if _contains_executable_field(child):
                return True
    elif isinstance(value, list):
        return any(_contains_executable_field(child) for child in value)
    return False


def _markdown(report: PublicBenchmarkReport) -> str:
    lines = [
        f"# Public Fusion MCP benchmark {report.run_id}",
        "",
        f"Manifest SHA-256: `{report.manifest_sha256}`",
        f"Mode: `{report.config.mode}`",
        f"Scoreable: `{str(report.summary['scoreable']).lower()}`",
        "",
        "| Subject | Completed | Failed | Not run |",
        "|---|---:|---:|---:|",
    ]
    for subject_id, counts in sorted(report.summary["by_subject"].items()):
        lines.append(
            f"| {subject_id} | {counts['completed']} | {counts['failed']} | {counts['not_run']} |"
        )
    lines.extend(
        [
            "",
            "## Subjects and prerequisites",
            "",
            "| Subject | Adapter | Pin | License | Redistributable | Entitlement |",
            "|---|---|---|---|---|---|",
        ]
    )
    for subject in report.subjects:
        lines.append(
            "| "
            + " | ".join(
                _md_cell(value)
                for value in (
                    subject.display_name,
                    subject.adapter_id,
                    f"{subject.pin.kind}:{subject.pin.value}",
                    subject.license,
                    str(subject.redistributable).lower(),
                    subject.entitlement,
                )
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Normalized task results",
            "",
            "| Subject | Task | State | Evidence | Success | Geometry oracle | Contract coverage | Constraint health | Latency ms | Calls | Payload bytes | Dispatches | Replays | Recovery | Install | Revision / reason |",
            "|---|---|---|---|---:|---:|---:|---|---:|---:|---:|---:|---:|---|---|---|",
        ]
    )
    for result in report.results:
        metrics = result.metrics
        revision_or_reason = result.observed_revision or result.reason or ""
        lines.append(
            "| "
            + " | ".join(
                _md_cell(value)
                for value in (
                    result.subject_id,
                    result.task.task_id,
                    result.state,
                    result.evidence_mode,
                    metrics.task_success,
                    metrics.oracle_passed,
                    metrics.contract_coverage,
                    metrics.constraint_health,
                    metrics.latency_ms,
                    metrics.tool_calls,
                    metrics.payload_bytes,
                    metrics.mutation_dispatch_count,
                    metrics.replay_count,
                    metrics.recovery_status,
                    metrics.install_status,
                    revision_or_reason,
                )
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Environment and provenance",
            "",
            "```json",
            json.dumps(report.environment, indent=2, sort_keys=True, default=str),
            "```",
            "",
            "`not_run` results are excluded from pass rates and never count as success.",
            "Mock, real Fusion, and not-run evidence are never merged into one score.",
            "",
        ]
    )
    return "\n".join(lines)


def _md_cell(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, float):
        value = round(value, 4)
    return str(value).replace("|", "\\|").replace("\n", " ")
