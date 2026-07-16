from __future__ import annotations

import math
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from agent_core.repair_loop import RepairLoop
from cad_spec.models import AcceptanceTestSpec, CadSpec
from fusion_mcp_adapter.tool_result import PUBLIC_DOWNSTREAM_ERROR_MESSAGE
from telemetry.journal import SessionJournal
from verifier import geometry
from verifier.geometry import GeometryVerifier
from verifier.result_models import DecisionReasonCode, DecisionStatus, FailureCode


class EvidenceFacade:
    def __init__(
        self,
        state: dict[str, Any],
        *,
        complete: bool = True,
        bounding_box: list[float] | None = None,
        interference: dict[str, Any] | None = None,
        physical_properties: dict[str, Any] | None = None,
        named_objects: dict[str, Any] | None = None,
        interference_error: bool = False,
        physical_error: bool = False,
    ) -> None:
        self.state = state
        self.complete = complete
        self.bounding_box = bounding_box or [10.0, 20.0, 30.0]
        self.interference = interference or {"count": 0, "pairs": []}
        self.physical_properties = physical_properties or {
            "fixture_component": {"mass_kg": 0.1, "volume_mm3": 100.0}
        }
        self.named_objects = named_objects or {"valid": True, "invalid": []}
        self.interference_error = interference_error
        self.physical_error = physical_error
        self.inspect_calls = 0
        self.bounding_box_calls = 0

    async def inspect_design(self) -> dict[str, Any]:
        self.inspect_calls += 1
        return {
            "state": deepcopy(self.state),
            "complete": self.complete,
            "counts_exact": True,
            "truncated": False,
            "stop_reason": "complete" if self.complete else "deadline",
            "producer": "coverage-facade",
            "document_identity": "document:fixture",
            "provenance": {"backend": "deterministic-test", 7: "discarded"},
        }

    async def measure_bounding_box(self, _target: str | None = None) -> list[float]:
        self.bounding_box_calls += 1
        return list(self.bounding_box)

    async def analyze_interference(self) -> dict[str, Any]:
        if self.interference_error:
            raise RuntimeError("private interference diagnostic")
        return {"interference": deepcopy(self.interference)}

    async def measure_physical_properties(self, _targets: list[str]) -> dict[str, Any]:
        if self.physical_error:
            raise RuntimeError("private physical diagnostic")
        return {"physical_properties": deepcopy(self.physical_properties)}

    async def validate_named_objects(self) -> dict[str, Any]:
        return deepcopy(self.named_objects)


def _acceptance_tests(export_path: Path) -> list[dict[str, Any]]:
    return [
        {"type": "body_count", "target": 1},
        {"type": "body_exists", "target": "fixture_body"},
        {"type": "component_count", "target": 1},
        {"type": "bounding_box", "target_mm": [10.0, 20.0, 30.0]},
        {
            "type": "target_bounding_box",
            "target": "fixture_body",
            "target_mm": [10.0, 20.0, 30.0],
        },
        {"type": "component_exists", "target": "fixture_component"},
        {"type": "named_bodies", "target": ["fixture_body"]},
        {"type": "named_parameters", "target": ["width"]},
        {
            "type": "nema17_dimensions",
            "target": {
                "mount_hole_count": 4,
                "mount_hole_spacing_mm": [31.0, 31.0],
                "shaft_diameter_mm": 5.0,
            },
        },
        {
            "type": "nema17_polish_details",
            "target": {
                "required_bodies": ["polish_body"],
                "min_lamination_bodies": 2,
                "wire_count": 4,
                "screw_shadow_count": 4,
            },
        },
        {
            "type": "nema17_external_assembly",
            "target": {
                "assembly_component": "fixture_assembly",
                "required_components": ["fixture_component"],
                "required_bodies": ["fixture_body"],
                "min_stator_lamination_count": 2,
                "wire_count": 4,
                "max_legacy_visible_nema17_body_count": 0,
            },
        },
        {
            "type": "profile2020_details",
            "target": {
                "component": "fixture_component",
                "body": "fixture_body",
                "size_mm": 20.0,
                "length_mm": 300.0,
                "slot_count": 4,
                "web_relief_count": 4,
            },
        },
        {
            "type": "mgn12_linear_rail_assembly",
            "target": {
                "assembly_component": "fixture_assembly",
                "required_components": ["fixture_component"],
                "required_bodies": ["fixture_body"],
                "rail_length_mm": 300.0,
                "rail_mount_hole_count": 12,
                "carriage_mount_spacing_mm": [20.0, 15.0],
                "max_legacy_visible_mgn12_body_count": 0,
            },
        },
        {
            "type": "desktop_cnc_assembly",
            "target": {
                "assembly_component": "fixture_assembly",
                "required_components": ["fixture_component"],
                "required_bodies": ["fixture_body"],
                "profile_count": 8,
                "rail_count": 4,
                "motor_count": 3,
                "leadscrew_count": 3,
                "coupler_count": 3,
                "spindle_diameter_mm": 65.0,
                "work_area_mm": [300.0, 250.0, 80.0],
                "max_legacy_visible_cnc_body_count": 0,
            },
        },
        {"type": "component_metadata"},
        {"type": "joint_contract"},
        {
            "type": "occurrence_contract",
            "target": {
                "occurrence_names": ["fixture_occurrence"],
                "component": "fixture_component",
                "count": 1,
                "component_names": ["fixture_component"],
            },
        },
        {"type": "interference_free", "target": {}},
        {"type": "physical_properties"},
        {"type": "screenshots_exist"},
        {"type": "named_objects"},
        {"type": "hole_count", "target": 2},
        {"type": "feature_health"},
        {"type": "export_exists", "target": [str(export_path)]},
    ]


