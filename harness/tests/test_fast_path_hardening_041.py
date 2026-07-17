from __future__ import annotations

import ast
import asyncio
import json
import math
from typing import Any

import pytest

from agent_core.fast_path import (
    FastPathService,
    GUARD_CONTROL_SCHEMA,
    _OperationCapabilityLedger,
    _component_query_id,
    _result_guard_rejection_reason,
    evaluate_verification,
    lint_fusion_script,
    validate_fast_execute_request,
)
from agent_core.request_context import RequestContext, bind_request_context
from fusion_mcp_adapter.tool_result import ToolResult


ADDITIVE_SCRIPT = """import adsk.core
import adsk.fusion

def run(_context: str):
    root = target_components["root"]
    root.bRepBodies.add(None)
"""

REBOUND_CALLABLE_SCRIPT = """import adsk.core

def run(_context: str):
    len = print
    len("provider-controlled callable")
"""


def _request(*, script: str = ADDITIVE_SCRIPT) -> dict[str, Any]:
    return {
        "intent": "Create Body1",
        "change_class": "additive",
        "script": script,
        "target_query_ids": ["body_target"],
        "verification": {
            "queries": [
                {
                    "id": "body_target",
                    "entity_type": "body",
                    "selector": {"component_path": "root", "name": "Body1"},
                    "fields": ["exists", "value"],
                }
            ],
            "assertions": [
                {
                    "id": "body_exists",
                    "query_id": "body_target",
                    "field": "exists",
                    "operator": "eq",
                    "expected": True,
                }
            ],
            "requirements": [
                {
                    "id": "body_created",
                    "assertion_ids": ["body_exists"],
                    "required": True,
                }
            ],
        },
    }


def _snapshot(
    applied: bool,
    *,
    complete: bool = True,
    ambiguous: bool = False,
    value: Any = 1.0,
    include_fingerprint: bool = True,
    document_id: str | None = "data-file-1",
    body_token: str = "body-1-token",
    state_fingerprint: str | None = None,
) -> dict[str, Any]:
    body = {
        "entity_type": "body",
        "name": "Body1",
        "entity_token": body_token,
        "exists": True,
        "value": value,
        "visible": True,
        "is_referenced_component": False,
        "occurrence_count_for_component": 1,
    }
    matches = [body] if applied else []
    if ambiguous and applied:
        matches.append({**body, "entity_token": "body-2-token"})
    summary: dict[str, Any] = {
        "components": 1,
        "occurrences": 0,
        "bodies": 1 if applied else 0,
        "state_fingerprint_truncated": not complete,
    }
    if include_fingerprint:
        summary["state_fingerprint"] = state_fingerprint or (
            "state-applied" if applied else "state-before"
        )
    return {
        "success": True,
        "complete": complete,
        "truncated": not complete,
        "counts_exact": complete,
        "stop_reason": None if complete else "response_limit",
        "document": {
            "name": "Untitled",
            "id": document_id or "",
            "runtime_id": f"data:{document_id}" if document_id else "",
        },
        "summary": summary,
        "results": [
            {
                "query_id": "body_target",
                "matches": matches,
                "ambiguous": ambiguous,
                "ambiguity_unknown": False,
                "truncated": not complete,
                "match_count": len(matches),
                "match_count_exact": complete,
            },
            {
                "query_id": _component_query_id("root"),
                "matches": [
                    {
                        "entity_type": "component",
                        "name": "root",
                        "path": "root",
                        "paths": ["root"],
                        "entity_token": "component-root-token",
                        "visible": True,
                        "is_referenced_component": False,
                        "occurrence_count_for_component": 0,
                    }
                ],
                "ambiguous": False,
                "ambiguity_unknown": False,
                "truncated": not complete,
                "match_count": 1,
                "match_count_exact": complete,
            },
        ],
        "warnings": [],
    }


