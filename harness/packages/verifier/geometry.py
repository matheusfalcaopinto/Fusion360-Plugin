"""Geometry and acceptance-test verifier."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Any

from cad_spec.models import CadSpec
from fusion_tool_facade.facade import FusionFacade
from verifier.result_models import FailureCode, VerificationIssue, VerificationResult


class GeometryVerifier:
    """Compare measured Fusion state against a validated CadSpec."""

    def __init__(self, facade: FusionFacade) -> None:
        self.facade = facade

    async def verify(self, spec: CadSpec) -> VerificationResult:
        """Run all acceptance tests in the spec."""

        state_payload = await self.facade.inspect_design()
        state = state_payload["state"]
        issues: list[VerificationIssue] = []
        metrics: dict[str, Any] = {
            "body_count": state.get("body_count") or len(state.get("bodies", {})),
            "component_count": state.get("component_count") or max(0, len(state.get("components", {})) - 1),
            "hole_count": state.get("hole_count")
            if state.get("hole_count") is not None
            else sum(body.get("holes", 0) for body in state.get("bodies", {}).values()),
            "parameter_names": sorted(state.get("parameters", {}).keys()),
            "metadata_components": sorted(_metadata_state(state).keys()),
            "joint_names": sorted((state.get("joints") or {}).keys()),
            "occurrence_names": sorted((state.get("occurrences") or {}).keys()),
        }

        if not state.get("active_document", False):
            issues.append(VerificationIssue(code=FailureCode.MCP_TOOL_ERROR, message="no active document"))
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
                    issues.append(_issue(FailureCode.FEATURE_CREATION_FAILED, "body count mismatch", expected, actual))
            elif check_type == "component_count":
                expected = int(acceptance.target)
                actual = metrics["component_count"]
                if actual != expected:
                    issues.append(_issue(FailureCode.WRONG_ACTIVE_COMPONENT, "component count mismatch", expected, actual))
            elif check_type == "bounding_box":
                expected = acceptance.target_mm or []
                tolerance = acceptance.tolerance_mm if acceptance.tolerance_mm is not None else 0.05
                actual = await self.facade.measure_bounding_box()
                metrics["bounding_box_mm"] = actual
                if not _bbox_close(actual, expected, tolerance):
                    issues.append(
                        VerificationIssue(
                            code=_classify_bbox(expected, actual),
                            message="bounding box mismatch",
                            details={"expected": expected, "actual": actual, "tolerance_mm": tolerance},
                        )
                    )
            elif check_type == "target_bounding_box":
                target = str(acceptance.target or "")
                expected = acceptance.target_mm or []
                tolerance = acceptance.tolerance_mm if acceptance.tolerance_mm is not None else 0.05
                actual = await self.facade.measure_bounding_box(target)
                metrics[f"{target}_bounding_box_mm"] = actual
                if not _bbox_close(actual, expected, tolerance):
                    issues.append(
                        VerificationIssue(
                            code=_classify_bbox(expected, actual),
                            message=f"bounding box mismatch for {target}",
                            details={"target": target, "expected": expected, "actual": actual, "tolerance_mm": tolerance},
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
                missing = [name for name in acceptance.target or [] if name not in state.get("bodies", {})]
                if missing:
                    issues.append(
                        VerificationIssue(
                            code=FailureCode.INVALID_REFERENCE,
                            message="missing named bodies",
                            details={"missing": missing},
                        )
                    )
            elif check_type == "named_parameters":
                missing = [name for name in acceptance.target or [] if name not in state.get("parameters", {})]
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
                tolerance = acceptance.tolerance_mm if acceptance.tolerance_mm is not None else 0.05
                metrics["nema17_metrics"] = actual_metrics
                dimension_issues = _metric_mismatches(actual_metrics, expected_metrics, tolerance)
                if dimension_issues:
                    issues.append(
                        VerificationIssue(
                            code=FailureCode.FEATURE_CREATION_FAILED,
                            message="NEMA17 measured dimensions mismatch",
                            details={"mismatches": dimension_issues, "actual": actual_metrics, "expected": expected_metrics},
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
                            details={"mismatches": polish_issues, "actual": actual_metrics, "expected": expected_metrics},
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
                            details={"mismatches": assembly_issues, "actual": actual_metrics, "expected": expected_metrics},
                        )
                    )
            elif check_type == "profile2020_details":
                expected_metrics = acceptance.target or {}
                actual_metrics = state.get("profile2020_metrics", {})
                tolerance = acceptance.tolerance_mm if acceptance.tolerance_mm is not None else 0.05
                metrics["profile2020_metrics"] = actual_metrics
                profile_issues = _profile2020_mismatches(actual_metrics, expected_metrics, tolerance)
                if profile_issues:
                    issues.append(
                        VerificationIssue(
                            code=FailureCode.FEATURE_CREATION_FAILED,
                            message="2020 aluminum profile details mismatch",
                            details={"mismatches": profile_issues, "actual": actual_metrics, "expected": expected_metrics},
                        )
                    )
            elif check_type == "mgn12_linear_rail_assembly":
                expected_metrics = acceptance.target or {}
                actual_metrics = state.get("mgn12_metrics", {})
                tolerance = acceptance.tolerance_mm if acceptance.tolerance_mm is not None else 0.05
                metrics["mgn12_metrics"] = actual_metrics
                mgn12_issues = _mgn12_mismatches(actual_metrics, expected_metrics, tolerance)
                if mgn12_issues:
                    issues.append(
                        VerificationIssue(
                            code=FailureCode.FEATURE_CREATION_FAILED,
                            message="MGN12 linear rail assembly mismatch",
                            details={"mismatches": mgn12_issues, "actual": actual_metrics, "expected": expected_metrics},
                        )
                    )
            elif check_type == "desktop_cnc_assembly":
                expected_metrics = acceptance.target or {}
                actual_metrics = state.get("cnc_metrics", {})
                tolerance = acceptance.tolerance_mm if acceptance.tolerance_mm is not None else 0.05
                metrics["cnc_metrics"] = actual_metrics
                cnc_issues = _cnc_mismatches(actual_metrics, expected_metrics, tolerance)
                if cnc_issues:
                    issues.append(
                        VerificationIssue(
                            code=FailureCode.FEATURE_CREATION_FAILED,
                            message="desktop CNC assembly mismatch",
                            details={"mismatches": cnc_issues, "actual": actual_metrics, "expected": expected_metrics},
                        )
                    )
            elif check_type == "component_metadata":
                metadata = _metadata_state(state)
                metrics["component_metadata"] = metadata
                metadata_issues = _metadata_mismatches(metadata, [item.model_dump(mode="json") for item in spec.component_metadata])
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
                joint_issues = _joint_mismatches(joints, [item.model_dump(mode="json") for item in spec.joints])
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
                occurrence_issues = _occurrence_mismatches(occurrences, state.get("components") or {}, acceptance.target or {})
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
                    interference = interference_payload.get("interference") or state.get("interference") or {}
                except Exception as exc:  # noqa: BLE001 - verifier must normalize facade failures
                    interference = {"count": None, "pairs": [], "error": f"{type(exc).__name__}: {exc}"}
                metrics["interference"] = interference
                interference_issues = _interference_mismatches(interference, acceptance.target or {})
                if interference_issues:
                    issues.append(
                        VerificationIssue(
                            code=FailureCode.INTERFERENCE_DETECTED,
                            message="interference analysis failed or detected unapproved interference",
                            details={"mismatches": interference_issues, "interference": interference},
                        )
                    )
            elif check_type == "physical_properties":
                targets = [item.component for item in spec.component_metadata]
                try:
                    measured_payload = await self.facade.measure_physical_properties(targets)
                    physical_properties = measured_payload.get("physical_properties") or {}
                except Exception as exc:  # noqa: BLE001 - verifier must normalize facade failures
                    physical_properties = dict(state.get("physical_properties") or {})
                    physical_properties["_error"] = f"{type(exc).__name__}: {exc}"
                metrics["physical_properties"] = physical_properties
                physical_issues = _physical_property_mismatches(physical_properties, targets)
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
                screenshot_issues = _screenshot_mismatches(screenshots, [item.model_dump(mode="json") for item in spec.outputs])
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
                    issues.append(_issue(FailureCode.FEATURE_CREATION_FAILED, "hole count mismatch", expected, actual))
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
                missing = [path for path in paths if not Path(path).exists() or Path(path).stat().st_size == 0]
                if missing:
                    issues.append(
                        VerificationIssue(
                            code=FailureCode.EXPORT_FAILED,
                            message="expected export files are missing or empty",
                            details={"missing": missing},
                        )
                    )

        return VerificationResult(passed=not issues, issues=issues, metrics=metrics)


def _metadata_state(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    metadata: dict[str, dict[str, Any]] = dict(state.get("component_metadata") or {})
    for name, component in (state.get("components") or {}).items():
        if isinstance(component, dict) and component.get("metadata"):
            metadata.setdefault(str(name), dict(component["metadata"]))
    return metadata


def _metadata_mismatches(actual: dict[str, dict[str, Any]], expected: list[dict[str, Any]]) -> list[dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []
    required_fields = ("part_number", "description", "role", "source_type", "physical_material")
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
                mismatches.append({"component": component, "field": field, "expected": expected_value, "actual": actual_value})
            elif expected_value and _normalize_scalar(actual_value) != _normalize_scalar(expected_value):
                mismatches.append({"component": component, "field": field, "expected": expected_value, "actual": actual_value})
    return mismatches


def _joint_mismatches(actual: dict[str, dict[str, Any]], expected: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
                mismatches.append({"joint": item["name"], "field": field, "expected": expected_value, "actual": actual_value})
        if actual_item.get("health") not in valid_health:
            mismatches.append({"joint": item["name"], "field": "health", "expected": "ok", "actual": actual_item.get("health")})
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
                {"occurrence": name, "actual": (occurrences.get(name) or {}).get("component"), "expected": component}
                for name in expected_names
                if name in occurrences and (occurrences.get(name) or {}).get("component") != component
            ]
            if wrong_component:
                mismatches.append({"key": "occurrence_components", "mismatches": wrong_component})
            visible_component_count = sum(
                1
                for item in occurrences.values()
                if item.get("component") == component and item.get("visible", True) is not False
            )
            if expected.get("count") is not None and visible_component_count != int(expected["count"]):
                mismatches.append(
                    {
                        "key": "visible_component_occurrence_count",
                        "expected": int(expected["count"]),
                        "actual": visible_component_count,
                    }
                )
        if expected.get("count") is not None and len([name for name in expected_names if name in occurrences]) != int(expected["count"]):
            mismatches.append(
                {
                    "key": "occurrence_count",
                    "expected": int(expected["count"]),
                    "actual": len([name for name in expected_names if name in occurrences]),
                }
            )

    component_names = list(expected.get("component_names") or [])
    if component_names:
        missing_components = [name for name in component_names if name not in components]
        if missing_components:
            mismatches.append({"key": "component_names", "missing": missing_components})
        actual_count = sum(
            1
            for item in occurrences.values()
            if item.get("component") in component_names and item.get("visible", True) is not False
        )
        if expected.get("count") is not None and actual_count != int(expected["count"]):
            mismatches.append({"key": "component_occurrence_count", "expected": int(expected["count"]), "actual": actual_count})
    return mismatches


def _interference_mismatches(actual: dict[str, Any], expected: dict[str, Any]) -> list[dict[str, Any]]:
    if actual.get("error"):
        return [{"key": "analysis", "error": actual["error"]}]
    if actual.get("analysis_warning"):
        return [{"key": "analysis", "error": actual["analysis_warning"]}]
    count = actual.get("count")
    if count is None:
        return [{"key": "count", "expected": 0, "actual": None}]
    pairs = actual.get("pairs") or []
    allowed = {frozenset((str(pair[0]), str(pair[1]))) for pair in expected.get("allowed_contact_pairs") or []}
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


def _physical_property_mismatches(actual: dict[str, Any], targets: list[str]) -> list[dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []
    if actual.get("_error"):
        mismatches.append({"key": "measurement", "error": actual["_error"]})
    for target in targets:
        payload = actual.get(target)
        if not payload:
            mismatches.append({"target": target, "missing": True})
            continue
        mass = float(payload.get("mass_kg") or 0)
        volume = float(payload.get("volume_mm3") or 0)
        if mass <= 0 or volume <= 0:
            mismatches.append({"target": target, "mass_kg": mass, "volume_mm3": volume})
    return mismatches


def _screenshot_mismatches(actual: dict[str, dict[str, Any]], expected: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
            mismatches.append({"output": item["name"], "paths": [str(path) for path in candidates]})
    return mismatches


def _normalize_scalar(value: Any) -> str:
    return str(value or "").strip().lower()


def _issue(code: FailureCode, message: str, expected: Any, actual: Any) -> VerificationIssue:
    return VerificationIssue(code=code, message=message, details={"expected": expected, "actual": actual})


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
        if ratios and all(abs(ratio - 10.0) < 0.05 or abs(ratio - 0.1) < 0.05 for ratio in ratios):
            return FailureCode.UNIT_MISMATCH
        if ratios and all(abs(ratio - 25.4) < 0.1 or abs(ratio - (1 / 25.4)) < 0.01 for ratio in ratios):
            return FailureCode.UNIT_MISMATCH
    return FailureCode.FEATURE_CREATION_FAILED


def _metric_mismatches(actual: dict[str, Any], expected: dict[str, Any], tolerance: float) -> list[dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []
    for key, expected_value in expected.items():
        actual_value = actual.get(key)
        if isinstance(expected_value, list):
            if not isinstance(actual_value, list) or len(actual_value) != len(expected_value):
                mismatches.append({"key": key, "expected": expected_value, "actual": actual_value})
                continue
            for index, (actual_item, expected_item) in enumerate(zip(actual_value, expected_value, strict=True)):
                if abs(float(actual_item) - float(expected_item)) > tolerance:
                    mismatches.append({"key": key, "index": index, "expected": expected_item, "actual": actual_item})
        elif isinstance(expected_value, int):
            if int(actual_value or 0) != expected_value:
                mismatches.append({"key": key, "expected": expected_value, "actual": actual_value})
        elif actual_value is None or abs(float(actual_value) - float(expected_value)) > tolerance:
            mismatches.append({"key": key, "expected": expected_value, "actual": actual_value})
    return mismatches


def _polish_mismatches(actual: dict[str, Any], expected: dict[str, Any]) -> list[dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []
    body_names = set(actual.get("body_names") or [])
    for body_name in expected.get("required_bodies") or []:
        if body_name not in body_names:
            mismatches.append({"key": "required_bodies", "missing": body_name})
    minimum_laminations = int(expected.get("min_lamination_bodies") or 0)
    actual_laminations = int(actual.get("lamination_body_count") or 0)
    if actual_laminations < minimum_laminations:
        mismatches.append({"key": "lamination_body_count", "expected_min": minimum_laminations, "actual": actual_laminations})
    for key in ("wire_count", "screw_shadow_count"):
        if key in expected and int(actual.get(key) or 0) != int(expected[key]):
            mismatches.append({"key": key, "expected": int(expected[key]), "actual": actual.get(key)})
    return mismatches


def _assembly_mismatches(actual: dict[str, Any], expected: dict[str, Any]) -> list[dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []
    if expected.get("assembly_component") and actual.get("assembly_component") != expected["assembly_component"]:
        mismatches.append(
            {"key": "assembly_component", "expected": expected["assembly_component"], "actual": actual.get("assembly_component")}
        )

    actual_components = set(actual.get("component_names") or [])
    for component_name in expected.get("required_components") or []:
        if component_name not in actual_components:
            mismatches.append({"key": "required_components", "missing": component_name})

    actual_bodies = set(actual.get("body_names") or [])
    body_components = actual.get("body_components") or {}
    actual_component_names = actual_components | {str(name) for name in body_components.values()}
    for body_name in expected.get("required_bodies") or []:
        if body_name not in actual_bodies:
            mismatches.append({"key": "required_bodies", "missing": body_name})
            continue
        owner = body_components.get(body_name)
        if owner not in actual_component_names or owner in {"root", ""} or str(owner).startswith("("):
            mismatches.append({"key": "body_components", "body": body_name, "invalid_owner": owner})

    minimum_laminations = int(expected.get("min_stator_lamination_count") or 0)
    actual_laminations = int(actual.get("stator_lamination_count") or 0)
    if actual_laminations < minimum_laminations:
        mismatches.append({"key": "stator_lamination_count", "expected_min": minimum_laminations, "actual": actual_laminations})

    if "wire_count" in expected and int(actual.get("wire_count") or 0) != int(expected["wire_count"]):
        mismatches.append({"key": "wire_count", "expected": int(expected["wire_count"]), "actual": actual.get("wire_count")})
    if bool(actual.get("connector_present")) is False:
        mismatches.append({"key": "connector_present", "expected": True, "actual": actual.get("connector_present")})

    max_legacy = int(expected.get("max_legacy_visible_nema17_body_count", 0))
    legacy_visible = int(actual.get("legacy_visible_nema17_body_count") or 0)
    if legacy_visible > max_legacy:
        mismatches.append({"key": "legacy_visible_nema17_body_count", "expected_max": max_legacy, "actual": legacy_visible})
    return mismatches


def _profile2020_mismatches(actual: dict[str, Any], expected: dict[str, Any], tolerance: float) -> list[dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []
    for key in ("component", "body"):
        if expected.get(key) and actual.get(key) != expected[key]:
            mismatches.append({"key": key, "expected": expected[key], "actual": actual.get(key)})

    for key in ("size_mm", "length_mm", "slot_width_mm", "slot_depth_mm", "center_bore_diameter_mm"):
        if key not in expected:
            continue
        actual_value = actual.get(key)
        if actual_value is None or abs(float(actual_value) - float(expected[key])) > tolerance:
            mismatches.append({"key": key, "expected": expected[key], "actual": actual_value})

    for key in ("slot_count", "web_relief_count"):
        if key in expected and int(actual.get(key) or 0) != int(expected[key]):
            mismatches.append({"key": key, "expected": int(expected[key]), "actual": actual.get(key)})

    if not bool(actual.get("center_bore_present")):
        mismatches.append({"key": "center_bore_present", "expected": True, "actual": actual.get("center_bore_present")})

    material = str(actual.get("material", "")).lower()
    if "aluminum" not in material and "alum" not in material:
        mismatches.append({"key": "material", "expected_contains": "aluminum", "actual": actual.get("material")})
    return mismatches


def _mgn12_mismatches(actual: dict[str, Any], expected: dict[str, Any], tolerance: float) -> list[dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []
    if expected.get("assembly_component") and actual.get("assembly_component") != expected["assembly_component"]:
        mismatches.append(
            {"key": "assembly_component", "expected": expected["assembly_component"], "actual": actual.get("assembly_component")}
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
            mismatches.append({"key": "body_components", "body": body_name, "invalid_owner": owner})

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
        if actual_value is None or abs(float(actual_value) - float(expected[key])) > tolerance:
            mismatches.append({"key": key, "expected": expected[key], "actual": actual_value})

    for key in ("rail_mount_hole_count", "rail_counterbore_count", "carriage_mount_hole_count"):
        if key in expected and int(actual.get(key) or 0) != int(expected[key]):
            mismatches.append({"key": key, "expected": int(expected[key]), "actual": actual.get(key)})

    expected_spacing = expected.get("carriage_mount_spacing_mm")
    actual_spacing = actual.get("carriage_mount_spacing_mm")
    if expected_spacing:
        if not isinstance(actual_spacing, list) or len(actual_spacing) != 2:
            mismatches.append({"key": "carriage_mount_spacing_mm", "expected": expected_spacing, "actual": actual_spacing})
        else:
            for index, (actual_item, expected_item) in enumerate(zip(actual_spacing, expected_spacing, strict=True)):
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
        mismatches.append({"key": "legacy_visible_mgn12_body_count", "expected_max": max_legacy, "actual": legacy_visible})

    for key in ("rail_material", "carriage_material"):
        material = str(actual.get(key, ""))
        material_tokens = _normalized_tokens(material)
        if material and not (material_tokens & {"steel", "aco", "inox", "inoxidavel", "stainless"}):
            mismatches.append({"key": key, "expected_contains": "steel", "actual": actual.get(key)})
    return mismatches


def _cnc_mismatches(actual: dict[str, Any], expected: dict[str, Any], tolerance: float) -> list[dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []
    if expected.get("assembly_component") and actual.get("assembly_component") != expected["assembly_component"]:
        mismatches.append(
            {"key": "assembly_component", "expected": expected["assembly_component"], "actual": actual.get("assembly_component")}
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
            mismatches.append({"key": "body_components", "body": body_name, "invalid_owner": owner})

    for key in ("profile_count", "rail_count", "motor_count", "leadscrew_count", "coupler_count"):
        if key in expected and int(actual.get(key) or 0) != int(expected[key]):
            mismatches.append({"key": key, "expected": int(expected[key]), "actual": actual.get(key)})

    if "spindle_diameter_mm" in expected:
        actual_diameter = actual.get("spindle_diameter_mm")
        if actual_diameter is None or abs(float(actual_diameter) - float(expected["spindle_diameter_mm"])) > tolerance:
            mismatches.append({"key": "spindle_diameter_mm", "expected": expected["spindle_diameter_mm"], "actual": actual_diameter})

    expected_area = expected.get("work_area_mm")
    actual_area = actual.get("work_area_mm")
    if expected_area:
        if not isinstance(actual_area, list) or len(actual_area) != 3:
            mismatches.append({"key": "work_area_mm", "expected": expected_area, "actual": actual_area})
        else:
            for index, (actual_item, expected_item) in enumerate(zip(actual_area, expected_area, strict=True)):
                if abs(float(actual_item) - float(expected_item)) > tolerance:
                    mismatches.append(
                        {"key": "work_area_mm", "index": index, "expected": expected_item, "actual": actual_item}
                    )

    max_legacy = int(expected.get("max_legacy_visible_cnc_body_count", 0))
    legacy_visible = int(actual.get("legacy_visible_cnc_body_count") or 0)
    if legacy_visible > max_legacy:
        mismatches.append({"key": "legacy_visible_cnc_body_count", "expected_max": max_legacy, "actual": legacy_visible})

    frame_tokens = _normalized_tokens(str(actual.get("frame_material", "")))
    if frame_tokens and not (frame_tokens & {"aluminum", "aluminium", "aluminio", "alum"}):
        mismatches.append({"key": "frame_material", "expected_contains": "aluminum", "actual": actual.get("frame_material")})
    rail_tokens = _normalized_tokens(str(actual.get("rail_material", "")))
    if rail_tokens and not (rail_tokens & {"steel", "aco", "inox", "inoxidavel", "stainless"}):
        mismatches.append({"key": "rail_material", "expected_contains": "steel", "actual": actual.get("rail_material")})
    return mismatches


def _normalized_tokens(value: str) -> set[str]:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii").lower()
    return set(re.findall(r"[a-z0-9]+", normalized))
