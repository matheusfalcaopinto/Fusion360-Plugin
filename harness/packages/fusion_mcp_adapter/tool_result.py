"""Shared adapter result and manifest models."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator


PUBLIC_DOWNSTREAM_ERROR_MESSAGE = "The downstream Fusion operation failed."

_PUBLIC_ERROR_MESSAGES: dict[str, str] = {
    "CALL_CANCELLED": "The Fusion operation was cancelled.",
    "CLIENT_CLOSED": "The Fusion connection is closed.",
    "CONNECTION_LOST": "The Fusion connection was lost.",
    "CONNECTION_UNAVAILABLE": "The Fusion connection is unavailable.",
    "FUSION_OPERATION_FAILED": PUBLIC_DOWNSTREAM_ERROR_MESSAGE,
    "MANIFEST_DRIFT": "The Fusion tool manifest changed; rediscovery is required.",
    "MUTATION_OUTCOME_UNKNOWN": ("The mutation outcome is unknown; do not replay it."),
    "READ_TIMEOUT_MAY_STILL_BE_RUNNING": "The Fusion read timed out.",
    "TIMEOUT": "The Fusion operation timed out.",
    "TOOL_SCHEMA_VALIDATION_ERROR": (
        "The Fusion tool request or result did not match its schema."
    ),
}


class PublicError(BaseModel):
    """Bounded error information safe for public responses and journals."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    generic_message: str
    correlation_id: str
    retryable: bool = False

    @classmethod
    def create(
        cls,
        *,
        code: str,
        generic_message: str,
        retryable: bool = False,
    ) -> "PublicError":
        return cls(
            code=code,
            generic_message=generic_message,
            correlation_id=f"diag-{uuid.uuid4().hex}",
            retryable=retryable,
        )

    @classmethod
    def downstream_failure(cls, code: str = "FUSION_OPERATION_FAILED") -> "PublicError":
        return cls.create(
            code=code,
            generic_message=PUBLIC_DOWNSTREAM_ERROR_MESSAGE,
            retryable=False,
        )


class ToolDefinition(BaseModel):
    """A native MCP tool definition after discovery."""

    name: str
    description: str = ""
    model_config = ConfigDict(populate_by_name=True)

    input_schema: dict[str, Any] | None = Field(
        default=None,
        validation_alias=AliasChoices("input_schema", "inputSchema"),
    )
    output_schema: dict[str, Any] | None = Field(
        default=None,
        validation_alias=AliasChoices("output_schema", "outputSchema"),
    )


class ToolManifest(BaseModel):
    """Collection of discovered native MCP tools."""

    schema_version: int = 2
    source: str = "unknown"
    tools: list[ToolDefinition] = Field(default_factory=list)
    fingerprint: str = ""
    captured_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    server: dict[str, Any] = Field(default_factory=dict)
    server_name: str | None = None
    server_version: str | None = None
    protocol_version: str | None = None
    previous_fingerprint: str | None = None

    @model_validator(mode="after")
    def _upgrade_and_fingerprint(self) -> "ToolManifest":
        # Loading an old manifest is itself the v1 -> v2 migration.  The
        # original fields remain accepted and no caller migration is required.
        if self.schema_version < 2:
            self.schema_version = 2
        if not self.fingerprint:
            self.fingerprint = self.calculate_fingerprint()
        return self

    def names(self) -> set[str]:
        """Return all native tool names in the manifest."""

        return {tool.name for tool in self.tools}

    def calculate_fingerprint(self) -> str:
        """Return the canonical schema fingerprint for this tool surface."""

        payload = [
            {
                "name": tool.name,
                "description": tool.description,
                "inputSchema": tool.input_schema,
                "outputSchema": tool.output_schema,
            }
            for tool in sorted(self.tools, key=lambda item: item.name)
        ]
        serialized = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def refresh_fingerprint(self) -> str:
        """Recompute the fingerprint after an intentional in-memory edit."""

        self.fingerprint = self.calculate_fingerprint()
        return self.fingerprint


