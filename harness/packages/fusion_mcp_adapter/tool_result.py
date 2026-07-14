"""Shared adapter result and manifest models."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator


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
    captured_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
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
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
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

        compatible_data = dict(structured_content) if structured_content is not None else data
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
        )

    @classmethod
    def from_mcp(cls, result: dict[str, Any]) -> "ToolResult":
        """Preserve every MCP result channel while keeping the legacy ``data`` view."""

        content = result.get("content") if isinstance(result.get("content"), list) else []
        structured = result.get("structuredContent", result.get("structured_content"))
        structured = structured if isinstance(structured, dict) else None
        meta = result.get("_meta", result.get("meta"))
        meta = meta if isinstance(meta, dict) else {}
        is_error = bool(result.get("isError", result.get("is_error", False)))
        data = dict(structured) if structured is not None else _compatible_data_from_content(content, result)
        semantic_error = _semantic_failure_message(data)
        if is_error or semantic_error:
            return cls.failure(
                "FUSION_OPERATION_FAILED",
                semantic_error or _content_text(content) or str(result),
                data=data,
                content=content,
                structured_content=structured,
                meta=meta,
            )
        return cls(
            ok=True,
            data=data,
            content=content,
            structured_content=structured,
            meta=meta,
            is_error=False,
        )


def _compatible_data_from_content(content: list[Any], fallback: dict[str, Any]) -> dict[str, Any]:
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
        if key not in {"content", "structuredContent", "structured_content", "_meta", "meta", "isError", "is_error"}
    }


def _content_text(content: list[Any]) -> str:
    texts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            texts.append(block["text"])
    return "\n".join(texts)


def _semantic_failure_message(data: dict[str, Any]) -> str | None:
    """Normalize native servers that encode errors in successful MCP envelopes."""

    return _semantic_failure_from_value(data, depth=0)


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
