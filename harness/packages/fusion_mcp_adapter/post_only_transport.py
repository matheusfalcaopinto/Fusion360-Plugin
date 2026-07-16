"""Streamable HTTP client variant without the optional standalone GET stream.

The MCP Streamable HTTP specification makes the server-initiated GET/SSE
channel optional.  Fusion's connector can stall after that channel is opened,
while normal POST responses (including response-scoped SSE) work correctly.
This context manager reuses the SDK transport implementation and changes only
the callback invoked after ``notifications/initialized``.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import anyio
import httpx
from mcp.client.streamable_http import StreamableHTTPTransport
from mcp.shared._httpx_utils import McpHttpClientFactory, create_mcp_http_client


@asynccontextmanager
async def post_only_streamablehttp_client(
    url: str,
    headers: dict[str, str] | None = None,
    timeout: float | timedelta = 30,
    sse_read_timeout: float | timedelta = 300,
    terminate_on_close: bool = True,
    httpx_client_factory: McpHttpClientFactory = create_mcp_http_client,
    auth: httpx.Auth | None = None,
) -> AsyncGenerator[tuple[Any, Any, Any], None]:
    """Yield SDK-compatible streams while never issuing standalone HTTP GET."""

    timeout_seconds = (
        timeout.total_seconds() if isinstance(timeout, timedelta) else timeout
    )
    sse_seconds = (
        sse_read_timeout.total_seconds()
        if isinstance(sse_read_timeout, timedelta)
        else sse_read_timeout
    )
    client = httpx_client_factory(
        headers=headers,
        timeout=httpx.Timeout(timeout_seconds, read=sse_seconds),
        auth=auth,
    )
    transport = StreamableHTTPTransport(url)
    read_writer, read_stream = anyio.create_memory_object_stream(0)
    write_stream, write_reader = anyio.create_memory_object_stream(0)

    async with client:
        async with anyio.create_task_group() as task_group:

            def suppress_get_stream() -> None:
                # Deliberately empty: POST JSON and POST-scoped SSE remain
                # handled by StreamableHTTPTransport.post_writer.
                return None

            task_group.start_soon(
                transport.post_writer,
                client,
                write_reader,
                read_writer,
                write_stream,
                suppress_get_stream,
                task_group,
            )
            try:
                yield read_stream, write_stream, transport.get_session_id
            finally:
                if transport.session_id and terminate_on_close:
                    await transport.terminate_session(client)
                task_group.cancel_scope.cancel()
                await read_writer.aclose()
                await write_stream.aclose()
