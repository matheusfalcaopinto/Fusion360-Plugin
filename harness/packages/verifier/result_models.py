"""Verification result models."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FailureCode(StrEnum):
    """Verification failure taxonomy."""

    UNIT_MISMATCH = "UNIT_MISMATCH"
    OPEN_PROFILE = "OPEN_PROFILE"
    MISSING_PROFILE = "MISSING_PROFILE"
    WRONG_ACTIVE_COMPONENT = "WRONG_ACTIVE_COMPONENT"
    NAME_COLLISION = "NAME_COLLISION"
    FEATURE_CREATION_FAILED = "FEATURE_CREATION_FAILED"
    FEATURE_SUPPRESSED_OR_FAILED = "FEATURE_SUPPRESSED_OR_FAILED"
    INVALID_REFERENCE = "INVALID_REFERENCE"
    EXPORT_FAILED = "EXPORT_FAILED"
    METADATA_MISSING = "METADATA_MISSING"
    JOINT_MISMATCH = "JOINT_MISMATCH"
    INTERFERENCE_DETECTED = "INTERFERENCE_DETECTED"
    PHYSICAL_PROPERTY_MISMATCH = "PHYSICAL_PROPERTY_MISMATCH"
    SCREENSHOT_FAILED = "SCREENSHOT_FAILED"
    MCP_TIMEOUT = "MCP_TIMEOUT"
    MCP_TOOL_ERROR = "MCP_TOOL_ERROR"
    UNSUPPORTED_ASSERTION = "UNSUPPORTED_ASSERTION"
    INCOMPLETE_INSPECTION = "INCOMPLETE_INSPECTION"
    INVALID_NUMERIC_EVIDENCE = "INVALID_NUMERIC_EVIDENCE"
    UNKNOWN = "UNKNOWN"


class DecisionStatus(StrEnum):
    """Tri-state verifier decision; incomplete is never projected as success."""

    PASSED = "passed"
    FAILED = "failed"
    INCOMPLETE = "incomplete"


class DecisionReasonCode(StrEnum):
    """Stable machine-readable decision reasons."""

    VERIFIED = "VERIFIED"
    ASSERTION_FAILED = "ASSERTION_FAILED"
    UNSUPPORTED_ASSERTION = "UNSUPPORTED_ASSERTION"
    INCOMPLETE_INSPECTION = "INCOMPLETE_INSPECTION"
    INVALID_NUMERIC_EVIDENCE = "INVALID_NUMERIC_EVIDENCE"


class EvidenceEnvelope(BaseModel):
    """Completeness and provenance carried with a verifier decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    producer: str
    provenance: dict[str, str] = Field(default_factory=dict)
    document_identity: str | None = None
    complete: bool
    counts_exact: bool
    truncated: bool
    stop_reason: str | None = None
    metrics_finite: bool = True
    assertion_ids: list[str] = Field(default_factory=list)
    assertion_count: int = Field(ge=0)
    evaluated_count: int = Field(ge=0)

    @property
    def conclusive(self) -> bool:
        """Whether this envelope can support a pass/fail decision."""

        return bool(
            self.complete
            and self.counts_exact
            and not self.truncated
            and self.stop_reason in (None, "", "complete")
            and self.metrics_finite
            and self.evaluated_count == self.assertion_count
        )

    def sha256(self) -> str:
        """Return the stable digest bound into the decision."""

        payload = json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


class DecisionResult(BaseModel):
    """Typed verdict with stable reasons and evidence binding."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: DecisionStatus
    reason_codes: list[DecisionReasonCode] = Field(default_factory=list)
    evidence_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class VerificationIssue(BaseModel):
    """One failed verification check."""

    code: FailureCode
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class VerificationResult(BaseModel):
    """Programmatic verification result."""

    passed: bool
    status: DecisionStatus | None = None
    reason_codes: list[DecisionReasonCode] = Field(default_factory=list)
    issues: list[VerificationIssue] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    evidence: EvidenceEnvelope | None = None
    decision: DecisionResult | None = None

    @model_validator(mode="after")
    def _typed_projection_is_consistent(self) -> "VerificationResult":
        inferred = self.status or (
            DecisionStatus.PASSED if self.passed else DecisionStatus.FAILED
        )
        if self.passed is not (inferred is DecisionStatus.PASSED):
            raise ValueError("passed may be true only when status is passed")
        reasons = list(self.reason_codes)
        if not reasons:
            if inferred is DecisionStatus.PASSED:
                reasons = [DecisionReasonCode.VERIFIED]
            elif inferred is DecisionStatus.INCOMPLETE:
                reasons = [DecisionReasonCode.INCOMPLETE_INSPECTION]
            else:
                reasons = [DecisionReasonCode.ASSERTION_FAILED]
        evidence_sha256 = self.evidence.sha256() if self.evidence is not None else None
        decision = self.decision or DecisionResult(
            status=inferred,
            reason_codes=reasons,
            evidence_sha256=evidence_sha256,
        )
        if decision.status is not inferred or decision.reason_codes != reasons:
            raise ValueError("decision must match verification status and reason codes")
        if decision.evidence_sha256 != evidence_sha256:
            raise ValueError(
                "decision evidence digest does not match evidence envelope"
            )
        object.__setattr__(self, "status", inferred)
        object.__setattr__(self, "reason_codes", reasons)
        object.__setattr__(self, "decision", decision)
        return self

    @classmethod
    def pass_result(cls, metrics: dict[str, Any] | None = None) -> "VerificationResult":
        """Build a passing result."""

        return cls(
            passed=True,
            status=DecisionStatus.PASSED,
            reason_codes=[DecisionReasonCode.VERIFIED],
            metrics=metrics or {},
        )

    @classmethod
    def incomplete_result(
        cls,
        *,
        evidence: EvidenceEnvelope,
        issues: list[VerificationIssue] | None = None,
        metrics: dict[str, Any] | None = None,
        reason: DecisionReasonCode = DecisionReasonCode.INCOMPLETE_INSPECTION,
    ) -> "VerificationResult":
        """Build an explicit inconclusive result that cannot authorize repair."""

        return cls(
            passed=False,
            status=DecisionStatus.INCOMPLETE,
            reason_codes=[reason],
            issues=issues or [],
            metrics=metrics or {},
            evidence=evidence,
        )
