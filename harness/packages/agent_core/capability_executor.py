"""Fail-closed executor for strict CadSpec v2 capability graphs."""

from __future__ import annotations

import math
import re
import uuid
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from agent_core.authority import (
    AuthorityBroker,
    AuthorityDeniedError,
    AuthorityPolicy,
    CadTargetBinding,
    HostOutputDisabledError,
    PreparedOperationGraph,
    REAL_HOST_OUTPUT_DENIED_MESSAGE,
    cad_graph_target_producers,
    cad_operation_produced_targets,
    cad_operation_target_requirements,
)
from agent_core.request_context import (
    RequestContext,
    bind_request_context,
    current_request_context,
)
from cad_spec.v2 import (
    CadSpecV2,
    ExportOperation,
    ImportOperation,
    InterferenceOperation,
    OperationSpec,
    PhysicalPropertiesOperation,
)
from fusion_mcp_adapter.errors import ErrorCode
from fusion_mcp_adapter.tool_result import PublicError
from verifier.result_models import EvidenceEnvelope


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
        if self.backend.provider != "mock" and any(
            isinstance(operation, ExportOperation) for operation in spec.operations
        ):
            raise HostOutputDisabledError(REAL_HOST_OUTPUT_DENIED_MESSAGE)

        # This is intentionally a single, complete preflight.  No partial
        # operation graph is dispatched when a later capability is missing.
        spec.ensure_supported(
            set(self.backend.capabilities),
            experimental_enabled=self.experimental_enabled,
        )
        # Reject every invalid host path and every ambiguous produced reference
        # before a read is sent to the provider.  Compilation likewise covers
        # the complete non-I/O graph before the first provider call.
        self.authority_broker.validate_host_requests(spec)
        host_io_operations = [
            operation
            for operation in spec.operations
            if isinstance(operation, (ImportOperation, ExportOperation))
        ]
        preflight_host_io = getattr(self.backend, "preflight_host_io_operations", None)
        if host_io_operations and callable(preflight_host_io):
            preflight_host_io(host_io_operations)
        producer_map = cad_graph_target_producers(spec)
        produced_target_bindings: dict[
            str, dict[tuple[str, str], CadTargetBinding]
        ] = {}
        compile_preflight = getattr(self.backend, "preflight_operations", None)
        if callable(compile_preflight):
            compile_preflight(
                [
                    operation
                    for operation in spec.operations
                    if not isinstance(operation, (ImportOperation, ExportOperation))
                ]
            )
        execute_bound = getattr(self.backend, "execute_bound_operation", None)
        bind_bound = getattr(self.backend, "bind_bound_operation", None)
        preflight_bound = getattr(self.backend, "preflight_bound_operations", None)
        has_mutations = any(
            not operation.kind.startswith("analysis.") for operation in spec.operations
        )
        if has_mutations and (
            not callable(execute_bound)
            or (not callable(bind_bound) and not callable(preflight_bound))
        ):
            raise AuthorityDeniedError(
                "backend cannot preserve just-in-time bound operation authority"
            )

        # Resolve every external target before the first mutation.  References
        # produced by this graph are deliberately omitted: they do not exist
        # yet and are resolved just in time after their producer succeeds.
        external_bindings: dict[str, dict[tuple[str, str], CadTargetBinding]] = {}
        document_operations = [
            operation
            for operation in spec.operations
            if not operation.kind.startswith("analysis.")
            and not isinstance(operation, ExportOperation)
        ]
        if document_operations:
            resolve_document = getattr(self.backend, "resolve_document_binding", None)
            if not callable(resolve_document):
                raise AuthorityDeniedError(
                    "backend cannot resolve lossless CAD document authority"
                )
            document_binding = await resolve_document()
            if not isinstance(document_binding, CadTargetBinding):
                raise AuthorityDeniedError(
                    "backend returned an invalid CAD document authority proof"
                )
            _validate_document_binding(document_binding)
            for operation in document_operations:
                requirements = cad_operation_target_requirements(operation)
                produced = producer_map.get(operation.id, {})
                external_requirements = tuple(
                    requirement
                    for requirement in requirements
                    if requirement not in produced
                )
                resolved = await _resolve_operation_target_bindings(
                    self.backend,
                    operation,
                    external_requirements,
                )
                external_bindings[operation.id] = dict(
                    zip(external_requirements, resolved, strict=True)
                )
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
                _validate_resolved_binding_batch(
                    (("export_target", str(operation.target_ref)),), (binding,)
                )
                external_bindings[operation.id] = {
                    ("export_target", str(operation.target_ref)): binding
                }

        resolved_session_id = session_id or f"cadspec-{uuid.uuid4().hex}"

        result = CapabilityExecutionResult(
            success=True,
            provider=self.backend.provider,
            required_capabilities=sorted(spec.capabilities),
            available_capabilities=sorted(self.backend.capabilities),
        )
        for operation in spec.operations:
            mutating = not operation.kind.startswith("analysis.")
            bound = None
            backend_returned = False
            backend_invoked = False
            try:
                if mutating:
                    if isinstance(operation, ExportOperation):
                        ordered_bindings = tuple(
                            external_bindings[operation.id].values()
                        )
                    else:
                        requirements = cad_operation_target_requirements(operation)
                        produced = producer_map.get(operation.id, {})
                        dynamic_requirements = tuple(
                            requirement
                            for requirement in requirements
                            if requirement in produced
                        )
                        dynamic_bindings = await _resolve_operation_target_bindings(
                            self.backend,
                            operation,
                            dynamic_requirements,
                        )
                        dynamic_by_requirement: dict[
                            tuple[str, str], CadTargetBinding
                        ] = {}
                        for requirement, binding in zip(
                            dynamic_requirements,
                            dynamic_bindings,
                            strict=True,
                        ):
                            producer_operation_id = produced[requirement]
                            producer_binding = produced_target_bindings.get(
                                producer_operation_id, {}
                            ).get(requirement)
                            if producer_binding is None:
                                raise AuthorityDeniedError(
                                    "CAD graph producer did not return the exact "
                                    "identity of its declared target"
                                )
                            if binding != producer_binding:
                                raise AuthorityDeniedError(
                                    "CAD graph produced target identity does not match "
                                    "the materialized consumer target"
                                )
                            dynamic_by_requirement[requirement] = replace(
                                binding,
                                producer_operation_id=producer_operation_id,
                            )
                        all_by_requirement = {
                            **external_bindings.get(operation.id, {}),
                            **dynamic_by_requirement,
                        }
                        ordered_bindings = (
                            document_binding,
                            *(all_by_requirement[item] for item in requirements),
                        )
                    bound = self.authority_broker.prepare_operation(
                        spec,
                        operation,
                        session_id=resolved_session_id,
                        provider=self.backend.provider,
                        target_bindings=ordered_bindings,
                    )
                    graph = PreparedOperationGraph(
                        spec_digest=bound.spec_digest,
                        session_id=bound.session_id,
                        provider=bound.provider,
                        operations=(bound,),
                    )
                    with _graph_request_context(graph):
                        if callable(bind_bound):
                            bind_bound(bound)
                        elif callable(preflight_bound):
                            preflight_bound([bound])
                    self.authority_broker.claim(bound)
                    backend_invoked = True
                    with _graph_request_context(graph):
                        payload = await execute_bound(bound)
                    backend_returned = True
                    produced_target_bindings[operation.id] = (
                        _returned_produced_target_bindings(operation, payload)
                    )
                    self.authority_broker.complete(bound, outcome="consumed")
                else:
                    backend_invoked = True
                    payload = await self.backend.execute_operation(operation)
            except Exception as exc:  # noqa: BLE001 - preserve typed transport evidence
                transport = _exception_transport(exc)
                if bound is not None and bound.capability is not None:
                    unknown = bool(
                        backend_returned
                        or transport.get("dispatched")
                        or transport.get("may_have_applied")
                        or transport.get("mutation_outcome") == "unknown"
                    )
                    self.authority_broker.fail(bound, outcome_unknown=unknown)
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
                    "native_result": _public_native_result(operation, payload),
                    "transport": transport,
                }
            )
            if operation.kind.startswith("analysis."):
                assertion_ids = [
                    assertion.id
                    for assertion in spec.assertions
                    if assertion.target_ref
                    in {
                        operation.id,
                        getattr(operation, "output_ref", None),
                    }
                ]
                result.evidence[operation.id] = _public_analysis_evidence(
                    operation,
                    payload,
                    assertion_ids=assertion_ids,
                    provider=self.backend.provider,
                )
        return result


