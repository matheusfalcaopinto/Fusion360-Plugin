"""Validated registries for canonical benchmark fixtures, actions, and oracles."""

from __future__ import annotations

from copy import deepcopy
from typing import Callable

from benchmark.fixtures import FIXTURE_REGISTRY, SCRIPT_REGISTRY, FixtureDefinition
from benchmark.models import BenchmarkCase, ExecutionObservation, OracleResult


Oracle = Callable[[FixtureDefinition, ExecutionObservation, BenchmarkCase], OracleResult]


def _number_in_range(value: object, minimum: float, maximum: float) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and minimum <= value <= maximum


def _oracle(oracle_id: str, checks: dict[str, bool], *, metrics: dict | None = None) -> OracleResult:
    passed = all(checks.values())
    return OracleResult(
        passed=passed,
        oracle_id=oracle_id,
        checks=checks,
        metrics=metrics or {},
        message="all checks passed" if passed else "one or more independent checks failed",
    )


def oracle_api_documentation(_: FixtureDefinition, result: ExecutionObservation, __: BenchmarkCase) -> OracleResult:
    docs = result.observation.get("api_documentation", {})
    return _oracle("api_documentation", {"class_found": docs.get("class") == "Application", "match": docs.get("matches", 0) >= 1})


def oracle_document_summary(_: FixtureDefinition, result: ExecutionObservation, __: BenchmarkCase) -> OracleResult:
    document = result.observation.get("document", {})
    return _oracle("document_summary", {"name": document.get("name") == "benchmark_fixture", "bodies": document.get("body_count") == 8})


def oracle_inspection(_: FixtureDefinition, result: ExecutionObservation, case: BenchmarkCase) -> OracleResult:
    inspection = result.observation.get("inspection", {})
    minimum = 100 if "large" in case.id else 8
    return _oracle("targeted_inspection", {"minimum_matches": inspection.get("matched", 0) >= minimum, "unambiguous": inspection.get("ambiguous") is False})


def oracle_persistent_cold_read(
    _: FixtureDefinition,
    result: ExecutionObservation,
    __: BenchmarkCase,
) -> OracleResult:
    transport = result.observation.get("transport", {})
    return _oracle(
        "persistent_cold_read",
        {
            "single_initialize": transport.get("initialize_count") == 1,
            "single_tools_list": transport.get("tools_list_count") == 1,
            "no_reconnect": transport.get("reconnect_count") == 0,
            "within_cold_gate": _number_in_range(transport.get("cold_first_read_ms"), 0, 2000),
        },
        metrics={"cold_first_read_ms": transport.get("cold_first_read_ms")},
    )


def oracle_bounded_global_inspection(
    _: FixtureDefinition,
    result: ExecutionObservation,
    __: BenchmarkCase,
) -> OracleResult:
    inspection = result.observation.get("inspection", {})
    return _oracle(
        "bounded_global_inspection",
        {
            "explicitly_partial": inspection.get("complete") is False
            and inspection.get("truncated") is True,
            "entity_budget": _number_in_range(inspection.get("visited_entities"), 1, 1000),
            "deadline": _number_in_range(inspection.get("elapsed_ms"), 0, 5000),
            "response_budget": _number_in_range(
                inspection.get("response_bytes"), 0, 1024 * 1024
            ),
            "stop_reason": inspection.get("stop_reason") in {
                "entity_budget",
                "deadline",
                "response_bytes",
            },
            "no_physical_properties": inspection.get("physical_properties_access_count") == 0,
        },
        metrics={
            "visited_entities": inspection.get("visited_entities"),
            "elapsed_ms": inspection.get("elapsed_ms"),
            "response_bytes": inspection.get("response_bytes"),
        },
    )


def oracle_targeted_token_inspection(
    _: FixtureDefinition,
    result: ExecutionObservation,
    __: BenchmarkCase,
) -> OracleResult:
    inspection = result.observation.get("inspection", {})
    return _oracle(
        "targeted_token_inspection",
        {
            "one_match": inspection.get("matched") == 1,
            "unambiguous": inspection.get("ambiguous") is False,
            "complete": inspection.get("complete") is True,
            "direct_lookup": inspection.get("lookup_strategy") == "entity_token",
            "no_global_scan": inspection.get("global_scan_count") == 0,
            "minimal_visit": inspection.get("visited_entities") == 1,
            "within_targeted_gate": _number_in_range(inspection.get("elapsed_ms"), 0, 1500),
        },
        metrics={
            "visited_entities": inspection.get("visited_entities"),
            "elapsed_ms": inspection.get("elapsed_ms"),
        },
    )


