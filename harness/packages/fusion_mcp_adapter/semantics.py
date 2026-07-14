"""Call semantics and lifecycle models for the real Fusion MCP transport."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from uuid import uuid4


class ConnectionState(StrEnum):
    """Observable lifecycle states for :class:`RealMcpClient`."""

    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    READY = "READY"
    BROKEN = "BROKEN"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"


class CallSemantics(StrEnum):
    """Whether a native call is safe to replay after a transport failure."""

    READ_ONLY = "read_only"
    MUTATING = "mutating"


class ReplayPolicy(StrEnum):
    """Whether a dispatched call may be replayed after transport failure.

    Effect and replay safety are intentionally independent.  In particular, a
    harness-owned inspection script is read-only but may still be executing in
    Fusion after the HTTP request times out, so it uses
    :attr:`BEFORE_DISPATCH_ONLY`.
    """

    TRANSPORT_RETRY = "transport_retry"
    BEFORE_DISPATCH_ONLY = "before_dispatch_only"


@dataclass(frozen=True, slots=True)
class McpCallOptions:
    """Per-call execution policy.

    ``operation_id`` is a local correlation identifier. It is deliberately not
    sent to Autodesk unless a future native schema explicitly supports it.
    """

    semantics: CallSemantics
    timeout_seconds: float | None = None
    operation_id: str = field(default_factory=lambda: uuid4().hex)
    trusted_internal_read: bool = False
    replay_policy: ReplayPolicy = ReplayPolicy.BEFORE_DISPATCH_ONLY

    @classmethod
    def for_read(
        cls,
        *,
        timeout_seconds: float = 120.0,
        operation_id: str | None = None,
    ) -> "McpCallOptions":
        return cls(
            CallSemantics.READ_ONLY,
            timeout_seconds,
            operation_id or uuid4().hex,
            replay_policy=ReplayPolicy.TRANSPORT_RETRY,
        )

    @classmethod
    def for_mutation(
        cls,
        *,
        timeout_seconds: float = 240.0,
        operation_id: str | None = None,
    ) -> "McpCallOptions":
        return cls(
            CallSemantics.MUTATING,
            timeout_seconds,
            operation_id or uuid4().hex,
            replay_policy=ReplayPolicy.BEFORE_DISPATCH_ONLY,
        )

    @classmethod
    def for_trusted_internal_read(
        cls,
        *,
        timeout_seconds: float = 120.0,
        operation_id: str | None = None,
    ) -> "McpCallOptions":
        """Mark an audited execute template as read-only but non-replayable."""

        return cls(
            CallSemantics.READ_ONLY,
            timeout_seconds,
            operation_id or uuid4().hex,
            trusted_internal_read=True,
            replay_policy=ReplayPolicy.BEFORE_DISPATCH_ONLY,
        )
