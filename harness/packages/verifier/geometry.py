"""Geometry and acceptance-test verifier."""

from __future__ import annotations

import math
import re
import unicodedata
from pathlib import Path
from types import MappingProxyType
from typing import Any

from cad_spec.models import CadSpec
from fusion_mcp_adapter.tool_result import PublicError
from fusion_tool_facade.facade import FusionFacade
from verifier.result_models import (
    DecisionReasonCode,
    DecisionStatus,
    EvidenceEnvelope,
    FailureCode,
    VerificationIssue,
    VerificationResult,
)


# This registry is the authoritative assertion surface.  GeometryVerifier
# validates the complete graph against it before its first facade read.  The
# inline branch names are kept explicit so adding a model without a verifier
# handler cannot silently turn into a pass.
ASSERTION_REGISTRY = MappingProxyType(
    {
        name: f"verify_{name}"
        for name in (
            "body_count",
            "body_exists",
            "component_count",
            "bounding_box",
            "target_bounding_box",
            "component_exists",
            "named_bodies",
            "named_parameters",
            "nema17_dimensions",
            "nema17_polish_details",
            "nema17_external_assembly",
            "profile2020_details",
            "mgn12_linear_rail_assembly",
            "desktop_cnc_assembly",
            "component_metadata",
            "joint_contract",
            "occurrence_contract",
            "interference_free",
            "physical_properties",
            "screenshots_exist",
            "named_objects",
            "hole_count",
            "feature_health",
            "export_exists",
        )
    }
)


# Only fields consumed numerically by the legacy assertion registry appear here.
# Semantic booleans elsewhere in the inspection payload remain valid state.
_REGISTRY_NUMERIC_EVIDENCE = MappingProxyType(
    {
        "nema17_dimensions": (
            "nema17_metrics",
            (
                "mount_hole_diameter_mm",
                "pilot_diameter_mm",
                "shaft_diameter_mm",
            ),
            ("mount_hole_count",),
            ("mount_hole_spacing_mm",),
        ),
        "nema17_polish_details": (
            "polish_metrics",
            (),
            (
                "lamination_body_count",
                "wire_count",
                "screw_shadow_count",
                "side_panel_count",
            ),
            (),
        ),
        "nema17_external_assembly": (
            "assembly_metrics",
            (),
            (
                "stator_lamination_count",
                "wire_count",
                "legacy_visible_nema17_body_count",
            ),
            (),
        ),
        "profile2020_details": (
            "profile2020_metrics",
            (
                "size_mm",
                "length_mm",
                "slot_width_mm",
                "slot_depth_mm",
                "center_bore_diameter_mm",
            ),
            ("slot_count", "web_relief_count"),
            ("bounding_box_mm",),
        ),
        "mgn12_linear_rail_assembly": (
            "mgn12_metrics",
            (
                "rail_length_mm",
                "rail_width_mm",
                "rail_height_mm",
                "rail_hole_pitch_mm",
                "rail_hole_diameter_mm",
                "rail_counterbore_diameter_mm",
                "carriage_length_mm",
                "carriage_width_mm",
                "carriage_total_height_mm",
                "carriage_mount_thread_diameter_mm",
            ),
            (
                "rail_mount_hole_count",
                "rail_counterbore_count",
                "carriage_mount_hole_count",
                "legacy_visible_mgn12_body_count",
            ),
            ("carriage_mount_spacing_mm",),
        ),
        "desktop_cnc_assembly": (
            "cnc_metrics",
            ("spindle_diameter_mm",),
            (
                "profile_count",
                "rail_count",
                "motor_count",
                "leadscrew_count",
                "coupler_count",
                "legacy_visible_cnc_body_count",
            ),
            ("work_area_mm",),
        ),
    }
)