class StatefulNative:
    def __init__(self) -> None:
        self.applied = False
        self.mutating_calls = 0
        self.update_calls = 0
        self.inspection_calls = 0
        self.active_probe_calls = 0
        self.active_command_readable = True
        self.busy_on_active_probe: int | None = None
        self.omit_first_fingerprint = False
        self.ambiguous_inspections: set[int] = set()
        self.incomplete_inspections: set[int] = set()
        self.numeric_values: dict[int, Any] = {}
        self.fingerprint_values: dict[int, str] = {}
        self.omit_query_ids: dict[int, set[str]] = {}
        self.fail_mutation_with: str | None = None
        self.update_outcome_unknown = False
        self.yield_active_probe = False
        self.document_id: str | None = "data-file-1"
        self.body_token = "body-1-token"
        self.state_fingerprint: str | None = None
        self.drift_on_active_probe: int | None = None
        self.drift_kind: str | None = None

    async def __call__(self, name, arguments, *, semantics, operation_id):
        del operation_id
        if name == "fusion_mcp_execute" and semantics == "read_only":
            script = arguments["object"]["script"]
            if "fusion_agent_active_command" in script:
                self.active_probe_calls += 1
                if self.yield_active_probe:
                    await asyncio.sleep(0)
                if self.active_probe_calls == self.drift_on_active_probe:
                    if self.drift_kind == "document":
                        self.document_id = "data-file-replaced"
                    elif self.drift_kind == "state":
                        self.state_fingerprint = "state-replaced"
                    elif self.drift_kind == "target":
                        self.body_token = "body-replaced-token"
                active_command = (
                    {"id": "Sketch", "isDefaultCommand": False}
                    if self.active_probe_calls == self.busy_on_active_probe
                    else None
                )
                return {
                    "ok": True,
                    "data": {
                        "message": json.dumps(
                            {
                                "success": self.active_command_readable,
                                "complete": self.active_command_readable,
                                "activeCommandRead": self.active_command_readable,
                                "activeCommand": active_command,
                            }
                        )
                    },
                }
            self.inspection_calls += 1
            payload = _snapshot(
                self.applied,
                complete=self.inspection_calls not in self.incomplete_inspections,
                ambiguous=self.inspection_calls in self.ambiguous_inspections,
                value=self.numeric_values.get(self.inspection_calls, 1.0),
                include_fingerprint=not (
                    self.omit_first_fingerprint and self.inspection_calls == 1
                ),
                document_id=self.document_id,
                body_token=self.body_token,
                state_fingerprint=self.state_fingerprint,
            )
            if self.inspection_calls in self.fingerprint_values:
                payload["summary"]["state_fingerprint"] = self.fingerprint_values[
                    self.inspection_calls
                ]
            omitted = self.omit_query_ids.get(self.inspection_calls, set())
            if omitted:
                payload["results"] = [
                    result
                    for result in payload["results"]
                    if result["query_id"] not in omitted
                ]
            return {"ok": True, "data": {"message": json.dumps(payload)}}
        if name == "fusion_mcp_execute" and semantics == "mutating":
            self.mutating_calls += 1
            self.applied = True
            if self.fail_mutation_with is not None:
                return {
                    "ok": False,
                    "error_code": "DOWNSTREAM_FAILURE",
                    "error_message": self.fail_mutation_with,
                    "data": {"dispatched": True},
                    "_meta": {
                        "fusion_agent_transport": {
                            "dispatched": True,
                            "mutation_outcome": "known",
                            "post_dispatch_replay_suppressed": True,
                        }
                    },
                }
            return {
                "ok": True,
                "data": {"success": True},
                "_meta": {
                    "fusion_agent_transport": {
                        "dispatched": True,
                        "mutation_outcome": "known",
                        "post_dispatch_replay_suppressed": True,
                    }
                },
            }
        if name == "fusion_mcp_update":
            self.update_calls += 1
            await asyncio.sleep(0)
            self.applied = arguments["featureType"] == "redo"
            if self.update_outcome_unknown:
                return {
                    "ok": False,
                    "error_code": "MUTATION_OUTCOME_UNKNOWN",
                    "error_message": "transport disconnected after dispatch",
                    "data": {"dispatched": True},
                    "_meta": {
                        "fusion_agent_transport": {
                            "dispatched": True,
                            "mutation_outcome": "unknown",
                            "post_dispatch_replay_suppressed": True,
                        }
                    },
                }
            return {
                "ok": True,
                "data": {"success": True},
                "_meta": {
                    "fusion_agent_transport": {
                        "dispatched": True,
                        "mutation_outcome": "known",
                        "post_dispatch_replay_suppressed": True,
                    }
                },
            }
        raise AssertionError((name, arguments, semantics))


