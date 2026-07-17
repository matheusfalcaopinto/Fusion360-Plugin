from __future__ import annotations

import json
import math

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from agent_core.capability_executor import CapabilityExecutionResult, CapabilityExecutor
from cad_spec.v2 import CadSpecV2
from fusion_agent_mcp import server
from fusion_agent_mcp.runtime import FusionAgentRuntime
from fusion_mcp_adapter.tool_result import ToolDefinition, ToolManifest, ToolResult
from fusion_tool_facade.autodesk_typed_backend import AutodeskTypedBackend
from verifier.result_models import EvidenceEnvelope


def _v2_payload(*, include_parameter: bool = False) -> dict:
    operations: list[dict] = []
    if include_parameter:
        operations.append(
            {
                "id": "set_length",
                "kind": "parameter.set",
                "name": "shaft_length",
                "expression": "20 mm",
                "requirement_ids": ["shaft_measured"],
            }
        )
    operations.append(
        {
            "id": "measure_shaft",
            "kind": "analysis.physical_properties",
            "target_refs": ["shaft"],
            "output_ref": "shaft_properties",
            "depends_on": ["set_length"] if include_parameter else [],
            "requirement_ids": ["shaft_measured"],
        }
    )
    return {
        "cad_spec_version": "2.0",
        "intent": "Measure a shaft",
        "requirements": [
            {
                "id": "shaft_measured",
                "description": "Shaft properties are recorded",
                "assertion_ids": ["mass_in_range"],
            }
        ],
        "operations": operations,
        "assertions": [
            {
                "id": "mass_in_range",
                "kind": "physical_property_range",
                "target_ref": "shaft_properties",
                "expected": {"min_kg": 0.0, "max_kg": 10.0},
            }
        ],
    }


def _legacy_payload() -> dict:
    return {
        "intent": "Use this supplied legacy spec",
        "parameters": [],
        "components": [
            {
                "name": "block_component",
                "features": [
                    {
                        "name": "block_feature",
                        "type": "extrude_rectangle",
                        "inputs": {
                            "sketch_name": "block_sketch",
                            "body_name": "block_body",
                            "width": "10 mm",
                            "height": "10 mm",
                            "distance": "10 mm",
                        },
                    }
                ],
            }
        ],
        "acceptance_tests": [{"type": "body_exists", "target": "block_body"}],
    }


def _entity_contract(*, independent: bool = False) -> CadSpecV2:
    stable_ref = "root/verified_body#1"
    assertion = {
        "id": "body_verified",
        "kind": "custom_oracle" if independent else "entity_exists",
        "target_ref": stable_ref,
        "expected": {"body": "verified_body"} if independent else True,
    }
    requirement = {
        "id": "body_requirement",
        "description": "The requested body is present",
        "assertion_ids": ["body_verified"],
        **({"oracle": "independent"} if independent else {}),
    }
    return CadSpecV2.model_validate(
        {
            "cad_spec_version": "2.0",
            "intent": "Create a verified body",
            "requirements": [requirement],
            "operations": [
                {
                    "id": "set_body_parameter",
                    "kind": "parameter.set",
                    "name": "verified_size",
                    "expression": "10 mm",
                    "requirement_ids": ["body_requirement"],
                }
            ],
            "assertions": [assertion],
        }
    )


def _complete_body_readback() -> dict:
    return {
        "snapshot": {
            "schema_version": "compact_snapshot.v2",
            "complete": True,
            "counts_exact": True,
            "truncated": False,
            "payload_capped": False,
            "stop_reason": None,
            "document": {"binding_identity": "d" * 64},
            "counts": {"bodies_total": 1},
            "bodies": [
                {
                    "name": "verified_body",
                    "key": "root/verified_body#1",
                    "component": "root",
                    "entity_token": "body-token",
                }
            ],
            "occurrences": [],
        }
    }


def test_run_schemas_require_exactly_one_session_input() -> None:
    schemas = [server._run_schema(), server._dry_run_schema()]
    for schema in schemas:
        validator = Draft202012Validator(schema)
        assert not list(validator.iter_errors({"prompt": "make a cube"}))
        assert not list(validator.iter_errors({"spec_json": "{}"}))
        assert list(validator.iter_errors({}))
        assert list(validator.iter_errors({"prompt": "make a cube", "spec_json": "{}"}))