class GeometryVerifier:
    """Compare measured Fusion state against a validated CadSpec."""

    def __init__(self, facade: FusionFacade) -> None:
        self.facade = facade

    async def verify(self, spec: CadSpec) -> VerificationResult:
        """Run all acceptance tests in the spec."""

        assertion_ids = [acceptance.type for acceptance in spec.acceptance_tests]
        unknown = sorted(set(assertion_ids) - set(ASSERTION_REGISTRY))
        if unknown:
            return VerificationResult(
                passed=False,
                status=DecisionStatus.FAILED,
                reason_codes=[DecisionReasonCode.UNSUPPORTED_ASSERTION],
                issues=[
                    VerificationIssue(
                        code=FailureCode.UNSUPPORTED_ASSERTION,
                        message="acceptance assertion has no registered verifier handler",
                        details={"types": unknown},
                    )
                ],
            )

        state_payload = await self.facade.inspect_design()
        state = state_payload.get("state")
        if not isinstance(state, dict):
            state = {}
        evidence = _evidence_envelope(
            state_payload,
            state,
            assertion_ids=assertion_ids,
            evaluated_count=0,
        )
        if not _inspection_is_complete(evidence):
            return VerificationResult.incomplete_result(
                evidence=evidence,
                issues=[
                    VerificationIssue(
                        code=FailureCode.INCOMPLETE_INSPECTION,
                        message="inspection evidence is incomplete and cannot support verification",
                        details={
                            "complete": evidence.complete,
                            "counts_exact": evidence.counts_exact,
                            "truncated": evidence.truncated,
                            "stop_reason": evidence.stop_reason,
                        },
                    )
                ],
            )
        issues: list[VerificationIssue] = []
        metrics: dict[str, Any] = {
            "body_count": (
                state["body_count"]
                if state.get("body_count") is not None
                else len(state.get("bodies", {}))
            ),
            "component_count": (
                state["component_count"]
                if state.get("component_count") is not None
                else max(0, len(state.get("components", {})) - 1)
            ),
            "hole_count": state.get("hole_count")
            if state.get("hole_count") is not None
            else sum(body.get("holes", 0) for body in state.get("bodies", {}).values()),
            "parameter_names": sorted(state.get("parameters", {}).keys()),
            "metadata_components": sorted(_metadata_state(state).keys()),
            "joint_names": sorted((state.get("joints") or {}).keys()),
            "occurrence_names": sorted((state.get("occurrences") or {}).keys()),
        }
        invalid_count_metrics = [
            name
            for name in ("body_count", "component_count", "hole_count")
            if not _is_nonnegative_integer(metrics[name])
        ]
        invalid_registry_metrics = _invalid_registry_numeric_evidence(
            state, assertion_ids
        )
        if (
            invalid_count_metrics
            or invalid_registry_metrics
            or _contains_non_finite_number(state, reject_boole=False)
        ):
            return _invalid_numeric_result(
                evidence=evidence,
                metrics=metrics,
                message="inspection returned invalid numeric evidence",
                details={
                    "invalid_count_metrics": invalid_count_metrics,
                    "invalid_registry_metrics": invalid_registry_metrics,
                },
            )

        if not state.get("active_document", False):
            issues.append(
                VerificationIssue(
                    code=FailureCode.MCP_TOOL_ERROR, message="no active document"
                )
            )
        if state.get("units") != spec.units:
            issues.append(
                VerificationIssue(
                    code=FailureCode.UNIT_MISMATCH,
                    message=f"document units {state.get('units')} do not match spec units {spec.units}",
                )
            )

        for acceptance in spec.acceptance_tests:
            check_type = acceptance.type
            if check_type == "body_count":
                expected = int(acceptance.target)
                actual = metrics["body_count"]
                if actual != expected:
                    issues.append(
                        _issue(
                            FailureCode.FEATURE_CREATION_FAILED,
                            "body count mismatch",
                            expected,
                            actual,
                        )
                    )
            elif check_type == "body_exists":
                body_name = str(acceptance.target or "")
                if body_name not in state.get("bodies", {}):
                    issues.append(
                        VerificationIssue(
                            code=FailureCode.INVALID_REFERENCE,
                            message="missing body",
                            details={"missing": body_name},
                        )
                    )
            elif check_type == "component_count":
                expected = int(acceptance.target)
                actual = metrics["component_count"]
                if actual != expected:
                    issues.append(
                        _issue(
                            FailureCode.WRONG_ACTIVE_COMPONENT,
                            "component count mismatch",
                            expected,
                            actual,
                        )
                    )
            elif check_type == "bounding_box":
                expected = acceptance.target_mm or []
                tolerance = (
                    acceptance.tolerance_mm
                    if acceptance.tolerance_mm is not None
                    else 0.05
                )
                actual = await self.facade.measure_bounding_box()
                metrics["bounding_box_mm"] = actual
                if not _is_finite_vector(actual):
                    return _invalid_numeric_result(
                        evidence=evidence,
                        metrics=metrics,
                        message="bounding box contains invalid numeric evidence",
                        details={"assertion": check_type},
                    )
                if not _bbox_close(actual, expected, tolerance):
                    issues.append(
                        VerificationIssue(
                            code=_classify_bbox(expected, actual),
                            message="bounding box mismatch",
                            details={
                                "expected": expected,
                                "actual": actual,
                                "tolerance_mm": tolerance,
                            },
                        )
                    )
            elif check_type == "target_bounding_box":
                target = str(acceptance.target or "")
                expected = acceptance.target_mm or []
                tolerance = (
                    acceptance.tolerance_mm
                    if acceptance.tolerance_mm is not None
                    else 0.05
                )
                actual = await self.facade.measure_bounding_box(target)
                metrics[f"{target}_bounding_box_mm"] = actual
                if not _is_finite_vector(actual):
                    return _invalid_numeric_result(
                        evidence=evidence,
                        metrics=metrics,
                        message="target bounding box contains invalid numeric evidence",
                        details={"assertion": check_type, "target": target},
                    )
                if not _bbox_close(actual, expected, tolerance):
                    issues.append(
                        VerificationIssue(
                            code=_classify_bbox(expected, actual),
                            message=f"bounding box mismatch for {target}",
                            details={
                                "target": target,
                                "expected": expected,
                                "actual": actual,
                                "tolerance_mm": tolerance,
                            },
                        )
                    )
            elif check_type == "component_exists":
                component_name = str(acceptance.target or "")
                if component_name not in state.get("components", {}):
                    issues.append(
                        VerificationIssue(
                            code=FailureCode.INVALID_REFERENCE,
                            message="missing component",
                            details={"missing": component_name},
                        )
                    )
            elif check_type == "named_bodies":
                missing = [
                    name
                    for name in acceptance.target or []
                    if name not in state.get("bodies", {})
                ]
                if missing:
                    issues.append(
                        VerificationIssue(
                            code=FailureCode.INVALID_REFERENCE,
                            message="missing named bodies",
                            details={"missing": missing},
                        )
                    )
            elif check_type == "named_parameters":
                missing = [
                    name
                    for name in acceptance.target or []
                    if name not in state.get("parameters", {})
                ]
                if missing:
                    issues.append(
                        VerificationIssue(
                            code=FailureCode.INVALID_REFERENCE,
                            message="missing named parameters",
                            details={"missing": missing},
                        )
                    )
            elif check_type == "nema17_dimensions":
                expected_metrics = acceptance.target or {}
                actual_metrics = state.get("nema17_metrics", {})
                tolerance = (
                    acceptance.tolerance_mm
                    if acceptance.tolerance_mm is not None
                    else 0.05
                )
                metrics["nema17_metrics"] = actual_metrics
                dimension_issues = _metric_mismatches(
                    actual_metrics, expected_metrics, tolerance
                )
                if dimension_issues:
                    issues.append(
                        VerificationIssue(
                            code=FailureCode.FEATURE_CREATION_FAILED,
                            message="NEMA17 measured dimensions mismatch",
                            details={
                                "mismatches": dimension_issues,
                                "actual": actual_metrics,
                                "expected": expected_metrics,
                            },
                        )
                    )
            elif check_type == "nema17_polish_details":
                expected_metrics = acceptance.target or {}
                actual_metrics = state.get("polish_metrics", {})
                metrics["polish_metrics"] = actual_metrics
                polish_issues = _polish_mismatches(actual_metrics, expected_metrics)
                if polish_issues:
                    issues.append(
                        VerificationIssue(
                            code=FailureCode.FEATURE_CREATION_FAILED,
                            message="NEMA17 polish details mismatch",
                            details={
                                "mismatches": polish_issues,
                                "actual": actual_metrics,
                                "expected": expected_metrics,
                            },
                        )
                    )
            elif check_type == "nema17_external_assembly":
                expected_metrics = acceptance.target or {}
                actual_metrics = state.get("assembly_metrics", {})
                metrics["assembly_metrics"] = actual_metrics
                assembly_issues = _assembly_mismatches(actual_metrics, expected_metrics)
                if assembly_issues:
                    issues.append(
                        VerificationIssue(
                            code=FailureCode.FEATURE_CREATION_FAILED,
                            message="NEMA17 external assembly structure mismatch",
                            details={
                                "mismatches": assembly_issues,
                                "actual": actual_metrics,
                                "expected": expected_metrics,
                            },
                        )
                    )
            elif check_type == "profile2020_details":
                expected_metrics = acceptance.target or {}
                actual_metrics = state.get("profile2020_metrics", {})
                tolerance = (
                    acceptance.tolerance_mm
                    if acceptance.tolerance_mm is not None
                    else 0.05
                )
                metrics["profile2020_metrics"] = actual_metrics
                profile_issues = _profile2020_mismatches(
                    actual_metrics, expected_metrics, tolerance
                )
                if profile_issues:
                    issues.append(
                        VerificationIssue(
                            code=FailureCode.FEATURE_CREATION_FAILED,
                            message="2020 aluminum profile details mismatch",
                            details={
                                "mismatches": profile_issues,
                                "actual": actual_metrics,
                                "expected": expected_metrics,
                            },
                        )
                    )
            elif check_type == "mgn12_linear_rail_assembly":
                expected_metrics = acceptance.target or {}
                actual_metrics = state.get("mgn12_metrics", {})
                tolerance = (
                    acceptance.tolerance_mm
                    if acceptance.tolerance_mm is not None
                    else 0.05
                )
                metrics["mgn12_metrics"] = actual_metrics
                mgn12_issues = _mgn12_mismatches(
                    actual_metrics, expected_metrics, tolerance
                )
                if mgn12_issues:
                    issues.append(
                        VerificationIssue(
                            code=FailureCode.FEATURE_CREATION_FAILED,
                            message="MGN12 linear rail assembly mismatch",
                            details={
                                "mismatches": mgn12_issues,
                                "actual": actual_metrics,
                                "expected": expected_metrics,
                            },
                        )
                    )
            elif check_type == "desktop_cnc_assembly":
                expected_metrics = acceptance.target or {}
                actual_metrics = state.get("cnc_metrics", {})
                tolerance = (
                    acceptance.tolerance_mm
                    if acceptance.tolerance_mm is not None
                    else 0.05
                )
                metrics["cnc_metrics"] = actual_metrics
                cnc_issues = _cnc_mismatches(
                    actual_metrics, expected_metrics, tolerance
                )
                if cnc_issues:
                    issues.append(
                        VerificationIssue(
                            code=FailureCode.FEATURE_CREATION_FAILED,
                            message="desktop CNC assembly mismatch",
                            details={
                                "mismatches": cnc_issues,
                                "actual": actual_metrics,
                                "expected": expected_metrics,
                            },
                        )
                    )
            elif check_type == "component_metadata":
                metadata = _metadata_state(state)
                metrics["component_metadata"] = metadata
                metadata_issues = _metadata_mismatches(
                    metadata,
                    [item.model_dump(mode="json") for item in spec.component_metadata],
                )
                if metadata_issues:
                    issues.append(
                        VerificationIssue(
                            code=FailureCode.METADATA_MISSING,
                            message="component metadata contract mismatch",
                            details={"mismatches": metadata_issues},
                        )
                    )
            elif check_type == "joint_contract":
                joints = state.get("joints") or {}
                metrics["joints"] = joints
                joint_issues = _joint_mismatches(
                    joints, [item.model_dump(mode="json") for item in spec.joints]
                )
                if joint_issues:
                    issues.append(
                        VerificationIssue(
                            code=FailureCode.JOINT_MISMATCH,
                            message="assembly joint contract mismatch",
                            details={"mismatches": joint_issues},
                        )
                    )
            elif check_type == "occurrence_contract":
                occurrences = state.get("occurrences") or {}
                metrics["occurrences"] = occurrences
                occurrence_issues = _occurrence_mismatches(
                    occurrences, state.get("components") or {}, acceptance.target or {}
                )
                if occurrence_issues:
                    issues.append(
                        VerificationIssue(
                            code=FailureCode.JOINT_MISMATCH,
                            message="assembly occurrence contract mismatch",
                            details={"mismatches": occurrence_issues},
                        )
                    )
            elif check_type == "interference_free":
                try:
                    interference_payload = await self.facade.analyze_interference()
                    candidate = (
                        interference_payload.get("interference")
                        if isinstance(interference_payload, dict)
                        else None
                    )
                    interference = candidate if isinstance(candidate, dict) else {}
                except Exception:  # noqa: BLE001 - verifier must normalize facade failures
                    interference = {}
                interference_unavailable = bool(
                    not interference
                    or interference.get("count") is None
                    or interference.get("error")
                    or interference.get("analysis_warning")
                )
                if interference_unavailable:
                    public_error = _public_downstream_failure()
                    interference = {"count": None, "pairs": [], "error": public_error}
                else:
                    interference = {
                        "count": interference.get("count"),
                        "pairs": interference.get("pairs") or [],
                    }
                metrics["interference"] = interference
                if interference_unavailable:
                    return _incomplete_auxiliary_result(
                        evidence=evidence,
                        metrics=metrics,
                        assertion=check_type,
                        public_error=public_error,
                    )
                if not _is_nonnegative_integer(
                    interference.get("count")
                ) or _contains_non_finite_number(
                    {
                        key: value
                        for key, value in interference.items()
                        if key not in {"error", "analysis_warning"}
                    }
                ):
                    return _invalid_numeric_result(
                        evidence=evidence,
                        metrics=metrics,
                        message="interference analysis contains invalid numeric evidence",
                        details={"assertion": check_type},
                    )
                interference_issues = _interference_mismatches(
                    interference, acceptance.target or {}
                )
                if interference_issues:
                    issues.append(
                        VerificationIssue(
                            code=FailureCode.INTERFERENCE_DETECTED,
                            message="interference analysis failed or detected unapproved interference",
                            details={
                                "mismatches": interference_issues,
                                "interference": interference,
                            },
                        )
                    )
            elif check_type == "physical_properties":
                targets = [item.component for item in spec.component_metadata]
                try:
                    measured_payload = await self.facade.measure_physical_properties(
                        targets
                    )
                    candidate = (
                        measured_payload.get("physical_properties")
                        if isinstance(measured_payload, dict)
                        else None
                    )
                    physical_properties = (
                        candidate if isinstance(candidate, dict) else {}
                    )
                except Exception:  # noqa: BLE001 - verifier must normalize facade failures
                    physical_properties = {}
                physical_properties_unavailable = bool(
                    physical_properties.get("_error")
                    or any(
                        not isinstance(physical_properties.get(target), dict)
                        or "mass_kg" not in physical_properties[target]
                        or "volume_mm3" not in physical_properties[target]
                        or physical_properties[target].get("error")
                        or physical_properties[target].get("error_message")
                        or physical_properties[target].get("_error")
                        for target in targets
                    )
                )
                if physical_properties_unavailable:
                    public_error = _public_downstream_failure()
                    physical_properties = {"_error": public_error}
                else:
                    physical_properties = {
                        target: {
                            "mass_kg": physical_properties[target].get("mass_kg"),
                            "volume_mm3": physical_properties[target].get("volume_mm3"),
                        }
                        for target in targets
                    }
                metrics["physical_properties"] = physical_properties
                if physical_properties_unavailable:
                    return _incomplete_auxiliary_result(
                        evidence=evidence,
                        metrics=metrics,
                        assertion=check_type,
                        public_error=public_error,
                    )
                invalid_physical_metrics = [
                    f"{target}.{field}"
                    for target in targets
                    for field in ("mass_kg", "volume_mm3")
                    if not _is_finite_numeric_scalar(
                        physical_properties[target].get(field)
                    )
                ]
                if invalid_physical_metrics or _contains_non_finite_number(
                    {
                        key: value
                        for key, value in physical_properties.items()
                        if key != "_error"
                    }
                ):
                    return _invalid_numeric_result(
                        evidence=evidence,
                        metrics=metrics,
                        message="physical properties contain invalid numeric evidence",
                        details={
                            "assertion": check_type,
                            "invalid_metrics": invalid_physical_metrics,
                        },
                    )
                physical_issues = _physical_property_mismatches(
                    physical_properties, targets
                )
                if physical_issues:
                    issues.append(
                        VerificationIssue(
                            code=FailureCode.PHYSICAL_PROPERTY_MISMATCH,
                            message="physical property contract mismatch",
                            details={"mismatches": physical_issues},
                        )
                    )
            elif check_type == "screenshots_exist":
                screenshots = state.get("screenshots") or {}
                metrics["screenshots"] = screenshots
                screenshot_issues = _screenshot_mismatches(
                    screenshots, [item.model_dump(mode="json") for item in spec.outputs]
                )
                if screenshot_issues:
                    issues.append(
                        VerificationIssue(
                            code=FailureCode.SCREENSHOT_FAILED,
                            message="expected viewport screenshots are missing or empty",
                            details={"mismatches": screenshot_issues},
                        )
                    )
            elif check_type == "named_objects":
                named = await self.facade.validate_named_objects()
                metrics["named_objects"] = named
                if not named.get("valid", False):
                    issues.append(
                        VerificationIssue(
                            code=FailureCode.NAME_COLLISION,
                            message="invalid or generated object names",
                            details=named,
                        )
                    )
            elif check_type == "hole_count":
                expected = int(acceptance.target)
                actual = metrics["hole_count"]
                if actual != expected:
                    issues.append(
                        _issue(
                            FailureCode.FEATURE_CREATION_FAILED,
                            "hole count mismatch",
                            expected,
                            actual,
                        )
                    )
            elif check_type == "feature_health":
                bad = [
                    feature
                    for feature in state.get("features", {}).values()
                    if feature.get("health", "ok") not in {"ok", "healthy"}
                ]
                if bad:
                    issues.append(
                        VerificationIssue(
                            code=FailureCode.FEATURE_SUPPRESSED_OR_FAILED,
                            message="one or more features are unhealthy",
                            details={"features": bad},
                        )
                    )
            elif check_type == "export_exists":
                paths = acceptance.target or []
                missing = [
                    path
                    for path in paths
                    if not Path(path).exists() or Path(path).stat().st_size == 0
                ]
                if missing:
                    issues.append(
                        VerificationIssue(
                            code=FailureCode.EXPORT_FAILED,
                            message="expected export files are missing or empty",
                            details={"missing": missing},
                        )
                    )
            else:  # pragma: no cover - registry/dispatcher drift guard
                return VerificationResult(
                    passed=False,
                    status=DecisionStatus.FAILED,
                    reason_codes=[DecisionReasonCode.UNSUPPORTED_ASSERTION],
                    issues=[
                        VerificationIssue(
                            code=FailureCode.UNSUPPORTED_ASSERTION,
                            message="registered assertion has no verifier dispatch branch",
                            details={"type": check_type},
                        )
                    ],
                    metrics=metrics,
                    evidence=evidence,
                )

        final_evidence = evidence.model_copy(
            update={"evaluated_count": len(spec.acceptance_tests)}
        )
        status = DecisionStatus.PASSED if not issues else DecisionStatus.FAILED
        reason = (
            DecisionReasonCode.VERIFIED
            if status is DecisionStatus.PASSED
            else DecisionReasonCode.ASSERTION_FAILED
        )
        return VerificationResult(
            passed=status is DecisionStatus.PASSED,
            status=status,
            reason_codes=[reason],
            issues=issues,
            metrics=metrics,
            evidence=final_evidence,
        )


