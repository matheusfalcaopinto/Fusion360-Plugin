"""Stable MCP tool profiles for the Fusion Agent public surface.

Profiles are intentionally resolved once by :func:`build_server`.  Changing an
environment variable in a running process does not silently change the tool
surface advertised to an already connected MCP client.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable


TOOL_PROFILES = ("normal", "advanced", "diagnostic", "benchmark", "all")

NORMAL_TOOLS = frozenset(
    {
        "fusion_agent_readiness_report",
        "fusion_agent_native_read",
        "fusion_agent_targeted_inspect",
        "fusion_agent_compact_snapshot",
        "fusion_agent_plan_spec",
        "fusion_agent_validate_spec",
        "fusion_agent_run_session",
        "fusion_agent_verify_active_design",
        "fusion_agent_safe_change_preview",
        "fusion_agent_safe_change_apply",
        "fusion_agent_recover_change",
        "fusion_agent_capture_viewport",
    }
)

ADVANCED_ONLY_TOOLS = frozenset(
    {
        "fusion_agent_fast_execute",
        "fusion_agent_inspect",
        "fusion_agent_hub_inventory",
        "fusion_agent_dry_run_session",
        "fusion_agent_export_spec_json",
        "fusion_agent_memory_search",
        "fusion_agent_memory_write",
        "fusion_agent_skills_rank",
    }
)

DIAGNOSTIC_TOOLS = frozenset(
    {
        "fusion_agent_doctor",
        "fusion_agent_readiness_report",
        "fusion_agent_probe",
        "fusion_agent_session_health",
        "fusion_agent_inspect",
        "fusion_agent_native_read",
        "fusion_agent_targeted_inspect",
        "fusion_agent_compact_snapshot",
        "fusion_agent_discover_tools",
        "fusion_agent_propose_mapping",
    }
)

BENCHMARK_TOOLS = frozenset(
    {
        "fusion_agent_readiness_report",
        "fusion_agent_native_read",
        "fusion_agent_targeted_inspect",
        "fusion_agent_compact_snapshot",
        "fusion_agent_verify_active_design",
        "fusion_agent_capture_viewport",
        "fusion_agent_list_benchmarks",
        "fusion_agent_run_benchmark",
        "fusion_agent_read_benchmark_report",
    }
)


@dataclass(frozen=True, slots=True)
class ToolProfileError(ValueError):
    """A requested tool is not available in the selected MCP profile."""

    tool_name: str
    profile: str
    available_profiles: tuple[str, ...]
    code: str = "TOOL_NOT_AVAILABLE_IN_PROFILE"

    def __str__(self) -> str:
        return (
            f"{self.code}: {self.tool_name} is not available in profile "
            f"{self.profile!r}; available profiles: {', '.join(self.available_profiles) or 'none'}"
        )

    def payload(self) -> dict[str, object]:
        return {
            "ok": False,
            "error_code": self.code,
            "tool": self.tool_name,
            "profile": self.profile,
            "available_profiles": list(self.available_profiles),
        }


def resolve_tool_profile(profile: str | None = None) -> str:
    """Resolve and validate a profile, defaulting to the task-oriented surface."""

    value = profile if profile is not None else os.getenv("FUSION_AGENT_TOOL_PROFILE", "normal")
    normalized = value.strip().lower()
    if normalized not in TOOL_PROFILES:
        raise ValueError(
            "FUSION_AGENT_TOOL_PROFILE must be one of: " + ", ".join(TOOL_PROFILES)
        )
    return normalized


def tools_for_profile(profile: str, all_tool_names: Iterable[str]) -> frozenset[str]:
    """Return the exact registry subset exposed by ``profile``."""

    resolved = resolve_tool_profile(profile)
    all_names = frozenset(all_tool_names)
    requested = {
        "normal": NORMAL_TOOLS,
        "advanced": NORMAL_TOOLS | ADVANCED_ONLY_TOOLS,
        "diagnostic": DIAGNOSTIC_TOOLS,
        "benchmark": BENCHMARK_TOOLS,
        "all": all_names,
    }[resolved]
    # Intersection makes profiles forward compatible with installations whose
    # registry is older than this module.  Missing names are caught by tests.
    return frozenset(requested & all_names)


def profiles_for_tool(tool_name: str, all_tool_names: Iterable[str]) -> tuple[str, ...]:
    """List profiles in which a registered tool is callable."""

    return tuple(
        profile
        for profile in TOOL_PROFILES
        if tool_name in tools_for_profile(profile, all_tool_names)
    )
