from __future__ import annotations

import asyncio
import math
import os
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from agent_core.repair_loop import RepairLoop
from agent_core.request_context import (
    RequestContext,
    bind_request_context,
    current_request_context,
)
from benchmark.runner import enforce_route_lock, route_lock
from cad_spec.models import AcceptanceTestSpec, CadSpec, ComponentSpec
from verifier.geometry import GeometryVerifier
from verifier.result_models import (
    DecisionReasonCode,
    DecisionStatus,
    EvidenceEnvelope,
    FailureCode,
    VerificationIssue,
    VerificationResult,
)


def _spec(acceptance: AcceptanceTestSpec) -> CadSpec:
    return CadSpec(
        intent="typed verifier regression",
        parameters=[],
        components=[ComponentSpec(name="typed_component", features=[])],
        acceptance_tests=[acceptance],
    )


class _Facade:
    def __init__(
        self, payload: dict, *, bounding_box: list[float] | None = None
    ) -> None:
        self.payload = payload
        self.bounding_box = bounding_box or [1.0, 1.0, 1.0]
        self.inspect_calls = 0

    async def inspect_design(self) -> dict:
        self.inspect_calls += 1
        return self.payload

    async def measure_bounding_box(self, _target: str | None = None) -> list[float]:
        return self.bounding_box


def _complete_payload() -> dict:
    return {
        "state": {
            "active_document": True,
            "units": "mm",
            "components": {"root": {}},
            "bodies": {},
            "parameters": {},
        },
        "complete": True,
        "counts_exact": True,
        "truncated": False,
        "stop_reason": "complete",
        "producer": "typed-test",
        "document_identity": "doc:test",
    }


@pytest.mark.asyncio
async def test_unknown_assertion_fails_closed_before_inspection() -> None:
    facade = _Facade(_complete_payload())
    result = await GeometryVerifier(facade).verify(
        _spec(AcceptanceTestSpec(type="future_assertion_without_handler"))
    )

    assert facade.inspect_calls == 0
    assert result.status is DecisionStatus.FAILED
    assert result.passed is False
    assert result.reason_codes == [DecisionReasonCode.UNSUPPORTED_ASSERTION]
    assert result.issues[0].code is FailureCode.UNSUPPORTED_ASSERTION


@pytest.mark.asyncio
async def test_incomplete_inspection_is_distinct_and_never_passes() -> None:
    payload = _complete_payload()
    payload.update(
        complete=False,
        counts_exact=False,
        truncated=True,
        stop_reason="max_entities_visited",
    )
    result = await GeometryVerifier(_Facade(payload)).verify(
        _spec(AcceptanceTestSpec(type="body_count", target=0))
    )

    assert result.status is DecisionStatus.INCOMPLETE
    assert result.passed is False
    assert result.reason_codes == [DecisionReasonCode.INCOMPLETE_INSPECTION]
    assert result.evidence is not None
    assert result.evidence.complete is False
    assert result.decision is not None
    assert result.decision.evidence_sha256 == result.evidence.sha256()


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, True])
def test_bbox_and_tolerance_reject_non_finite_or_boolean_numbers(value: float) -> None:
    with pytest.raises(ValidationError):
        AcceptanceTestSpec(type="bounding_box", target_mm=[value, 1.0, 1.0])
    with pytest.raises(ValidationError):
        AcceptanceTestSpec(
            type="bounding_box", target_mm=[1.0, 1.0, 1.0], tolerance_mm=value
        )


def test_tolerance_rejects_negative_values() -> None:
    with pytest.raises(ValidationError):
        AcceptanceTestSpec(
            type="bounding_box", target_mm=[1.0, 1.0, 1.0], tolerance_mm=-0.01
        )