def _evidence_envelope(
    payload: dict[str, Any],
    state: dict[str, Any],
    *,
    assertion_ids: list[str],
    evaluated_count: int,
) -> EvidenceEnvelope:
    provenance = payload.get("provenance")
    safe_provenance = {
        str(key): str(value)
        for key, value in (provenance.items() if isinstance(provenance, dict) else [])
        if isinstance(key, str) and isinstance(value, str)
    }
    document_identity = payload.get("document_identity")
    if not isinstance(document_identity, str) or not document_identity:
        document_identity = state.get("document_identity") or state.get(
            "active_document_name"
        )
    return EvidenceEnvelope(
        producer=str(payload.get("producer") or "fusion_facade.inspect_design"),
        provenance=safe_provenance,
        document_identity=str(document_identity) if document_identity else None,
        complete=payload.get("complete") is True,
        counts_exact=payload.get("counts_exact") is True,
        truncated=payload.get("truncated") is True,
        stop_reason=str(payload.get("stop_reason"))
        if payload.get("stop_reason") is not None
        else None,
        metrics_finite=True,
        assertion_ids=assertion_ids,
        assertion_count=len(assertion_ids),
        evaluated_count=evaluated_count,
    )


def _inspection_is_complete(evidence: EvidenceEnvelope) -> bool:
    return bool(
        evidence.complete
        and evidence.counts_exact
        and not evidence.truncated
        and evidence.stop_reason in (None, "", "complete")
    )