async def _applied_service(native: StatefulNative) -> tuple[FastPathService, str]:
    service = FastPathService(native)
    response = await service.fast_execute(_request())
    assert response.payload["status"] == "applied_verified"
    return service, str(response.payload["operation_id"])


def _recovery_request(operation_id: str) -> dict[str, Any]:
    return {
        "action": "undo",
        "operation_id": operation_id,
        "confirm": True,
        "verification": {
            "queries": [
                {
                    "id": "body_target",
                    "entity_type": "body",
                    "selector": {"component_path": "root", "name": "Body1"},
                    "fields": ["exists"],
                }
            ],
            "assertions": [
                {
                    "id": "body_removed",
                    "query_id": "body_target",
                    "field": "exists",
                    "operator": "eq",
                    "expected": False,
                }
            ],
        },
    }


def _recovery_context(owner: str) -> RequestContext:
    return RequestContext(
        request_id=f"request-{owner}",
        session_id=f"session-{owner}",
        trial_id=f"trial-{owner}",
        profile="advanced",
        mode="mock",
        backend="mock",
        document_identity="data:data-file-1",
        capabilities=("fast_path:enabled", "execution_path:native_fast"),
    )


def _benchmark_fixture_context(
    *,
    document_identity: str | None = "data:data-file-1",
    fixture_capabilities: tuple[str, ...] = ("benchmark_fixture:" + "a" * 64,),
) -> RequestContext:
    return RequestContext(
        request_id="reference:trial-fixture:initial",
        session_id="reference-run",
        trial_id="trial-fixture",
        profile="advanced",
        mode="real",
        backend="autodesk_http",
        document_identity=document_identity,
        capabilities=(
            "fast_path:enabled",
            "execution_path:native_fast",
            *fixture_capabilities,
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("active_document_id", "reason"),
    [
        ("replacement-data-file", "request_document_identity_mismatch"),
        (None, "request_document_identity_unavailable"),
    ],
)
async def test_benchmark_document_swap_blocks_before_any_mutating_transport(
    active_document_id: str | None,
    reason: str,
) -> None:
    native = StatefulNative()
    native.document_id = active_document_id
    service = FastPathService(native)

    with bind_request_context(_benchmark_fixture_context()):
        response = await service.fast_execute(_request())

    assert response.payload["status"] == "blocked_before_apply"
    assert response.payload["reason"] == reason
    assert response.payload["transport_mutating_dispatch_count"] == 0
    assert response.payload["mutating_call_count"] == 0
    assert native.mutating_calls == 0


@pytest.mark.asyncio
async def test_bound_benchmark_fixture_control_dispatches_once() -> None:
    native = StatefulNative()
    service = FastPathService(native)

    with bind_request_context(_benchmark_fixture_context()):
        response = await service.fast_execute(_request())

    assert response.payload["status"] == "applied_verified"
    assert response.payload["transport_mutating_dispatch_count"] == 1
    assert native.mutating_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("context", "reason"),
    [
        (
            _benchmark_fixture_context(document_identity=None),
            "benchmark_fixture_document_identity_missing",
        ),
        (
            _benchmark_fixture_context(
                fixture_capabilities=(
                    "benchmark_fixture:" + "a" * 64,
                    "benchmark_fixture:" + "b" * 64,
                )
            ),
            "benchmark_fixture_capability_invalid",
        ),
        (
            _benchmark_fixture_context(
                fixture_capabilities=("benchmark_fixture:not-a-digest",)
            ),
            "benchmark_fixture_capability_invalid",
        ),
    ],
)
async def test_malformed_benchmark_fixture_context_is_zero_dispatch(
    context: RequestContext,
    reason: str,
) -> None:
    native = StatefulNative()
    service = FastPathService(native)

    with bind_request_context(context):
        response = await service.fast_execute(_request())

    assert response.payload["status"] == "blocked_before_apply"
    assert response.payload["reason"] == reason
    assert response.payload["transport_mutating_dispatch_count"] == 0
    assert response.payload["mutating_call_count"] == 0
    assert native.mutating_calls == 0