async def _resolve_operation_target_bindings(
    backend: CapabilityBackend,
    operation: OperationSpec,
    requirements: tuple[tuple[str, str], ...],
) -> tuple[CadTargetBinding, ...]:
    if not requirements:
        return ()
    resolver = getattr(backend, "resolve_operation_target_bindings", None)
    if not callable(resolver):
        raise AuthorityDeniedError("backend cannot resolve exact CAD operation targets")
    candidate = await resolver(operation, requirements=requirements)
    if not isinstance(candidate, (tuple, list)) or not all(
        isinstance(binding, CadTargetBinding) for binding in candidate
    ):
        raise AuthorityDeniedError(
            "backend returned invalid CAD operation target proofs"
        )
    bindings = tuple(candidate)
    _validate_resolved_binding_batch(requirements, bindings)
    return bindings


def _validate_document_binding(binding: CadTargetBinding) -> None:
    _validate_resolved_binding_batch(
        (("active_document", "active_document"),), (binding,)
    )


def _validate_resolved_binding_batch(
    requirements: tuple[tuple[str, str], ...],
    bindings: tuple[CadTargetBinding, ...],
) -> None:
    if len(bindings) != len(requirements):
        raise AuthorityDeniedError(
            "backend returned an incomplete CAD operation target proof"
        )
    for requirement, binding in zip(requirements, bindings, strict=True):
        expected_kind, expected_ref = requirement
        kind_matches = (
            binding.reference_kind in {"parameter_existing", "parameter_absent"}
            if expected_kind == "parameter_target"
            else binding.reference_kind == expected_kind
        )
        if not kind_matches or binding.requested_ref != expected_ref:
            if expected_kind == "export_target":
                raise AuthorityDeniedError(
                    "CAD target binding does not match export reference"
                )
            raise AuthorityDeniedError(
                "CAD target binding does not match the requested reference"
            )
        if binding.producer_operation_id is not None:
            raise AuthorityDeniedError(
                "provider target proof cannot self-assert graph producer authority"
            )
        if not all(
            isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
            for value in (
                binding.document_identity,
                binding.entity_identity,
                binding.fingerprint,
            )
        ):
            raise AuthorityDeniedError("CAD target binding proof is invalid")


