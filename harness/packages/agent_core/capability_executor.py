"""Fail-closed executor for strict CadSpec v2 capability graphs."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Protocol

from cad_spec.v2 import CadSpecV2, OperationSpec


class CapabilityBackend(Protocol):
    """Small typed backend boundary used by the v2 executor."""

    @property
    def capabilities(self) -> set[str]: ...

    @property
    def provider(self) -> str: ...

    async def execute_operation(self, operation: OperationSpec) -> dict[str, Any]: ...

    def preflight_operations(self, operations: list[OperationSpec]) -> None: ...


@dataclass(slots=True)
class CapabilityExecutionResult:
    success: bool
    provider: str
    dry_run: bool = False
    required_capabilities: list[str] = field(default_factory=list)
    available_capabilities: list[str] = field(default_factory=list)
    transactions: list[dict[str, Any]] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    dispatched: bool = False
    may_have_applied: bool = False
    post_dispatch_replay_suppressed: bool = False
    mutation_outcome: str = "known"
    transport_evidence_complete: bool = True
    error_code: str | None = None
    error_message: str | None = None


class CapabilityExecutor:
    """Validate the complete graph before sending its first native operation."""

    def __init__(self, backend: CapabilityBackend | None = None) -> None:
        self.backend = backend

    async def execute(
        self,
        spec: CadSpecV2,
        *,
        dry_run: bool = False,
    ) -> CapabilityExecutionResult:
        experimental_enabled = os.getenv(
            "FUSION_AGENT_EXPERIMENTAL_MANUFACTURING", "0"
        ).strip().lower() in {"1", "true", "yes", "on"}

        if dry_run:
            # Dry-run still enforces the experimental feature flag, but uses
            # the spec's own capabilities so it does not require a live backend.
            spec.ensure_supported(
                spec.capabilities,
                experimental_enabled=experimental_enabled,
            )
            return CapabilityExecutionResult(
                success=True,
                provider="dry_run",
                dry_run=True,
                required_capabilities=sorted(spec.capabilities),
                available_capabilities=sorted(spec.capabilities),
                transactions=[
                    {
                        "operation_id": operation.id,
                        "kind": operation.kind,
                        "status": "simulated",
                        "requirement_ids": list(operation.requirement_ids),
                    }
                    for operation in spec.operations
                ],
            )

        if self.backend is None:
            raise RuntimeError("CadSpec v2 execution requires a typed capability backend")

        # This is intentionally a single, complete preflight.  No partial
        # operation graph is dispatched when a later capability is missing.
        spec.ensure_supported(
            set(self.backend.capabilities),
            experimental_enabled=experimental_enabled,
        )
        preflight = getattr(self.backend, "preflight_operations", None)
        if callable(preflight):
            preflight(list(spec.operations))

        result = CapabilityExecutionResult(
            success=True,
            provider=self.backend.provider,
            required_capabilities=sorted(spec.capabilities),
            available_capabilities=sorted(self.backend.capabilities),
        )
        for operation in spec.operations:
            mutating = not operation.kind.startswith("analysis.")
            try:
                payload = await self.backend.execute_operation(operation)
            except Exception as exc:  # noqa: BLE001 - preserve typed transport evidence
                transport = _exception_transport(exc)
                _merge_transport(result, transport, mutating=mutating)
                result.success = False
                result.error_code = str(
                    getattr(exc, "error_code", None)
                    or transport.get("error_code")
                    or type(exc).__name__
                )
                result.error_message = str(exc)
                result.transactions.append(
                    {
                        "operation_id": operation.id,
                        "kind": operation.kind,
                        "status": "failed",
                        "provider": self.backend.provider,
                        "requirement_ids": list(operation.requirement_ids),
                        "transport": transport,
                        "error_code": result.error_code,
                        "error_message": result.error_message,
                    }
                )
                return result
            transport = _payload_transport(payload)
            _merge_transport(result, transport, mutating=mutating)
            result.transactions.append(
                {
                    "operation_id": operation.id,
                    "kind": operation.kind,
                    "status": "ok",
                    "provider": self.backend.provider,
                    "requirement_ids": list(operation.requirement_ids),
                    "native_result": payload,
                    "transport": transport,
                }
            )
            if operation.kind.startswith("analysis."):
                result.evidence[operation.id] = payload
        return result


def _payload_transport(payload: dict[str, Any]) -> dict[str, Any]:
    direct = payload.get("fusion_agent_transport")
    if isinstance(direct, dict):
        return dict(direct)
    calls = payload.get("calls")
    if isinstance(calls, list):
        transports = [
            item.get("transport")
            for item in calls
            if isinstance(item, dict) and isinstance(item.get("transport"), dict)
        ]
        if transports:
            return _combine_transports(transports)
    return {}


def _exception_transport(exc: Exception) -> dict[str, Any]:
    value = getattr(exc, "transport", None)
    return dict(value) if isinstance(value, dict) else {}


def _combine_transports(transports: list[dict[str, Any]]) -> dict[str, Any]:
    dispatched = any(bool(item.get("dispatched")) for item in transports)
    unknown = any(item.get("mutation_outcome") == "unknown" for item in transports)
    return {
        "dispatched": dispatched,
        "may_have_applied": any(bool(item.get("may_have_applied")) for item in transports),
        "post_dispatch_replay_suppressed": any(
            bool(item.get("post_dispatch_replay_suppressed")) for item in transports
        ),
        "mutation_outcome": "unknown" if unknown else "known",
        "operation_ids": [
            str(item["operation_id"])
            for item in transports
            if item.get("operation_id")
        ],
    }


def _merge_transport(
    result: CapabilityExecutionResult,
    transport: dict[str, Any],
    *,
    mutating: bool,
) -> None:
    if not mutating:
        return
    if not transport:
        # A non-mock backend that returned without dispatch evidence cannot be
        # promoted to a known mutation outcome merely from an attempted call.
        if result.provider not in {"mock", "dry_run"}:
            result.transport_evidence_complete = False
            result.may_have_applied = True
            result.mutation_outcome = "unknown"
        return
    result.dispatched = result.dispatched or bool(transport.get("dispatched"))
    result.may_have_applied = result.may_have_applied or bool(
        transport.get("may_have_applied")
    )
    result.post_dispatch_replay_suppressed = (
        result.post_dispatch_replay_suppressed
        or bool(transport.get("post_dispatch_replay_suppressed"))
    )
    if transport.get("mutation_outcome") == "unknown":
        result.mutation_outcome = "unknown"
