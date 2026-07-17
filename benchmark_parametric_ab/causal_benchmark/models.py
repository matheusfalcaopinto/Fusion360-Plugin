"""Strict wire and runtime models for the three-layer causal benchmark."""

from __future__ import annotations

import math
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from benchmark.provenance import RevisionIdentity


CausalLayer = Literal["transport_replay", "planner_isolated", "native_e2e"]
RiskClass = Literal["read_only", "additive", "scoped_update", "destructive"]
_PUBLIC_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,119}$")
_PUBLIC_VALUE = re.compile(r"^[A-Za-z0-9_.:+-]{1,160}$")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ArtifactRef(_StrictModel):
    path: str = Field(min_length=1, max_length=500)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ArmDefinition(_StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,39}$")
    label: str = Field(min_length=1, max_length=120)
    provider: str = Field(min_length=1, max_length=80)
    model: str = Field(min_length=1, max_length=120)
    reasoning_profile: str = Field(min_length=1, max_length=80)
    system: str = Field(min_length=1, max_length=120)
    metadata: dict[str, str] = Field(default_factory=dict)


class TransportReplayInput(_StrictModel):
    runner_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,79}$")
    script: ArtifactRef


class PlannerArmArtifacts(_StrictModel):
    arm_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,39}$")
    plan: ArtifactRef
    script: ArtifactRef


class PlannerIsolatedInput(_StrictModel):
    runner_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,79}$")
    artifacts: list[PlannerArmArtifacts] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def _unique_arms(self) -> "PlannerIsolatedInput":
        ids = [item.arm_id for item in self.artifacts]
        if len(set(ids)) != len(ids):
            raise ValueError("planner artifact arm_ids must be unique")
        return self


class NativeRoute(_StrictModel):
    arm_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,39}$")
    route_lock: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,79}$")


class NativeE2EInput(_StrictModel):
    routes: list[NativeRoute] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def _unique_routes(self) -> "NativeE2EInput":
        arm_ids = [item.arm_id for item in self.routes]
        route_locks = [item.route_lock for item in self.routes]
        if len(set(arm_ids)) != len(arm_ids):
            raise ValueError("native route arm_ids must be unique")
        if len(set(route_locks)) != len(route_locks):
            raise ValueError("native route locks must be distinct")
        return self


class CausalCase(_StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,79}$")
    prompt: str = Field(min_length=1, max_length=16_000)
    category: str = Field(min_length=1, max_length=120)
    risk: RiskClass
    timeout_seconds: float = Field(gt=0, le=3_600)
    fixture_id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,79}$")
    oracle_id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,79}$")
    transport_replay: TransportReplayInput
    planner_isolated: PlannerIsolatedInput
    native_e2e: NativeE2EInput


class CausalSuite(_StrictModel):
    schema_version: Literal["fusion_causal_suite.v1"]
    suite_id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,79}$")
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4_000)
    arms: list[ArmDefinition] = Field(min_length=2, max_length=2)
    cases: list[CausalCase] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def _unique_ids(self) -> "CausalSuite":
        arm_ids = [arm.id for arm in self.arms]
        case_ids = [case.id for case in self.cases]
        if len(set(arm_ids)) != 2:
            raise ValueError("suite must contain exactly two distinct arms")
        if len(set(case_ids)) != len(case_ids):
            raise ValueError("case ids must be unique")
        expected = set(arm_ids)
        for case in self.cases:
            planner_ids = {item.arm_id for item in case.planner_isolated.artifacts}
            native_ids = {item.arm_id for item in case.native_e2e.routes}
            if planner_ids != expected:
                raise ValueError(
                    f"case {case.id}: planner artifacts must cover exactly {sorted(expected)}"
                )
            if native_ids != expected:
                raise ValueError(
                    f"case {case.id}: native routes must cover exactly {sorted(expected)}"
                )
        return self


class CausalRunConfig(_StrictModel):
    repetitions: int = Field(default=3, ge=1, le=100)
    warmups: int = Field(default=1, ge=0, le=20)
    seed: int = Field(default=42, ge=0, le=2**31 - 1)
    expected_git_commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    expected_source_manifest_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )

    @model_validator(mode="after")
    def _complete_revision_pin(self) -> "CausalRunConfig":
        if (self.expected_git_commit is None) != (
            self.expected_source_manifest_sha256 is None
        ):
            raise ValueError(
                "expected_git_commit and expected_source_manifest_sha256 "
                "must be provided together"
            )
        return self