def _spec(export_path: Path, screenshot_path: Path) -> CadSpec:
    return CadSpec.model_validate(
        {
            "intent": "verify every registered assertion through typed evidence",
            "units": "mm",
            "parameters": [{"name": "width", "expression": "10 mm"}],
            "components": [{"name": "fixture_component", "features": []}],
            "component_metadata": [
                {
                    "component": "fixture_component",
                    "part_number": "PN-001",
                    "description": "Coverage fixture",
                    "role": "test fixture",
                    "source_type": "custom",
                    "physical_material": "Aluminum",
                }
            ],
            "joints": [
                {
                    "name": "fixture_joint",
                    "type": "revolute",
                    "parent": "root",
                    "child": "fixture_component",
                    "axis": "z",
                }
            ],
            "outputs": [
                {
                    "name": "overview",
                    "path": str(screenshot_path),
                    "view": "isometric",
                }
            ],
            "acceptance_tests": _acceptance_tests(export_path),
        }
    )


def _physical_properties_spec() -> CadSpec:
    return CadSpec.model_validate(
        {
            "intent": "verify typed physical property evidence",
            "units": "mm",
            "parameters": [],
            "components": [{"name": "fixture_component", "features": []}],
            "component_metadata": [
                {
                    "component": "fixture_component",
                    "part_number": "PN-001",
                    "description": "Physical property fixture",
                    "role": "test fixture",
                    "source_type": "custom",
                    "physical_material": "Aluminum",
                }
            ],
            "acceptance_tests": [{"type": "physical_properties"}],
        }
    )


def _interference_spec() -> CadSpec:
    return CadSpec.model_validate(
        {
            "intent": "verify interference evidence",
            "units": "mm",
            "parameters": [],
            "components": [{"name": "fixture_component", "features": []}],
            "acceptance_tests": [{"type": "interference_free", "target": {}}],
        }
    )


def _minimal_passing_state() -> dict[str, Any]:
    return {
        "active_document": True,
        "units": "mm",
        "body_count": 0,
        "component_count": 0,
        "hole_count": 0,
        "bodies": {},
        "components": {"root": {}},
        "parameters": {},
    }


