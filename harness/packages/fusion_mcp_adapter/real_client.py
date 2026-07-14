"""Persistent, mutation-safe client for Autodesk Fusion's native MCP server."""

from __future__ import annotations

import asyncio
import ast
import json
import os
import shlex
import subprocess
import time
import urllib.request
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import timedelta
from json import JSONDecodeError
from typing import Any, Callable

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.exceptions import McpError

from fusion_mcp_adapter.errors import ErrorCode, FusionHarnessError, RealMcpNotConfigured
from fusion_mcp_adapter.manifest_store import ManifestStore
from fusion_mcp_adapter.post_only_transport import post_only_streamablehttp_client
from fusion_mcp_adapter.semantics import (
    CallSemantics,
    ConnectionState,
    McpCallOptions,
    ReplayPolicy,
)
from fusion_mcp_adapter.tool_result import ToolDefinition, ToolManifest, ToolResult
from telemetry.trace import JsonlTraceLogger


_READ_ONLY_NATIVE_TOOLS = {"fusion_mcp_read", "fusion_mcp_electronics_read"}
_EXECUTE_TOOL = "fusion_mcp_execute"
_PERSISTENT_MODES = {"persistent", "persistent_post_only"}
_LEGACY_READINESS_TTL_SECONDS = 60.0
_COOLDOWN_SECONDS = float(os.getenv("FUSION_MCP_POST_DISPATCH_COOLDOWN_SECONDS", "5"))
_AUTO_CANARY_TIMEOUT_SECONDS = float(os.getenv("FUSION_MCP_AUTO_CANARY_TIMEOUT_SECONDS", "2"))


class ConnectionDiagnostics(dict[str, Any]):
    """Mapping snapshot that is also callable for method-style consumers."""

    def __call__(self) -> "ConnectionDiagnostics":
        return self


@dataclass(slots=True)
class _WorkerRequest:
    """One serialized operation owned by the persistent transport worker."""

    action: str
    future: asyncio.Future[Any]
    queued_at: float
    name: str | None = None
    arguments: dict[str, Any] | None = None
    options: McpCallOptions | None = None
    refresh: bool = False
    dispatched: bool = False
    cancelled: bool = False


@dataclass(frozen=True, slots=True)
class _WorkerCallResult:
    result: ToolResult
    attempts: int
    queue_ms: int
    duration_ms: int
    reconnected: bool
    connection_ms: int


