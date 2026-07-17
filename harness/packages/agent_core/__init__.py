"""Core planning, execution, verification orchestration.

The package exports its public convenience names lazily.  Security boundaries
such as ``fusion_mcp_adapter.execute_guard`` import the lightweight
``agent_core.request_context`` module during adapter startup; eagerly importing
the executor and facade here would create an adapter/facade import cycle.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "AuthorityBroker": ("agent_core.authority", "AuthorityBroker"),
    "AuthorityDeniedError": ("agent_core.authority", "AuthorityDeniedError"),
    "AuthorityPolicy": ("agent_core.authority", "AuthorityPolicy"),
    "CapabilityLedger": ("agent_core.authority", "CapabilityLedger"),
    "ExecutionContext": ("agent_core.executor", "ExecutionContext"),
    "ExecutionResult": ("agent_core.executor", "ExecutionResult"),
    "Executor": ("agent_core.executor", "Executor"),
    "PlanningRequest": ("agent_core.planner", "PlanningRequest"),
    "PromptPlanner": ("agent_core.planner", "PromptPlanner"),
    "RepairLoop": ("agent_core.repair_loop", "RepairLoop"),
    "RequestContext": ("agent_core.request_context", "RequestContext"),
    "bind_request_context": ("agent_core.request_context", "bind_request_context"),
    "current_request_context": (
        "agent_core.request_context",
        "current_request_context",
    ),
    "CaptureViewportResult": (
        "agent_core.session_controller",
        "CaptureViewportResult",
    ),
    "RuleBasedPlanner": ("agent_core.planner", "RuleBasedPlanner"),
    "SessionController": ("agent_core.session_controller", "SessionController"),
    "SessionOptions": ("agent_core.session_controller", "SessionOptions"),
    "SessionResult": ("agent_core.session_controller", "SessionResult"),
    "VerifyActiveResult": (
        "agent_core.session_controller",
        "VerifyActiveResult",
    ),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    """Resolve one public export without importing unrelated runtime layers."""

    try:
        module_name, attribute_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