def _set_nested(payload: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    cursor: dict[str, Any] = payload
    for key in path[:-1]:
        cursor = cursor[key]
    cursor[path[-1]] = value


def _passing_state(screenshot_path: Path) -> dict[str, Any]:
    metadata = {
        "part_number": "pn-001",
        "description": "coverage fixture",
        "role": "test fixture",
        "source_type": "custom",
        "physical_material": "aluminum",
    }
    return {
        "active_document": True,
        "active_document_name": "fixture",
        "units": "mm",
        "body_count": 1,
        "component_count": 1,
        "hole_count": 2,
        "bodies": {"fixture_body": {"holes": 2}},
        "components": {
            "root": {"name": "root"},
            "fixture_component": {"name": "fixture_component", "metadata": metadata},
        },
        "parameters": {"width": "10 mm"},
        "features": {"fixture_feature": {"health": "healthy"}},
        "component_metadata": {"fixture_component": metadata},
        "joints": {
            "fixture_joint": {
                "type": "revolute",
                "parent": "root",
                "child": "fixture_component",
                "axis": "z",
                "health": "ok",
            }
        },
        "occurrences": {
            "fixture_occurrence": {
                "component": "fixture_component",
                "visible": True,
            }
        },
        "nema17_metrics": {
            "mount_hole_count": 4,
            "mount_hole_spacing_mm": [31.0, 31.0],
            "shaft_diameter_mm": 5.0,
        },
        "polish_metrics": {
            "body_names": ["polish_body"],
            "lamination_body_count": 2,
            "wire_count": 4,
            "screw_shadow_count": 4,
        },
        "assembly_metrics": {
            "assembly_component": "fixture_assembly",
            "component_names": ["fixture_component"],
            "body_names": ["fixture_body"],
            "body_components": {"fixture_body": "fixture_component"},
            "stator_lamination_count": 2,
            "wire_count": 4,
            "connector_present": True,
            "legacy_visible_nema17_body_count": 0,
        },
        "profile2020_metrics": {
            "component": "fixture_component",
            "body": "fixture_body",
            "size_mm": 20.0,
            "length_mm": 300.0,
            "slot_count": 4,
            "web_relief_count": 4,
            "center_bore_present": True,
            "material": "Aluminum 6063",
        },
        "mgn12_metrics": {
            "assembly_component": "fixture_assembly",
            "component_names": ["fixture_component"],
            "body_names": ["fixture_body"],
            "body_components": {"fixture_body": "fixture_component"},
            "rail_length_mm": 300.0,
            "rail_mount_hole_count": 12,
            "carriage_mount_spacing_mm": [20.0, 15.0],
            "legacy_visible_mgn12_body_count": 0,
            "rail_material": "stainless steel",
            "carriage_material": "aço inoxidável",
        },
        "cnc_metrics": {
            "assembly_component": "fixture_assembly",
            "component_names": ["fixture_component"],
            "body_names": ["fixture_body"],
            "body_components": {"fixture_body": "fixture_component"},
            "profile_count": 8,
            "rail_count": 4,
            "motor_count": 3,
            "leadscrew_count": 3,
            "coupler_count": 3,
            "spindle_diameter_mm": 65.0,
            "work_area_mm": [300.0, 250.0, 80.0],
            "legacy_visible_cnc_body_count": 0,
            "frame_material": "alumínio",
            "rail_material": "steel",
        },
        "interference": {"count": 0, "pairs": []},
        "physical_properties": {
            "fixture_component": {"mass_kg": 0.1, "volume_mm3": 100.0}
        },
        "screenshots": {"overview": {"path": str(screenshot_path), "bytes": 4}},
    }


@pytest.mark.asyncio
async def test_verifier_dispatches_every_registered_assertion_with_complete_evidence(
    tmp_path: Path,
) -> None:
    export_path = tmp_path / "fixture.step"
    screenshot_path = tmp_path / "overview.png"
    export_path.write_text("STEP", encoding="utf-8")
    screenshot_path.write_bytes(b"PNG!")
    spec = _spec(export_path, screenshot_path)
    facade = EvidenceFacade(_passing_state(screenshot_path))

    result = await GeometryVerifier(facade).verify(spec)  # type: ignore[arg-type]

    assert result.status is DecisionStatus.PASSED
    assert result.passed is True
    assert result.reason_codes == [DecisionReasonCode.VERIFIED]
    assert result.issues == []
    assert result.evidence is not None
    assert result.evidence.evaluated_count == len(spec.acceptance_tests)
    assert result.evidence.provenance == {"backend": "deterministic-test"}
    assert facade.bounding_box_calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "boolean_value"),
    [
        (("nema17_metrics", "mount_hole_count"), True),
        (("polish_metrics", "wire_count"), True),
        (("assembly_metrics", "stator_lamination_count"), True),
        (("profile2020_metrics", "size_mm"), True),
        (("mgn12_metrics", "carriage_mount_spacing_mm"), [True, 15.0]),
        (("cnc_metrics", "work_area_mm"), [300.0, True, 80.0]),
        (("screenshots", "overview", "bytes"), True),
    ],
)
async def test_boolean_in_registry_numeric_evidence_is_incomplete_and_never_repairs(
    tmp_path: Path,
    path: tuple[str, ...],
    boolean_value: Any,
) -> None:
    export_path = tmp_path / "fixture.step"
    screenshot_path = tmp_path / "overview.png"
    export_path.write_text("STEP", encoding="utf-8")
    screenshot_path.write_bytes(b"PNG!")
    state = _passing_state(screenshot_path)
    _set_nested(state, path, boolean_value)
    mutation_probe = _MutationProbe()
    repair_loop = RepairLoop(
        GeometryVerifier(EvidenceFacade(state)),  # type: ignore[arg-type]
        executor=mutation_probe,
    )

    result = await repair_loop.run(_spec(export_path, screenshot_path))

    assert result.status is DecisionStatus.INCOMPLETE
    assert result.passed is False
    assert result.reason_codes == [DecisionReasonCode.INVALID_NUMERIC_EVIDENCE]
    assert result.issues[0].code is FailureCode.INVALID_NUMERIC_EVIDENCE
    assert result.evidence is not None and result.evidence.metrics_finite is False
    assert mutation_probe.calls == 0
    assert repair_loop.attempts == []


def test_boolean_occurrence_count_is_rejected_by_the_parser() -> None:
    with pytest.raises(ValidationError):
        AcceptanceTestSpec(
            type="occurrence_contract",
            target={"component_names": ["fixture_component"], "count": True},
        )


class _MutationProbe:
    def __init__(self) -> None:
        self.calls = 0

    async def activate_component(self, _target: str) -> bool:
        self.calls += 1
        return True

    async def replay_features(self, *_args: Any) -> bool:
        self.calls += 1
        return True

    async def replay_exports(self, *_args: Any) -> bool:
        self.calls += 1
        return True


