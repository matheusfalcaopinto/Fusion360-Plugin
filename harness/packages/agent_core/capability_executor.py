"""Fail-closed executor for strict CadSpec v2 capability graphs."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from agent_core.authority import (
    AuthorityBroker,
    AuthorityDeniedError,
    AuthorityPolicy,
    CadTargetBinding,
)
from cad_spec.v2 import CadSpecV2, ExportOperation, OperationSpec
from fusion_mcp_adapter.errors import ErrorCode
from fusion_mcp_adapter.tool_result import PublicError


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
    public_error: dict[str, Any] | None = None


class CapabilityExecutor:
    """Validate the complete graph before sending its first native operation."""

    def __init__(
        self,
        backend: CapabilityBackend | None = None,
        *,
        authority_broker: AuthorityBroker | None = None,
        experimental_enabled: bool = False,
    ) -> None:
        self.backend = backend
        # Startup owners must inject configured authority.  A standalone
        # executor never reads process environment and therefore fails closed.
        self.authority_broker = authority_broker or AuthorityBroker(
            AuthorityPolicy.deny_all()
        )
        self.experimental_enabled = bool(experimental_enabled)

    async def execute(
        self,
        spec: CadSpecV2,
        *,
        dry_run: bool = False,
        session_id: str | None = None,
    ) -> CapabilityExecutionResult:
        if dry_run:
            # Dry-run still enforces the experimental feature flag, but uses
            # the spec's own capabilities so it does not require a live backend.
            spec.ensure_supported(
                spec.capabilities,
                experimental_enabled=self.experimental_enabled,
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
            raise RuntimeError(
                "CadSpec v2 execution requires a typed capability backend"
            )

        # This is intentionally a single, complete preflight.  No partial
        # operation graph is dispatched when a later capability is missing.
        spec.ensure_supported(
            set(self.backend.capabilities),
            experimental_enabled=self.experimental_enabled,
        )
        # Reject every invalid host path before a read is sent to the provider.
        # Export grants additionally require a provider-resolved document/entity
        # identity; a caller-supplied label is never an authority proof.
        self.authority_broker.validate_host_requests(spec)
        target_bindings: dict[str, tuple[CadTargetBinding, ...]] = {}
        export_operations = [
            operation
            for operation in spec.operations
            if isinstance(operation, ExportOperation)
        ]
        if export_operations:
            resolve_target = getattr(self.backend, "resolve_cad_target_binding", None)
            if not callable(resolve_target):
                raise AuthorityDeniedError(
                    "backend cannot resolve lossless CAD target authority"
                )
            for operation in export_operations:
                binding = await resolve_target(operation)
                if not isinstance(binding, CadTargetBinding):
                    raise AuthorityDeniedError(
                        "backend returned an invalid CAD target authority proof"
                    )
                target_bindings[operation.id] = (binding,)
        graph = self.authority_broker.prepare_graph(
            spec,
            session_id=session_id or f"cadspec-{uuid.uuid4().hex}",
            provider=self.backend.provider,
            target_bindings_by_operation=target_bindings,
        )
        by_id = {bound.operation.id: bound for bound in graph.operations}
        host_io = [bound for bound in graph.operations if bound.capability is not None]
        try:
            if host_io:
                preflight_bound = getattr(
                    self.backend, "preflight_bound_operations", None
                )
                execute_bound = getattr(self.backend, "execute_bound_operation", None)
                if not callable(preflight_bound) or not callable(execute_bound):
                    raise AuthorityDeniedError(
                        "backend cannot preserve bound host I/O authority"
                    )
                preflight_bound(list(graph.operations))
            else:
                preflight = getattr(self.backend, "preflight_operations", None)
                if callable(preflight):
                    preflight(list(spec.operations))
        except Exception:
            for bound in host_io:
                self.authority_broker.revoke(bound)
            raise

        result = CapabilityExecutionResult(
            success=True,
            provider=self.backend.provider,
            required_capabilities=sorted(spec.capabilities),
            available_capabilities=sorted(self.backend.capabilities),
        )
        for operation in spec.operations:
            mutating = not operation.kind.startswith("analysis.")
            bound = by_id[operation.id]
            backend_returned = False
            backend_invoked = False
            try:
                if bound.capability is not None:
                    self.authority_broker.claim(bound)
                    backend_invoked = True
                    payload = await self.backend.execute_bound_operation(bound)  # type: ignore[attr-defined]
                    backend_returned = True
                    self.authority_broker.complete(bound, outcome="consumed")
                else:
                    backend_invoked = True
                    payload = await self.backend.execute_operation(operation)
            except Exception as exc:  # noqa: BLE001 - preserve typed transport evidence
                transport = _exception_transport(exc)
                if bound.capability is not None:
                    unknown = bool(
                        backend_returned
                        or transport.get("dispatched")
                        or transport.get("may_have_applied")
                        or transport.get("mutation_outcome") == "unknown"
                    )
                    self.authority_broker.fail(bound, outcome_unknown=unknown)
                for remaining in host_io:
                    if remaining.operation.id != operation.id:
                        self.authority_broker.revoke(remaining)
                _merge_transport(
                    result,
                    transport,
                    mutating=mutating,
                    backend_invoked=backend_invoked,
                )
                result.success = False
                result.error_code = _public_execution_error_code(exc, transport)
                public_error = PublicError.downstream_failure(result.error_code)
                result.error_message = public_error.generic_message
                result.public_error = public_error.model_dump(mode="json")
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
                        "public_error": result.public_error,
                    }
                )
                return result
            transport = _payload_transport(payload)
            _merge_transport(
                result,
                transport,
                mutating=mutating,
                backend_invoked=True,
            )
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
        return _public_transport_evidence(direct)
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
    return _public_transport_evidence(value) if isinstance(value, dict) else {}


def _public_execution_error_code(exc: Exception, transport: dict[str, Any]) -> str:
    if transport.get("mutation_outcome") == "unknown":
        return ErrorCode.MUTATION_OUTCOME_UNKNOWN.value
    candidate = getattr(exc, "error_code", None) or transport.get("error_code")
    value = str(candidate) if candidate is not None else ""
    allowed = {item.value for item in ErrorCode}
    return value if value in allowed else ErrorCode.FUSION_OPERATION_FAILED.value


def _public_transport_evidence(value: dict[str, Any]) -> dict[str, Any]:
    """Allowlist typed dispatch evidence; never retain diagnostics or paths."""

    public: dict[str, Any] = {}
    for key in (
        "dispatched",
        "may_have_applied",
        "post_dispatch_replay_suppressed",
    ):
        if isinstance(value.get(key), bool):
            public[key] = value[key]
    outcome = value.get("mutation_outcome")
    if outcome in {"known", "unknown"}:
        public["mutation_outcome"] = outcome
    semantics = value.get("semantics")
    if semantics in {"read_only", "mutating"}:
        public["semantics"] = semantics
    operation_id = value.get("operation_id")
    if _safe_transport_identifier(operation_id):
        public["operation_id"] = operation_id
    operation_ids = value.get("operation_ids")
    if isinstance(operation_ids, list) and all(
        _safe_transport_identifier(item) for item in operation_ids
    ):
        public["operation_ids"] = list(operation_ids)
    return public


def _safe_transport_identifier(value: Any) -> bool:
    return bool(
        isinstance(value, str)
        and 1 <= len(value) <= 128
        and all(character.isalnum() or character in "._:-" for character in value)
    )


def _combine_transports(transports: list[dict[str, Any]]) -> dict[str, Any]:
    dispatched = any(bool(item.get("dispatched")) for item in transports)
    unknown = any(item.get("mutation_outcome") == "unknown" for item in transports)
    return {
        "dispatched": dispatched,
        "may_have_applied": any(
            bool(item.get("may_have_applied")) for item in transports
        ),
        "post_dispatch_replay_suppressed": any(
            bool(item.get("post_dispatch_replay_suppressed")) for item in transports
        ),
        "mutation_outcome": "unknown" if unknown else "known",
        "operation_ids": [
            str(item["operation_id"]) for item in transports if item.get("operation_id")
        ],
    }


def _merge_transport(
    result: CapabilityExecutionResult,
    transport: dict[str, Any],
    *,
    mutating: bool,
    backend_invoked: bool,
) -> None:
    if not mutating:
        return
    if not transport:
        # Authority and capability claims happen before the backend boundary.
        # A rejection there is a known zero-dispatch outcome, not an unknown
        # mutation merely because no provider transport evidence exists.
        if not backend_invoked:
            return
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