def _invalid_numeric_result(
    *,
    evidence: EvidenceEnvelope,
    metrics: dict[str, Any],
    message: str,
    details: dict[str, Any],
) -> VerificationResult:
    invalid_evidence = evidence.model_copy(update={"metrics_finite": False})
    return VerificationResult.incomplete_result(
        evidence=invalid_evidence,
        metrics=metrics,
        reason=DecisionReasonCode.INVALID_NUMERIC_EVIDENCE,
        issues=[
            VerificationIssue(
                code=FailureCode.INVALID_NUMERIC_EVIDENCE,
                message=message,
                details=details,
            )
        ],
    )


def _public_downstream_failure() -> dict[str, Any]:
    return PublicError.downstream_failure().model_dump(mode="json")


def _incomplete_auxiliary_result(
    *,
    evidence: EvidenceEnvelope,
    metrics: dict[str, Any],
    assertion: str,
    public_error: dict[str, Any],
) -> VerificationResult:
    incomplete_evidence = evidence.model_copy(
        update={"complete": False, "stop_reason": "downstream_unavailable"}
    )
    return VerificationResult.incomplete_result(
        evidence=incomplete_evidence,
        metrics=metrics,
        issues=[
            VerificationIssue(
                code=FailureCode.INCOMPLETE_INSPECTION,
                message="required downstream verification evidence is unavailable",
                details={"assertion": assertion, "error": public_error},
            )
        ],
    )