@pytest.mark.asyncio
@pytest.mark.parametrize("boolean_field", ["mass_kg", "volume_mm3"])
@pytest.mark.parametrize("boolean_value", [True, False])
@pytest.mark.parametrize("measurement_error", [False, True])
async def test_boolean_physical_property_is_invalid_and_cannot_authorize_mutation(
    boolean_field: str,
    boolean_value: bool,
    measurement_error: bool,
) -> None:
    properties: dict[str, Any] = {"mass_kg": 0.1, "volume_mm3": 100.0}
    properties[boolean_field] = boolean_value
    state = _minimal_passing_state()
    if measurement_error:
        state["physical_properties"] = {"fixture_component": properties}
    facade = EvidenceFacade(
        state,
        physical_properties={"fixture_component": properties},
        physical_error=measurement_error,
    )
    mutation_probe = _MutationProbe()
    repair_loop = RepairLoop(
        GeometryVerifier(facade),  # type: ignore[arg-type]
        executor=mutation_probe,
    )

    result = await repair_loop.run(_physical_properties_spec())

    assert result.status is DecisionStatus.INCOMPLETE
    assert result.passed is False
    assert result.evidence is not None
    if measurement_error:
        assert result.reason_codes == [DecisionReasonCode.INCOMPLETE_INSPECTION]
        assert result.issues[0].code is FailureCode.INCOMPLETE_INSPECTION
        assert result.evidence.metrics_finite is True
    else:
        assert result.reason_codes == [DecisionReasonCode.INVALID_NUMERIC_EVIDENCE]
        assert result.issues[0].code is FailureCode.INVALID_NUMERIC_EVIDENCE
        assert result.evidence.metrics_finite is False
    assert mutation_probe.calls == 0
    assert repair_loop.attempts == []


@pytest.mark.asyncio
async def test_finite_physical_property_numbers_remain_valid() -> None:
    facade = EvidenceFacade(
        _minimal_passing_state(),
        physical_properties={
            "fixture_component": {"mass_kg": 0.1, "volume_mm3": 100.0}
        },
    )

    result = await GeometryVerifier(facade).verify(  # type: ignore[arg-type]
        _physical_properties_spec()
    )

    assert result.status is DecisionStatus.PASSED
    assert result.passed is True
    assert result.reason_codes == [DecisionReasonCode.VERIFIED]


_CAN025_SECRET = "SECRET=CAN025_LEGACY_SECRET"
_CAN025_PATH = r"C:\private\CAN025_LEGACY\provider.json"
_CAN025_ARGV = "argv=['fusion-provider', '--token', 'CAN025_ARGV']"
_CAN025_CANARIES = (_CAN025_SECRET, _CAN025_PATH, _CAN025_ARGV)


class _CanaryFailureFacade(EvidenceFacade):
    async def analyze_interference(self) -> dict[str, Any]:
        raise RuntimeError("; ".join(_CAN025_CANARIES))

    async def measure_physical_properties(self, _targets: list[str]) -> dict[str, Any]:
        raise RuntimeError("; ".join(_CAN025_CANARIES))


class _UnavailableAuxiliaryFacade(EvidenceFacade):
    async def analyze_interference(self) -> dict[str, Any]:
        return {}

    async def measure_physical_properties(self, _targets: list[str]) -> dict[str, Any]:
        return {}


class _CanaryPayloadFacade(EvidenceFacade):
    async def analyze_interference(self) -> dict[str, Any]:
        return {
            "interference": {
                "count": 0,
                "pairs": [],
                "error": "; ".join(_CAN025_CANARIES),
                "diagnostic": "; ".join(_CAN025_CANARIES),
            }
        }

    async def measure_physical_properties(self, _targets: list[str]) -> dict[str, Any]:
        return {
            "physical_properties": {
                "fixture_component": {
                    "mass_kg": 0.1,
                    "volume_mm3": 100.0,
                    "error_message": "; ".join(_CAN025_CANARIES),
                    "diagnostic": "; ".join(_CAN025_CANARIES),
                }
            }
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("assertion", ["interference", "physical_properties"])
async def test_legacy_downstream_exception_is_public_in_result_and_journal(
    tmp_path: Path,
    assertion: str,
) -> None:
    spec = (
        _interference_spec()
        if assertion == "interference"
        else _physical_properties_spec()
    )
    mutation_probe = _MutationProbe()
    repair_loop = RepairLoop(
        GeometryVerifier(  # type: ignore[arg-type]
            _CanaryFailureFacade(_minimal_passing_state())
        ),
        executor=mutation_probe,
    )
    result = await repair_loop.run(spec)

    assert result.status is DecisionStatus.INCOMPLETE
    assert result.passed is False
    assert result.reason_codes == [DecisionReasonCode.INCOMPLETE_INSPECTION]
    assert result.issues[0].code is FailureCode.INCOMPLETE_INSPECTION
    assert mutation_probe.calls == 0
    assert repair_loop.attempts == []
    if assertion == "interference":
        public_error = result.metrics["interference"]["error"]
    else:
        public_error = result.metrics["physical_properties"]["_error"]
    verification_json = result.model_dump_json()
    for canary in _CAN025_CANARIES:
        assert canary not in verification_json
    assert public_error["code"] == "FUSION_OPERATION_FAILED"
    assert public_error["generic_message"] == PUBLIC_DOWNSTREAM_ERROR_MESSAGE
    assert public_error["correlation_id"].startswith("diag-")
    assert public_error["retryable"] is False

    journal = SessionJournal(tmp_path, "can025", f"session-{assertion}")
    cad_spec_path = journal.write_text("cad_spec.json", spec.to_json_text())
    journal.write_json("verification.json", result)
    journal_path = journal.finalize(
        mode="real",
        user_prompt="verify safe auxiliary evidence",
        cad_spec_path=cad_spec_path,
        verification=result,
        final_status="incomplete",
        summary="Auxiliary verification was inconclusive and failed safely.",
    )

    persisted = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (journal.session_dir / "verification.json", journal_path)
    )
    for canary in _CAN025_CANARIES:
        assert canary not in verification_json
        assert canary not in persisted
    assert PUBLIC_DOWNSTREAM_ERROR_MESSAGE in verification_json
    assert PUBLIC_DOWNSTREAM_ERROR_MESSAGE in persisted


