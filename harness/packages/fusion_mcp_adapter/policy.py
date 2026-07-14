"""Deny-by-default native tool policy."""

from __future__ import annotations

from dataclasses import dataclass, field

from fusion_mcp_adapter.errors import ToolNotAllowed


@dataclass
class ToolPolicy:
    """Allowlist policy for native MCP tools."""

    allowed_tools: set[str] = field(default_factory=set)

    def ensure_allowed(self, tool_name: str) -> None:
        """Raise when a native tool is not allowlisted."""

        if tool_name not in self.allowed_tools:
            raise ToolNotAllowed(tool_name)

    def allow(self, tool_name: str) -> None:
        """Add one native tool to the allowlist."""

        self.allowed_tools.add(tool_name)

    @classmethod
    def from_manifest(cls, tool_names: set[str]) -> "ToolPolicy":
        """Create an allowlist from a known-safe manifest."""

        return cls(allowed_tools=set(tool_names))