class RealMcpClient:
    """Lazy Fusion MCP client with a reusable Streamable HTTP session.

    Endpoint mode defaults to the proven ``legacy`` one-shot transport.
    Persistent modes are driven by one actor task so the task that enters the
    AnyIO/MCP contexts is also the only task that calls or exits them.
    """

    def __init__(
        self,
        endpoint: str | None = None,
        command: str | None = None,
        timeout_seconds: float = 120.0,
        *,
        transport_mode: str | None = None,
        connect_timeout_seconds: float | None = None,
        read_timeout_seconds: float | None = None,
        mutation_timeout_seconds: float = 240.0,
        sse_read_timeout_seconds: float = 300.0,
        manifest_store: ManifestStore | None = None,
        trace_logger: JsonlTraceLogger | None = None,
        transport_factory: Callable[..., Any] | None = None,
        session_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.endpoint = endpoint or os.getenv("FUSION_MCP_ENDPOINT")
        self.command = command or os.getenv("FUSION_MCP_COMMAND")
        configured_mode = transport_mode or os.getenv("FUSION_MCP_TRANSPORT_MODE", "legacy")
        self.transport_mode = configured_mode.strip().lower()
        valid_modes = {"legacy", "persistent_post_only", "persistent", "auto"}
        if self.transport_mode not in valid_modes:
            raise ValueError(
                "FUSION_MCP_TRANSPORT_MODE must be legacy, persistent_post_only, persistent, or auto"
            )
        self._effective_transport_mode = (
            "persistent_post_only" if self.transport_mode == "auto" else self.transport_mode
        )

        self.timeout_seconds = timeout_seconds  # v0.1 compatibility attribute
        self.connect_timeout_seconds = (
            connect_timeout_seconds
            if connect_timeout_seconds is not None
            else min(5.0, timeout_seconds)
        )
        self.read_timeout_seconds = read_timeout_seconds or timeout_seconds
        self.mutation_timeout_seconds = mutation_timeout_seconds
        self.sse_read_timeout_seconds = sse_read_timeout_seconds
        self.manifest_store = manifest_store
        self.trace_logger = trace_logger
        self._explicit_transport_factory = transport_factory
        self._session_factory = session_factory or ClientSession

        self.state = ConnectionState.DISCONNECTED
        self.connection_generation = 0
        self._connection_lock = asyncio.Lock()
        self._operation_lock = asyncio.Lock()
        self._worker_start_lock = asyncio.Lock()
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | Any | None = None
        self._get_session_id: Callable[[], str | None] | None = None
        self._manifest: ToolManifest | None = None
        self._accepted_fingerprint: str | None = None
        self._manifest_drift = False
        self._closing = False
        self._active_tasks: set[asyncio.Task[Any]] = set()
        self._worker_queue: asyncio.Queue[_WorkerRequest] | None = None
        self._worker_task: asyncio.Task[None] | None = None
        self._worker_owner_task: asyncio.Task[Any] | None = None
        self._active_worker_request: _WorkerRequest | None = None
        self._legacy_manifest_at = 0.0
        self._cooldown_until = 0.0
        self._fallback_reason: str | None = None
        self._auto_canary_completed = False
        self._auto_canary_count = 0
        self._auto_canary_ms = 0
        self._mutation_dispatched = False

        self._initialize_count = 0
        self._tools_list_count = 0
        self._call_count = 0
        self._reconnect_count = 0
        self._retry_count = 0
        self._last_error: str | None = None
        self._last_persistence_error: str | None = None
        self._last_connect_ms = 0

    async def __aenter__(self) -> "RealMcpClient":
        # Deliberately lazy: entering the runtime must work with Fusion closed.
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()

    @property
    def diagnostics(self) -> ConnectionDiagnostics:
        """Return a secret-free lifecycle and transport snapshot."""

        return ConnectionDiagnostics({
            "state": self.state.value,
            "transport_mode": "command" if self.command else self.transport_mode,
            "requested_transport_mode": "command" if self.command else self.transport_mode,
            "effective_transport_mode": "command" if self.command else self._effective_transport_mode,
            "connection_generation": self.connection_generation,
            "initialize_count": self._initialize_count,
            "tools_list_count": self._tools_list_count,
            "call_count": self._call_count,
            "reconnect_count": self._reconnect_count,
            "retry_count": self._retry_count,
            "fingerprint": self._manifest.fingerprint if self._manifest else None,
            "manifest_drift": self._manifest_drift,
            "last_error": self._last_error,
            "manifest_persistence_error": self._last_persistence_error,
            "session_established": bool(self._get_session_id and self._get_session_id()),
            "worker_running": bool(self._worker_task and not self._worker_task.done()),
            "worker_owner_task": self._worker_owner_task.get_name() if self._worker_owner_task else None,
            "queue_depth": self._worker_queue.qsize() if self._worker_queue else 0,
            "cooldown_remaining_seconds": round(self._cooldown_remaining(), 3),
            "fallback_reason": self._fallback_reason,
            "auto_canary_completed": self._auto_canary_completed,
            "auto_canary_count": self._auto_canary_count,
            "auto_canary_ms": self._auto_canary_ms,
            "mutation_dispatched": self._mutation_dispatched,
        })

    def diagnostic_snapshot(self) -> dict[str, Any]:
        """Method-form compatibility for health handlers."""

        return self.diagnostics

    @property
    def current_manifest(self) -> ToolManifest | None:
        """Return the manifest captured by the live connection without accepting drift.

        Health checks need to inspect the surface negotiated by ``initialize``
        without turning a detected reconnect drift into an implicit approval.
        Explicit ``list_tools`` remains the revalidation boundary.
        """

        if self._manifest is None:
            return None
        return self._manifest.model_copy(deep=True)

    async def ensure_ready(self) -> None:
        """Lazily establish transport readiness without changing task ownership."""

        self._ensure_callable()
        task = self._register_current_task()
        try:
            if self.command or self._effective_transport_mode == "legacy":
                await self._ensure_legacy_ready()
                return
            await self._submit_worker("ensure")
        finally:
            self._active_tasks.discard(task)

    async def start(self) -> None:
        """Explicitly connect when a runtime wants eager readiness."""

        await self.ensure_ready()

    async def ping(self) -> None:
        """Prove that the current persistent session is responsive.

        The one-shot compatibility transports do not have a reusable session,
        so their liveness proof is a normal ``tools/list`` transaction.
        """

        if self.command or self._effective_transport_mode == "legacy":
            await self.list_tools(refresh=True)
            return

        task = self._register_current_task()
        try:
            await self._submit_worker("ping")
        finally:
            self._active_tasks.discard(task)

    async def list_tools(self, *, refresh: bool = False) -> ToolManifest:
        """Return the native manifest, reusing discovery from initialization."""

        task = self._register_current_task()
        try:
            if self.command or self._effective_transport_mode == "legacy":
                return await self._legacy_manifest(refresh=refresh, accept=True)
            return await self._submit_worker("list_tools", refresh=refresh)
        finally:
            self._active_tasks.discard(task)

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        options: McpCallOptions | None = None,
    ) -> ToolResult:
        """Call one native tool under replay-safe semantics."""

        try:
            self._ensure_callable()
        except FusionHarnessError as exc:
            return ToolResult.failure(str(exc.code), str(exc))

        call_options = self._resolve_options(name, options)
        queued_at = time.perf_counter()
        task = self._register_current_task()
        try:
            if not self.command and self._effective_transport_mode in _PERSISTENT_MODES:
                outcome = await self._submit_worker_call(name, arguments, call_options, queued_at)
                self._trace_tool_call(
                    name=name,
                    arguments=arguments,
                    options=call_options,
                    result=outcome.result,
                    attempts=outcome.attempts,
                    queue_ms=outcome.queue_ms,
                    duration_ms=outcome.duration_ms,
                    reconnected=outcome.reconnected,
                    connection_ms=outcome.connection_ms,
                )
                return outcome.result

            async with self._operation_lock:
                queue_ms = int((time.perf_counter() - queued_at) * 1000)
                if self._closing:
                    return ToolResult.failure(ErrorCode.CLIENT_CLOSED, "Fusion MCP client is closing")
                cooldown = self._cooldown_result()
                if cooldown is not None:
                    return cooldown
                started = time.perf_counter()
                generation_before = self.connection_generation
                reconnects_before = self._reconnect_count
                if self.command:
                    result, attempts = await self._command_call(name, arguments, call_options)
                elif self._effective_transport_mode == "legacy":
                    result, attempts = await self._legacy_call(name, arguments, call_options)
                else:
                    raise AssertionError("persistent calls must run on the transport worker")
                duration_ms = int((time.perf_counter() - started) * 1000)
                self._trace_tool_call(
                    name=name,
                    arguments=arguments,
                    options=call_options,
                    result=result,
                    attempts=attempts,
                    queue_ms=queue_ms,
                    duration_ms=duration_ms,
                    reconnected=self._reconnect_count > reconnects_before,
                    connection_ms=(
                        self._last_connect_ms
                        if self.connection_generation != generation_before
                        else 0
                    ),
                )
                return result
        except asyncio.CancelledError:
            # Cancellation before dispatch is known-safe.  Post-dispatch
            # cancellation is converted inside the transport-specific path.
            result = ToolResult.failure(
                ErrorCode.CALL_CANCELLED,
                "Fusion MCP call cancelled before dispatch",
            )
            elapsed_ms = int((time.perf_counter() - queued_at) * 1000)
            self._trace_tool_call(
                name=name,
                arguments=arguments,
                options=call_options,
                result=result,
                attempts=0,
                queue_ms=elapsed_ms,
                duration_ms=elapsed_ms,
                reconnected=False,
                connection_ms=0,
            )
            return result
        finally:
            self._active_tasks.discard(task)

    async def aclose(self, timeout_seconds: float = 2.0) -> None:
        """Cancel pending work and close the persistent transport promptly."""

        if self.state == ConnectionState.CLOSED:
            return
        self._closing = True
        self.state = ConnectionState.CLOSING
        current = asyncio.current_task()
        pending = [task for task in self._active_tasks if task is not current and not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            try:
                await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=timeout_seconds)
            except TimeoutError:
                pass
        worker = self._worker_task
        if worker is not None and not worker.done():
            worker.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(worker), timeout=timeout_seconds)
            except (asyncio.CancelledError, TimeoutError):
                pass
        self._worker_task = None
        self._worker_queue = None
        self.state = ConnectionState.CLOSED
        self._trace_event("client_closed", connection_generation=self.connection_generation)

    async def close(self, timeout_seconds: float = 2.0) -> None:
        """Alias used by runtime lifespan implementations."""

        await self.aclose(timeout_seconds)

    async def _ensure_worker(self) -> None:
        """Start the persistent transport actor once per event loop."""

        self._ensure_callable()
        if self._worker_task is not None and not self._worker_task.done():
            return
        async with self._worker_start_lock:
            if self._worker_task is not None and not self._worker_task.done():
                return
            self._worker_queue = asyncio.Queue()
            self._worker_task = asyncio.create_task(
                self._worker_loop(self._worker_queue),
                name="fusion-mcp-transport-owner",
            )

    async def _submit_worker(
        self,
        action: str,
        *,
        refresh: bool = False,
    ) -> Any:
        await self._ensure_worker()
        queue = self._worker_queue
        if queue is None:
            raise FusionHarnessError("MCP transport worker unavailable", ErrorCode.CONNECTION_UNAVAILABLE)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        request = _WorkerRequest(action, future, time.perf_counter(), refresh=refresh)
        queue.put_nowait(request)
        return await asyncio.shield(future)

    async def _submit_worker_call(
        self,
        name: str,
        arguments: dict[str, Any],
        options: McpCallOptions,
        queued_at: float,
    ) -> _WorkerCallResult:
        cooldown = self._cooldown_result()
        if cooldown is not None:
            elapsed_ms = int((time.perf_counter() - queued_at) * 1000)
            return _WorkerCallResult(cooldown, 0, elapsed_ms, 0, False, 0)
        await self._ensure_worker()
        queue = self._worker_queue
        if queue is None:
            result = ToolResult.failure(ErrorCode.CONNECTION_UNAVAILABLE, "MCP transport worker unavailable")
            return _WorkerCallResult(result, 0, 0, 0, False, 0)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        request = _WorkerRequest(
            "call",
            future,
            queued_at,
            name=name,
            arguments=arguments,
            options=options,
        )
        queue.put_nowait(request)
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            request.cancelled = True
            future.cancel()
            elapsed_ms = int((time.perf_counter() - queued_at) * 1000)
            if request.dispatched:
                self._set_cooldown("call cancelled after dispatch")
                if self.state not in {ConnectionState.CLOSING, ConnectionState.CLOSED}:
                    self.state = ConnectionState.BROKEN
                asyncio.create_task(self._abort_worker(), name="fusion-mcp-cancel-cleanup")
                if options.semantics == CallSemantics.MUTATING:
                    result = self._unknown_mutation("mutation cancelled after dispatch")
                else:
                    result = self._nonreplayable_read_failure(
                        ErrorCode.CALL_CANCELLED,
                        "read cancelled after dispatch; native work may still be running",
                    )
                return _WorkerCallResult(result, 1, 0, elapsed_ms, False, 0)
            result = ToolResult.failure(ErrorCode.CALL_CANCELLED, "Fusion MCP call cancelled before dispatch")
            return _WorkerCallResult(result, 0, elapsed_ms, elapsed_ms, False, 0)

    async def _worker_loop(self, queue: asyncio.Queue[_WorkerRequest]) -> None:
        """Own every persistent context, session operation, and close."""

        self._worker_owner_task = asyncio.current_task()
        self._trace_event("worker_started", owner_task=self._worker_owner_task.get_name())
        try:
            while True:
                request = await queue.get()
                self._active_worker_request = request
                if request.cancelled:
                    self._active_worker_request = None
                    continue
                try:
                    if request.action == "ensure":
                        await self._owner_ensure_ready()
                        value: Any = None
                    elif request.action == "ping":
                        await self._owner_ensure_ready()
                        if self._effective_transport_mode == "legacy":
                            await self._legacy_manifest(refresh=True, accept=True)
                        else:
                            session = self._require_owner_session()
                            await _await_with_timeout(
                                session.send_ping(),
                                self.connect_timeout_seconds,
                            )
                        value = None
                    elif request.action == "list_tools":
                        await self._owner_ensure_ready()
                        if self._effective_transport_mode == "legacy":
                            value = await self._legacy_manifest(refresh=request.refresh, accept=True)
                        elif request.refresh:
                            value = await self._refresh_manifest_owned()
                            self._accept_manifest(value)
                        else:
                            value = self._manifest
                            if value is None:
                                raise FusionHarnessError(
                                    "MCP manifest unavailable",
                                    ErrorCode.CONNECTION_UNAVAILABLE,
                                )
                            self._accept_manifest(value)
                    elif request.action == "call":
                        value = await self._worker_call(request)
                    else:  # pragma: no cover - defensive programming
                        raise RuntimeError(f"unknown worker action: {request.action}")
                    if not request.future.done():
                        request.future.set_result(value)
                except asyncio.CancelledError:
                    if not request.future.done():
                        request.future.set_exception(asyncio.CancelledError())
                    raise
                except BaseException as exc:
                    if not request.future.done():
                        request.future.set_exception(exc)
                finally:
                    self._active_worker_request = None
        finally:
            await self._dispose_stack_owned(timeout_seconds=2.0)
            while not queue.empty():
                pending = queue.get_nowait()
                if not pending.future.done():
                    pending.future.set_exception(
                        FusionHarnessError("Fusion MCP client is closed", ErrorCode.CLIENT_CLOSED)
                    )
            self._trace_event("worker_stopped")
            self._worker_owner_task = None

    async def _worker_call(self, request: _WorkerRequest) -> _WorkerCallResult:
        options = request.options
        if request.name is None or request.arguments is None or options is None:
            raise RuntimeError("invalid worker call request")
        queue_ms = int((time.perf_counter() - request.queued_at) * 1000)
        started = time.perf_counter()
        generation_before = self.connection_generation
        reconnects_before = self._reconnect_count
        cooldown = self._cooldown_result()
        if cooldown is not None:
            return _WorkerCallResult(cooldown, 0, queue_ms, 0, False, 0)
        if self._effective_transport_mode == "legacy":
            result, attempts = await self._legacy_call(request.name, request.arguments, options)
        else:
            result, attempts = await self._persistent_call_owned(
                request.name,
                request.arguments,
                options,
                request=request,
            )
        duration_ms = int((time.perf_counter() - started) * 1000)
        return _WorkerCallResult(
            result,
            attempts,
            queue_ms,
            duration_ms,
            self._reconnect_count > reconnects_before,
            self._last_connect_ms if self.connection_generation != generation_before else 0,
        )

    async def _owner_ensure_ready(self) -> None:
        self._assert_worker_owner()
        if self._effective_transport_mode == "legacy":
            await self._ensure_legacy_ready()
            return
        if self.state != ConnectionState.READY or self._session is None:
            await self._connect_owned()
        if self.transport_mode == "auto" and not self._auto_canary_completed:
            await self._run_auto_canary_owned()

    async def _run_auto_canary_owned(self) -> None:
        """Prove the post-only path before allowing user work onto it."""

        self._assert_worker_owner()
        if self._mutation_dispatched:
            raise FusionHarnessError(
                "automatic transport fallback is disabled after mutation dispatch",
                ErrorCode.CONNECTION_LOST,
            )
        session = self._require_owner_session()
        started = time.perf_counter()
        self._auto_canary_count += 1
        try:
            self._call_count += 1
            response = await _await_with_timeout(
                session.call_tool(
                    "fusion_mcp_read",
                    {"queryType": "document", "operation": "open"},
                    read_timeout_seconds=timedelta(seconds=_AUTO_CANARY_TIMEOUT_SECONDS),
                ),
                _AUTO_CANARY_TIMEOUT_SECONDS,
            )
            payload = response.model_dump(by_alias=True, mode="json", exclude_none=True)
            result = ToolResult.from_mcp(payload)
            if not result.ok:
                raise FusionHarnessError(
                    result.error_message or "auto canary returned a functional error",
                    ErrorCode.MCP_PROTOCOL_ERROR,
                )
            self._auto_canary_completed = True
            self._auto_canary_ms = int((time.perf_counter() - started) * 1000)
            self._trace_event("auto_canary_succeeded", duration_ms=self._auto_canary_ms)
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            self._auto_canary_ms = int((time.perf_counter() - started) * 1000)
            self._fallback_reason = f"{type(exc).__name__}: {exc}"
            if isinstance(exc, TimeoutError):
                self._set_cooldown("auto canary timed out after dispatch")
            await self._mark_broken_owned(f"auto canary failed: {self._fallback_reason}")
            self._effective_transport_mode = "legacy"
            self._auto_canary_completed = True
            self._trace_event(
                "auto_fallback",
                fallback_reason=self._fallback_reason,
                effective_transport_mode="legacy",
                duration_ms=self._auto_canary_ms,
            )
            await self._ensure_legacy_ready()

    async def _abort_worker(self) -> None:
        worker = self._worker_task
        if worker is None or worker.done() or worker is asyncio.current_task():
            return
        worker.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(worker), timeout=2.0)
        except (asyncio.CancelledError, TimeoutError):
            pass

    async def _persistent_call_owned(
        self,
        name: str,
        arguments: dict[str, Any],
        options: McpCallOptions,
        *,
        request: _WorkerRequest,
    ) -> tuple[ToolResult, int]:
        if options.semantics == CallSemantics.MUTATING:
            prepared = await self._prepare_mutation()
            if prepared is not None:
                return prepared, 0
            if self._effective_transport_mode == "legacy":
                cooldown = self._cooldown_result()
                if cooldown is not None:
                    return cooldown, 0
                return await self._legacy_call(name, arguments, options)

        if self._manifest_drift:
            return (
                ToolResult.failure(
                    ErrorCode.MANIFEST_DRIFT,
                    "native Fusion tool manifest changed; rediscover and revalidate before calling",
                ),
                0,
            )

        attempt = 0
        predispatch_reconnects = 0
        postdispatch_retries = 0
        while True:
            attempt += 1
            dispatched = False
            try:
                if self.state != ConnectionState.READY or self._session is None:
                    await self._owner_ensure_ready()
                if self._effective_transport_mode == "legacy":
                    cooldown = self._cooldown_result()
                    if cooldown is not None:
                        return cooldown, attempt - 1
                    return await self._legacy_call(name, arguments, options)
                if self._manifest_drift:
                    return ToolResult.failure(ErrorCode.MANIFEST_DRIFT, "native Fusion tool manifest changed"), attempt - 1
                session = self._session
                if session is None:
                    raise ConnectionError("MCP session unavailable")
                dispatched = True
                request.dispatched = True
                if options.semantics == CallSemantics.MUTATING:
                    self._mutation_dispatched = True
                self._call_count += 1
                timeout_seconds = options.timeout_seconds or self.read_timeout_seconds
                response = await _await_with_timeout(
                    session.call_tool(
                        name,
                        arguments,
                        read_timeout_seconds=timedelta(seconds=timeout_seconds),
                    ),
                    timeout_seconds,
                )
                payload = response.model_dump(by_alias=True, mode="json", exclude_none=True)
                # A functional MCP error is a completed call, not a transport
                # retry candidate.
                return ToolResult.from_mcp(payload), attempt
            except McpError as exc:
                if not _is_retryable_mcp_error(exc):
                    return ToolResult.failure(ErrorCode.MCP_PROTOCOL_ERROR, str(exc)), attempt
                await self._mark_broken_owned(f"MCP transport error: {exc}")
                if dispatched and options.semantics == CallSemantics.MUTATING:
                    return self._unknown_mutation(f"mutation transport outcome unknown: {exc}"), attempt
                if dispatched and not self._can_retry_after_dispatch(options):
                    self._set_cooldown("non-replayable read lost transport after dispatch")
                    return self._nonreplayable_read_failure(
                        ErrorCode.CONNECTION_LOST,
                        f"read transport outcome unknown after dispatch: {exc}",
                    ), attempt
                can_retry = (
                    (not dispatched and predispatch_reconnects < 2)
                    or (
                        dispatched
                        and self._can_retry_after_dispatch(options)
                        and postdispatch_retries < 1
                    )
                )
                if can_retry:
                    if dispatched:
                        postdispatch_retries += 1
                    else:
                        predispatch_reconnects += 1
                    self._retry_count += 1
                    self._trace_event("read_retry", attempt=attempt, error=f"McpError:{_mcp_error_code(exc)}")
                    continue
                return ToolResult.failure(_mcp_failure_code(exc), str(exc)), attempt
            except asyncio.CancelledError:
                await self._mark_broken_owned("call cancelled after dispatch" if dispatched else "call cancelled")
                if dispatched and options.semantics == CallSemantics.MUTATING:
                    return self._unknown_mutation("mutation cancelled after dispatch"), attempt
                if dispatched:
                    self._set_cooldown("read cancelled after dispatch")
                    return self._nonreplayable_read_failure(
                        ErrorCode.CALL_CANCELLED,
                        "read cancelled after dispatch; native work may still be running",
                    ), attempt
                raise
            except TimeoutError as exc:
                await self._mark_broken_owned(f"timeout: {exc}")
                if dispatched and options.semantics == CallSemantics.MUTATING:
                    self._set_cooldown("mutation timed out after dispatch")
                    return self._unknown_mutation("mutation timed out after dispatch"), attempt
                if dispatched and not self._can_retry_after_dispatch(options):
                    self._set_cooldown("non-replayable read timed out after dispatch")
                    return self._nonreplayable_read_failure(
                        ErrorCode.READ_TIMEOUT_MAY_STILL_BE_RUNNING,
                        "Fusion MCP read timed out; native work may still be running",
                    ), attempt
                can_retry = (
                    (not dispatched and predispatch_reconnects < 2)
                    or (
                        dispatched
                        and self._can_retry_after_dispatch(options)
                        and postdispatch_retries < 1
                    )
                )
                if can_retry:
                    if dispatched:
                        postdispatch_retries += 1
                    else:
                        predispatch_reconnects += 1
                    self._retry_count += 1
                    self._trace_event("read_retry", attempt=attempt, error="timeout")
                    continue
                return ToolResult.failure(ErrorCode.TIMEOUT, "Fusion MCP read timed out"), attempt
            except Exception as exc:
                await self._mark_broken_owned(f"{type(exc).__name__}: {exc}")
                if dispatched and options.semantics == CallSemantics.MUTATING:
                    return self._unknown_mutation(f"connection lost after mutation dispatch: {exc}"), attempt
                if dispatched and not self._can_retry_after_dispatch(options):
                    self._set_cooldown("non-replayable read lost connection after dispatch")
                    return self._nonreplayable_read_failure(
                        ErrorCode.CONNECTION_LOST,
                        f"connection lost after read dispatch: {exc}",
                    ), attempt
                can_retry = (
                    (not dispatched and predispatch_reconnects < 2)
                    or (
                        dispatched
                        and self._can_retry_after_dispatch(options)
                        and postdispatch_retries < 1
                    )
                )
                if can_retry:
                    if dispatched:
                        postdispatch_retries += 1
                    else:
                        predispatch_reconnects += 1
                    self._retry_count += 1
                    self._trace_event("read_retry", attempt=attempt, error=type(exc).__name__)
                    continue
                return self._connection_failure(exc), attempt
    async def _prepare_mutation(self) -> ToolResult | None:
        """Reconnect and ping before dispatch; this path is safe to retry."""

        for attempt in range(1, 4):
            try:
                await self._owner_ensure_ready()
                if self._effective_transport_mode == "legacy":
                    return None
                if self._manifest_drift:
                    return ToolResult.failure(
                        ErrorCode.MANIFEST_DRIFT,
                        "native Fusion tool manifest changed; rediscover before mutation",
                    )
                session = self._session
                if session is None:
                    raise ConnectionError("MCP session unavailable")
                await _await_with_timeout(session.send_ping(), self.connect_timeout_seconds)
                return None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._mark_broken_owned(f"mutation preflight failed: {type(exc).__name__}: {exc}")
                if attempt >= 3:
                    return self._connection_failure(exc)
                self._trace_event("predispatch_reconnect", attempt=attempt)
        return ToolResult.failure(ErrorCode.CONNECTION_UNAVAILABLE, "mutation preflight failed")

    async def _connect_owned(self) -> None:
        self._assert_worker_owner()
        if not self.endpoint:
            raise RealMcpNotConfigured()
        reconnect = self.connection_generation > 0
        self.state = ConnectionState.CONNECTING
        started = time.perf_counter()
        stack = AsyncExitStack()
        try:
            transport_factory = self._transport_factory_for_effective_mode()
            transport = transport_factory(
                self.endpoint,
                timeout=self.connect_timeout_seconds,
                sse_read_timeout=self.sse_read_timeout_seconds,
            )
            read_stream, write_stream, get_session_id = await _await_with_timeout(
                stack.enter_async_context(transport),
                self.connect_timeout_seconds,
            )
            session = self._session_factory(
                read_stream,
                write_stream,
                read_timeout_seconds=timedelta(seconds=self.read_timeout_seconds),
            )
            session = await _await_with_timeout(
                stack.enter_async_context(session),
                self.connect_timeout_seconds,
            )
            initialize_result = await _await_with_timeout(
                session.initialize(),
                self.connect_timeout_seconds,
            )
            self._initialize_count += 1
            tools_result = await _await_with_timeout(
                session.list_tools(),
                self.read_timeout_seconds,
            )
            self._tools_list_count += 1
            manifest = _manifest_from_results(initialize_result, tools_result)

            if self._closing:
                raise FusionHarnessError(
                    "Fusion MCP client closed during connection",
                    ErrorCode.CLIENT_CLOSED,
                )

            previous = self._accepted_fingerprint
            self._manifest_drift = previous is not None and previous != manifest.fingerprint
            if previous is None:
                self._accepted_fingerprint = manifest.fingerprint
            self._stack = stack
            self._session = session
            self._get_session_id = get_session_id
            self._manifest = manifest
            self.connection_generation += 1
            if reconnect:
                self._reconnect_count += 1
            self.state = ConnectionState.READY
            self._last_error = None
            self._last_connect_ms = int((time.perf_counter() - started) * 1000)
            self._persist_manifest(manifest)
            self._trace_event(
                "connection_ready",
                connection_generation=self.connection_generation,
                reconnect=reconnect,
                fingerprint=manifest.fingerprint,
                manifest_drift=self._manifest_drift,
                connection_ms=self._last_connect_ms,
            )
        except BaseException as exc:
            try:
                await _await_with_timeout(stack.aclose(), 2.0)
            except BaseException:
                pass
            self.state = ConnectionState.CLOSING if self._closing else ConnectionState.BROKEN
            self._last_error = f"{type(exc).__name__}: {exc}"
            self._trace_event("connection_failed", error=self._last_error)
            raise

    async def _refresh_manifest_owned(self) -> ToolManifest:
        self._assert_worker_owner()
        session = self._session
        if session is None:
            raise ConnectionError("MCP session unavailable")
        result = await _await_with_timeout(session.list_tools(), self.read_timeout_seconds)
        self._tools_list_count += 1
        initialize_stub = {"protocolVersion": self._manifest.protocol_version if self._manifest else None}
        manifest = _manifest_from_results(initialize_stub, result)
        if self._accepted_fingerprint and manifest.fingerprint != self._accepted_fingerprint:
            self._manifest_drift = True
        self._manifest = manifest
        self._persist_manifest(manifest)
        return manifest

    def _accept_manifest(self, manifest: ToolManifest) -> None:
        self._manifest = manifest
        self._accepted_fingerprint = manifest.fingerprint
        self._manifest_drift = False

    async def _mark_broken_owned(self, message: str) -> None:
        self._assert_worker_owner()
        self._last_error = message
        if self.state not in {ConnectionState.CLOSING, ConnectionState.CLOSED}:
            self.state = ConnectionState.BROKEN
        await self._dispose_stack_owned(timeout_seconds=2.0)
        self._trace_event("connection_broken", error=message, connection_generation=self.connection_generation)

    async def _dispose_stack_owned(self, *, timeout_seconds: float) -> None:
        self._assert_worker_owner()
        stack, self._stack = self._stack, None
        self._session = None
        self._get_session_id = None
        if stack is None:
            return
        try:
            await _await_with_timeout(stack.aclose(), timeout_seconds)
        except BaseException as exc:
            self._last_error = f"transport close: {type(exc).__name__}: {exc}"

    def _assert_worker_owner(self) -> None:
        current = asyncio.current_task()
        if current is None or current is not self._worker_owner_task:
            raise RuntimeError("persistent MCP session may only be used by its owner worker task")

    def _require_owner_session(self) -> ClientSession | Any:
        self._assert_worker_owner()
        if self._session is None:
            raise FusionHarnessError("MCP session unavailable", ErrorCode.CONNECTION_UNAVAILABLE)
        return self._session

    def _transport_factory_for_effective_mode(self) -> Callable[..., Any]:
        if self._explicit_transport_factory is not None:
            return self._explicit_transport_factory
        if self._effective_transport_mode == "persistent_post_only":
            return post_only_streamablehttp_client
        return streamablehttp_client

    def _legacy_transport_factory(self) -> Callable[..., Any]:
        return self._explicit_transport_factory or streamablehttp_client

    async def _ensure_legacy_ready(self) -> None:
        self._ensure_callable()
        await self._legacy_manifest(refresh=False, accept=True)

    async def _legacy_manifest(self, *, refresh: bool, accept: bool) -> ToolManifest:
        now = time.monotonic()
        cache_valid = (
            self._manifest is not None
            and self.state == ConnectionState.READY
            and now - self._legacy_manifest_at < _LEGACY_READINESS_TTL_SECONDS
        )
        if cache_valid and not refresh:
            return self._manifest.model_copy(deep=True)
        async with self._connection_lock:
            now = time.monotonic()
            cache_valid = (
                self._manifest is not None
                and self.state == ConnectionState.READY
                and now - self._legacy_manifest_at < _LEGACY_READINESS_TTL_SECONDS
            )
            if cache_valid and not refresh:
                return self._manifest.model_copy(deep=True)
            previous_generation = self.connection_generation
            manifest = await self._list_tools_one_shot_with_retry()
            if accept:
                self._accept_manifest(manifest)
            else:
                self._manifest = manifest
            self._legacy_manifest_at = time.monotonic()
            self.state = ConnectionState.READY
            if previous_generation == 0:
                self.connection_generation = 1
            elif not cache_valid:
                self.connection_generation += 1
                self._reconnect_count += 1
            self._last_error = None
            return manifest.model_copy(deep=True)

    def _cooldown_remaining(self) -> float:
        return max(0.0, self._cooldown_until - time.monotonic())

    def _set_cooldown(self, reason: str) -> None:
        self._cooldown_until = max(self._cooldown_until, time.monotonic() + _COOLDOWN_SECONDS)
        self._last_error = reason
        self._trace_event(
            "transport_cooldown",
            reason=reason,
            cooldown_seconds=_COOLDOWN_SECONDS,
        )

    def _cooldown_result(self) -> ToolResult | None:
        remaining = self._cooldown_remaining()
        if remaining <= 0:
            return None
        return ToolResult.failure(
            ErrorCode.CONNECTION_UNAVAILABLE,
            f"Fusion MCP transport is cooling down; retry after {remaining:.2f}s",
            data={"retry_after_seconds": round(remaining, 3)},
        )

    @staticmethod
    def _can_retry_after_dispatch(options: McpCallOptions) -> bool:
        return (
            options.semantics == CallSemantics.READ_ONLY
            and options.replay_policy == ReplayPolicy.TRANSPORT_RETRY
            and not options.trusted_internal_read
        )

    def _nonreplayable_read_failure(self, code: ErrorCode, message: str) -> ToolResult:
        return ToolResult.failure(
            code,
            message,
            data={
                "dispatched": True,
                "may_still_be_running": True,
                "retry_suppressed": True,
                "retry_after_seconds": round(self._cooldown_remaining(), 3),
            },
        )

    async def _list_tools_one_shot_with_retry(self) -> ToolManifest:
        last_error: Exception | None = None
        for attempt in range(1, 3):
            try:
                if self.command:
                    payload = await self._command_jsonrpc_async(
                        "tools/list",
                        {},
                        self.read_timeout_seconds,
                    )
                    manifest = _manifest_from_payload(payload)
                else:
                    manifest = await self._legacy_list_tools_once()
                self._persist_manifest(manifest)
                return manifest
            except Exception as exc:
                last_error = exc
                if attempt >= 2 or isinstance(exc, McpError):
                    raise
                self._retry_count += 1
                await asyncio.sleep(0.1)
        raise last_error or ConnectionError("tools/list failed")

    async def _legacy_list_tools_once(self) -> ToolManifest:
        if not self.endpoint:
            raise RealMcpNotConfigured()
        async with self._legacy_transport_factory()(
            self.endpoint,
            timeout=self.connect_timeout_seconds,
            sse_read_timeout=self.sse_read_timeout_seconds,
        ) as (read_stream, write_stream, _get_session_id):
            async with self._session_factory(
                read_stream,
                write_stream,
                read_timeout_seconds=timedelta(seconds=self.read_timeout_seconds),
            ) as session:
                initialized = await asyncio.wait_for(session.initialize(), timeout=self.connect_timeout_seconds)
                self._initialize_count += 1
                result = await asyncio.wait_for(session.list_tools(), timeout=self.read_timeout_seconds)
                self._tools_list_count += 1
                return _manifest_from_results(initialized, result)

    async def _legacy_call(
        self,
        name: str,
        arguments: dict[str, Any],
        options: McpCallOptions,
    ) -> tuple[ToolResult, int]:
        max_attempts = 2 if self._can_retry_after_dispatch(options) else 1
        for attempt in range(1, max_attempts + 1):
            dispatched = False
            try:
                if not self.endpoint:
                    raise RealMcpNotConfigured()
                async with self._legacy_transport_factory()(
                    self.endpoint,
                    timeout=self.connect_timeout_seconds,
                    sse_read_timeout=self.sse_read_timeout_seconds,
                ) as (read_stream, write_stream, _get_session_id):
                    async with self._session_factory(
                        read_stream,
                        write_stream,
                        read_timeout_seconds=timedelta(
                            seconds=options.timeout_seconds
                            or (
                                self.read_timeout_seconds
                                if options.semantics == CallSemantics.READ_ONLY
                                else self.mutation_timeout_seconds
                            )
                        ),
                    ) as session:
                        await asyncio.wait_for(session.initialize(), timeout=self.connect_timeout_seconds)
                        self._initialize_count += 1
                        if options.semantics == CallSemantics.MUTATING:
                            await asyncio.wait_for(session.send_ping(), timeout=self.connect_timeout_seconds)
                        dispatched = True
                        if options.semantics == CallSemantics.MUTATING:
                            self._mutation_dispatched = True
                        self._call_count += 1
                        timeout_seconds = options.timeout_seconds or (
                            self.read_timeout_seconds
                            if options.semantics == CallSemantics.READ_ONLY
                            else self.mutation_timeout_seconds
                        )
                        response = await asyncio.wait_for(
                            session.call_tool(
                                name,
                                arguments,
                                read_timeout_seconds=timedelta(seconds=timeout_seconds),
                            ),
                            timeout=timeout_seconds,
                        )
                        return ToolResult.from_mcp(
                            response.model_dump(by_alias=True, mode="json", exclude_none=True)
                        ), attempt
            except McpError as exc:
                if not _is_retryable_mcp_error(exc):
                    return ToolResult.failure(ErrorCode.MCP_PROTOCOL_ERROR, str(exc)), attempt
                if dispatched and options.semantics == CallSemantics.MUTATING:
                    return self._unknown_mutation(f"legacy mutation transport outcome unknown: {exc}"), attempt
                if dispatched and not self._can_retry_after_dispatch(options):
                    self._set_cooldown("legacy non-replayable read lost transport after dispatch")
                    return self._nonreplayable_read_failure(
                        ErrorCode.CONNECTION_LOST,
                        f"legacy read transport outcome unknown after dispatch: {exc}",
                    ), attempt
                if attempt < max_attempts:
                    self._retry_count += 1
                    continue
                return ToolResult.failure(_mcp_failure_code(exc), str(exc)), attempt
            except asyncio.CancelledError:
                if dispatched and options.semantics == CallSemantics.MUTATING:
                    return self._unknown_mutation("legacy mutation cancelled after dispatch"), attempt
                if dispatched:
                    self._set_cooldown("legacy read cancelled after dispatch")
                    return self._nonreplayable_read_failure(
                        ErrorCode.CALL_CANCELLED,
                        "legacy read cancelled after dispatch; native work may still be running",
                    ), attempt
                raise
            except TimeoutError:
                if dispatched and options.semantics == CallSemantics.MUTATING:
                    self._set_cooldown("legacy mutation timed out after dispatch")
                    return self._unknown_mutation("legacy mutation timed out after dispatch"), attempt
                if dispatched and not self._can_retry_after_dispatch(options):
                    self._set_cooldown("legacy non-replayable read timed out after dispatch")
                    return self._nonreplayable_read_failure(
                        ErrorCode.READ_TIMEOUT_MAY_STILL_BE_RUNNING,
                        "legacy Fusion MCP read timed out; native work may still be running",
                    ), attempt
                if attempt < max_attempts:
                    self._retry_count += 1
                    continue
                return ToolResult.failure(ErrorCode.TIMEOUT, "Fusion MCP read timed out"), attempt
            except Exception as exc:
                if dispatched and options.semantics == CallSemantics.MUTATING:
                    return self._unknown_mutation(f"legacy connection lost after mutation dispatch: {exc}"), attempt
                if dispatched and not self._can_retry_after_dispatch(options):
                    self._set_cooldown("legacy non-replayable read lost connection after dispatch")
                    return self._nonreplayable_read_failure(
                        ErrorCode.CONNECTION_LOST,
                        f"legacy connection lost after read dispatch: {exc}",
                    ), attempt
                if attempt < max_attempts:
                    self._retry_count += 1
                    continue
                return self._connection_failure(exc), attempt
        return ToolResult.failure(ErrorCode.CONNECTION_LOST, "legacy Fusion MCP call failed"), max_attempts

    async def _command_call(
        self,
        name: str,
        arguments: dict[str, Any],
        options: McpCallOptions,
    ) -> tuple[ToolResult, int]:
        max_attempts = 2 if self._can_retry_after_dispatch(options) else 1
        for attempt in range(1, max_attempts + 1):
            # subprocess.run either returns a complete JSON response or raises;
            # once launched, a mutating command has an unknown outcome on any
            # timeout/transport failure and therefore is never replayed.
            dispatched = True
            try:
                if options.semantics == CallSemantics.MUTATING:
                    self._mutation_dispatched = True
                self._call_count += 1
                payload = await self._command_jsonrpc_async(
                    "tools/call",
                    {"name": name, "arguments": arguments},
                    options.timeout_seconds
                    or (
                        self.read_timeout_seconds
                        if options.semantics == CallSemantics.READ_ONLY
                        else self.mutation_timeout_seconds
                    ),
                )
                if "error" in payload:
                    if options.semantics == CallSemantics.MUTATING:
                        return self._unknown_mutation(f"command mutation outcome unknown: {payload['error']}"), attempt
                    return ToolResult.failure(ErrorCode.MCP_PROTOCOL_ERROR, str(payload["error"])), attempt
                result = payload.get("result", payload)
                if not isinstance(result, dict):
                    return ToolResult.failure(ErrorCode.MCP_PROTOCOL_ERROR, "invalid command result"), attempt
                return ToolResult.from_mcp(result), attempt
            except asyncio.CancelledError:
                if options.semantics == CallSemantics.MUTATING:
                    return self._unknown_mutation("command mutation cancelled after dispatch"), attempt
                self._set_cooldown("command read cancelled after dispatch")
                return self._nonreplayable_read_failure(
                    ErrorCode.CALL_CANCELLED,
                    "command read cancelled after dispatch; native work may still be running",
                ), attempt
            except Exception as exc:
                if dispatched and options.semantics == CallSemantics.MUTATING:
                    return self._unknown_mutation(f"command mutation outcome unknown: {exc}"), attempt
                if dispatched and not self._can_retry_after_dispatch(options):
                    self._set_cooldown("command non-replayable read failed after dispatch")
                    code = (
                        ErrorCode.READ_TIMEOUT_MAY_STILL_BE_RUNNING
                        if isinstance(exc, TimeoutError)
                        else ErrorCode.CONNECTION_LOST
                    )
                    return self._nonreplayable_read_failure(
                        code,
                        f"command read outcome unknown after dispatch: {exc}",
                    ), attempt
                if attempt < max_attempts:
                    self._retry_count += 1
                    continue
                return self._connection_failure(exc), attempt
        return ToolResult.failure(ErrorCode.CONNECTION_LOST, "command Fusion MCP call failed"), max_attempts

    def _resolve_options(self, name: str, options: McpCallOptions | None) -> McpCallOptions:
        default_semantics = CallSemantics.READ_ONLY if name in _READ_ONLY_NATIVE_TOOLS else CallSemantics.MUTATING
        if options is None:
            if default_semantics == CallSemantics.READ_ONLY:
                return McpCallOptions.for_read(timeout_seconds=self.read_timeout_seconds)
            return McpCallOptions.for_mutation(timeout_seconds=self.mutation_timeout_seconds)
        # Any native tool that is mutating-by-default can never opt itself into
        # replay. Only the audited internal execute templates carry the marker
        # that permits READ_ONLY semantics.
        if (
            default_semantics == CallSemantics.MUTATING
            and options.semantics == CallSemantics.READ_ONLY
            and not (name == _EXECUTE_TOOL and options.trusted_internal_read)
        ):
            return McpCallOptions.for_mutation(
                timeout_seconds=max(options.timeout_seconds or 0.0, self.mutation_timeout_seconds),
                operation_id=options.operation_id,
            )
        replay_policy = options.replay_policy
        if options.semantics == CallSemantics.MUTATING or options.trusted_internal_read:
            replay_policy = ReplayPolicy.BEFORE_DISPATCH_ONLY
        if options.timeout_seconds is None:
            timeout = (
                self.read_timeout_seconds
                if options.semantics == CallSemantics.READ_ONLY
                else self.mutation_timeout_seconds
            )
            return McpCallOptions(
                options.semantics,
                timeout,
                options.operation_id,
                trusted_internal_read=options.trusted_internal_read,
                replay_policy=replay_policy,
            )
        if replay_policy != options.replay_policy:
            return McpCallOptions(
                options.semantics,
                options.timeout_seconds,
                options.operation_id,
                trusted_internal_read=options.trusted_internal_read,
                replay_policy=replay_policy,
            )
        return options

    def _persist_manifest(self, manifest: ToolManifest) -> None:
        if self.manifest_store is None:
            return
        try:
            self.manifest_store.save_if_changed(manifest)
            self._last_persistence_error = self.manifest_store.last_error
        except Exception as exc:
            # OneDrive or filesystem failures must not take down a healthy live
            # Fusion session.
            self._last_persistence_error = f"{type(exc).__name__}: {exc}"
            self._trace_event("manifest_persistence_failed", error=self._last_persistence_error)

    def _ensure_callable(self) -> None:
        if self._closing or self.state in {ConnectionState.CLOSING, ConnectionState.CLOSED}:
            raise FusionHarnessError("Fusion MCP client is closed", ErrorCode.CLIENT_CLOSED)
        if not self.endpoint and not self.command:
            raise RealMcpNotConfigured()

    def _register_current_task(self) -> asyncio.Task[Any]:
        task = asyncio.current_task()
        if task is None:  # async public methods always have a task in asyncio
            raise RuntimeError("RealMcpClient requires an asyncio task")
        self._active_tasks.add(task)
        return task

    def _connection_failure(self, exc: Exception) -> ToolResult:
        if isinstance(exc, RealMcpNotConfigured):
            return ToolResult.failure(ErrorCode.CONNECTION_UNAVAILABLE, str(exc))
        code = ErrorCode.CONNECTION_UNAVAILABLE if self.connection_generation == 0 else ErrorCode.CONNECTION_LOST
        return ToolResult.failure(code, f"{type(exc).__name__}: {exc}")

    @staticmethod
    def _unknown_mutation(message: str) -> ToolResult:
        return ToolResult.failure(ErrorCode.MUTATION_OUTCOME_UNKNOWN, message)

    def _trace_tool_call(
        self,
        *,
        name: str,
        arguments: dict[str, Any],
        options: McpCallOptions,
        result: ToolResult,
        attempts: int,
        queue_ms: int,
        duration_ms: int,
        reconnected: bool,
        connection_ms: int,
    ) -> None:
        if self.trace_logger is None:
            return
        self.trace_logger.log_tool_call(
            session_id="fusion-agent-runtime",
            facade_tool=name,
            native_tool=name,
            arguments=arguments,
            result_status="ok" if result.ok else "error",
            duration_ms=duration_ms,
            error_code=result.error_code,
            connection_generation=self.connection_generation,
            fingerprint=self._manifest.fingerprint if self._manifest else None,
            semantics=options.semantics.value,
            attempts=attempts,
            reconnect=reconnected,
            queue_ms=queue_ms,
            connection_ms=connection_ms,
            timeout_seconds=options.timeout_seconds,
            operation_id=options.operation_id,
            outcome=result.error_code or "ok",
            transport_mode="command" if self.command else self._effective_transport_mode,
        )
        self._trace_event(
            "call_replay_policy",
            operation_id=options.operation_id,
            replay_policy=options.replay_policy.value,
            trusted_internal_read=options.trusted_internal_read,
            effective_transport_mode=(
                "command" if self.command else self._effective_transport_mode
            ),
            dispatched=attempts > 0,
            retry_suppressed=(
                attempts > 0 and options.replay_policy == ReplayPolicy.BEFORE_DISPATCH_ONLY
            ),
        )

    def _trace_event(self, event: str, **fields: Any) -> None:
        if self.trace_logger is not None:
            self.trace_logger.log_transport_event(event, **fields)

    def _http_jsonrpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Legacy raw HTTP helper retained for external diagnostic callers."""

        request_data = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint or "",
            data=request_data,
            headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310 - local endpoint
            return _load_json(response.read().decode("utf-8"))

    def _command_jsonrpc(
        self,
        method: str,
        params: dict[str, Any],
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        request_data = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        command = self.command
        if not command:
            return {"error": "No FUSION_MCP_COMMAND configured"}
        completed = subprocess.run(
            shlex.split(command),
            input=request_data,
            text=True,
            capture_output=True,
            timeout=timeout_seconds or self.timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            return {"error": completed.stderr.strip() or f"command exited {completed.returncode}"}
        return _load_json(completed.stdout)

    async def _command_jsonrpc_async(
        self,
        method: str,
        params: dict[str, Any],
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Run one command transport request with cancellable process ownership."""

        command = self.command
        if not command:
            return {"error": "No FUSION_MCP_COMMAND configured"}
        request_data = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        ).encode("utf-8")
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        process = await asyncio.create_subprocess_exec(
            *shlex.split(command),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=creationflags,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(request_data),
                timeout=timeout_seconds or self.timeout_seconds,
            )
        except BaseException:
            if process.returncode is None:
                process.kill()
                try:
                    await asyncio.wait_for(process.wait(), timeout=2.0)
                except BaseException:
                    pass
            raise
        if process.returncode != 0:
            message = stderr.decode("utf-8", errors="replace").strip()
            return {"error": message or f"command exited {process.returncode}"}
        return _load_json(stdout.decode("utf-8", errors="replace"))