def test_approved_builtin_name_rebinding_is_rejected_but_real_builtins_remain_valid() -> (
    None
):
    rebound = lint_fusion_script(REBOUND_CALLABLE_SCRIPT, "read_only")
    pattern_rebound = lint_fusion_script(
        """import adsk.core

def run(_context: str):
    match [print]:
        case [len]:
            len("pattern-bound callable")
""",
        "read_only",
    )
    legitimate = lint_fusion_script(
        """import adsk.core

def run(_context: str):
    values = [1, 2]
    print(len(values), sum(values))
""",
        "read_only",
    )

    assert rebound.allowed is False
    assert any("callable" in error and "shadow" in error for error in rebound.errors)
    assert pattern_rebound.allowed is False
    assert any(
        "callable" in error and "shadow" in error for error in pattern_rebound.errors
    )
    assert legitimate.allowed is True


def test_operation_capability_is_exactly_bound_and_single_use(monkeypatch) -> None:
    ledger = _OperationCapabilityLedger()
    rejected = ledger.issue(
        operation_id="op-a",
        document_binding="data:doc-a",
        target_binding_digest="targets-a",
        baseline_fingerprint="baseline-a",
        script_sha256="script-a",
    )

    claimed, reason = ledger.claim(
        rejected.capability_id,
        operation_id="op-a",
        document_binding="data:doc-a",
        target_binding_digest="targets-a",
        baseline_fingerprint="baseline-a",
        script_sha256="script-b",
    )

    assert claimed is False
    assert reason == "capability_binding_mismatch"
    assert ledger.state(rejected.capability_id) == "revoked"

    accepted = ledger.issue(
        operation_id="op-b",
        document_binding="data:doc-b",
        target_binding_digest="targets-b",
        baseline_fingerprint="baseline-b",
        script_sha256="script-b",
    )
    first, _ = ledger.claim(
        accepted.capability_id,
        operation_id="op-b",
        document_binding="data:doc-b",
        target_binding_digest="targets-b",
        baseline_fingerprint="baseline-b",
        script_sha256="script-b",
    )
    replay, replay_reason = ledger.claim(
        accepted.capability_id,
        operation_id="op-b",
        document_binding="data:doc-b",
        target_binding_digest="targets-b",
        baseline_fingerprint="baseline-b",
        script_sha256="script-b",
    )

    assert first is True
    assert replay is False
    assert replay_reason == "capability_claimed"
    ledger.finish(accepted.capability_id, "consumed")
    assert ledger.state(accepted.capability_id) == "consumed"

    clock = [100.0]
    monkeypatch.setattr("agent_core.fast_path.time.monotonic", lambda: clock[0])
    expiring = _OperationCapabilityLedger(ttl_seconds=0.5)
    expired = expiring.issue(
        operation_id="op-expired",
        document_binding="data:doc-expired",
        target_binding_digest="targets-expired",
        baseline_fingerprint="baseline-expired",
        script_sha256="script-expired",
    )
    clock[0] += 0.6
    expired_claim, expired_reason = expiring.claim(
        expired.capability_id,
        operation_id="op-expired",
        document_binding="data:doc-expired",
        target_binding_digest="targets-expired",
        baseline_fingerprint="baseline-expired",
        script_sha256="script-expired",
    )
    assert expired_claim is False
    assert expired_reason == "capability_expired"
    assert expiring.state(expired.capability_id) == "expired"


@pytest.mark.asyncio
async def test_rebound_approved_callable_is_blocked_with_zero_dispatch() -> None:
    native = StatefulNative()
    response = await FastPathService(native).fast_execute(
        {
            "intent": "Read with a rebound callable",
            "change_class": "read_only",
            "script": REBOUND_CALLABLE_SCRIPT,
        }
    )

    assert response.payload["status"] == "blocked_before_apply"
    assert native.mutating_calls == 0
    assert native.inspection_calls == 0


@pytest.mark.asyncio
async def test_unreadable_active_command_state_blocks_with_zero_dispatch() -> None:
    native = StatefulNative()
    native.active_command_readable = False

    response = await FastPathService(native).fast_execute(_request())

    assert response.payload["status"] == "blocked_before_apply"
    assert response.payload["reason"] == "active_command_check_failed"
    assert native.mutating_calls == 0
    assert native.inspection_calls == 0


