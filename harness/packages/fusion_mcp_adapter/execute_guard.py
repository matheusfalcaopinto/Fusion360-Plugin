"""Executor hardening shared by every Fusion Python-script dispatch."""

from __future__ import annotations

import ast
import hashlib
import os
from typing import Any


EXECUTE_TOOL_NAME = "fusion_mcp_execute"
PROTECTED_SCRIPT_LIMIT_ENV = "FUSION_AGENT_MAX_PROTECTED_SCRIPT_BYTES"
DEFAULT_PROTECTED_SCRIPT_LIMIT_BYTES = 28 * 1024
EXECUTOR_PREAMBLE_VERSION = 1
_STREAM_ALIAS = "_fusion_agent_runtime_sys"
_STREAM_PREAMBLE_SOURCE = """import sys as _fusion_agent_runtime_sys
def _fusion_agent_is_ns_writer(_value):
    try:
        _class = object.__getattribute__(_value, "__class__")
        _name = object.__getattribute__(_class, "__name__")
        object.__getattribute__(_value, "_original")
    except BaseException:
        return False
    return _name == "_NsSanitizedWriter"
def _fusion_agent_collapse_stream(_outer, _fallback):
    if not _fusion_agent_is_ns_writer(_outer):
        return _outer if _outer is not None else _fallback
    _current = _outer
    _seen = set()
    _delegate = _fallback
    for _index in range(512):
        _identity = id(_current)
        if _identity in _seen:
            _delegate = _fallback
            break
        _seen.add(_identity)
        if not _fusion_agent_is_ns_writer(_current):
            _delegate = _current
            break
        try:
            _current = object.__getattribute__(_current, "_original")
        except BaseException:
            _delegate = _fallback
            break
        if _current is None:
            _delegate = _fallback
            break
    try:
        object.__setattr__(_outer, "_original", _delegate)
    except BaseException:
        return _fallback
    return _outer
_fusion_agent_runtime_sys.stdout = _fusion_agent_collapse_stream(
    _fusion_agent_runtime_sys.stdout, _fusion_agent_runtime_sys.__stdout__
)
_fusion_agent_runtime_sys.stderr = _fusion_agent_collapse_stream(
    _fusion_agent_runtime_sys.stderr, _fusion_agent_runtime_sys.__stderr__
)
del _fusion_agent_collapse_stream
del _fusion_agent_is_ns_writer
del _fusion_agent_runtime_sys
"""
_STREAM_PREAMBLE_NODES = ast.parse(_STREAM_PREAMBLE_SOURCE).body


def normalize_execute_script(script: str) -> str:
    """Make stream-chain collapse the first action of one synchronous ``run``.

    Autodesk wraps ``sys.stdout`` and ``sys.stderr`` on every script execution.
    Rewiring the current wrappers directly to their base delegates prevents a
    recursively nested chain while preserving the current call's capture and
    sanitization.  The transformation is structural and idempotent, so an
    already protected script is returned byte-for-byte.
    """

    if not isinstance(script, str) or not script.strip():
        raise ValueError("fusion_mcp_execute script must be a non-empty string")
    try:
        tree = ast.parse(script)
    except SyntaxError as exc:
        raise ValueError(f"fusion_mcp_execute script syntax error at line {exc.lineno}: {exc.msg}") from exc
    entrypoints = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run"
    ]
    if len(entrypoints) != 1 or isinstance(entrypoints[0], ast.AsyncFunctionDef):
        raise ValueError("fusion_mcp_execute script must define exactly one synchronous run function")
    entrypoint = entrypoints[0]
    if _has_stream_preamble(entrypoint):
        return script
    entrypoint.body[:0] = ast.parse(_STREAM_PREAMBLE_SOURCE).body
    return ast.unparse(ast.fix_missing_locations(tree)).rstrip() + "\n"


def prepare_execute_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Copy and protect script-shaped native execute arguments."""

    payload = dict(arguments)
    if str(payload.get("featureType") or "") != "script":
        return payload
    raw_object = payload.get("object")
    if not isinstance(raw_object, dict):
        raise ValueError("fusion_mcp_execute featureType=script requires object.script")
    protected_object = dict(raw_object)
    protected_object["script"] = normalize_execute_script(protected_object.get("script"))
    payload["object"] = protected_object
    return payload


def protected_script_descriptor(script: str) -> dict[str, Any]:
    """Return exact, content-free payload telemetry and the configured gate."""

    payload = script.encode("utf-8")
    limit = protected_script_limit_bytes()
    return {
        "preamble_version": EXECUTOR_PREAMBLE_VERSION,
        "stream_normalization": "preserve_current_ns_writer_collapse_original_chain",
        "fallback_streams": "sys.__stdout__/sys.__stderr__",
        "payload_kind": "guarded_fusion_python",
        "protected_payload_sha256": hashlib.sha256(payload).hexdigest(),
        "protected_payload_bytes": len(payload),
        "limit_bytes": limit,
        "limit_environment_variable": PROTECTED_SCRIPT_LIMIT_ENV,
        "within_limit": len(payload) <= limit,
    }


def execute_script_telemetry(original: str, transmitted: str) -> dict[str, Any]:
    """Return separate content-free facts for source and wire payloads."""

    original_bytes = original.encode("utf-8")
    transmitted_bytes = transmitted.encode("utf-8")
    return {
        "executor_original_sha256": hashlib.sha256(original_bytes).hexdigest(),
        "executor_original_bytes": len(original_bytes),
        "executor_transmitted_sha256": hashlib.sha256(transmitted_bytes).hexdigest(),
        "executor_transmitted_bytes": len(transmitted_bytes),
        "executor_preamble_version": EXECUTOR_PREAMBLE_VERSION,
    }


def protected_script_limit_bytes() -> int:
    """Read a byte limit without allowing invalid configuration to disable it."""

    raw = os.getenv(PROTECTED_SCRIPT_LIMIT_ENV)
    if raw is None:
        return DEFAULT_PROTECTED_SCRIPT_LIMIT_BYTES
    try:
        configured = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_PROTECTED_SCRIPT_LIMIT_BYTES
    return configured if configured >= 0 else DEFAULT_PROTECTED_SCRIPT_LIMIT_BYTES


def _has_stream_preamble(entrypoint: ast.FunctionDef) -> bool:
    if len(entrypoint.body) < len(_STREAM_PREAMBLE_NODES):
        return False
    return all(
        ast.dump(actual, include_attributes=False) == ast.dump(expected, include_attributes=False)
        for actual, expected in zip(
            entrypoint.body[: len(_STREAM_PREAMBLE_NODES)],
            _STREAM_PREAMBLE_NODES,
            strict=True,
        )
    )