@pytest.mark.asyncio
@pytest.mark.parametrize("assertion", ["interference", "physical_properties"])
async def test_legacy_unavailable_auxiliary_evidence_is_incomplete_without_repair(
    assertion: str,
) -> None:
    spec = (
        _interference_spec()
        if assertion == "interference"
        else _physical_properties_spec()
    )
    mutation_probe = _MutationProbe()
    repair_loop = RepairLoop(
        GeometryVerifier(  # type: ignore[arg-type]
            _UnavailableAuxiliaryFacade(_minimal_passing_state())
        ),
        executor=mutation_probe,
    )

    result = await repair_loop.run(spec)

    assert result.status is DecisionStatus.INCOMPLETE
    assert result.passed is False
    assert result.reason_codes == [DecisionReasonCode.INCOMPLETE_INSPECTION]
    assert result.issues[0].code is FailureCode.INCOMPLETE_INSPECTION
    assert mutation_probe.calls == 0
    assert repair_loop.attempts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("assertion", ["interference", "physical_properties"])
async def test_legacy_error_payload_channels_are_discarded_and_incomplete(
    assertion: str,
) -> None:
    spec = (
        _interference_spec()
        if assertion == "interference"
        else _physical_properties_spec()
    )

    result = await GeometryVerifier(
        _CanaryPayloadFacade(_minimal_passing_state())
    ).verify(spec)  # type: ignore[arg-type]
    serialized = result.model_dump_json()

    assert result.status is DecisionStatus.INCOMPLETE
    assert result.reason_codes == [DecisionReasonCode.INCOMPLETE_INSPECTION]
    for canary in _CAN025_CANARIES:
        assert canary not in serialized
    assert PUBLIC_DOWNSTREAM_ERROR_MESSAGE in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize("assertion", ["interference", "physical_properties"])
async def test_legacy_auxiliary_success_remains_a_passing_control(
    assertion: str,
) -> None:
    spec = (
        _interference_spec()
        if assertion == "interference"
        else _physical_properties_spec()
    )

    result = await GeometryVerifier(EvidenceFacade(_minimal_passing_state())).verify(
        spec
    )  # type: ignore[arg-type]

    assert result.status is DecisionStatus.PASSED
    assert result.passed is True
    assert result.issues == []


@pytest.mark.asyncio
async def test_verifier_reports_each_failed_contract_without_false_success(
    tmp_path: Path,
) -> None:
    export_path = tmp_path / "missing.step"
    screenshot_path = tmp_path / "missing.png"
    spec = _spec(export_path, screenshot_path)
    state = {
        "active_document": False,
        "units": "cm",
        "body_count": 0,
        "component_count": 0,
        "hole_count": 0,
        "bodies": {},
        "components": {"root": {}},
        "parameters": {},
        "features": {"bad_feature": {"health": "failed"}},
        "nema17_metrics": {},
        "polish_metrics": {},
        "assembly_metrics": {},
        "profile2020_metrics": {},
        "mgn12_metrics": {},
        "cnc_metrics": {},
        "joints": {},
        "occurrences": {},
        "screenshots": {},
    }
    facade = EvidenceFacade(
        state,
        bounding_box=[9.0, 9.0, 9.0],
        interference={"count": 1, "pairs": []},
        physical_properties={"fixture_component": {"mass_kg": 0, "volume_mm3": 0}},
        named_objects={"valid": False, "invalid": ["BadName"]},
    )

    result = await GeometryVerifier(facade).verify(spec)  # type: ignore[arg-type]

    assert result.status is DecisionStatus.FAILED
    assert result.passed is False
    assert result.reason_codes == [DecisionReasonCode.ASSERTION_FAILED]
    codes = {issue.code for issue in result.issues}
    assert {
        FailureCode.MCP_TOOL_ERROR,
        FailureCode.UNIT_MISMATCH,
        FailureCode.INVALID_REFERENCE,
        FailureCode.FEATURE_CREATION_FAILED,
        FailureCode.METADATA_MISSING,
        FailureCode.JOINT_MISMATCH,
        FailureCode.INTERFERENCE_DETECTED,
        FailureCode.PHYSICAL_PROPERTY_MISMATCH,
        FailureCode.SCREENSHOT_FAILED,
        FailureCode.NAME_COLLISION,
        FailureCode.FEATURE_SUPPRESSED_OR_FAILED,
        FailureCode.EXPORT_FAILED,
    }.issubset(codes)