@pytest.mark.asyncio
async def test_malformed_active_command_evidence_blocks_with_zero_dispatch() -> None:
    class MalformedActiveCommandNative(StatefulNative):
        async def __call__(self, name, arguments, *, semantics, operation_id):
            if (
                name == "fusion_mcp_execute"
                and semantics == "read_only"
                and "fusion_agent_active_command" in arguments["object"]["script"]
            ):
                return {
                    "ok": True,
                    "data": {
                        "message": json.dumps(
                            {
                                "success": True,
                                "complete": True,
                                "activeCommandRead": True,
                                "activeCommand": {},
                            }
                        )
                    },
                }
            return await super().__call__(
                name,
                arguments,
                semantics=semantics,
                operation_id=operation_id,
            )

    native = MalformedActiveCommandNative()
    response = await FastPathService(native).fast_execute(_request())

    assert response.payload["status"] == "blocked_before_apply"
    assert response.payload["reason"] == "active_command_check_failed"
    assert native.mutating_calls == 0
    assert native.inspection_calls == 0


@pytest.mark.asyncio
async def test_explicit_clear_active_command_allows_legitimate_dispatch() -> None:
    native = StatefulNative()

    response = await FastPathService(native).fast_execute(_request())

    assert response.payload["status"] == "applied_verified"
    assert response.payload["operation_capability_state"] == "consumed"
    assert native.inspection_calls == 3
    assert native.mutating_calls == 1


@pytest.mark.parametrize(
    "assertion",
    [
        {
            "query_id": "body_target",
            "field": "value",
            "operator": "eq",
            "expected": math.nan,
        },
        {
            "query_id": "body_target",
            "field": "value",
            "operator": "approx",
            "expected": 1.0,
            "tolerance": -0.1,
        },
        {
            "query_id": "body_target",
            "field": "value",
            "operator": "approx",
            "expected": 1.0,
            "tolerance": math.inf,
        },
        {
            "query_id": "body_target",
            "field": "value",
            "operator": "approx",
            "expected": 1.0,
            "tolerance": True,
        },
        {
            "query_id": "body_target",
            "field": "value",
            "operator": "gte",
            "expected": True,
        },
    ],
)
def test_assertion_numeric_contract_rejects_invalid_request_values(
    assertion: dict[str, Any],
) -> None:
    request = _request()
    request["verification"]["assertions"] = [{"id": "body_exists", **assertion}]

    with pytest.raises(ValueError, match="finite|numeric|tolerance"):
        validate_fast_execute_request(request)


@pytest.mark.parametrize("actual", [math.nan, math.inf, -math.inf, True])
def test_invalid_numeric_readback_is_incomplete_and_never_contract_verified(
    actual: Any,
) -> None:
    baseline = _snapshot(True, value=1.0)
    after = _snapshot(True, value=actual)
    assertion = {
        "id": "numeric_value",
        "query_id": "body_target",
        "field": "value",
        "operator": "approx",
        "expected": 1.0,
        "tolerance": 0.01,
    }

    result = evaluate_verification(
        baseline,
        after,
        [assertion],
        "scoped_update",
        [{"id": "value_verified", "assertion_ids": ["numeric_value"]}],
    )

    assert result["passed"] is False
    assert result["assertion_status"] == "incomplete"
    assert result["reason_code"] == "INVALID_NUMERIC_EVIDENCE"
    assert result["contract_verified"] is False


def test_missing_after_field_cannot_compare_equal_to_null() -> None:
    baseline = _snapshot(True)
    after = _snapshot(True)
    after["results"][0]["matches"][0].pop("value")

    result = evaluate_verification(
        baseline,
        after,
        [
            {
                "id": "missing_value",
                "query_id": "body_target",
                "field": "value",
                "operator": "eq",
                "expected": None,
            }
        ],
        "scoped_update",
        [{"id": "value_verified", "assertion_ids": ["missing_value"]}],
    )

    assert result["passed"] is False
    assert result["assertion_status"] == "incomplete"
    assert result["reason_code"] == "INCOMPLETE_INSPECTION"
    assert result["contract_verified"] is False


