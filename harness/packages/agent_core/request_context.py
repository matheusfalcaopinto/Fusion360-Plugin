"""Immutable, request-local security and execution context.

Environment variables are startup configuration, not an authorization channel.
Code that needs request, trial, timeout, or capability state reads this context
instead of mutating process-global state across an ``await`` boundary.
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, TypeAlias


ExecutionMode: TypeAlias = Literal["mock", "real"]


def validate_execution_mode(value: object) -> ExecutionMode:
    """Return a canonical execution mode or fail before provider selection."""

    if value == "mock":
        return "mock"
    if value == "real":
        return "real"
    raise ValueError("mode must be 'mock' or 'real'")


@dataclass(frozen=True, slots=True)
class RequestContext:
    """One immutable snapshot owned by exactly one request or benchmark trial."""

    request_id: str
    profile: str
    mode: ExecutionMode
    backend: str
    session_id: str | None = None
    trial_id: str | None = None
    document_identity: str | None = None
    spec_digest: str | None = None
    timeouts: Mapping[str, float] = field(default_factory=dict)
    limits: Mapping[str, int] = field(default_factory=dict)
    capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("request_id", "profile", "mode", "backend"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        object.__setattr__(self, "mode", validate_execution_mode(self.mode))
        normalized_timeouts: dict[str, float] = {}
        for name, value in dict(self.timeouts).items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError("timeout names must be non-empty strings")
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise ValueError(f"timeout {name} must be numeric")
            number = float(value)
            if not math.isfinite(number) or number < 0:
                raise ValueError(f"timeout {name} must be finite and non-negative")
            normalized_timeouts[name] = number
        capabilities = tuple(self.capabilities)
        if any(not isinstance(value, str) or not value for value in capabilities):
            raise ValueError("capabilities must be non-empty strings")
        normalized_limits: dict[str, int] = {}
        for name, value in dict(self.limits).items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError("limit names must be non-empty strings")
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"limit {name} must be a non-negative integer")
            normalized_limits[name] = value
        object.__setattr__(self, "timeouts", MappingProxyType(normalized_timeouts))
        object.__setattr__(self, "limits", MappingProxyType(normalized_limits))
        object.__setattr__(self, "capabilities", capabilities)


_REQUEST_CONTEXT: ContextVar[RequestContext | None] = ContextVar(
    "fusion_agent_request_context",
    default=None,
)


def current_request_context() -> RequestContext | None:
    """Return the current task's context without consulting the environment."""

    return _REQUEST_CONTEXT.get()


def require_request_context() -> RequestContext:
    """Return the current context or fail closed when none was bound."""

    context = current_request_context()
    if context is None:
        raise RuntimeError("request context is not bound")
    return context


@contextmanager
def bind_request_context(context: RequestContext) -> Iterator[RequestContext]:
    """Bind and reliably restore an immutable context for this logical task."""

    if not isinstance(context, RequestContext):
        raise TypeError("context must be RequestContext")
    token: Token[RequestContext | None] = _REQUEST_CONTEXT.set(context)
    try:
        yield context
    finally:
        _REQUEST_CONTEXT.reset(token)