@pytest.mark.asyncio
async def test_incomplete_inspection_stops_before_assertion_dispatch(
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path / "missing.step", tmp_path / "missing.png")
    facade = EvidenceFacade({}, complete=False)

    result = await GeometryVerifier(facade).verify(spec)  # type: ignore[arg-type]

    assert result.status is DecisionStatus.INCOMPLETE
    assert result.reason_codes == [DecisionReasonCode.INCOMPLETE_INSPECTION]
    assert result.issues[0].code is FailureCode.INCOMPLETE_INSPECTION
    assert facade.bounding_box_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_count", [True, False, -1, 1.5])
async def test_invalid_count_evidence_is_incomplete_not_passed(
    tmp_path: Path, invalid_count: object
) -> None:
    spec = CadSpec.model_validate(
        {
            "intent": "reject invalid numeric evidence",
            "parameters": [],
            "components": [{"name": "fixture_component", "features": []}],
            "acceptance_tests": [{"type": "body_count", "target": 0}],
        }
    )
    state = {
        "active_document": True,
        "units": "mm",
        "body_count": invalid_count,
        "component_count": 0,
        "hole_count": 0,
        "bodies": {},
        "components": {"root": {}},
    }

    result = await GeometryVerifier(EvidenceFacade(state)).verify(spec)  # type: ignore[arg-type]

    assert result.status is DecisionStatus.INCOMPLETE
    assert result.reason_codes == [DecisionReasonCode.INVALID_NUMERIC_EVIDENCE]
    assert result.evidence is not None and result.evidence.metrics_finite is False


@pytest.mark.asyncio
@pytest.mark.parametrize("assertion", ["bounding_box", "target_bounding_box"])
async def test_nonfinite_bounding_box_is_incomplete(
    tmp_path: Path, assertion: str
) -> None:
    acceptance: dict[str, Any] = {"type": assertion, "target_mm": [1.0, 2.0, 3.0]}
    if assertion == "target_bounding_box":
        acceptance["target"] = "fixture_body"
    spec = CadSpec.model_validate(
        {
            "intent": "reject nonfinite bounding evidence",
            "parameters": [],
            "components": [{"name": "fixture_component", "features": []}],
            "acceptance_tests": [acceptance],
        }
    )
    state = {
        "active_document": True,
        "units": "mm",
        "body_count": 0,
        "component_count": 0,
        "hole_count": 0,
        "bodies": {},
        "components": {"root": {}},
    }

    result = await GeometryVerifier(
        EvidenceFacade(state, bounding_box=[1.0, math.nan, 3.0])
    ).verify(spec)  # type: ignore[arg-type]

    assert result.status is DecisionStatus.INCOMPLETE
    assert result.reason_codes == [DecisionReasonCode.INVALID_NUMERIC_EVIDENCE]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["interference", "physical"])
async def test_auxiliary_measurement_failure_is_incomplete_and_cannot_repair(
    tmp_path: Path, failure: str
) -> None:
    check = "interference_free" if failure == "interference" else "physical_properties"
    spec = CadSpec.model_validate(
        {
            "intent": "normalize auxiliary measurement failure",
            "parameters": [],
            "components": [{"name": "fixture_component", "features": []}],
            "component_metadata": [
                {
                    "component": "fixture_component",
                    "part_number": "PN",
                    "description": "fixture",
                    "role": "fixture",
                    "source_type": "custom",
                    "physical_material": "steel",
                }
            ],
            "acceptance_tests": [{"type": check}],
        }
    )
    state = {
        "active_document": True,
        "units": "mm",
        "body_count": 0,
        "component_count": 0,
        "hole_count": 0,
        "bodies": {},
        "components": {"root": {}},
        "physical_properties": {},
    }
    facade = EvidenceFacade(
        state,
        interference_error=failure == "interference",
        physical_error=failure == "physical",
    )

    mutation_probe = _MutationProbe()
    repair_loop = RepairLoop(
        GeometryVerifier(facade),  # type: ignore[arg-type]
        executor=mutation_probe,
    )
    result = await repair_loop.run(spec)

    assert result.status is DecisionStatus.INCOMPLETE
    assert result.passed is False
    assert result.reason_codes == [DecisionReasonCode.INCOMPLETE_INSPECTION]
    assert result.issues[0].code is FailureCode.INCOMPLETE_INSPECTION
    assert mutation_probe.calls == 0
    assert repair_loop.attempts == []


