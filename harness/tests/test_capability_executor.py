from __future__ import annotations

import hashlib
import json

import pytest

from agent_core.authority import CadTargetBinding
from agent_core.capability_executor import CapabilityExecutor
from agent_core.request_context import (
    RequestContext,
    bind_request_context,
    current_request_context,
)
from cad_spec.v2 import CadSpecV2


def _spec() -> CadSpecV2:
    return CadSpecV2.model_validate(
        {
            "cad_spec_version": "2.0",
            "intent": "Measure a component",
            "requirements": [
                {
                    "id": "mass_recorded",
                    "description": "Mass is recorded",
                    "assertion_ids": ["mass_range"],
                }
            ],
            "operations": [
                {
                    "id": "measure_mass",
                    "kind": "analysis.physical_properties",
                    "target_refs": ["part"],
                    "output_ref": "mass_report",
                    "requirement_ids": ["mass_recorded"],
                }
            ],
            "assertions": [
                {
                    "id": "mass_range",
                    "kind": "physical_property_range",
                    "target_ref": "mass_report",
                    "expected": {"min_kg": 0.0, "max_kg": 10.0},
                }
            ],
        }
    )


class Backend:
    provider = "fake"

    def __init__(self, capabilities: set[str]) -> None:
        self.capabilities = capabilities
        self.calls: list[str] = []

    async def execute_operation(self, operation):
        self.calls.append(operation.id)
        return {
            "physical_properties": {
                "part": {
                    "entity_identity": "e" * 64,
                    "mass_kg": 1.0,
                    "volume_mm3": 10.0,
                    "area_mm2": 5.0,
                    "raw_path": "C:/private/design.f3d",
                }
            },
            "evidence_envelope": {
                "schema_version": "fusion_agent.evidence.v1",
                "producer": "fake.physical_properties",
                "document_identity": "d" * 64,
                "complete": True,
                "counts_exact": True,
                "truncated": False,
                "stop_reason": None,
                "metrics_finite": True,
                "observed_count": 1,
            },
            "raw_error": "secret downstream diagnostic",
        }


@pytest.mark.asyncio
async def test_capability_preflight_happens_before_first_dispatch() -> None:
    backend = Backend(set())
    with pytest.raises(ValueError, match="physical_properties"):
        await CapabilityExecutor(backend).execute(_spec())
    assert backend.calls == []


@pytest.mark.asyncio
async def test_capability_executor_records_provider_and_evidence() -> None:
    backend = Backend({"physical_properties"})
    result = await CapabilityExecutor(backend).execute(_spec())
    assert result.success is True
    assert result.provider == "fake"
    evidence = result.evidence["measure_mass"]
    assert evidence["envelope"]["complete"] is True
    assert evidence["envelope"]["document_identity"] == "d" * 64
    assert evidence["envelope"]["assertion_ids"] == ["mass_range"]
    assert evidence["data"] == {
        "part": {
            "entity_identity": "e" * 64,
            "mass_kg": 1.0,
            "volume_mm3": 10.0,
            "area_mm2": 5.0,
        }
    }
    assert "secret downstream diagnostic" not in repr(evidence)
    assert "C:/private" not in repr(evidence)
    assert backend.calls == ["measure_mass"]


@pytest.mark.asyncio
async def test_dry_run_needs_no_backend() -> None:
    result = await CapabilityExecutor().execute(_spec(), dry_run=True)
    assert result.dry_run is True
    assert result.transactions[0]["status"] == "simulated"


@pytest.mark.asyncio
async def test_provider_dispatch_receives_bound_spec_document_and_session_context() -> (
    None
):
    spec = CadSpecV2.model_validate(
        {
            "cad_spec_version": "2.0",
            "intent": "Create one exact component",
            "requirements": [
                {
                    "id": "fixture_exists",
                    "description": "fixture exists",
                    "assertion_ids": ["fixture_created"],
                }
            ],
            "operations": [
                {
                    "id": "create_fixture",
                    "kind": "component.create",
                    "name": "fixture",
                    "requirement_ids": ["fixture_exists"],
                }
            ],
            "assertions": [
                {
                    "id": "fixture_created",
                    "kind": "entity_exists",
                    "target_ref": "fixture",
                }
            ],
        }
    )
    document_identity = hashlib.sha256(b"bound-document").hexdigest()

    class BoundContextBackend:
        provider = "bound-context"
        capabilities = {"components"}

        def __init__(self) -> None:
            self.preflight_context: RequestContext | None = None
            self.dispatch_context: RequestContext | None = None

        async def resolve_document_binding(self) -> CadTargetBinding:
            return CadTargetBinding(
                reference_kind="active_document",
                requested_ref="active_document",
                document_identity=document_identity,
                entity_identity=hashlib.sha256(b"bound-root").hexdigest(),
                fingerprint=hashlib.sha256(b"bound-document-proof").hexdigest(),
            )

        def preflight_operations(self, _operations) -> None:
            return None

        def preflight_bound_operations(self, _operations) -> None:
            self.preflight_context = current_request_context()

        async def execute_bound_operation(self, _operation):
            self.dispatch_context = current_request_context()
            return {"success": True}

    backend = BoundContextBackend()
    outer = RequestContext(
        request_id="request-outer",
        session_id=None,
        profile="normal",
        mode="mock",
        backend="unresolved",
        spec_digest=hashlib.sha256(b"raw-mcp-arguments").hexdigest(),
    )
    with bind_request_context(outer):
        result = await CapabilityExecutor(backend).execute(
            spec, session_id="bound-session"
        )

    expected_digest = hashlib.sha256(
        json.dumps(
            spec.model_dump(mode="json", exclude_none=False),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    assert result.success is True
    for context in (backend.preflight_context, backend.dispatch_context):
        assert context is not None
        assert context.session_id == "bound-session"
        assert context.backend == "bound-context"
        assert context.document_identity == document_identity
        assert context.spec_digest == expected_digest
        assert any(
            value.startswith("operation_capability:") for value in context.capabilities
        )
    assert current_request_context() is None