async def _await_with_timeout(awaitable: Any, timeout_seconds: float) -> Any:
    """Await inline so AnyIO context ownership stays on the worker task."""

    async with asyncio.timeout(timeout_seconds):
        return await awaitable


def _manifest_from_results(initialize_result: Any, tools_result: Any) -> ToolManifest:
    initialize_payload = _model_dump(initialize_result)
    tools_payload = _model_dump(tools_result)
    tools = tools_payload.get("tools", [])
    server = initialize_payload.get("serverInfo", initialize_payload.get("server_info"))
    server = server if isinstance(server, dict) else {}
    return ToolManifest(
        source="fusion_real",
        server=server,
        server_name=server.get("name") if isinstance(server.get("name"), str) else None,
        server_version=server.get("version") if isinstance(server.get("version"), str) else None,
        protocol_version=initialize_payload.get("protocolVersion", initialize_payload.get("protocol_version")),
        tools=[
            ToolDefinition(
                name=tool.get("name", ""),
                description=tool.get("description", ""),
                input_schema=tool.get("inputSchema", tool.get("input_schema")),
                output_schema=tool.get("outputSchema", tool.get("output_schema")),
            )
            for tool in tools
            if isinstance(tool, dict) and tool.get("name")
        ],
    )


def _manifest_from_payload(payload: dict[str, Any]) -> ToolManifest:
    if "error" in payload:
        raise RuntimeError(payload["error"])
    result = payload.get("result", payload)
    if not isinstance(result, dict):
        raise RuntimeError(f"invalid tool payload type: {type(result).__name__}")
    if "error" in result:
        raise RuntimeError(result["error"])
    return _manifest_from_results({}, result)