def _returned_produced_target_bindings(
    operation: OperationSpec,
    payload: dict[str, Any],
) -> dict[tuple[str, str], CadTargetBinding]:
    raw_bindings = payload.get("produced_target_bindings")
    if raw_bindings is None:
        return {}
    if not isinstance(raw_bindings, list):
        raise AuthorityDeniedError("backend returned invalid produced target proofs")

    allowed_fields = {
        "reference_kind",
        "requested_ref",
        "document_identity",
        "entity_identity",
        "fingerprint",
    }
    declared = set(cad_operation_produced_targets(operation))
    produced: dict[tuple[str, str], CadTargetBinding] = {}
    for raw in raw_bindings:
        if not isinstance(raw, dict) or set(raw) != allowed_fields:
            raise AuthorityDeniedError(
                "backend returned invalid produced target proofs"
            )
        binding = CadTargetBinding(
            reference_kind=raw["reference_kind"],
            requested_ref=raw["requested_ref"],
            document_identity=raw["document_identity"],
            entity_identity=raw["entity_identity"],
            fingerprint=raw["fingerprint"],
        )
        requirement = (binding.reference_kind, binding.requested_ref)
        _validate_resolved_binding_batch((requirement,), (binding,))
        if requirement not in declared:
            raise AuthorityDeniedError(
                "backend returned an undeclared produced target proof"
            )
        if requirement in produced:
            raise AuthorityDeniedError(
                "backend returned duplicate produced target proofs"
            )
        produced[requirement] = binding
    return produced


def _graph_request_context(
    graph: PreparedOperationGraph,
) -> AbstractContextManager[RequestContext | None]:
    """Bind the normalized spec and resolved CAD identity at provider boundaries."""

    current = current_request_context()
    if current is None:
        return nullcontext()
    document_identities = {
        binding.document_identity
        for operation in graph.operations
        for binding in operation.target_bindings
    }
    if len(document_identities) > 1:
        raise AuthorityDeniedError(
            "operation graph resolved more than one CAD document identity"
        )
    document_identity = (
        next(iter(document_identities))
        if document_identities
        else current.document_identity
    )
    capability_markers = tuple(
        f"operation_capability:{operation.capability.capability_id}"
        for operation in graph.operations
        if operation.capability is not None
    )
    bound_context: RequestContext = replace(
        current,
        session_id=graph.session_id,
        backend=graph.provider,
        document_identity=document_identity,
        spec_digest=graph.spec_digest,
        capabilities=tuple(dict.fromkeys((*current.capabilities, *capability_markers))),
    )
    return bind_request_context(bound_context)


