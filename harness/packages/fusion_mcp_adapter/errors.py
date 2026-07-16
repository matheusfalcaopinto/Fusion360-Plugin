"""Normalized adapter errors."""

from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    """Stable error taxonomy for MCP and Fusion operations."""

    MCP_PROTOCOL_ERROR = "MCP_PROTOCOL_ERROR"
    TOOL_NOT_ALLOWED = "TOOL_NOT_ALLOWED"
    TOOL_SCHEMA_VALIDATION_ERROR = "TOOL_SCHEMA_VALIDATION_ERROR"
    FUSION_DOCUMENT_NOT_READY = "FUSION_DOCUMENT_NOT_READY"
    FUSION_OPERATION_FAILED = "FUSION_OPERATION_FAILED"
    FUSION_FEATURE_FAILED = "FUSION_FEATURE_FAILED"
    FUSION_SELECTION_CONTEXT_ERROR = "FUSION_SELECTION_CONTEXT_ERROR"
    TIMEOUT = "TIMEOUT"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    CONNECTION_UNAVAILABLE = "CONNECTION_UNAVAILABLE"
    CONNECTION_LOST = "CONNECTION_LOST"
    CALL_CANCELLED = "CALL_CANCELLED"
    READ_TIMEOUT_MAY_STILL_BE_RUNNING = "READ_TIMEOUT_MAY_STILL_BE_RUNNING"
    MUTATION_OUTCOME_UNKNOWN = "MUTATION_OUTCOME_UNKNOWN"
    SCRIPT_SIZE_LIMIT_EXCEEDED = "SCRIPT_SIZE_LIMIT_EXCEEDED"
    MANIFEST_DRIFT = "MANIFEST_DRIFT"
    AUTHORITY_DENIED = "AUTHORITY_DENIED"
    CLIENT_CLOSED = "CLIENT_CLOSED"
    UNKNOWN = "UNKNOWN"


class FusionHarnessError(RuntimeError):
    """Base class for harness failures with a normalized error code."""

    def __init__(self, message: str, code: ErrorCode = ErrorCode.UNKNOWN) -> None:
        super().__init__(message)
        self.code = code


class ToolNotAllowed(FusionHarnessError):
    """Raised when a native tool is blocked by policy."""

    def __init__(self, tool_name: str) -> None:
        super().__init__(
            f"tool is not allowlisted: {tool_name}", ErrorCode.TOOL_NOT_ALLOWED
        )
        self.tool_name = tool_name


class RealMcpNotConfigured(FusionHarnessError):
    """Raised when real Fusion MCP transport is not configured."""

    def __init__(self) -> None:
        super().__init__(
            "real Fusion MCP is not configured; set FUSION_MCP_ENDPOINT or FUSION_MCP_COMMAND",
            ErrorCode.CONNECTION_UNAVAILABLE,
        )
