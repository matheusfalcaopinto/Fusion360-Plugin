"""Fusion MCP adapter layer."""

from fusion_mcp_adapter.adapter import FusionMcpAdapter
from fusion_mcp_adapter.client import McpClient
from fusion_mcp_adapter.mock_client import MockMcpClient
from fusion_mcp_adapter.policy import ToolPolicy
from fusion_mcp_adapter.real_client import RealMcpClient
from fusion_mcp_adapter.semantics import (
    CallSemantics,
    ConnectionState,
    McpCallOptions,
    ReplayPolicy,
)
from fusion_mcp_adapter.tool_result import ToolManifest, ToolResult

__all__ = [
    "FusionMcpAdapter",
    "McpClient",
    "MockMcpClient",
    "RealMcpClient",
    "CallSemantics",
    "ConnectionState",
    "McpCallOptions",
    "ReplayPolicy",
    "ToolManifest",
    "ToolPolicy",
    "ToolResult",
]