def _model_dump(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        payload = value.model_dump(by_alias=True, mode="json", exclude_none=True)
        return payload if isinstance(payload, dict) else {}
    return {}


def _load_json(value: str) -> dict[str, Any]:
    value = _sse_data_payload(value) or value
    try:
        payload = json.loads(value)
    except JSONDecodeError as exc:
        return {"error": f"invalid json response: {exc}"}
    if not isinstance(payload, dict):
        return {"error": f"unexpected payload type: {type(payload).__name__}"}
    return payload


def _mcp_error_code(exc: McpError) -> int | str | None:
    code = getattr(getattr(exc, "error", None), "code", None)
    try:
        return int(code) if code is not None else None
    except (TypeError, ValueError):
        return str(code) if code is not None else None


def _is_timeout_mcp_error(exc: McpError) -> bool:
    message = str(exc).lower()
    return _mcp_error_code(exc) == 408 or "timed out" in message or "timeout" in message


def _is_retryable_mcp_error(exc: McpError) -> bool:
    if _is_timeout_mcp_error(exc):
        return True
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "connection lost",
            "session expired",
            "session closed",
            "stream closed",
            "transport closed",
        )
    )


def _mcp_failure_code(exc: McpError) -> ErrorCode:
    return ErrorCode.TIMEOUT if _is_timeout_mcp_error(exc) else ErrorCode.CONNECTION_LOST


