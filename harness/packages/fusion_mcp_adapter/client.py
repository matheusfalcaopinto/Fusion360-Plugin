"""MCP client protocol."""

from __future__ import annotations

from typing import Protocol

from fusion_mcp_adapter.semantics import McpCallOptions
from fusion_mcp_adapter.tool_result import ToolManifest, ToolResult


class McpClient(Protocol):
    """Minimal client contract for Fusion MCP transports."""

    async def list_tools(self) -> ToolManifest:
        """Return the current native MCP tool manifest."""

    async def call_tool(
        self,
        name: str,
        arguments: dict,
        *,
        options: McpCallOptions | None = None,
    ) -> ToolResult:
        """Call one native MCP tool."""