@pytest.mark.asyncio
async def test_mutation_without_bound_baseline_fingerprint_has_zero_dispatch() -> None:
    native = StatefulNative()
    native.omit_first_fingerprint = True

    response = await FastPathService(native).fast_execute(_request())

    assert response.payload["status"] == "blocked_before_apply"
    assert response.payload["reason"] == "incomplete_baseline"
    assert response.payload["baseline_issue"] == "state_fingerprint_unavailable"
    assert response.payload["transport_mutating_dispatch_count"] == 0
    assert native.mutating_calls == 0


@pytest.mark.asyncio
async def test_mutation_with_omitted_declared_query_has_zero_dispatch() -> None:
    native = StatefulNative()
    native.omit_query_ids[1] = {"body_target"}

    response = await FastPathService(native).fast_execute(_request())

    assert response.payload["status"] == "blocked_before_apply"
    assert response.payload["reason"] == "incomplete_baseline"
    assert response.payload["baseline_issue"] == "query_result_missing:body_target"
    assert response.payload["transport_mutating_dispatch_count"] == 0
    assert native.mutating_calls == 0


@pytest.mark.asyncio
async def test_preview_drift_immediately_before_sink_has_zero_dispatch() -> None:
    native = StatefulNative()
    native.fingerprint_values[2] = "state-drifted-before-dispatch"

    response = await FastPathService(native).fast_execute(_request())

    assert response.payload["status"] == "blocked_before_apply"
    assert response.payload["reason"] == "preview_binding_drift"
    assert response.payload["transport_mutating_dispatch_count"] == 0
    assert native.inspection_calls == 2
    assert native.mutating_calls == 0


@pytest.mark.asyncio
async def test_active_command_started_after_preview_has_zero_dispatch() -> None:
    native = StatefulNative()
    native.busy_on_active_probe = 2

    response = await FastPathService(native).fast_execute(_request())

    assert response.payload["status"] == "blocked_before_apply"
    assert response.payload["reason"] == "active_command_changed_before_dispatch"
    assert response.payload["transport_mutating_dispatch_count"] == 0
    assert native.inspection_calls == 2
    assert native.mutating_calls == 0


@pytest.mark.asyncio
async def test_sink_guard_rechecks_fingerprint_after_final_active_probe() -> None:
    class SinkGuardNative(StatefulNative):
        def __init__(self) -> None:
            super().__init__()
            self.captured_script = ""

        async def __call__(self, name, arguments, *, semantics, operation_id):
            if name == "fusion_mcp_execute" and semantics == "mutating":
                del operation_id
                self.mutating_calls += 1
                self.captured_script = arguments["object"]["script"]
                assignments = {
                    node.targets[0].id: ast.literal_eval(node.value)
                    for node in ast.walk(ast.parse(self.captured_script))
                    if isinstance(node, ast.Assign)
                    and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id
                    in {
                        "_fusion_agent_guard_token",
                        "_fusion_agent_guard_binding_digest",
                    }
                }
                control = {
                    "fusion_agent_guard": {
                        "schema": GUARD_CONTROL_SCHEMA,
                        "token": assignments["_fusion_agent_guard_token"],
                        "binding_digest": assignments[
                            "_fusion_agent_guard_binding_digest"
                        ],
                        "status": "rejected_before_apply",
                        "reason_code": "SINK_STATE_FINGERPRINT_CHANGED",
                    }
                }
                return ToolResult.from_mcp(
                    {
                        "content": [{"type": "text", "text": json.dumps(control)}],
                        "_meta": {
                            "fusion_agent_transport": {
                                "dispatched": True,
                                "mutation_outcome": "known",
                                "post_dispatch_replay_suppressed": True,
                            }
                        },
                    }
                )
            return await super().__call__(
                name,
                arguments,
                semantics=semantics,
                operation_id=operation_id,
            )

    native = SinkGuardNative()
    native.drift_on_active_probe = 2
    native.drift_kind = "state"
    service = FastPathService(native)

    response = await service.fast_execute(_request())

    assert response.payload["status"] == "blocked_before_apply"
    assert response.payload["verification"]["source"] == "sink_state_guard"
    assert response.payload["transport_mutating_dispatch_count"] == 1
    assert response.payload["mutating_call_count"] == 0
    assert response.payload["operation_capability_state"] == "revoked"
    assert response.payload["execution"]["ok"] is False
    assert response.payload["execution"]["guard_rejected"] is True
    assert (
        response.payload["execution"]["guard_reason_code"]
        == "SINK_STATE_FINGERPRINT_CHANGED"
    )
    assert native.applied is False
    assert '_expected_state_fingerprint = "state-before"' in native.captured_script
    assert "b85decode" in native.captured_script
    assert "findEntityByToken" in native.captured_script
    assert "SINK_STATE_FINGERPRINT_CHANGED" in native.captured_script
    assert response.payload["operation_id"] not in service._recovery_records


