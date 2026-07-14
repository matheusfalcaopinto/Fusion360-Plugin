"""Privacy-preserving JSONL transport and tool trace writer."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_SENSITIVE_KEY_PARTS = (
    "script",
    "content",
    "token",
    "secret",
    "password",
    "passwd",
    "authorization",
    "credential",
    "api_key",
    "apikey",
    "mcp_session",
    "session_header",
)


def redact_sensitive(value: Any, *, key: str | None = None) -> Any:
    """Recursively redact scripts, content, tokens, and secrets.

    Sensitive values are replaced with a deterministic descriptor containing
    only hash, type, and encoded size.  This retains correlation value without
    writing user code or credentials to disk.
    """

    if (
        isinstance(value, dict)
        and value.get("redacted") is True
        and {"redacted", "sha256", "type", "size"}.issubset(value)
    ):
        return value
    if key is not None and _is_sensitive_key(key):
        return _redacted_descriptor(value)
    if hasattr(value, "model_dump"):
        value = value.model_dump(by_alias=True, mode="json")
    if isinstance(value, dict):
        return {
            str(child_key): redact_sensitive(child_value, key=str(child_key))
            for child_key, child_value in value.items()
            if not str(child_key).startswith("_")
        }
    if isinstance(value, (list, tuple)):
        return [redact_sensitive(child) for child in value]
    if isinstance(value, bytes):
        return _redacted_descriptor(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_").replace(" ", "_")
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _redacted_descriptor(value: Any) -> dict[str, Any]:
    if isinstance(value, bytes):
        serialized = value
    else:
        try:
            serialized = json.dumps(value, sort_keys=True, default=str, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError):
            serialized = str(value).encode("utf-8")
    return {
        "redacted": True,
        "sha256": hashlib.sha256(serialized).hexdigest(),
        "type": type(value).__name__,
        "size": len(serialized),
    }


class JsonlTraceLogger:
    """Append-only, recursively redacted JSONL trace."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, event: dict[str, Any]) -> None:
        """Append one sanitized event to the trace."""

        payload = redact_sensitive({"timestamp": datetime.now(timezone.utc).isoformat(), **event})
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True, ensure_ascii=False) + "\n")

    def log_tool_call(
        self,
        session_id: str,
        facade_tool: str,
        native_tool: str,
        arguments: dict[str, Any],
        result_status: str,
        duration_ms: int,
        error_code: str | None = None,
        *,
        connection_generation: int | None = None,
        fingerprint: str | None = None,
        semantics: str | None = None,
        attempts: int | None = None,
        reconnect: bool | None = None,
        queue_ms: int | None = None,
        connection_ms: int | None = None,
        timeout_seconds: float | None = None,
        operation_id: str | None = None,
        outcome: str | None = None,
        transport_mode: str | None = None,
        executor_original_sha256: str | None = None,
        executor_original_bytes: int | None = None,
        executor_transmitted_sha256: str | None = None,
        executor_transmitted_bytes: int | None = None,
        executor_preamble_version: int | None = None,
    ) -> None:
        """Record one tool call and transport diagnostics without raw payloads."""

        serialized = json.dumps(arguments, sort_keys=True, default=str, ensure_ascii=False)
        event: dict[str, Any] = {
            "session_id": session_id,
            "event": "tool_call",
            "facade_tool": facade_tool,
            "native_tool": native_tool,
            "arguments_hash": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
            "arguments_redacted": redact_sensitive(arguments),
            "result_status": result_status,
            "duration_ms": duration_ms,
            "call_ms": duration_ms,
            "error_code": error_code,
        }
        optional = {
            "transport_mode": transport_mode,
            "connection_generation": connection_generation,
            "manifest_fingerprint": fingerprint,
            "call_semantics": semantics,
            "attempt_count": attempts,
            "reconnected": reconnect,
            "queue_wait_ms": queue_ms,
            "connection_ms": connection_ms,
            "timeout_seconds": timeout_seconds,
            "operation_id": operation_id,
            "outcome": outcome,
            "executor_original_sha256": executor_original_sha256,
            "executor_original_bytes": executor_original_bytes,
            "executor_transmitted_sha256": executor_transmitted_sha256,
            "executor_transmitted_bytes": executor_transmitted_bytes,
            "executor_preamble_version": executor_preamble_version,
        }
        event.update({key: value for key, value in optional.items() if value is not None})
        self.log(event)

    def log_transport_event(self, event: str, **fields: Any) -> None:
        """Record lifecycle, retry, timeout, and reconnect diagnostics."""

        self.log({"event": event, **fields})