def test_numeric_and_bbox_helpers_cover_security_classification() -> None:
    assert geometry._contains_non_finite_number(True)
    assert not geometry._contains_non_finite_number(1.0)
    assert geometry._contains_non_finite_number({"nested": [1.0, math.inf]})
    assert not geometry._contains_non_finite_number("nan")
    assert geometry._is_nonnegative_integer(0)
    assert not geometry._is_nonnegative_integer(True)
    assert not geometry._is_nonnegative_integer(-1)
    assert geometry._is_finite_vector((1, 2.0, 3))
    assert not geometry._is_finite_vector([])
    assert not geometry._is_finite_vector([1, True, 3])
    assert not geometry._is_finite_vector([1, math.nan, 3])
    assert geometry._physical_property_mismatches(
        {"fixture_component": {"mass_kg": True, "volume_mm3": 100.0}},
        ["fixture_component"],
    )
    assert geometry._physical_property_mismatches(
        {"fixture_component": {"mass_kg": 0.1, "volume_mm3": True}},
        ["fixture_component"],
    )
    assert (
        geometry._physical_property_mismatches(
            {"fixture_component": {"mass_kg": 0.1, "volume_mm3": 100.0}},
            ["fixture_component"],
        )
        == []
    )
    assert geometry._bbox_close([1, 2, 3], [1.01, 2, 3], 0.02)
    assert not geometry._bbox_close([1, 2], [1, 2], 0.1)
    assert not geometry._bbox_close([1, 2, 3], [2, 2, 3], 0.1)
    assert geometry._classify_bbox([1, 2, 3], [10, 20, 30]) is FailureCode.UNIT_MISMATCH
    assert (
        geometry._classify_bbox([1, 2, 3], [25.4, 50.8, 76.2])
        is FailureCode.UNIT_MISMATCH
    )
    assert (
        geometry._classify_bbox([0, 2, 3], [7, 3, 4])
        is FailureCode.FEATURE_CREATION_FAILED
    )


def test_interference_and_screenshot_helpers_fail_closed(tmp_path: Path) -> None:
    assert geometry._interference_mismatches({"error": "failed"}, {})
    assert geometry._interference_mismatches({"analysis_warning": "partial"}, {})
    assert geometry._interference_mismatches({"count": None}, {})
    assert geometry._interference_mismatches({"count": 0}, {}) == []
    assert geometry._interference_mismatches({"count": 1, "pairs": []}, {})
    allowed = {"allowed_contact_pairs": [["a", "b"]]}
    assert (
        geometry._interference_mismatches(
            {"count": 1, "pairs": [{"a": "a", "b": "b"}]}, allowed
        )
        == []
    )
    assert geometry._interference_mismatches(
        {"count": 1, "pairs": [["a", "c"]]}, allowed
    )

    existing = tmp_path / "existing.png"
    existing.write_bytes(b"image")
    assert (
        geometry._screenshot_mismatches(
            {"shot": {"path": str(existing)}},
            [{"name": "shot", "path": str(tmp_path / "configured.png")}],
        )
        == []
    )
    assert (
        geometry._screenshot_mismatches(
            {"shot": {"bytes": 4}}, [{"name": "shot", "path": "relative.png"}]
        )
        == []
    )
    assert geometry._screenshot_mismatches(
        {}, [{"name": "missing", "path": str(tmp_path / "missing.png")}]
    )