class ToolResult(BaseModel):
    """Normalized native tool result."""

    model_config = ConfigDict(populate_by_name=True)

    ok: bool
    data: dict[str, Any] = Field(default_factory=dict)
    content: list[Any] = Field(default_factory=list)
    structured_content: dict[str, Any] | None = Field(
        default=None,
        validation_alias=AliasChoices("structured_content", "structuredContent"),
        serialization_alias="structuredContent",
    )
    meta: dict[str, Any] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("meta", "_meta"),
        serialization_alias="_meta",
    )
    is_error: bool = Field(
        default=False,
        validation_alias=AliasChoices("is_error", "isError"),
        serialization_alias="isError",
    )
    error_code: str | None = None
    error_message: str | None = None
    public_error: PublicError | None = None

    @classmethod
    def success(
        cls,
        *,
        content: list[Any] | None = None,
        structured_content: dict[str, Any] | None = None,
        meta: dict[str, Any] | None = None,
        **data: Any,
    ) -> "ToolResult":
        """Build a successful result."""

        compatible_data = (
            dict(structured_content) if structured_content is not None else data
        )
        return cls(
            ok=True,
            data=compatible_data,
            content=content or [],
            structured_content=structured_content,
            meta=meta or {},
        )

    @classmethod
    def failure(
        cls,
        error_code: str,
        error_message: str,
        *,
        data: dict[str, Any] | None = None,
        content: list[Any] | None = None,
        structured_content: dict[str, Any] | None = None,
        meta: dict[str, Any] | None = None,
        is_error: bool = True,
        public_error: PublicError | None = None,
    ) -> "ToolResult":
        """Build a failed result."""

        return cls(
            ok=False,
            data=data or {},
            content=content or [],
            structured_content=structured_content,
            meta=meta or {},
            is_error=is_error,
            error_code=error_code,
            error_message=error_message,
            public_error=public_error,
        )

    @classmethod
    def public_failure(
        cls,
        error_code: str,
        *,
        data: dict[str, Any] | None = None,
        retryable: bool = False,
    ) -> "ToolResult":
        """Build a failure that is safe for every public and durable channel.

        Callers must use this constructor for provider exceptions, schema
        validator failures, subprocess errors, and other untrusted diagnostic
        sources. Raw details deliberately have no parameter and therefore
        cannot accidentally enter ``error_message`` or an MCP side channel.
        """

        code = str(error_code)
        public_error = PublicError.create(
            code=code,
            generic_message=_PUBLIC_ERROR_MESSAGES.get(
                code, "The operation could not be completed."
            ),
            retryable=retryable,
        )
        return cls.failure(
            code,
            public_error.generic_message,
            data=data,
            content=[],
            structured_content=None,
            meta={},
            public_error=public_error,
        )

    @classmethod
    def from_mcp(cls, result: dict[str, Any]) -> "ToolResult":
        """Preserve every MCP result channel while keeping the legacy ``data`` view."""

        raw_content = result.get("content")
        content: list[Any] = list(raw_content) if isinstance(raw_content, list) else []
        structured = result.get("structuredContent", result.get("structured_content"))
        structured = structured if isinstance(structured, dict) else None
        meta = result.get("_meta", result.get("meta"))
        meta = meta if isinstance(meta, dict) else {}
        is_error = bool(result.get("isError", result.get("is_error", False)))
        data = (
            dict(structured)
            if structured is not None
            else _compatible_data_from_content(content, result)
        )
        semantic_error = _semantic_failure_message(result) or _semantic_failure_message(
            data
        )
        if (
            is_error
            or semantic_error
            or _explicit_error_field(result)
            or _explicit_error_field(data)
        ):
            # Downstream error channels are untrusted diagnostics. They must
            # not become canonical result data, exception text, MCP content,
            # telemetry, or durable session artifacts.
            return cls.public_failure("FUSION_OPERATION_FAILED")
        return cls(
            ok=True,
            data=data,
            content=content,
            structured_content=structured,
            meta=meta,
            is_error=False,
        )


