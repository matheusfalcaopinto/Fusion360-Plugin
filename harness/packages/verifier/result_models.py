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


_REASONS_BY_STATUS: dict[DecisionStatus, frozenset[DecisionReasonCode]] = {
    DecisionStatus.PASSED: frozenset({DecisionReasonCode.VERIFIED}),
    DecisionStatus.FAILED: frozenset(
        {
            DecisionReasonCode.ASSERTION_FAILED,
            DecisionReasonCode.UNSUPPORTED_ASSERTION,
        }
    ),
    DecisionStatus.INCOMPLETE: frozenset(
        {
            DecisionReasonCode.INCOMPLETE_INSPECTION,
            DecisionReasonCode.INVALID_NUMERIC_EVIDENCE,
        }
    ),
}


def _require_status_reason_consistency(
    status: DecisionStatus,
    reason_codes: list[DecisionReasonCode],
) -> None:
    """Reject empty, duplicate, or status-incompatible decision reasons."""

    if not reason_codes:
        raise ValueError("decision reason_codes must not be empty")
    if len(set(reason_codes)) != len(reason_codes):
        raise ValueError("decision reason_codes must be unique")
    if not set(reason_codes).issubset(_REASONS_BY_STATUS[status]):
        raise ValueError("decision reason_codes are incompatible with status")


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

    @model_validator(mode="after")
    def _coverage_is_exact(self) -> "EvidenceEnvelope":
        if len(set(self.assertion_ids)) != len(self.assertion_ids):
            raise ValueError("evidence assertion_ids must be unique")
        if any(not item.strip() for item in self.assertion_ids):
            raise ValueError("evidence assertion_ids must be non-empty")
        if self.assertion_count != len(self.assertion_ids):
            raise ValueError("evidence assertion_count does not match assertion_ids")
        if self.evaluated_count > self.assertion_count:
            raise ValueError("evidence evaluated_count exceeds assertion_count")
        return self

    @property
    def conclusive(self) -> bool:
        """Whether this envelope can support a pass/fail decision."""

        return bool(
            self.complete
            and self.counts_exact
            and not self.truncated
            and self.stop_reason in (None, "", "complete")
            and self.metrics_finite
            and isinstance(self.document_identity, str)
            and bool(self.document_identity.strip())
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

    @model_validator(mode="after")
    def _status_reasons_are_consistent(self) -> "DecisionResult":
        _require_status_reason_consistency(self.status, self.reason_codes)
        return self


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
        declared_status = self.status or (
            DecisionStatus.PASSED if self.passed else DecisionStatus.FAILED
        )
        if self.passed is not (declared_status is DecisionStatus.PASSED):
            raise ValueError("passed may be true only when status is passed")
        reasons = list(self.reason_codes)
        if not reasons:
            if declared_status is DecisionStatus.PASSED:
                reasons = [DecisionReasonCode.VERIFIED]
            elif declared_status is DecisionStatus.INCOMPLETE:
                reasons = [DecisionReasonCode.INCOMPLETE_INSPECTION]
            else:
                reasons = [DecisionReasonCode.ASSERTION_FAILED]
        _require_status_reason_consistency(declared_status, reasons)
        evidence_sha256 = self.evidence.sha256() if self.evidence is not None else None
        supplied_decision = self.decision
        if supplied_decision is not None and (
            supplied_decision.status is not declared_status
            or supplied_decision.reason_codes != reasons
        ):
            raise ValueError("decision must match verification status and reason codes")
        if (
            supplied_decision is not None
            and supplied_decision.evidence_sha256 != evidence_sha256
        ):
            raise ValueError(
                "decision evidence digest does not match evidence envelope"
            )

        inferred = declared_status
        evidence_is_incomplete = self.evidence is None or not self.evidence.conclusive
        if evidence_is_incomplete and declared_status in {
            DecisionStatus.PASSED,
            DecisionStatus.FAILED,
        }:
            # A pass or repairable failure without conclusive evidence would
            # turn absence into authority.  Normalize it to the only safe
            # projection instead of letting callers mistake it for a verdict.
            inferred = DecisionStatus.INCOMPLETE
            reasons = [DecisionReasonCode.INCOMPLETE_INSPECTION]
            object.__setattr__(self, "passed", False)
            if not any(
                issue.code is FailureCode.INCOMPLETE_INSPECTION for issue in self.issues
            ):
                object.__setattr__(
                    self,
                    "issues",
                    [
                        VerificationIssue(
                            code=FailureCode.INCOMPLETE_INSPECTION,
                            message=(
                                "verification evidence is absent"
                                if self.evidence is None
                                else "verification evidence is inconclusive"
                            ),
                        ),
                        *self.issues,
                    ],
                )
            decision = DecisionResult(
                status=inferred,
                reason_codes=reasons,
                evidence_sha256=evidence_sha256,
            )
        else:
            decision = supplied_decision or DecisionResult(
                status=inferred,
                reason_codes=reasons,
                evidence_sha256=evidence_sha256,
            )

        object.__setattr__(self, "status", inferred)
        object.__setattr__(self, "reason_codes", reasons)
        object.__setattr__(self, "decision", decision)
        return self

    @classmethod
    def pass_result(
        cls,
        *,
        evidence: EvidenceEnvelope,
        metrics: dict[str, Any] | None = None,
    ) -> "VerificationResult":
        """Build a passing result."""

        return cls(
            passed=True,
            status=DecisionStatus.PASSED,
            reason_codes=[DecisionReasonCode.VERIFIED],
            metrics=metrics or {},
            evidence=evidence,
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
