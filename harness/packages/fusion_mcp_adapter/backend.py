"""Explicit Fusion backend selection with no automatic provider fallback."""

from __future__ import annotations

import os
import shlex
import warnings
from pathlib import Path
from typing import Any

from fusion_mcp_adapter.real_client import RealMcpClient
from fusion_mcp_adapter.stdio_client import StdioMcpClient


BACKENDS = {"autodesk_http", "faust_stdio"}


def selected_backend(configured: str | None = None) -> str:
    value = (
        (
            configured
            if configured is not None
            else os.getenv("FUSION_AGENT_BACKEND", "autodesk_http")
        )
        .strip()
        .lower()
    )
    if value not in BACKENDS:
        raise ValueError("FUSION_AGENT_BACKEND must be autodesk_http or faust_stdio")
    return value


def create_fusion_client(
    *,
    backend: str | None = None,
    endpoint: str | None = None,
    command: str | None = None,
    transport_mode: str | None = None,
    faust_command: str | None = None,
    faust_cwd: str | Path | None = None,
    remote_policy: str | None = None,
    remote_allowlist: str | None = None,
    bearer_token: str | None = None,
    manifest_store: Any = None,
    trace_logger: Any = None,
    connect_timeout_seconds: float = 5.0,
    read_timeout_seconds: float = 120.0,
    mutation_timeout_seconds: float = 240.0,
    sse_read_timeout_seconds: float = 300.0,
    auto_canary_timeout_seconds: float | None = None,
    post_dispatch_cooldown_seconds: float | None = None,
) -> RealMcpClient | StdioMcpClient:
    """Build exactly one configured provider; never try another on failure."""

    selected = selected_backend(backend)
    if selected == "autodesk_http":
        return RealMcpClient(
            endpoint=endpoint,
            command=command,
            transport_mode=transport_mode,
            connect_timeout_seconds=connect_timeout_seconds,
            read_timeout_seconds=read_timeout_seconds,
            mutation_timeout_seconds=mutation_timeout_seconds,
            sse_read_timeout_seconds=sse_read_timeout_seconds,
            remote_policy=remote_policy,
            remote_allowlist=remote_allowlist,
            bearer_token=bearer_token,
            auto_canary_timeout_seconds=auto_canary_timeout_seconds,
            post_dispatch_cooldown_seconds=post_dispatch_cooldown_seconds,
            manifest_store=manifest_store,
            trace_logger=trace_logger,
        )

    raw_command = faust_command
    legacy_command = command
    if backend is None and faust_command is None:
        raw_command = os.getenv("FUSION_FAUST_COMMAND")
    if backend is None and command is None:
        legacy_command = os.getenv("FUSION_MCP_COMMAND")
    if not raw_command and legacy_command:
        warnings.warn(
            "FUSION_MCP_COMMAND is deprecated for faust_stdio; use FUSION_FAUST_COMMAND",
            DeprecationWarning,
            stacklevel=2,
        )
        raw_command = legacy_command
    raw_command = raw_command or "fusion360-mcp-server"
    parts = shlex.split(raw_command, posix=os.name != "nt")
    if not parts:
        raise ValueError("FUSION_FAUST_COMMAND must contain an executable")
    cwd = faust_cwd
    if backend is None and faust_cwd is None:
        cwd = os.getenv("FUSION_FAUST_CWD")
    return StdioMcpClient(
        command=parts[0],
        args=parts[1:],
        cwd=Path(cwd) if cwd else None,
        connect_timeout_seconds=max(connect_timeout_seconds, 15.0),
        read_timeout_seconds=read_timeout_seconds,
        mutation_timeout_seconds=mutation_timeout_seconds,
        manifest_store=manifest_store,
        trace_logger=trace_logger,
    )