def _compatible_data_from_content(
    content: list[Any], fallback: dict[str, Any]
) -> dict[str, Any]:
    text = _content_text(content)
    if text:
        try:
            parsed = json.loads(text.strip())
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            return parsed
        return {"text": text}
    return {
        key: value
        for key, value in fallback.items()
        if key
        not in {
            "content",
            "structuredContent",
            "structured_content",
            "_meta",
            "meta",
            "isError",
            "is_error",
        }
    }


def _content_text(content: list[Any]) -> str:
    texts: list[str] = []
    for block in content:
        if (
            isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ):
            texts.append(block["text"])
    return "\n".join(texts)


def _semantic_failure_message(data: dict[str, Any]) -> str | None:
    """Normalize native servers that encode errors in successful MCP envelopes."""

    return _semantic_failure_from_value(data, depth=0)


def _explicit_error_field(data: dict[str, Any]) -> bool:
    """Fail closed for contradictory or malformed success envelopes."""

    return _explicit_error_from_value(data, depth=0)


def _explicit_error_from_value(value: Any, *, depth: int) -> bool:
    """Find downstream failure channels at any JSON position.

    Native servers are not required to put their acknowledgement under a
    particular wrapper.  Walking only ``result``/``data`` allowed a positive
    outer acknowledgement to hide a raw error under an arbitrary key or list.
    The traversal is deliberately bounded; malformed, cyclic, over-deep, or
    over-large envelopes fail closed instead of being accepted as successes.
    """

    seen: set[int] = set()
    visited = 0
    max_depth = 32
    max_nodes = 4096
    error_keys = {"error", "error_message", "exception", "traceback"}
    failure_statuses = {"error", "failed", "failure", "fatal"}

    def meaningful_error(candidate: Any) -> bool:
        if candidate is None or candidate is False:
            return False
        if isinstance(candidate, str):
            return bool(candidate.strip())
        if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
            return candidate != 0
        if isinstance(candidate, (dict, list)):
            return bool(candidate)
        return True

    def walk(candidate: Any, current_depth: int) -> bool:
        nonlocal visited
        visited += 1
        if visited > max_nodes or current_depth > max_depth:
            return True

        if isinstance(candidate, str):
            stripped = candidate.strip()
            if not stripped or stripped[:1] not in {"{", "["}:
                return False
            try:
                parsed = json.loads(stripped)
            except (TypeError, ValueError):
                return False
            return walk(parsed, current_depth + 1)

        if isinstance(candidate, list):
            identity = id(candidate)
            if identity in seen:
                return True
            seen.add(identity)
            try:
                return any(walk(item, current_depth + 1) for item in candidate)
            finally:
                seen.remove(identity)

        if not isinstance(candidate, dict):
            return False

        identity = id(candidate)
        if identity in seen:
            return True
        seen.add(identity)
        try:
            for raw_key, nested in candidate.items():
                key = str(raw_key).strip().lower()
                if key in error_keys and meaningful_error(nested):
                    return True
                if key in {"iserror", "is_error"} and nested is True:
                    return True
                if (
                    key in {"status", "state", "outcome"}
                    and isinstance(nested, str)
                    and nested.strip().lower() in failure_statuses
                ):
                    return True
                if walk(nested, current_depth + 1):
                    return True
            return False
        finally:
            seen.remove(identity)

    return walk(value, depth)


def _semantic_failure_from_value(value: Any, *, depth: int) -> str | None:
    if depth > 5:
        return None
    if isinstance(value, str):
        try:
            parsed = json.loads(value.strip())
        except (TypeError, ValueError):
            return None
        return _semantic_failure_from_value(parsed, depth=depth + 1)
    if not isinstance(value, dict):
        return None

    negative = value.get("success") is False or value.get("ok") is False
    if negative:
        for key in ("error", "error_message"):
            message = value.get(key)
            if isinstance(message, str) and message.strip():
                return message.strip()
        message = value.get("message")
        nested = _semantic_failure_from_value(message, depth=depth + 1)
        if nested:
            return nested
        if isinstance(message, str) and message.strip():
            return message.strip()
        return "Fusion operation returned a negative acknowledgement"

    for key in ("result", "message", "text", "data"):
        nested = _semantic_failure_from_value(value.get(key), depth=depth + 1)
        if nested:
            return nested
    return None