def test_guard_control_survives_sanitization_but_requires_exact_operation_binding() -> (
    None
):
    control = {
        "fusion_agent_guard": {
            "schema": GUARD_CONTROL_SCHEMA,
            "token": "a" * 32,
            "binding_digest": "b" * 64,
            "status": "rejected_before_apply",
            "reason_code": "SINK_STATE_FINGERPRINT_CHANGED",
        }
    }
    normalized = ToolResult.from_mcp(
        {"content": [{"type": "text", "text": json.dumps(control)}]}
    )

    assert (
        _result_guard_rejection_reason(
            normalized,
            expected_token="a" * 32,
            expected_binding_digest="b" * 64,
        )
        == "SINK_STATE_FINGERPRINT_CHANGED"
    )
    assert (
        _result_guard_rejection_reason(
            normalized,
            expected_token="c" * 32,
            expected_binding_digest="b" * 64,
        )
        is None
    )
    assert (
        _result_guard_rejection_reason(
            normalized,
            expected_token="a" * 32,
            expected_binding_digest="d" * 64,
        )
        is None
    )

    sanitized_error = ToolResult.from_mcp(
        {
            "isError": True,
            "content": [
                {
                    "type": "text",
                    "text": "Fusion Agent sink guard: state fingerprint changed",
                }
            ],
        }
    )
    assert sanitized_error.error_code == "FUSION_OPERATION_FAILED"
    assert (
        _result_guard_rejection_reason(
            sanitized_error,
            expected_token="a" * 32,
            expected_binding_digest="b" * 64,
        )
        is None
    )


@pytest.mark.asyncio
async def test_ambiguous_after_state_never_promotes_contract_verified() -> None:
    native = StatefulNative()
    native.ambiguous_inspections.add(3)

    response = await FastPathService(native).fast_execute(_request())

    assert native.mutating_calls == 1
    assert response.payload["status"] == "applied_unverified"
    assert response.payload["verification"]["assertion_status"] == "incomplete"
    assert response.payload["verification"]["contract_verified"] is False
    assert response.payload["verification"]["readback_issue"].startswith(
        "query_ambiguous:body_target"
    )


@pytest.mark.asyncio
async def test_partial_change_is_error_and_drops_raw_downstream_error() -> None:
    canary = "authorization=Bearer FAST_PATH_SECRET C:\\private\\design.f3d"
    native = StatefulNative()
    native.fail_mutation_with = canary

    response = await FastPathService(native).fast_execute(_request())

    encoded = json.dumps(response.payload, sort_keys=True)
    assert response.payload["status"] == "partial_change_detected"
    assert response.is_error is True
    assert canary not in encoded
    assert "FAST_PATH_SECRET" not in encoded
    assert "C:\\\\private" not in encoded
    assert response.payload["execution"]["error"] == (
        "The downstream Fusion operation failed."
    )


@pytest.mark.asyncio
async def test_incomplete_recovery_baseline_revokes_claim_with_zero_update_dispatch() -> (
    None
):
    native = StatefulNative()
    service, operation_id = await _applied_service(native)
    native.incomplete_inspections.add(5)

    response = await service.recover_change(_recovery_request(operation_id))

    assert response.payload["status"] == "blocked_before_apply"
    assert response.payload["reason"] == "recovery_verification_baseline_incomplete"
    assert native.update_calls == 0