def _contains_non_finite_number(value: Any, *, reject_boole: bool = True) -> bool:
    if isinstance(value, bool):
        return reject_boole
    if isinstance(value, int | float):
        return not math.isfinite(float(value))
    if isinstance(value, dict):
        return any(
            _contains_non_finite_number(item, reject_boole=reject_boole)
            for item in value.values()
        )
    if isinstance(value, list | tuple):
        return any(
            _contains_non_finite_number(item, reject_boole=reject_boole)
            for item in value
        )
    return False


def _is_nonnegative_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_finite_numeric_scalar(value: Any) -> bool:
    return bool(
        not isinstance(value, bool)
        and isinstance(value, int | float)
        and math.isfinite(float(value))
    )


def _is_finite_vector(value: Any) -> bool:
    return bool(
        isinstance(value, list | tuple)
        and value
        and all(
            not isinstance(item, bool)
            and isinstance(item, int | float)
            and math.isfinite(float(item))
            for item in value
        )
    )


def _invalid_registry_numeric_evidence(
    state: dict[str, Any], assertion_ids: list[str]
) -> list[str]:
    invalid: list[str] = []
    for assertion in set(assertion_ids):
        declaration = _REGISTRY_NUMERIC_EVIDENCE.get(assertion)
        if declaration is None:
            continue
        section_name, scalar_fields, integer_fields, vector_fields = declaration
        section = state.get(section_name)
        if not isinstance(section, dict):
            continue
        for field in scalar_fields:
            value = section.get(field)
            if value is not None and not _is_finite_numeric_scalar(value):
                invalid.append(f"{section_name}.{field}")
        for field in integer_fields:
            value = section.get(field)
            if value is not None and not _is_nonnegative_integer(value):
                invalid.append(f"{section_name}.{field}")
        for field in vector_fields:
            value = section.get(field)
            if value is not None and not _is_finite_vector(value):
                invalid.append(f"{section_name}.{field}")

    if "hole_count" in assertion_ids:
        bodies = state.get("bodies")
        if isinstance(bodies, dict):
            for name, body in bodies.items():
                if not isinstance(body, dict) or "holes" not in body:
                    continue
                if not _is_nonnegative_integer(body["holes"]):
                    invalid.append(f"bodies.{name}.holes")

    if "screenshots_exist" in assertion_ids:
        screenshots = state.get("screenshots")
        if isinstance(screenshots, dict):
            for name, screenshot in screenshots.items():
                if not isinstance(screenshot, dict) or "bytes" not in screenshot:
                    continue
                if not _is_nonnegative_integer(screenshot["bytes"]):
                    invalid.append(f"screenshots.{name}.bytes")
    return sorted(set(invalid))