def _sse_data_payload(value: str) -> str | None:
    """Extract the last JSON-looking data payload from an SSE response."""

    data_payloads: list[str] = []
    current: list[str] = []
    for line in value.splitlines():
        if not line.strip():
            if current:
                data_payloads.append("\n".join(current))
                current = []
            continue
        if line.startswith("data:"):
            current.append(line.partition(":")[2].lstrip())
    if current:
        data_payloads.append("\n".join(current))
    for payload in reversed(data_payloads):
        candidate = payload.strip()
        if candidate.startswith("{") and candidate.endswith("}"):
            return candidate
    return None


# These helpers are retained because older downstream tests import them even
# though ToolResult.from_mcp now owns result normalization.
def _content_text(result: dict[str, Any]) -> str:
    content = result.get("content")
    if not isinstance(content, list):
        return ""
    return "\n".join(
        block["text"]
        for block in content
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
    )


def _parse_content_text(text: str) -> dict[str, Any]:
    if not text:
        return {}
    try:
        loaded = json.loads(text.strip())
    except JSONDecodeError:
        loaded = None
    if isinstance(loaded, dict):
        return loaded
    data: dict[str, Any] = {"text": text}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        key, separator, value = line.partition(":")
        if separator and key and " " not in key:
            data[key] = _parse_scalar(value.strip())
    return data


def _parse_scalar(value: str) -> Any:
    if value == "":
        return ""
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    try:
        return ast.literal_eval(value)
    except (SyntaxError, ValueError):
        return value
