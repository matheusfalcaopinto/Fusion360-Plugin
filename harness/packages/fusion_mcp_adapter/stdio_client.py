"""Persistent stdio MCP transport for optional Fusion Personal backends."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from fusion_mcp_adapter.errors import ErrorCode, FusionHarnessError
from fusion_mcp_adapter.manifest_store import ManifestStore
from fusion_mcp_adapter.semantics import (
    CallSemantics,
    ConnectionState,
    McpCallOptions,
    ReplayPolicy,
)
from fusion_mcp_adapter.tool_result import ToolDefinition, ToolManifest, ToolResult
from telemetry.trace import JsonlTraceLogger


class StdioMcpClient:
    """One lifecycle-owned stdio session with no post-dispatch replay.

    This replaces the historical process-per-call command transport when the
    explicit ``faust_stdio`` backend is selected.  The upstream server process
    is created once and serialized through one operation lock.
    """

    def __init__(
        self,
        command: str,
        args: list[str] | None = None,
        *,
        cwd: str | Path | None = None,
        env: dict[str, str] | None = None,
        connect_timeout_seconds: float = 15.0,
        read_timeout_seconds: float = 120.0,
        mutation_timeout_seconds: float = 240.0,
        manifest_store: ManifestStore | None = None,
        trace_logger: JsonlTraceLogger | None = None,
        transport_factory: Callable[..., Any] | None = None,
        session_factory: Callable[..., Any] | None = None,
    ) -> None:
        if not command.strip():
            raise ValueError("stdio MCP command must not be empty")
        self.command = command
        self.args = list(args or [])
        self.cwd = Path(cwd).resolve() if cwd else None
        self.env = dict(env) if env is not None else None
        self.connect_timeout_seconds = connect_timeout_seconds
        self.read_timeout_seconds = read_timeout_seconds
        self.mutation_timeout_seconds = mutation_timeout_seconds
        self.manifest_store = manifest_store
        self.trace_logger = trace_logger
        self._transport_factory = transport_factory or stdio_client
        self._session_factory = session_factory or ClientSession

        self.state = ConnectionState.DISCONNECTED
        self.connection_generation = 0
        self._connect_lock = asyncio.Lock()
        self._operation_lock = asyncio.Lock()
        self._stack: AsyncExitStack | None = None
        self._session: Any | None = None
        self._manifest: ToolManifest | None = None
        self._closing = False
        self._call_count = 0
        self._last_error: str | None = None
        self._mutation_dispatched = False

    async def __aenter__(self) -> "StdioMcpClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    @property
    def diagnostics(self) -> dict[str, Any]:
        return {
            "backend": "faust_stdio",
            "state": self.state.value,
            "transport_mode": "persistent_stdio",
            "requested_transport_mode": "persistent_stdio",
            "effective_transport_mode": "persistent_stdio",
            "connection_generation": self.connection_generation,
            "call_count": self._call_count,
            "retry_count": 0,
            "reconnect_count": max(self.connection_generation - 1, 0),
            "fingerprint": self._manifest.fingerprint if self._manifest else None,
            "manifest_drift": False,
            "last_error": self._last_error,
            "session_established": self._session is not None,
            "mutation_dispatched": self._mutation_dispatched,
            "command": self.command,
            "args": list(self.args),
        }

    def diagnostic_snapshot(self) -> dict[str, Any]:
        return self.diagnostics

    @property
    def current_manifest(self) -> ToolManifest | None:
        return self._manifest.model_copy(deep=True) if self._manifest else None

    async def ensure_ready(self) -> None:
        if self._closing or self.state == ConnectionState.CLOSED:
            raise FusionHarnessError("stdio MCP client is closed", ErrorCode.CLIENT_CLOSED)
        if self.state == ConnectionState.READY and self._session is not None:
            return
        async with self._connect_lock:
            if self.state == ConnectionState.READY and self._session is not None:
                return
            self.state = ConnectionState.CONNECTING
            stack = AsyncExitStack()
            try:
                parameters = StdioServerParameters(
                    command=self.command,
                    args=self.args,
                    env=self.env,
                    cwd=self.cwd,
                    encoding="utf-8",
                    encoding_error_handler="replace",
                )
                read_stream, write_stream = await asyncio.wait_for(
                    stack.enter_async_context(self._transport_factory(parameters)),
                    timeout=self.connect_timeout_seconds,
                )
                session = await stack.enter_async_context(
                    self._session_factory(
                        read_stream,
                        write_stream,
                        read_timeout_seconds=timedelta(seconds=self.read_timeout_seconds),
                    )
                )
                initialize_result = await asyncio.wait_for(
                    session.initialize(), timeout=self.connect_timeout_seconds
                )
                tools_result = await asyncio.wait_for(
                    session.list_tools(), timeout=self.connect_timeout_seconds
                )
                self._manifest = _manifest_from_results(initialize_result, tools_result)
                if self.manifest_store is not None:
                    self.manifest_store.save_if_changed(self._manifest)
                self._stack = stack
                self._session = session
                self.connection_generation += 1
                self.state = ConnectionState.READY
            except BaseException as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"
                self.state = ConnectionState.BROKEN
                await stack.aclose()
                raise

    async def list_tools(self) -> ToolManifest:
        await self.ensure_ready()
        assert self._session is not None
        async with self._operation_lock:
            result = await asyncio.wait_for(
                self._session.list_tools(), timeout=self.read_timeout_seconds
            )
            self._manifest = _manifest_from_results({}, result)
            if self.manifest_store is not None:
                self.manifest_store.save_if_changed(self._manifest)
            return self._manifest.model_copy(deep=True)

    async def call_tool(
        self,
        name: str,
        arguments: dict,
        *,
        options: McpCallOptions | None = None,
    ) -> ToolResult:
        await self.ensure_ready()
        assert self._session is not None
        call_options = options or McpCallOptions.for_mutation(
            timeout_seconds=self.mutation_timeout_seconds
        )
        timeout = call_options.timeout_seconds or (
            self.read_timeout_seconds
            if call_options.semantics == CallSemantics.READ_ONLY
            else self.mutation_timeout_seconds
        )
        async with self._operation_lock:
            dispatched = False
            try:
                dispatched = True
                if call_options.semantics == CallSemantics.MUTATING:
                    self._mutation_dispatched = True
                self._call_count += 1
                raw = await asyncio.wait_for(
                    self._session.call_tool(name, arguments), timeout=timeout
                )
                return _with_transport(
                    ToolResult.from_mcp(_model_dump(raw)),
                    call_options,
                    dispatched=True,
                    mutation_outcome="known",
                )
            except asyncio.CancelledError:
                if dispatched and call_options.semantics == CallSemantics.MUTATING:
                    return _with_transport(
                        _unknown_mutation("stdio mutation cancelled after dispatch"),
                        call_options,
                        dispatched=True,
                        mutation_outcome="unknown",
                    )
                raise
            except TimeoutError:
                if dispatched and call_options.semantics == CallSemantics.MUTATING:
                    return _with_transport(
                        _unknown_mutation("stdio mutation timed out after dispatch"),
                        call_options,
                        dispatched=True,
                        mutation_outcome="unknown",
                    )
                return _with_transport(
                    ToolResult.failure(ErrorCode.TIMEOUT, "stdio MCP read timed out"),
                    call_options,
                    dispatched=dispatched,
                    mutation_outcome="known",
                )
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"
                if dispatched and call_options.semantics == CallSemantics.MUTATING:
                    return _with_transport(
                        _unknown_mutation(
                            f"stdio connection lost after mutation dispatch: {exc}"
                        ),
                        call_options,
                        dispatched=True,
                        mutation_outcome="unknown",
                    )
                return _with_transport(
                    ToolResult.failure(ErrorCode.CONNECTION_LOST, self._last_error),
                    call_options,
                    dispatched=dispatched,
                    mutation_outcome="known",
                )

    async def aclose(self, timeout_seconds: float = 2.0) -> None:
        if self.state == ConnectionState.CLOSED:
            return
        self._closing = True
        self.state = ConnectionState.CLOSING
        stack, self._stack = self._stack, None
        self._session = None
        if stack is not None:
            try:
                await asyncio.wait_for(stack.aclose(), timeout=timeout_seconds)
            except BaseException as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"
        self.state = ConnectionState.CLOSED

    async def close(self, timeout_seconds: float = 2.0) -> None:
        await self.aclose(timeout_seconds=timeout_seconds)


def _unknown_mutation(message: str) -> ToolResult:
    return ToolResult.failure(
        ErrorCode.MUTATION_OUTCOME_UNKNOWN,
        message,
        data={
            "dispatched": True,
            "may_have_applied": True,
            "post_dispatch_replay_suppressed": True,
            "mutation_outcome": "unknown",
        },
    )


def _with_transport(
    result: ToolResult,
    options: McpCallOptions,
    *,
    dispatched: bool,
    mutation_outcome: str,
) -> ToolResult:
    result.meta = {
        **result.meta,
        "fusion_agent_transport": {
            "operation_id": options.operation_id,
            "semantics": options.semantics.value,
            "dispatched": dispatched,
            "may_have_applied": bool(
                dispatched
                and options.semantics == CallSemantics.MUTATING
                and mutation_outcome == "unknown"
            ),
            "post_dispatch_replay_suppressed": bool(
                dispatched and options.replay_policy == ReplayPolicy.BEFORE_DISPATCH_ONLY
            ),
            "mutation_outcome": mutation_outcome,
            "error_code": result.error_code,
            "attempts": 1,
        },
    }
    return result


def _model_dump(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        payload = value.model_dump(by_alias=True, mode="json", exclude_none=True)
        return payload if isinstance(payload, dict) else {}
    return {}


def _manifest_from_results(initialize_result: Any, tools_result: Any) -> ToolManifest:
    initialize = _model_dump(initialize_result)
    tools_payload = _model_dump(tools_result)
    server = initialize.get("serverInfo", initialize.get("server_info"))
    server = server if isinstance(server, dict) else {}
    tools = tools_payload.get("tools") or []
    return ToolManifest(
        source="fusion_faust_stdio",
        server=server,
        server_name=server.get("name") if isinstance(server.get("name"), str) else None,
        server_version=server.get("version") if isinstance(server.get("version"), str) else None,
        protocol_version=initialize.get(
            "protocolVersion", initialize.get("protocol_version")
        ),
        tools=[
            ToolDefinition.model_validate(tool)
            for tool in tools
            if isinstance(tool, dict) and tool.get("name")
        ],
    )
