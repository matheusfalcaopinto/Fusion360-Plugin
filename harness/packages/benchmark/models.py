"""Strict benchmark-suite v2, execution, and report models."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


BenchmarkDriver = Literal["internal", "codex_e2e"]
BenchmarkMode = Literal["mock", "real"]
ExecutionPath = Literal["safe_harness", "native_fast"]
RiskClass = Literal["read_only", "additive", "scoped_update", "destructive"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class BenchmarkExpectations(_StrictModel):
    """Declarative expectations; correctness still comes from a code oracle."""

    expected_status: str | None = None
    should_succeed: bool = True
    must_block: bool = False
    max_call_count: int | None = Field(default=None, ge=0)
    mutation_dispatch_count: int | None = Field(default=None, ge=0)


class BenchmarkCase(_StrictModel):
    """One v2 case referencing code-owned fixture, script, and oracle IDs."""

    id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,79}$")
    prompt: str = Field(min_length=1, max_length=8_000)
    category: str = Field(min_length=1, max_length=80)
    risk: RiskClass
    timeout_seconds: float = Field(gt=0, le=900)
    fixture_id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,79}$")
    script_id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,79}$")
    oracle_id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,79}$")
    execution_paths: list[ExecutionPath] = Field(min_length=1, max_length=2)
    expectations: BenchmarkExpectations = Field(default_factory=BenchmarkExpectations)

    @model_validator(mode="after")
    def _unique_paths(self) -> "BenchmarkCase":
        if len(set(self.execution_paths)) != len(self.execution_paths):
            raise ValueError("execution_paths must not contain duplicates")
        return self


class BenchmarkSuite(_StrictModel):
    """The only accepted suite wire format."""

    schema_version: Literal["benchmark_suite.v2"]
    suite_id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,79}$")
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2_000)
    cases: list[BenchmarkCase] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def _unique_case_ids(self) -> "BenchmarkSuite":
        case_ids = [case.id for case in self.cases]
        if len(set(case_ids)) != len(case_ids):
            raise ValueError("benchmark case ids must be unique")
        return self


class BenchmarkRunConfig(_StrictModel):
    """Decision-complete execution configuration for one suite run."""

    driver: BenchmarkDriver = "internal"
    mode: BenchmarkMode = "mock"
    execution_paths: list[ExecutionPath] = Field(
        default_factory=lambda: ["safe_harness", "native_fast"],
        min_length=1,
        max_length=2,
    )
    repetitions: int = Field(default=1, ge=1, le=100)
    warmups: int = Field(default=0, ge=0, le=20)
    seed: int = Field(default=42, ge=0, le=2**31 - 1)
    model: str | None = Field(default=None, min_length=1, max_length=200)
    reasoning_effort: Literal[
        "none", "minimal", "low", "medium", "high", "xhigh", "ultra"
    ] = "high"
    confirm_real_benchmark: bool = False
    baseline_run_id: str | None = Field(
        default=None, pattern=r"^bench_[A-Za-z0-9_-]{8,96}$"
    )
    project: str = Field(
        default="fusion_agent_benchmark", pattern=r"^[A-Za-z0-9_.-]{1,80}$"
    )
    dry_run: bool = False

    @model_validator(mode="after")
    def _validate_driver(self) -> "BenchmarkRunConfig":
        if len(set(self.execution_paths)) != len(self.execution_paths):
            raise ValueError("execution_paths must not contain duplicates")
        if self.driver == "codex_e2e" and not self.model:
            raise ValueError("model is required for codex_e2e")
        return self


class ExecutionObservation(_StrictModel):
    """Normalized output from an internal route or a Codex subprocess."""

    status: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,120}$")
    execution_success: bool
    duration_ms: float = Field(ge=0)
    execution_ms: float = Field(default=0, ge=0)
    planning_ms: float = Field(default=0, ge=0)
    setup_ms: float = Field(default=0, ge=0)
    queue_wait_ms: float = Field(default=0, ge=0)
    connection_ms: float = Field(default=0, ge=0)
    call_ms: float = Field(default=0, ge=0)
    verification_ms: float = Field(default=0, ge=0)
    teardown_ms: float = Field(default=0, ge=0)
    restoration_ms: float = Field(default=0, ge=0)
    call_count: int = Field(default=0, ge=0)
    initialize_count: int = Field(default=0, ge=0)
    reconnect_count: int = Field(default=0, ge=0)
    retry_count: int = Field(default=0, ge=0)
    script_count: int = Field(default=0, ge=0)
    bytes_transferred: int = Field(default=0, ge=0)
    token_count: int | None = Field(default=None, ge=0)
    mutation_dispatch_count: int = Field(default=0, ge=0)
    unexpected_diff_count: int = Field(default=0, ge=0)
    duplicate_count: int = Field(default=0, ge=0)
    save_count: int = Field(default=0, ge=0)
    hub_sync_count: int = Field(default=0, ge=0)
    personal_project_access_count: int = Field(default=0, ge=0)
    parallel_overlap_count: int = Field(default=0, ge=0)
    transport_session_key: str | None = Field(
        default=None, min_length=1, max_length=200
    )
    connection_generation: int | None = Field(default=None, ge=0)
    independent_metric_fields: list[str] = Field(default_factory=list)
    fixture_marker_verified: bool = True
    fingerprint_verified: bool = True
    closed_without_save: bool = True
    restored: bool = True
    blocked_destructive: bool = False
    outcome_unknown: bool = False
    observation: dict[str, Any] = Field(default_factory=dict)
    trace: dict[str, Any] = Field(default_factory=dict)

    @field_validator(
        "duration_ms",
        "execution_ms",
        "planning_ms",
        "setup_ms",
        "queue_wait_ms",
        "connection_ms",
        "call_ms",
        "verification_ms",
        "teardown_ms",
        "restoration_ms",
    )
    @classmethod
    def _finite_duration(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("numeric evidence must be finite")
        return value

    @field_validator("observation", "trace")
    @classmethod
    def _finite_evidence(cls, value: dict[str, Any]) -> dict[str, Any]:
        _require_finite_tree(value)
        return value


class OracleResult(_StrictModel):
    """Independent correctness verdict for one trial."""

    passed: bool
    oracle_id: str
    checks: dict[str, bool] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    message: str = ""

    @field_validator("metrics")
    @classmethod
    def _finite_metrics(cls, value: dict[str, Any]) -> dict[str, Any]:
        _require_finite_tree(value)
        return value


class BenchmarkTrial(_StrictModel):
    """One route arm for one case/repetition pair."""

    trial_id: str
    run_id: str
    pair_id: str
    case_id: str
    prompt: str
    category: str
    risk: RiskClass
    driver: BenchmarkDriver
    mode: BenchmarkMode
    execution_path: ExecutionPath
    repetition: int = Field(ge=0)
    warmup: bool
    order_index: int = Field(ge=0)
    model: str | None = None
    reasoning_effort: str | None = None
    status: str
    first_pass_success: bool
    final_success: bool
    repair_loop_count: int = Field(default=0, ge=0)
    oracle: OracleResult
    metrics: dict[str, Any] = Field(default_factory=dict)
    journal_path: Path | None = None

    model_config = ConfigDict(extra="forbid", strict=True, arbitrary_types_allowed=True)

    @field_validator("metrics")
    @classmethod
    def _finite_metrics(cls, value: dict[str, Any]) -> dict[str, Any]:
        _require_finite_tree(value)
        return value


class BenchmarkResult(BaseModel):
    """Legacy-compatible flattened result returned by ``BenchmarkRunner.run``."""

    id: str
    prompt: str
    status: str
    first_pass_success: bool
    final_success: bool
    repair_loop_count: int
    metrics: dict[str, Any] = Field(default_factory=dict)
    journal_path: Path | None = None
    run_id: str | None = None
    execution_path: str | None = None
    repetition: int = 0

    model_config = ConfigDict(arbitrary_types_allowed=True)

    @classmethod
    def from_trial(cls, trial: BenchmarkTrial) -> "BenchmarkResult":
        return cls(
            id=trial.case_id,
            prompt=trial.prompt,
            status=trial.status,
            first_pass_success=trial.first_pass_success,
            final_success=trial.final_success,
            repair_loop_count=trial.repair_loop_count,
            metrics=trial.metrics,
            journal_path=trial.journal_path,
            run_id=trial.run_id,
            execution_path=trial.execution_path,
            repetition=trial.repetition,
        )


class BenchmarkReport(_StrictModel):
    """Canonical report persisted under one immutable run directory."""

    schema_version: Literal["benchmark_report.v2"] = "benchmark_report.v2"
    status: Literal["completed", "aborted"] = "completed"
    run_id: str
    suite_id: str
    suite_fingerprint: str
    started_at: str
    finished_at: str
    config: BenchmarkRunConfig
    summary: dict[str, Any]
    trials: list[BenchmarkTrial]
    artifact_dir: Path
    error: dict[str, Any] | None = None

    model_config = ConfigDict(extra="forbid", strict=True, arbitrary_types_allowed=True)


class BenchmarkRun(BaseModel):
    """Report plus concrete artifact locations for server integration."""

    report: BenchmarkReport
    report_path: Path
    summary_path: Path
    trials_path: Path
    environment_path: Path

    model_config = ConfigDict(arbitrary_types_allowed=True)


def _require_finite_tree(value: Any) -> None:
    if value is None or isinstance(value, (bool, str, int, Path)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("numeric evidence must be finite")
        return
    if isinstance(value, dict):
        for child in value.values():
            _require_finite_tree(child)
        return
    if isinstance(value, (list, tuple)):
        for child in value:
            _require_finite_tree(child)