@pytest.mark.asyncio
async def test_v2_run_session_uses_typed_mock_and_persists_conservative_journal(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(server, "WORKSPACE_ROOT", tmp_path / "workspace")
    runtime = FusionAgentRuntime(
        manifest_root=tmp_path / "manifests",
        outputs_root=tmp_path / "outputs",
    )
    try:
        response = await server.execute_tool_response(
            "fusion_agent_run_session",
            {
                "spec_json": json.dumps(_v2_payload()),
                "project": "typed_demo",
                "mode": "mock",
            },
            runtime=runtime,
            profile="normal",
        )
    finally:
        await runtime.close()

    payload = response.payload
    assert payload["cad_spec_version"] == "2.0"
    assert payload["contract_eligible"] is True
    assert payload["execution"]["provider"] == "mock"
    assert payload["final_status"] == "simulated"
    assert payload["verification"]["contract_verified"] is False
    assert payload["verification"]["mutation_status"] == "not_dispatched"
    assert (
        tmp_path
        / "workspace"
        / "projects"
        / "typed_demo"
        / "sessions"
        / payload["session_id"]
        / "execution.json"
    ).is_file()


def test_v2_complete_readback_can_verify_declared_contract(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(server, "WORKSPACE_ROOT", tmp_path / "workspace")
    result = server._record_v2_session(
        _entity_contract(),
        execution=CapabilityExecutionResult(
            success=True,
            provider="autodesk_http",
            dispatched=True,
            mutation_outcome="known",
        ),
        project="verified_contract",
        mode="real",
        dry_run=False,
        warnings=[],
        readback=_complete_body_readback(),
    )

    assert result["final_status"] == "applied_verified"
    assert result["verification"]["mutation_status"] == "observed_in_readback"
    assert result["verification"]["assertion_status"] == "passed"
    assert result["verification"]["intent_coverage"] == "complete"
    assert result["verification"]["contract_verified"] is True


def test_v2_entity_label_without_stable_reference_is_incomplete(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(server, "WORKSPACE_ROOT", tmp_path / "workspace")
    payload = _entity_contract().model_dump(mode="json")
    payload["assertions"][0]["target_ref"] = "verified_body"
    result = server._record_v2_session(
        CadSpecV2.model_validate(payload),
        execution=CapabilityExecutionResult(
            success=True,
            provider="autodesk_http",
            dispatched=True,
            mutation_outcome="known",
        ),
        project="label_is_not_identity",
        mode="real",
        dry_run=False,
        warnings=[],
        readback=_complete_body_readback(),
    )

    assert result["verification"]["assertion_status"] == "incomplete"
    assert result["verification"]["contract_verified"] is False


@pytest.mark.parametrize("invalid", [True, math.nan, math.inf, -math.inf])
def test_v2_physical_range_rejects_invalid_numeric_bounds(invalid) -> None:
    payload = _v2_payload()
    payload["assertions"][0]["expected"]["min_kg"] = invalid
    with pytest.raises(ValidationError):
        CadSpecV2.model_validate(payload)


def test_v2_analysis_requires_conclusive_document_bound_envelope(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(server, "WORKSPACE_ROOT", tmp_path / "workspace")
    envelope = EvidenceEnvelope(
        producer="capability_executor",
        provenance={
            "provider": "autodesk_http",
            "operation_id": "measure_shaft",
            "operation_kind": "analysis.physical_properties",
        },
        document_identity="d" * 64,
        complete=True,
        counts_exact=True,
        truncated=False,
        metrics_finite=True,
        assertion_ids=["mass_in_range"],
        assertion_count=1,
        evaluated_count=1,
    )
    execution = CapabilityExecutionResult(
        success=True,
        provider="autodesk_http",
        evidence={
            "measure_shaft": {
                "envelope": envelope.model_dump(mode="json"),
                "data": {
                    "shaft": {
                        "entity_identity": "e" * 64,
                        "mass_kg": 1.0,
                        "volume_mm3": 100.0,
                        "area_mm2": 20.0,
                    }
                },
            }
        },
    )
    result = server._record_v2_session(
        CadSpecV2.model_validate(_v2_payload()),
        execution=execution,
        project="analysis_bound",
        mode="real",
        dry_run=False,
        warnings=[],
        readback=_complete_body_readback(),
    )
    assert result["verification"]["assertion_status"] == "passed"

    drifted = execution.evidence["measure_shaft"]["envelope"].copy()
    drifted["document_identity"] = "a" * 64
    execution.evidence["measure_shaft"]["envelope"] = drifted
    result = server._record_v2_session(
        CadSpecV2.model_validate(_v2_payload()),
        execution=execution,
        project="analysis_drifted",
        mode="real",
        dry_run=False,
        warnings=[],
        readback=_complete_body_readback(),
    )
    assert result["verification"]["assertion_status"] == "incomplete"


def test_v2_export_assertion_uses_bound_transaction_not_filesystem(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(server, "WORKSPACE_ROOT", tmp_path / "workspace")
    spec = CadSpecV2.model_validate(
        {
            "cad_spec_version": "2.0",
            "intent": "Export one bound body",
            "requirements": [
                {
                    "id": "export_required",
                    "description": "The requested body is exported",
                    "assertion_ids": ["export_verified"],
                }
            ],
            "operations": [
                {
                    "id": "export_part",
                    "kind": "io.export",
                    "target_ref": "part_body",
                    "path": str(tmp_path / "does-not-exist.step"),
                    "format": "step",
                    "requirement_ids": ["export_required"],
                }
            ],
            "assertions": [
                {
                    "id": "export_verified",
                    "kind": "export_exists",
                    "target_ref": "export_part",
                    "expected": True,
                }
            ],
        }
    )
    result = server._record_v2_session(
        spec,
        execution=CapabilityExecutionResult(
            success=True,
            provider="autodesk_http",
            dispatched=True,
            mutation_outcome="known",
            transactions=[
                {
                    "operation_id": "export_part",
                    "kind": "io.export",
                    "status": "ok",
                    "native_result": {
                        "completed": True,
                        "kind": "io.export",
                        "format": "step",
                        "bytes": 128,
                    },
                }
            ],
        ),
        project="export_bound",
        mode="real",
        dry_run=False,
        warnings=[],
        readback=_complete_body_readback(),
    )

    assert not (tmp_path / "does-not-exist.step").exists()
    assert result["verification"]["assertion_status"] == "passed"
    assert result["verification"]["assertions"][0]["evidence_source"] == (
        "bound_export_transaction"
    )


def test_v2_positive_readback_never_promotes_unknown_mutation(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(server, "WORKSPACE_ROOT", tmp_path / "workspace")
    result = server._record_v2_session(
        _entity_contract(),
        execution=CapabilityExecutionResult(
            success=False,
            provider="autodesk_http",
            dispatched=True,
            may_have_applied=True,
            post_dispatch_replay_suppressed=True,
            mutation_outcome="unknown",
            error_code="MUTATION_OUTCOME_UNKNOWN",
        ),
        project="unknown_contract",
        mode="real",
        dry_run=False,
        warnings=[],
        readback=_complete_body_readback(),
    )

    assert result["final_status"] == "mutation_outcome_unknown"
    assert result["verification"]["mutation_status"] == "outcome_unknown"
    assert result["verification"]["mutation_outcome"] == "unknown"
    assert result["verification"]["post_dispatch_replay_suppressed"] is True
    assert result["verification"]["contract_verified"] is False


def test_v2_custom_oracle_declaration_cannot_self_verify(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(server, "WORKSPACE_ROOT", tmp_path / "workspace")
    result = server._record_v2_session(
        _entity_contract(independent=True),
        execution=CapabilityExecutionResult(
            success=True,
            provider="autodesk_http",
            dispatched=True,
            mutation_outcome="known",
        ),
        project="independent_oracle",
        mode="real",
        dry_run=False,
        warnings=[],
        readback=_complete_body_readback(),
    )

    assert result["final_status"] == "applied_unverified"
    assert result["verification"]["verification_level"] == "independent_oracle"
    assert result["verification"]["assertion_status"] == "incomplete"
    assert result["verification"]["intent_coverage"] == "none"
    assert (
        result["verification"]["requirements"][0]["oracle_evidence"] == "not_available"
    )
    assert result["verification"]["contract_verified"] is False


@pytest.mark.asyncio
async def test_supplied_legacy_spec_is_executed_without_replanning(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(server, "WORKSPACE_ROOT", tmp_path / "workspace")
    runtime = FusionAgentRuntime(
        manifest_root=tmp_path / "manifests",
        outputs_root=tmp_path / "outputs",
    )

    async def must_not_plan(_request):
        raise AssertionError("caller-supplied CadSpec was replanned")

    monkeypatch.setattr(runtime.controller.planner, "plan", must_not_plan)
    try:
        response = await server.execute_tool_response(
            "fusion_agent_dry_run_session",
            {
                "spec_json": json.dumps(_legacy_payload()),
                "project": "legacy_demo",
                "mode": "mock",
            },
            runtime=runtime,
            profile="advanced",
        )
    finally:
        await runtime.close()

    assert response.payload["cad_spec_version"] == "1"
    assert response.payload["contract_eligible"] is False
    assert "deprecated" in response.payload["warnings"][0]
    assert response.payload["status"] == "simulated"


@pytest.mark.asyncio
async def test_autodesk_missing_later_capability_blocks_all_dispatch() -> None:
    client = _Client()
    manifest = ToolManifest(
        source="autodesk-test",
        tools=[ToolDefinition(name="fusion_mcp_execute")],
    )
    backend = AutodeskTypedBackend.from_client(client, manifest)
    spec = CadSpecV2.model_validate(_v2_with_revolve())

    with pytest.raises(ValueError, match="revolve"):
        await CapabilityExecutor(backend).execute(spec)
    assert client.calls == []


class _Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def list_tools(self):
        raise AssertionError("manifest is already supplied")

    async def call_tool(self, name, arguments, *, options=None):
        self.calls.append((name, arguments))
        return ToolResult.success(success=True)


def _v2_with_revolve() -> dict:
    payload = _v2_payload(include_parameter=True)
    payload["operations"].insert(
        1,
        {
            "id": "revolve_shaft",
            "kind": "feature.revolve",
            "component_ref": "root",
            "profile_ref": "shaft_profile",
            "axis_ref": "x_axis",
            "result_name": "shaft",
            "depends_on": ["set_length"],
            "requirement_ids": ["shaft_measured"],
        },
    )
    payload["operations"][-1]["depends_on"] = ["revolve_shaft"]
    return payload