class TrialContext(_StrictModel):
    run_id: str
    pair_id: str
    trial_id: str
    suite_id: str
    case_id: str
    layer: CausalLayer
    arm_id: str
    prompt: str
    category: str
    risk: RiskClass
    fixture_id: str
    oracle_id: str
    timeout_seconds: float
    repetition: int = Field(ge=0)
    warmup: bool
    order_index: int = Field(ge=0, le=1)
    seed: int
    runner_id: str | None = None
    route_lock: str | None = None
    artifacts: dict[str, str] = Field(default_factory=dict)


class ExecutionObservation(_StrictModel):
    status: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,120}$")
    execution_success: bool
    duration_ms: float = Field(ge=0)
    planning_ms: float = Field(default=0, ge=0)
    execution_ms: float = Field(default=0, ge=0)
    connection_ms: float = Field(default=0, ge=0)
    verification_ms: float = Field(default=0, ge=0)
    call_count: int = Field(default=0, ge=0)
    script_count: int = Field(default=0, ge=0)
    bytes_transferred: int = Field(default=0, ge=0)
    token_count: int | None = Field(default=None, ge=0)
    mutation_dispatch_count: int = Field(default=0, ge=0)
    retry_count: int = Field(default=0, ge=0)
    duplicate_count: int = Field(default=0, ge=0)
    save_count: int = Field(default=0, ge=0)
    outcome_unknown: bool = False
    observed_runner_id: str | None = None
    observed_route_lock: str | None = None
    consumed_artifacts: dict[str, str] = Field(default_factory=dict)
    trace: dict[str, Any] = Field(default_factory=dict)

    @field_validator(
        "duration_ms",
        "planning_ms",
        "execution_ms",
        "connection_ms",
        "verification_ms",
    )
    @classmethod
    def _finite_duration(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("numeric evidence must be finite")
        return value

    @field_validator("trace")
    @classmethod
    def _finite_trace(cls, value: dict[str, Any]) -> dict[str, Any]:
        _require_finite_tree(value)
        return value


class OracleObservation(_StrictModel):
    passed: bool
    checks: dict[str, bool] = Field(default_factory=dict)
    metrics: dict[str, float | int | bool | str | None] = Field(default_factory=dict)
    message: str = Field(default="", max_length=500)

    @field_validator("metrics")
    @classmethod
    def _finite_metrics(
        cls, value: dict[str, float | int | bool | str | None]
    ) -> dict[str, float | int | bool | str | None]:
        _require_finite_tree(value)
        for key, child in value.items():
            if not _PUBLIC_KEY.fullmatch(key):
                raise ValueError("oracle metric key must be a bounded public token")
            if isinstance(child, str) and not _PUBLIC_VALUE.fullmatch(child):
                raise ValueError("oracle string metric must be a bounded public token")
        return value

    @field_validator("checks")
    @classmethod
    def _safe_checks(cls, value: dict[str, bool]) -> dict[str, bool]:
        if any(not _PUBLIC_KEY.fullmatch(key) for key in value):
            raise ValueError("oracle check key must be a bounded public token")
        return value


class TrialRecord(_StrictModel):
    trial_id: str
    run_id: str
    pair_id: str
    case_id: str
    layer: CausalLayer
    arm_id: str
    repetition: int
    warmup: bool
    order_index: int
    runner_id: str | None
    route_lock: str | None
    artifacts: dict[str, str]
    wall_duration_ms: float = Field(ge=0)
    execution: ExecutionObservation
    oracle: OracleObservation
    final_success: bool

    @field_validator("wall_duration_ms")
    @classmethod
    def _finite_wall_duration(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("wall duration must be finite")
        return value


class PublicBenchmarkError(_StrictModel):
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{2,79}$")
    generic_message: str = Field(min_length=1, max_length=200)
    correlation_id: str = Field(pattern=r"^[0-9a-f]{16}$")
    retryable: bool = False


class CausalReport(_StrictModel):
    schema_version: Literal["fusion_causal_report.v1"] = "fusion_causal_report.v1"
    status: Literal["completed", "aborted"]
    run_id: str
    suite_id: str
    suite_fingerprint: str
    started_at: str
    finished_at: str
    config: CausalRunConfig
    revision_identity: RevisionIdentity
    summary: dict[str, Any]
    trials: list[TrialRecord]
    error: PublicBenchmarkError | None = None


def _require_finite_tree(value: Any) -> None:
    if isinstance(value, bool) or value is None or isinstance(value, (str, int)):
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