@pytest.mark.asyncio
async def test_recovery_owner_mismatch_has_zero_dispatch_and_does_not_consume_owner() -> (
    None
):
    native = StatefulNative()
    owner_a = _recovery_context("a")
    owner_b = _recovery_context("b")
    with bind_request_context(owner_a):
        service, operation_id = await _applied_service(native)

    with bind_request_context(owner_b):
        blocked = await service.recover_change(_recovery_request(operation_id))

    assert blocked.payload["status"] == "blocked_before_apply"
    assert blocked.payload["reason"] == "recovery_owner_mismatch"
    assert native.update_calls == 0

    with bind_request_context(owner_a):
        recovered = await service.recover_change(_recovery_request(operation_id))

    assert recovered.payload["status"] == "recovered_verified"
    assert native.update_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("drift_kind", ["document", "state", "target"])
async def test_recovery_revalidates_binding_after_last_active_command_probe(
    drift_kind: str,
) -> None:
    native = StatefulNative()
    service, operation_id = await _applied_service(native)
    native.drift_kind = drift_kind
    native.drift_on_active_probe = native.active_probe_calls + 2

    response = await service.recover_change(_recovery_request(operation_id))

    assert response.payload["status"] == "blocked_before_apply"
    assert response.payload["reason"] == "recovery_final_binding_drift"
    assert response.payload["transport_mutating_dispatch_count"] == 0
    assert response.payload["mutating_call_count"] == 0
    assert native.update_calls == 0


@pytest.mark.asyncio
async def test_incomplete_recovery_after_state_is_never_recovered_verified() -> None:
    native = StatefulNative()
    service, operation_id = await _applied_service(native)
    native.incomplete_inspections.add(7)

    response = await service.recover_change(_recovery_request(operation_id))

    assert native.update_calls == 1
    assert response.payload["status"] == "recovery_unverified"
    assert response.payload["verification"]["assertion_status"] == "incomplete"
    assert response.payload["verification"]["contract_verified"] is False
    assert response.is_error is True


@pytest.mark.asyncio
async def test_ambiguous_recovery_after_state_is_never_recovered_verified() -> None:
    native = StatefulNative()
    service, operation_id = await _applied_service(native)
    native.ambiguous_inspections.add(7)

    response = await service.recover_change(_recovery_request(operation_id))

    assert native.update_calls == 1
    assert response.payload["status"] == "recovery_unverified"
    assert response.payload["verification"]["assertion_status"] == "incomplete"
    assert response.payload["verification"]["contract_verified"] is False
    assert response.payload["verification"]["readback_issue"].startswith(
        "query_ambiguous:body_target"
    )


@pytest.mark.asyncio
async def test_legitimate_complete_exact_recovery_remains_available() -> None:
    native = StatefulNative()
    service, operation_id = await _applied_service(native)

    response = await service.recover_change(_recovery_request(operation_id))

    assert response.payload["status"] == "recovered_verified"
    assert response.payload["verification"]["assertion_status"] == "passed"
    assert response.payload["verification"]["contract_verified"] is True
    assert native.update_calls == 1


@pytest.mark.asyncio
async def test_concurrent_recovery_claim_is_single_use_and_dispatches_once() -> None:
    native = StatefulNative()
    service, operation_id = await _applied_service(native)
    native.yield_active_probe = True

    responses = await asyncio.gather(
        service.recover_change(_recovery_request(operation_id)),
        service.recover_change(_recovery_request(operation_id)),
    )

    assert native.update_calls == 1
    assert sorted(response.payload["status"] for response in responses) == [
        "blocked_before_apply",
        "recovered_verified",
    ]
    blocked = next(
        response
        for response in responses
        if response.payload["status"] == "blocked_before_apply"
    )
    assert blocked.payload["reason"] == "recovery_claim_unavailable"


@pytest.mark.asyncio
async def test_unknown_recovery_outcome_is_single_use_and_never_replayed() -> None:
    native = StatefulNative()
    service, operation_id = await _applied_service(native)
    native.update_outcome_unknown = True

    first = await service.recover_change(_recovery_request(operation_id))
    second = await service.recover_change(_recovery_request(operation_id))

    assert first.payload["status"] == "outcome_unknown"
    assert first.is_error is True
    assert second.payload["status"] == "blocked_before_apply"
    assert second.payload["reason"] == "recovery_claim_unavailable"
    assert second.payload["claim_state"] == "unknown"
    assert native.update_calls == 1
