"""Verification result models."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


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
    UNKNOWN = "UNKNOWN"


class VerificationIssue(BaseModel):
    """One failed verification check."""

    code: FailureCode
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class VerificationResult(BaseModel):
    """Programmatic verification result."""

    passed: bool
    issues: list[VerificationIssue] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def pass_result(cls, metrics: dict[str, Any] | None = None) -> "VerificationResult":
        """Build a passing result."""

        return cls(passed=True, metrics=metrics or {})
