"""Privacy-preserving JSONL transport and tool trace writer."""

from __future__ import annotations

import hashlib
import json
import math
import re
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
    "fallback_reason",
    "command",
    "argv",
    "endpoint",
    "uri",
    "path",
    "error",
)

_SAFE_SENSITIVE_NAMES = frozenset({"error_code"})

_TRANSPORT_FIELD_ALLOWLIST = frozenset(
    {
        "attempt",
        "connection_generation",
        "connection_ms",
        "cooldown_seconds",
        "dispatched",
        "duration_ms",
        "effective_transport_mode",
        "error_code",
        "fingerprint",
        "manifest_drift",
        "operation_id",
        "reconnect",
        "replay_policy",
        "retry_suppressed",
        "trusted_internal_read",
    }
)

_TRANSPORT_EVENTS = frozenset(
    {
        "auto_canary_succeeded",
        "auto_fallback",
        "call_replay_policy",
        "client_closed",
        "connection_broken",
        "connection_failed",
        "connection_ready",
        "manifest_persistence_failed",
        "predispatch_reconnect",
        "read_retry",
        "transport_cooldown",
        "worker_started",
        "worker_stopped",
    }
)

_CANONICAL_TOOL_NAMES = frozenset(
    {
        "fusion_agent_fast_execute",
        "fusion_mcp_electronics_read",
        "fusion_mcp_execute",
        "fusion_mcp_read",
    }
)
_TRANSPORT_MODES = frozenset(
    {"auto", "command", "legacy", "persistent", "persistent_post_only"}
)
_CALL_SEMANTICS = frozenset({"mutating", "read_only"})
_REPLAY_POLICIES = frozenset({"before_dispatch_only", "transport_retry"})
_HEX_DIGEST = re.compile(r"[0-9a-fA-F]{64}\Z")
_ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")


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
    if normalized in _SAFE_SENSITIVE_NAMES:
        return False
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _redacted_descriptor(value: Any) -> dict[str, Any]:
    if isinstance(value, bytes):
        serialized = value
    else:
        try:
            serialized = json.dumps(
                value, sort_keys=True, default=str, ensure_ascii=False
            ).encode("utf-8")
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
        """Append one schema-bounded generic event to the trace."""

        event_name = event.get("event")
        if event_name == "dry_run_skipped_execution":
            safe_event: dict[str, Any] = {"event": event_name}
            _add_identifier_hash(safe_event, "session_id", event.get("session_id"))
            _add_identifier_hash(safe_event, "project", event.get("project"))
            mode = event.get("mode")
            if mode in {"normal", "advanced", "diagnostic", "benchmark", "all"}:
                safe_event["mode"] = mode
        elif event_name == "repair_action":
            safe_event = {"event": event_name}
            _add_identifier_hash(safe_event, "session_id", event.get("session_id"))
            code = _safe_error_code(event.get("code"))
            if code is not None:
                safe_event["code"] = code
            _add_identifier_hash(safe_event, "action", event.get("action"))
            for key in ("action_applied",):
                if type(event.get(key)) is bool:
                    safe_event[key] = event[key]
            for key in ("planned_components", "planned_features"):
                value = _safe_nonnegative_int(event.get(key))
                if value is not None:
                    safe_event[key] = value
        else:
            safe_event = {"event": "trace_event_rejected"}

        self._append(safe_event)

    def _append(self, event: dict[str, Any]) -> None:
        """Write an event that has already passed its specific schema."""

        payload = redact_sensitive(
            {"timestamp": datetime.now(timezone.utc).isoformat(), **event}
        )
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
        dispatched: bool | None = None,
        may_have_applied: bool | None = None,
        post_dispatch_replay_suppressed: bool | None = None,
        mutation_outcome: str | None = None,
    ) -> None:
        """Record one tool call and transport diagnostics without raw payloads."""

        serialized = json.dumps(
            arguments, sort_keys=True, default=str, ensure_ascii=False
        )
        encoded_arguments = serialized.encode("utf-8")
        event: dict[str, Any] = {
            "event": "tool_call",
            "arguments_hash": hashlib.sha256(encoded_arguments).hexdigest(),
            "arguments_present": bool(arguments),
            "arguments_bytes": min(len(encoded_arguments), 2**31 - 1),
            "argument_count": min(len(arguments), 2**31 - 1),
            "result_status": result_status
            if result_status in {"ok", "error"}
            else "error",
            "duration_ms": _safe_nonnegative_int(duration_ms) or 0,
            "call_ms": _safe_nonnegative_int(duration_ms) or 0,
        }
        _add_identifier_hash(event, "session_id", session_id)
        _add_tool_identity(event, "facade_tool", facade_tool)
        _add_tool_identity(event, "native_tool", native_tool)
        safe_error_code = _safe_error_code(error_code)
        if safe_error_code is not None:
            event["error_code"] = safe_error_code
        _add_digest_or_hash(event, "manifest_fingerprint", fingerprint)
        _add_identifier_hash(event, "operation_id", operation_id)
        optional = {
            "transport_mode": transport_mode
            if transport_mode in _TRANSPORT_MODES
            else None,
            "connection_generation": _safe_nonnegative_int(connection_generation),
            "call_semantics": semantics if semantics in _CALL_SEMANTICS else None,
            "attempt_count": _safe_nonnegative_int(attempts),
            "reconnected": reconnect if type(reconnect) is bool else None,
            "queue_wait_ms": _safe_nonnegative_int(queue_ms),
            "connection_ms": _safe_nonnegative_int(connection_ms),
            "timeout_seconds": _safe_nonnegative_number(timeout_seconds),
            "outcome": _safe_outcome(outcome),
            "executor_original_bytes": _safe_nonnegative_int(executor_original_bytes),
            "executor_transmitted_bytes": _safe_nonnegative_int(
                executor_transmitted_bytes
            ),
            "executor_preamble_version": _safe_nonnegative_int(
                executor_preamble_version
            ),
            "dispatched": dispatched if type(dispatched) is bool else None,
            "may_have_applied": may_have_applied
            if type(may_have_applied) is bool
            else None,
            "post_dispatch_replay_suppressed": (
                post_dispatch_replay_suppressed
                if type(post_dispatch_replay_suppressed) is bool
                else None
            ),
            "mutation_outcome": mutation_outcome
            if mutation_outcome in {"known", "unknown"}
            else None,
        }
        _add_digest_or_hash(event, "executor_original_sha256", executor_original_sha256)
        _add_digest_or_hash(
            event, "executor_transmitted_sha256", executor_transmitted_sha256
        )
        event.update(
            {key: value for key, value in optional.items() if value is not None}
        )
        self._append(event)

    def log_transport_event(self, event: str, **fields: Any) -> None:
        """Record lifecycle, retry, timeout, and reconnect diagnostics."""

        event_name = event if event in _TRANSPORT_EVENTS else "transport_event_rejected"
        public_fields: dict[str, Any] = {}
        for key, value in fields.items():
            if key not in _TRANSPORT_FIELD_ALLOWLIST:
                continue
            if key in {
                "attempt",
                "connection_generation",
                "connection_ms",
                "duration_ms",
            }:
                safe_value = _safe_nonnegative_int(value)
            elif key == "cooldown_seconds":
                safe_value = _safe_nonnegative_number(value)
            elif key in {
                "dispatched",
                "manifest_drift",
                "reconnect",
                "retry_suppressed",
                "trusted_internal_read",
            }:
                safe_value = value if type(value) is bool else None
            elif key == "effective_transport_mode":
                safe_value = value if value in _TRANSPORT_MODES else None
            elif key == "replay_policy":
                safe_value = value if value in _REPLAY_POLICIES else None
            elif key == "error_code":
                safe_value = _safe_error_code(value)
            elif key == "fingerprint":
                _add_digest_or_hash(public_fields, key, value)
                continue
            elif key == "operation_id":
                _add_identifier_hash(public_fields, key, value)
                continue
            else:  # pragma: no cover - exhaustive defensive branch
                safe_value = None
            if safe_value is not None:
                public_fields[key] = safe_value
        self._append({"event": event_name, **public_fields})


def _identifier_hash(value: Any) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _add_identifier_hash(event: dict[str, Any], key: str, value: Any) -> None:
    if value is not None:
        event[f"{key}_hash"] = _identifier_hash(value)


def _add_digest_or_hash(event: dict[str, Any], key: str, value: Any) -> None:
    if value is None:
        return
    text = str(value)
    if _HEX_DIGEST.fullmatch(text):
        event[key] = text.lower()
    else:
        event[f"{key}_hash"] = _identifier_hash(text)


def _add_tool_identity(event: dict[str, Any], key: str, value: Any) -> None:
    if value in _CANONICAL_TOOL_NAMES:
        event[key] = value
    elif value is not None:
        event[f"{key}_hash"] = _identifier_hash(value)


def _safe_nonnegative_int(value: Any) -> int | None:
    if type(value) is int and 0 <= value <= 2**31 - 1:
        return value
    return None


def _safe_nonnegative_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(float(value)) or value < 0:
        return None
    return value


def _safe_error_code(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if _ERROR_CODE.fullmatch(text) else None


def _safe_outcome(value: Any) -> str | None:
    if value == "ok":
        return "ok"
    return _safe_error_code(value)