def test_mismatch_helpers_report_identity_material_and_measurement_drift() -> None:
    metadata = geometry._metadata_state(
        {
            "component_metadata": {"direct": {"part_number": "D"}},
            "components": {
                "direct": {"metadata": {"part_number": "ignored"}},
                "nested": {"metadata": {"part_number": "N"}},
                "invalid": "not-a-component",
            },
        }
    )
    assert metadata["direct"]["part_number"] == "D"
    assert metadata["nested"]["part_number"] == "N"
    assert geometry._metadata_mismatches(
        metadata,
        [
            {
                "component": "missing",
                "part_number": "PN",
                "description": "d",
                "role": "r",
                "source_type": "custom",
                "physical_material": "steel",
            },
            {
                "component": "direct",
                "part_number": "different",
                "description": "required",
            },
        ],
    )

    assert geometry._joint_mismatches(
        {
            "bad": {
                "type": "rigid",
                "parent": "root",
                "child": "part",
                "health": "failed",
            }
        },
        [
            {"name": "missing"},
            {
                "name": "bad",
                "type": "revolute",
                "parent": "root",
                "child": "part",
                "axis": None,
            },
        ],
    )
    assert geometry._occurrence_mismatches(
        {
            "wrong": {"component": "other", "visible": True},
            "hidden": {"component": "fixture", "visible": False},
        },
        {"fixture": {}},
        {
            "occurrence_names": ["missing", "wrong"],
            "component": "fixture",
            "component_names": ["fixture", "missing_component"],
            "count": 2,
        },
    )
    assert geometry._physical_property_mismatches(
        {
            "_error": "measurement failed",
            "bad": {"mass_kg": 0, "volume_mm3": -1},
        },
        ["missing", "bad"],
    )

    assert geometry._metric_mismatches(
        {"vector": "bad", "count": 1, "scalar": None},
        {"vector": [1, 2], "count": 2, "scalar": 1.0},
        0.1,
    )
    assert geometry._metric_mismatches({"vector": [1, 9]}, {"vector": [1, 2]}, 0.1)
    assert geometry._polish_mismatches(
        {"body_names": [], "lamination_body_count": 0, "wire_count": 1},
        {
            "required_bodies": ["body"],
            "min_lamination_bodies": 2,
            "wire_count": 4,
            "screw_shadow_count": 4,
        },
    )


def test_assembly_profile_rail_and_cnc_helpers_reject_unbound_owners() -> None:
    assembly_expected = {
        "assembly_component": "assembly",
        "required_components": ["component"],
        "required_bodies": ["missing", "unbound"],
        "min_stator_lamination_count": 2,
        "wire_count": 4,
        "max_legacy_visible_nema17_body_count": 0,
    }
    assert geometry._assembly_mismatches(
        {
            "assembly_component": "wrong",
            "component_names": [],
            "body_names": ["unbound"],
            "body_components": {"unbound": "root"},
            "stator_lamination_count": 0,
            "wire_count": 1,
            "connector_present": False,
            "legacy_visible_nema17_body_count": 1,
        },
        assembly_expected,
    )

    assert geometry._profile2020_mismatches(
        {
            "component": "wrong",
            "body": "wrong",
            "size_mm": None,
            "length_mm": 10,
            "slot_count": 1,
            "web_relief_count": 1,
            "center_bore_present": False,
            "material": "plastic",
        },
        {
            "component": "component",
            "body": "body",
            "size_mm": 20,
            "length_mm": 300,
            "slot_count": 4,
            "web_relief_count": 4,
        },
        0.1,
    )

    rail_expected = {
        "assembly_component": "assembly",
        "required_components": ["component"],
        "required_bodies": ["missing", "unbound"],
        "rail_length_mm": 300,
        "rail_mount_hole_count": 12,
        "carriage_mount_spacing_mm": [20, 15],
        "max_legacy_visible_mgn12_body_count": 0,
    }
    assert geometry._mgn12_mismatches(
        {
            "assembly_component": "wrong",
            "component_names": [],
            "body_names": ["unbound"],
            "body_components": {"unbound": "root"},
            "rail_length_mm": None,
            "rail_mount_hole_count": 1,
            "carriage_mount_spacing_mm": [1],
            "legacy_visible_mgn12_body_count": 1,
            "rail_material": "plastic",
            "carriage_material": "steel",
        },
        rail_expected,
        0.1,
    )
    assert geometry._mgn12_mismatches(
        {"carriage_mount_spacing_mm": [20, 99]},
        {"carriage_mount_spacing_mm": [20, 15]},
        0.1,
    )

    cnc_expected = {
        "assembly_component": "assembly",
        "required_components": ["component"],
        "required_bodies": ["missing", "unbound"],
        "profile_count": 8,
        "rail_count": 4,
        "motor_count": 3,
        "leadscrew_count": 3,
        "coupler_count": 3,
        "spindle_diameter_mm": 65,
        "work_area_mm": [300, 250, 80],
        "max_legacy_visible_cnc_body_count": 0,
    }
    assert geometry._cnc_mismatches(
        {
            "assembly_component": "wrong",
            "component_names": [],
            "body_names": ["unbound"],
            "body_components": {"unbound": "root"},
            "profile_count": 1,
            "rail_count": 1,
            "motor_count": 1,
            "leadscrew_count": 1,
            "coupler_count": 1,
            "spindle_diameter_mm": None,
            "work_area_mm": [1],
            "legacy_visible_cnc_body_count": 1,
            "frame_material": "plastic",
            "rail_material": "plastic",
        },
        cnc_expected,
        0.1,
    )
    assert geometry._cnc_mismatches(
        {"work_area_mm": [300, 250, 1]}, {"work_area_mm": [300, 250, 80]}, 0.1
    )
    assert geometry._normalized_tokens("Aço inoxidável / ALUMÍNIO") == {
        "aco",
        "inoxidavel",
        "aluminio",
    }