def _metadata_state(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    metadata: dict[str, dict[str, Any]] = dict(state.get("component_metadata") or {})
    for name, component in (state.get("components") or {}).items():
        if isinstance(component, dict) and component.get("metadata"):
            metadata.setdefault(str(name), dict(component["metadata"]))
    return metadata


def _metadata_mismatches(
    actual: dict[str, dict[str, Any]], expected: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []
    required_fields = (
        "part_number",
        "description",
        "role",
        "source_type",
        "physical_material",
    )
    for item in expected:
        component = item["component"]
        actual_item = actual.get(component)
        if not actual_item:
            mismatches.append({"component": component, "missing": True})
            continue
        for field in required_fields:
            expected_value = item.get(field)
            actual_value = actual_item.get(field)
            if expected_value and not actual_value:
                mismatches.append(
                    {
                        "component": component,
                        "field": field,
                        "expected": expected_value,
                        "actual": actual_value,
                    }
                )
            elif expected_value and _normalize_scalar(
                actual_value
            ) != _normalize_scalar(expected_value):
                mismatches.append(
                    {
                        "component": component,
                        "field": field,
                        "expected": expected_value,
                        "actual": actual_value,
                    }
                )
    return mismatches


def _joint_mismatches(
    actual: dict[str, dict[str, Any]], expected: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []
    valid_health = {"ok", "healthy", None, ""}
    for item in expected:
        actual_item = actual.get(item["name"])
        if not actual_item:
            mismatches.append({"joint": item["name"], "missing": True})
            continue
        for field in ("type", "parent", "child", "axis"):
            expected_value = item.get(field)
            if expected_value is None:
                continue
            actual_value = actual_item.get(field)
            if _normalize_scalar(actual_value) != _normalize_scalar(expected_value):
                mismatches.append(
                    {
                        "joint": item["name"],
                        "field": field,
                        "expected": expected_value,
                        "actual": actual_value,
                    }
                )
        if actual_item.get("health") not in valid_health:
            mismatches.append(
                {
                    "joint": item["name"],
                    "field": "health",
                    "expected": "ok",
                    "actual": actual_item.get("health"),
                }
            )
    return mismatches


def _occurrence_mismatches(
    occurrences: dict[str, dict[str, Any]],
    components: dict[str, Any],
    expected: dict[str, Any],
) -> list[dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []
    expected_names = list(expected.get("occurrence_names") or [])
    if expected_names:
        missing = [name for name in expected_names if name not in occurrences]
        if missing:
            mismatches.append({"key": "occurrence_names", "missing": missing})
        component = expected.get("component")
        if component:
            wrong_component = [
                {
                    "occurrence": name,
                    "actual": (occurrences.get(name) or {}).get("component"),
                    "expected": component,
                }
                for name in expected_names
                if name in occurrences
                and (occurrences.get(name) or {}).get("component") != component
            ]
            if wrong_component:
                mismatches.append(
                    {"key": "occurrence_components", "mismatches": wrong_component}
                )
            visible_component_count = sum(
                1
                for item in occurrences.values()
                if item.get("component") == component
                and item.get("visible", True) is not False
            )
            if expected.get("count") is not None and visible_component_count != int(
                expected["count"]
            ):
                mismatches.append(
                    {
                        "key": "visible_component_occurrence_count",
                        "expected": int(expected["count"]),
                        "actual": visible_component_count,
                    }
                )
        if expected.get("count") is not None and len(
            [name for name in expected_names if name in occurrences]
        ) != int(expected["count"]):
            mismatches.append(
                {
                    "key": "occurrence_count",
                    "expected": int(expected["count"]),
                    "actual": len(
                        [name for name in expected_names if name in occurrences]
                    ),
                }
            )

    component_names = list(expected.get("component_names") or [])
    if component_names:
        missing_components = [
            name for name in component_names if name not in components
        ]
        if missing_components:
            mismatches.append({"key": "component_names", "missing": missing_components})
        actual_count = sum(
            1
            for item in occurrences.values()
            if item.get("component") in component_names
            and item.get("visible", True) is not False
        )
        if expected.get("count") is not None and actual_count != int(expected["count"]):
            mismatches.append(
                {
                    "key": "component_occurrence_count",
                    "expected": int(expected["count"]),
                    "actual": actual_count,
                }
            )
    return mismatches


def _interference_mismatches(
    actual: dict[str, Any], expected: dict[str, Any]
) -> list[dict[str, Any]]:
    if actual.get("error"):
        return [{"key": "analysis", "error": actual["error"]}]
    if actual.get("analysis_warning"):
        return [{"key": "analysis", "error": actual["analysis_warning"]}]
    count = actual.get("count")
    if count is None:
        return [{"key": "count", "expected": 0, "actual": None}]
    pairs = actual.get("pairs") or []
    allowed = {
        frozenset((str(pair[0]), str(pair[1])))
        for pair in expected.get("allowed_contact_pairs") or []
    }
    if int(count) == 0:
        return []
    if not pairs:
        return [{"key": "count", "expected": 0, "actual": count}]
    unapproved = []
    for pair in pairs:
        if isinstance(pair, dict):
            pair_set = frozenset((str(pair.get("a", "")), str(pair.get("b", ""))))
        else:
            pair_set = frozenset(str(item) for item in pair)
        if pair_set not in allowed:
            unapproved.append(pair)
    if unapproved:
        return [{"key": "pairs", "unapproved": unapproved}]
    return []


def _physical_property_mismatches(
    actual: dict[str, Any], targets: list[str]
) -> list[dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []
    if actual.get("_error"):
        mismatches.append({"key": "measurement", "error": actual["_error"]})
    for target in targets:
        payload = actual.get(target)
        if not payload:
            mismatches.append({"target": target, "missing": True})
            continue
        mass_value = payload.get("mass_kg")
        volume_value = payload.get("volume_mm3")
        if not _is_finite_numeric_scalar(mass_value) or not _is_finite_numeric_scalar(
            volume_value
        ):
            mismatches.append(
                {
                    "target": target,
                    "mass_kg": mass_value,
                    "volume_mm3": volume_value,
                    "invalid_numeric_evidence": True,
                }
            )
            continue
        mass = float(mass_value or 0)
        volume = float(volume_value or 0)
        if mass <= 0 or volume <= 0:
            mismatches.append({"target": target, "mass_kg": mass, "volume_mm3": volume})
    return mismatches


def _screenshot_mismatches(
    actual: dict[str, dict[str, Any]], expected: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []
    for item in expected:
        payload = actual.get(item["name"]) or {}
        candidates = []
        if payload.get("path"):
            candidates.append(Path(str(payload["path"])))
        configured = Path(str(item["path"]))
        candidates.append(configured)
        if not configured.is_absolute():
            candidates.append(Path("outputs") / configured)
        if payload.get("bytes") and int(payload["bytes"]) > 0:
            continue
        if not any(path.exists() and path.stat().st_size > 0 for path in candidates):
            mismatches.append(
                {"output": item["name"], "paths": [str(path) for path in candidates]}
            )
    return mismatches


def _normalize_scalar(value: Any) -> str:
    return str(value or "").strip().lower()


def _issue(
    code: FailureCode, message: str, expected: Any, actual: Any
) -> VerificationIssue:
    return VerificationIssue(
        code=code, message=message, details={"expected": expected, "actual": actual}
    )


def _bbox_close(actual: list[float], expected: list[float], tolerance: float) -> bool:
    if len(actual) != 3 or len(expected) != 3:
        return False
    return all(abs(a - e) <= tolerance for a, e in zip(actual, expected, strict=True))


def _classify_bbox(expected: list[float], actual: list[float]) -> FailureCode:
    if len(actual) == 3 and len(expected) == 3:
        ratios = []
        for exp, act in zip(expected, actual, strict=True):
            if exp:
                ratios.append(act / exp)
        if ratios and all(
            abs(ratio - 10.0) < 0.05 or abs(ratio - 0.1) < 0.05 for ratio in ratios
        ):
            return FailureCode.UNIT_MISMATCH
        if ratios and all(
            abs(ratio - 25.4) < 0.1 or abs(ratio - (1 / 25.4)) < 0.01
            for ratio in ratios
        ):
            return FailureCode.UNIT_MISMATCH
    return FailureCode.FEATURE_CREATION_FAILED


def _metric_mismatches(
    actual: dict[str, Any], expected: dict[str, Any], tolerance: float
) -> list[dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []
    for key, expected_value in expected.items():
        actual_value = actual.get(key)
        if isinstance(expected_value, list):
            if not isinstance(actual_value, list) or len(actual_value) != len(
                expected_value
            ):
                mismatches.append(
                    {"key": key, "expected": expected_value, "actual": actual_value}
                )
                continue
            for index, (actual_item, expected_item) in enumerate(
                zip(actual_value, expected_value, strict=True)
            ):
                if abs(float(actual_item) - float(expected_item)) > tolerance:
                    mismatches.append(
                        {
                            "key": key,
                            "index": index,
                            "expected": expected_item,
                            "actual": actual_item,
                        }
                    )
        elif isinstance(expected_value, int):
            if int(actual_value or 0) != expected_value:
                mismatches.append(
                    {"key": key, "expected": expected_value, "actual": actual_value}
                )
        elif (
            actual_value is None
            or abs(float(actual_value) - float(expected_value)) > tolerance
        ):
            mismatches.append(
                {"key": key, "expected": expected_value, "actual": actual_value}
            )
    return mismatches


def _polish_mismatches(
    actual: dict[str, Any], expected: dict[str, Any]
) -> list[dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []
    body_names = set(actual.get("body_names") or [])
    for body_name in expected.get("required_bodies") or []:
        if body_name not in body_names:
            mismatches.append({"key": "required_bodies", "missing": body_name})
    minimum_laminations = int(expected.get("min_lamination_bodies") or 0)
    actual_laminations = int(actual.get("lamination_body_count") or 0)
    if actual_laminations < minimum_laminations:
        mismatches.append(
            {
                "key": "lamination_body_count",
                "expected_min": minimum_laminations,
                "actual": actual_laminations,
            }
        )
    for key in ("wire_count", "screw_shadow_count"):
        if key in expected and int(actual.get(key) or 0) != int(expected[key]):
            mismatches.append(
                {"key": key, "expected": int(expected[key]), "actual": actual.get(key)}
            )
    return mismatches


def _assembly_mismatches(
    actual: dict[str, Any], expected: dict[str, Any]
) -> list[dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []
    if (
        expected.get("assembly_component")
        and actual.get("assembly_component") != expected["assembly_component"]
    ):
        mismatches.append(
            {
                "key": "assembly_component",
                "expected": expected["assembly_component"],
                "actual": actual.get("assembly_component"),
            }
        )

    actual_components = set(actual.get("component_names") or [])
    for component_name in expected.get("required_components") or []:
        if component_name not in actual_components:
            mismatches.append({"key": "required_components", "missing": component_name})

    actual_bodies = set(actual.get("body_names") or [])
    body_components = actual.get("body_components") or {}
    actual_component_names = actual_components | {
        str(name) for name in body_components.values()
    }
    for body_name in expected.get("required_bodies") or []:
        if body_name not in actual_bodies:
            mismatches.append({"key": "required_bodies", "missing": body_name})
            continue
        owner = body_components.get(body_name)
        if (
            owner not in actual_component_names
            or owner in {"root", ""}
            or str(owner).startswith("(")
        ):
            mismatches.append(
                {"key": "body_components", "body": body_name, "invalid_owner": owner}
            )

    minimum_laminations = int(expected.get("min_stator_lamination_count") or 0)
    actual_laminations = int(actual.get("stator_lamination_count") or 0)
    if actual_laminations < minimum_laminations:
        mismatches.append(
            {
                "key": "stator_lamination_count",
                "expected_min": minimum_laminations,
                "actual": actual_laminations,
            }
        )

    if "wire_count" in expected and int(actual.get("wire_count") or 0) != int(
        expected["wire_count"]
    ):
        mismatches.append(
            {
                "key": "wire_count",
                "expected": int(expected["wire_count"]),
                "actual": actual.get("wire_count"),
            }
        )
    if bool(actual.get("connector_present")) is False:
        mismatches.append(
            {
                "key": "connector_present",
                "expected": True,
                "actual": actual.get("connector_present"),
            }
        )

    max_legacy = int(expected.get("max_legacy_visible_nema17_body_count", 0))
    legacy_visible = int(actual.get("legacy_visible_nema17_body_count") or 0)
    if legacy_visible > max_legacy:
        mismatches.append(
            {
                "key": "legacy_visible_nema17_body_count",
                "expected_max": max_legacy,
                "actual": legacy_visible,
            }
        )
    return mismatches


def _profile2020_mismatches(
    actual: dict[str, Any], expected: dict[str, Any], tolerance: float
) -> list[dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []
    for key in ("component", "body"):
        if expected.get(key) and actual.get(key) != expected[key]:
            mismatches.append(
                {"key": key, "expected": expected[key], "actual": actual.get(key)}
            )

    for key in (
        "size_mm",
        "length_mm",
        "slot_width_mm",
        "slot_depth_mm",
        "center_bore_diameter_mm",
    ):
        if key not in expected:
            continue
        actual_value = actual.get(key)
        if (
            actual_value is None
            or abs(float(actual_value) - float(expected[key])) > tolerance
        ):
            mismatches.append(
                {"key": key, "expected": expected[key], "actual": actual_value}
            )

    for key in ("slot_count", "web_relief_count"):
        if key in expected and int(actual.get(key) or 0) != int(expected[key]):
            mismatches.append(
                {"key": key, "expected": int(expected[key]), "actual": actual.get(key)}
            )

    if not bool(actual.get("center_bore_present")):
        mismatches.append(
            {
                "key": "center_bore_present",
                "expected": True,
                "actual": actual.get("center_bore_present"),
            }
        )

    material = str(actual.get("material", "")).lower()
    if "aluminum" not in material and "alum" not in material:
        mismatches.append(
            {
                "key": "material",
                "expected_contains": "aluminum",
                "actual": actual.get("material"),
            }
        )
    return mismatches


def _mgn12_mismatches(
    actual: dict[str, Any], expected: dict[str, Any], tolerance: float
) -> list[dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []
    if (
        expected.get("assembly_component")
        and actual.get("assembly_component") != expected["assembly_component"]
    ):
        mismatches.append(
            {
                "key": "assembly_component",
                "expected": expected["assembly_component"],
                "actual": actual.get("assembly_component"),
            }
        )

    actual_components = set(actual.get("component_names") or [])
    for component_name in expected.get("required_components") or []:
        if component_name not in actual_components:
            mismatches.append({"key": "required_components", "missing": component_name})

    actual_bodies = set(actual.get("body_names") or [])
    body_components = actual.get("body_components") or {}
    for body_name in expected.get("required_bodies") or []:
        if body_name not in actual_bodies:
            mismatches.append({"key": "required_bodies", "missing": body_name})
            continue
        owner = body_components.get(body_name)
        if owner in {None, "", "root"} or str(owner).startswith("("):
            mismatches.append(
                {"key": "body_components", "body": body_name, "invalid_owner": owner}
            )

    numeric_keys = (
        "rail_length_mm",
        "rail_width_mm",
        "rail_height_mm",
        "rail_hole_pitch_mm",
        "carriage_length_mm",
        "carriage_width_mm",
        "carriage_total_height_mm",
    )
    for key in numeric_keys:
        if key not in expected:
            continue
        actual_value = actual.get(key)
        if (
            actual_value is None
            or abs(float(actual_value) - float(expected[key])) > tolerance
        ):
            mismatches.append(
                {"key": key, "expected": expected[key], "actual": actual_value}
            )

    for key in (
        "rail_mount_hole_count",
        "rail_counterbore_count",
        "carriage_mount_hole_count",
    ):
        if key in expected and int(actual.get(key) or 0) != int(expected[key]):
            mismatches.append(
                {"key": key, "expected": int(expected[key]), "actual": actual.get(key)}
            )

    expected_spacing = expected.get("carriage_mount_spacing_mm")
    actual_spacing = actual.get("carriage_mount_spacing_mm")
    if expected_spacing:
        if not isinstance(actual_spacing, list) or len(actual_spacing) != 2:
            mismatches.append(
                {
                    "key": "carriage_mount_spacing_mm",
                    "expected": expected_spacing,
                    "actual": actual_spacing,
                }
            )
        else:
            for index, (actual_item, expected_item) in enumerate(
                zip(actual_spacing, expected_spacing, strict=True)
            ):
                if abs(float(actual_item) - float(expected_item)) > tolerance:
                    mismatches.append(
                        {
                            "key": "carriage_mount_spacing_mm",
                            "index": index,
                            "expected": expected_item,
                            "actual": actual_item,
                        }
                    )

    max_legacy = int(expected.get("max_legacy_visible_mgn12_body_count", 0))
    legacy_visible = int(actual.get("legacy_visible_mgn12_body_count") or 0)
    if legacy_visible > max_legacy:
        mismatches.append(
            {
                "key": "legacy_visible_mgn12_body_count",
                "expected_max": max_legacy,
                "actual": legacy_visible,
            }
        )

    for key in ("rail_material", "carriage_material"):
        material = str(actual.get(key, ""))
        material_tokens = _normalized_tokens(material)
        if material and not (
            material_tokens & {"steel", "aco", "inox", "inoxidavel", "stainless"}
        ):
            mismatches.append(
                {"key": key, "expected_contains": "steel", "actual": actual.get(key)}
            )
    return mismatches


def _cnc_mismatches(
    actual: dict[str, Any], expected: dict[str, Any], tolerance: float
) -> list[dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []
    if (
        expected.get("assembly_component")
        and actual.get("assembly_component") != expected["assembly_component"]
    ):
        mismatches.append(
            {
                "key": "assembly_component",
                "expected": expected["assembly_component"],
                "actual": actual.get("assembly_component"),
            }
        )

    actual_components = set(actual.get("component_names") or [])
    for component_name in expected.get("required_components") or []:
        if component_name not in actual_components:
            mismatches.append({"key": "required_components", "missing": component_name})

    actual_bodies = set(actual.get("body_names") or [])
    body_components = actual.get("body_components") or {}
    for body_name in expected.get("required_bodies") or []:
        if body_name not in actual_bodies:
            mismatches.append({"key": "required_bodies", "missing": body_name})
            continue
        owner = body_components.get(body_name)
        if owner in {None, "", "root"} or str(owner).startswith("("):
            mismatches.append(
                {"key": "body_components", "body": body_name, "invalid_owner": owner}
            )

    for key in (
        "profile_count",
        "rail_count",
        "motor_count",
        "leadscrew_count",
        "coupler_count",
    ):
        if key in expected and int(actual.get(key) or 0) != int(expected[key]):
            mismatches.append(
                {"key": key, "expected": int(expected[key]), "actual": actual.get(key)}
            )

    if "spindle_diameter_mm" in expected:
        actual_diameter = actual.get("spindle_diameter_mm")
        if (
            actual_diameter is None
            or abs(float(actual_diameter) - float(expected["spindle_diameter_mm"]))
            > tolerance
        ):
            mismatches.append(
                {
                    "key": "spindle_diameter_mm",
                    "expected": expected["spindle_diameter_mm"],
                    "actual": actual_diameter,
                }
            )

    expected_area = expected.get("work_area_mm")
    actual_area = actual.get("work_area_mm")
    if expected_area:
        if not isinstance(actual_area, list) or len(actual_area) != 3:
            mismatches.append(
                {
                    "key": "work_area_mm",
                    "expected": expected_area,
                    "actual": actual_area,
                }
            )
        else:
            for index, (actual_item, expected_item) in enumerate(
                zip(actual_area, expected_area, strict=True)
            ):
                if abs(float(actual_item) - float(expected_item)) > tolerance:
                    mismatches.append(
                        {
                            "key": "work_area_mm",
                            "index": index,
                            "expected": expected_item,
                            "actual": actual_item,
                        }
                    )

    max_legacy = int(expected.get("max_legacy_visible_cnc_body_count", 0))
    legacy_visible = int(actual.get("legacy_visible_cnc_body_count") or 0)
    if legacy_visible > max_legacy:
        mismatches.append(
            {
                "key": "legacy_visible_cnc_body_count",
                "expected_max": max_legacy,
                "actual": legacy_visible,
            }
        )

    frame_tokens = _normalized_tokens(str(actual.get("frame_material", "")))
    if frame_tokens and not (
        frame_tokens & {"aluminum", "aluminium", "aluminio", "alum"}
    ):
        mismatches.append(
            {
                "key": "frame_material",
                "expected_contains": "aluminum",
                "actual": actual.get("frame_material"),
            }
        )
    rail_tokens = _normalized_tokens(str(actual.get("rail_material", "")))
    if rail_tokens and not (
        rail_tokens & {"steel", "aco", "inox", "inoxidavel", "stainless"}
    ):
        mismatches.append(
            {
                "key": "rail_material",
                "expected_contains": "steel",
                "actual": actual.get("rail_material"),
            }
        )
    return mismatches


def _normalized_tokens(value: str) -> set[str]:
    normalized = (
        unicodedata.normalize("NFKD", value)
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )
    return set(re.findall(r"[a-z0-9]+", normalized))