def oracle_screenshot(_: FixtureDefinition, result: ExecutionObservation, __: BenchmarkCase) -> OracleResult:
    screenshot = result.observation.get("screenshot", {})
    return _oracle("screenshot", {"png": screenshot.get("mime_type") == "image/png", "verified": screenshot.get("verified") is True, "nonempty": screenshot.get("bytes", 0) > 0})


def oracle_cube(_: FixtureDefinition, result: ExecutionObservation, __: BenchmarkCase) -> OracleResult:
    feature = result.observation.get("feature", {})
    return _oracle("cube_geometry", {"name": feature.get("name") == "benchmark_cube", "health": feature.get("health") == "ok", "bbox": feature.get("bbox_mm") == [10, 10, 10]})


def oracle_plate(_: FixtureDefinition, result: ExecutionObservation, __: BenchmarkCase) -> OracleResult:
    feature = result.observation.get("feature", {})
    return _oracle("plate_geometry", {"health": feature.get("health") == "ok", "holes": result.observation.get("hole_count") == 4})


def oracle_parameter(_: FixtureDefinition, result: ExecutionObservation, __: BenchmarkCase) -> OracleResult:
    return _oracle("parameter_update", {"value": result.observation.get("parameters", {}).get("width") == "25 mm", "health": result.observation.get("feature_health") == "ok"})


def oracle_destructive_block(_: FixtureDefinition, result: ExecutionObservation, __: BenchmarkCase) -> OracleResult:
    evidence = result.observation
    return _oracle("destructive_block", {"blocked": evidence.get("blocked") is True, "zero_dispatch": evidence.get("mutation_dispatch_count") == 0, "zero_save": evidence.get("save_count") == 0})


def oracle_mutation_timeout(_: FixtureDefinition, result: ExecutionObservation, __: BenchmarkCase) -> OracleResult:
    evidence = result.observation
    return _oracle("mutation_timeout", {"unknown": evidence.get("error_code") == "MUTATION_OUTCOME_UNKNOWN", "single_dispatch": evidence.get("mutation_dispatch_count") == 1, "not_replayed": evidence.get("replayed") is False, "zero_duplicate": evidence.get("duplicate_count") == 0})


def oracle_manifest_drift(_: FixtureDefinition, result: ExecutionObservation, __: BenchmarkCase) -> OracleResult:
    evidence = result.observation
    return _oracle("manifest_drift", {"detected": evidence.get("error_code") == "MANIFEST_DRIFT", "blocked": evidence.get("blocked_before_retry") is True, "reconnected": evidence.get("reconnect_count") == 1})


ORACLE_REGISTRY: dict[str, Oracle] = {
    "persistent_cold_read": oracle_persistent_cold_read,
    "api_documentation": oracle_api_documentation,
    "document_summary": oracle_document_summary,
    "targeted_inspection": oracle_inspection,
    "bounded_global_inspection": oracle_bounded_global_inspection,
    "targeted_token_inspection": oracle_targeted_token_inspection,
    "screenshot": oracle_screenshot,
    "cube_geometry": oracle_cube,
    "plate_geometry": oracle_plate,
    "parameter_update": oracle_parameter,
    "destructive_block": oracle_destructive_block,
    "mutation_timeout": oracle_mutation_timeout,
    "manifest_drift": oracle_manifest_drift,
}


class RegistryError(ValueError):
    """Raised when a suite references anything outside reviewed code registries."""


def validate_case_registry(case: BenchmarkCase) -> None:
    missing: list[str] = []
    if case.fixture_id not in FIXTURE_REGISTRY:
        missing.append(f"fixture:{case.fixture_id}")
    if case.script_id not in SCRIPT_REGISTRY:
        missing.append(f"script:{case.script_id}")
    if case.oracle_id not in ORACLE_REGISTRY:
        missing.append(f"oracle:{case.oracle_id}")
    if missing:
        raise RegistryError(f"case {case.id} references unregistered ids: {', '.join(missing)}")
    script = SCRIPT_REGISTRY[case.script_id]
    unsupported = sorted(set(case.execution_paths) - set(script.profiles))
    if unsupported:
        raise RegistryError(f"case {case.id} has unsupported paths: {', '.join(unsupported)}")


def fixture_for(case: BenchmarkCase) -> FixtureDefinition:
    validate_case_registry(case)
    fixture = FIXTURE_REGISTRY[case.fixture_id]
    return FixtureDefinition(fixture.id, deepcopy(fixture.state))


def oracle_for(case: BenchmarkCase) -> Oracle:
    validate_case_registry(case)
    return ORACLE_REGISTRY[case.oracle_id]