@pytest.mark.asyncio
async def test_non_finite_measured_bbox_is_invalid_evidence_not_success() -> None:
    result = await GeometryVerifier(
        _Facade(_complete_payload(), bounding_box=[math.nan, 1.0, 1.0])
    ).verify(_spec(AcceptanceTestSpec(type="bounding_box", target_mm=[1.0, 1.0, 1.0])))

    assert result.status is DecisionStatus.INCOMPLETE
    assert result.reason_codes == [DecisionReasonCode.INVALID_NUMERIC_EVIDENCE]
    assert result.issues[0].code is FailureCode.INVALID_NUMERIC_EVIDENCE


@pytest.mark.asyncio
async def test_repair_loop_never_mutates_after_incomplete_verdict() -> None:
    incomplete = VerificationResult.incomplete_result(
        evidence=EvidenceEnvelope(
            producer="test",
            complete=False,
            counts_exact=False,
            truncated=True,
            stop_reason="deadline_ms",
            assertion_ids=["component_count"],
            assertion_count=1,
            evaluated_count=0,
        ),
        issues=[
            VerificationIssue(
                code=FailureCode.WRONG_ACTIVE_COMPONENT,
                message="inconclusive component state",
            )
        ],
    )

    class _Verifier:
        async def verify(self, _spec: CadSpec) -> VerificationResult:
            return incomplete

    class _Executor:
        calls = 0

        async def activate_component(self, _target: str) -> bool:
            self.calls += 1
            return True

    executor = _Executor()
    loop = RepairLoop(_Verifier(), executor=executor)
    result = await loop.run(_spec(AcceptanceTestSpec(type="component_count", target=1)))
    assert result.status is DecisionStatus.INCOMPLETE
    assert executor.calls == 0
    assert loop.attempts == []


@pytest.mark.asyncio
async def test_request_and_route_contexts_are_task_local_and_do_not_mutate_environment() -> (
    None
):
    before = {
        "FUSION_AGENT_BENCHMARK_ROUTE_LOCK": os.environ.get(
            "FUSION_AGENT_BENCHMARK_ROUTE_LOCK"
        ),
        "FUSION_AGENT_EXECUTION_PATH": os.environ.get("FUSION_AGENT_EXECUTION_PATH"),
        "FUSION_AGENT_BENCHMARK_TRIAL_ID": os.environ.get(
            "FUSION_AGENT_BENCHMARK_TRIAL_ID"
        ),
    }
    entered = asyncio.Event()
    release = asyncio.Event()
    observed: dict[str, tuple[str, str]] = {}

    async def worker(name: str, path: str) -> None:
        context = RequestContext(
            request_id=f"request-{name}",
            session_id=f"session-{name}",
            trial_id=f"trial-{name}",
            profile="benchmark",
            mode="mock",
            backend="internal",
            document_identity=f"document-{name}",
            spec_digest="a" * 64,
            timeouts={"trial": 30.0},
            capabilities=(f"benchmark:{path}",),
        )
        with (
            bind_request_context(context),
            route_lock(path, SimpleNamespace(trial_id=f"trial-{name}")),
        ):
            if name == "a":
                entered.set()
                await release.wait()
            else:
                await entered.wait()
                release.set()
            await asyncio.sleep(0)
            enforce_route_lock(path)
            active = current_request_context()
            assert active is not None
            observed[name] = (active.request_id, active.trial_id or "")

    await asyncio.gather(worker("a", "safe_harness"), worker("b", "native_fast"))
    assert observed == {
        "a": ("request-a", "trial-a"),
        "b": ("request-b", "trial-b"),
    }
    assert current_request_context() is None
    assert {name: os.environ.get(name) for name in before} == before


def test_request_context_is_immutable_and_copies_mutable_inputs() -> None:
    source = {"trial": 15.0}
    context = RequestContext(
        request_id="request-immutable",
        profile="normal",
        mode="real",
        backend="autodesk",
        timeouts=source,
    )
    source["trial"] = 999.0
    assert context.timeouts["trial"] == 15.0
    with pytest.raises((AttributeError, TypeError)):
        context.request_id = "changed"  # type: ignore[misc]