def _public_native_result(
    operation: OperationSpec, payload: dict[str, Any]
) -> dict[str, Any]:
    """Project provider results without retaining paths or raw diagnostics."""

    projected: dict[str, Any] = {
        "completed": payload.get("success") is not False,
        "kind": operation.kind,
    }
    if isinstance(operation, ExportOperation):
        projected["format"] = operation.format
        export = payload.get("export")
        if isinstance(export, dict):
            size = export.get("bytes")
            if isinstance(size, int) and not isinstance(size, bool) and size >= 0:
                projected["bytes"] = size
    elif isinstance(operation, ImportOperation):
        projected["format"] = operation.format
        imported = payload.get("import")
        if isinstance(imported, dict):
            count = imported.get("entity_count")
            if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                projected["entity_count"] = count
    elif operation.kind.startswith("analysis."):
        projected["evidence_available"] = bool(payload)
    return projected


def _public_analysis_evidence(
    operation: OperationSpec,
    payload: dict[str, Any],
    *,
    assertion_ids: list[str],
    provider: str,
) -> dict[str, Any]:
    downstream = payload.get("evidence_envelope")
    downstream = downstream if isinstance(downstream, dict) else {}
    document_identity = downstream.get("document_identity")
    document_identity = document_identity if _is_sha256(document_identity) else None
    data, data_complete, metrics_finite, observed_count = _project_analysis_data(
        operation, payload
    )
    complete = bool(
        downstream.get("schema_version") == "fusion_agent.evidence.v1"
        and isinstance(downstream.get("producer"), str)
        and downstream.get("producer")
        and document_identity
        and downstream.get("complete") is True
        and downstream.get("counts_exact") is True
        and downstream.get("truncated") is False
        and downstream.get("stop_reason") in {None, "", "complete"}
        and downstream.get("metrics_finite") is True
        and downstream.get("observed_count") == observed_count
        and data_complete
        and metrics_finite
    )
    envelope = EvidenceEnvelope(
        producer="capability_executor",
        provenance={
            "provider": provider,
            "operation_id": operation.id,
            "operation_kind": operation.kind,
            "downstream_producer": str(downstream.get("producer") or "unavailable"),
        },
        document_identity=document_identity,
        complete=complete,
        counts_exact=complete,
        truncated=bool(downstream.get("truncated") is True),
        stop_reason=None if complete else "incomplete_provider_evidence",
        metrics_finite=metrics_finite,
        assertion_ids=assertion_ids,
        assertion_count=len(assertion_ids),
        evaluated_count=len(assertion_ids) if complete else 0,
    )
    return {"envelope": envelope.model_dump(mode="json"), "data": data}


def _project_analysis_data(
    operation: OperationSpec, payload: dict[str, Any]
) -> tuple[dict[str, Any], bool, bool, int]:
    if isinstance(operation, PhysicalPropertiesOperation):
        raw = payload.get("physical_properties")
        if not isinstance(raw, dict) or set(raw) != set(operation.target_refs):
            return {}, False, True, 0
        projected: dict[str, Any] = {}
        metrics_finite = True
        for target in operation.target_refs:
            value = raw.get(target)
            if not isinstance(value, dict) or not _is_sha256(
                value.get("entity_identity")
            ):
                return {}, False, True, 0
            record: dict[str, Any] = {"entity_identity": value["entity_identity"]}
            for field in ("mass_kg", "volume_mm3", "area_mm2"):
                number = value.get(field)
                if (
                    not isinstance(number, int | float)
                    or isinstance(number, bool)
                    or not math.isfinite(float(number))
                    or float(number) < 0
                ):
                    metrics_finite = False
                    break
                record[field] = float(number)
            if not metrics_finite:
                return {}, False, False, 0
            projected[target] = record
        return projected, True, True, len(projected)
    if isinstance(operation, InterferenceOperation):
        raw = payload.get("interference")
        if not isinstance(raw, dict):
            return {}, False, True, 0
        count = raw.get("count")
        pairs = raw.get("pairs")
        if (
            not isinstance(count, int)
            or isinstance(count, bool)
            or count < 0
            or not isinstance(pairs, list)
            or len(pairs) != count
        ):
            return {}, False, True, 0
        projected_pairs: list[dict[str, str]] = []
        for pair in pairs:
            if not isinstance(pair, dict) or set(pair) != {
                "a_identity",
                "b_identity",
            }:
                return {}, False, True, 0
            if not _is_sha256(pair.get("a_identity")) or not _is_sha256(
                pair.get("b_identity")
            ):
                return {}, False, True, 0
            projected_pairs.append(
                {
                    "a_identity": pair["a_identity"],
                    "b_identity": pair["b_identity"],
                }
            )
        return {"count": count, "pairs": projected_pairs}, True, True, count
    return {}, False, True, 0


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


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
