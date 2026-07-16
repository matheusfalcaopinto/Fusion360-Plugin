from __future__ import annotations

import ast
import asyncio
import base64
import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
SOURCE_PACKAGES = ROOT / "harness" / "packages"
SOURCE_APPS = ROOT / "harness" / "apps"
sys.path[:0] = [str(SOURCE_PACKAGES), str(SOURCE_APPS)]

from agent_core import fast_path as fast_path_module  # noqa: E402
from agent_core.fast_path import (  # noqa: E402
    ASSERTION_OPERATORS,
    lint_fusion_script,
    validate_fast_execute_request,
)
from agent_core.targeted_inspection import SUPPORTED_ENTITY_TYPES  # noqa: E402


SUITE_ROOT = ROOT / "benchmark_parametric_suite"
CASES_ROOT = SUITE_ROOT / "cases"
SUITE_DEFINITION = SUITE_ROOT / "suite_definition.json"
RUNNER = SUITE_ROOT / "run_reference_suite.py"
REQUIRED_VIEWS = {"isometric", "front", "top", "right"}
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CASE_ID_RE = re.compile(r"^b\d{2}_[a-z0-9]+(?:_[a-z0-9]+)*$")
_REFERENCE_RUNNER_MODULE: ModuleType | None = None


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict), f"{path} must contain a JSON object"
    return value


def _definition_paths() -> list[Path]:
    return sorted(
        CASES_ROOT.glob("*/definition.json"), key=lambda path: path.parent.name
    )


def _case_id(value: object) -> str:
    if isinstance(value, Path):
        return value.parent.name
    return str(value)


CASE_DEFINITIONS = _definition_paths()


def _reference_runner_module() -> ModuleType:
    global _REFERENCE_RUNNER_MODULE
    if _REFERENCE_RUNNER_MODULE is None:
        module_name = "_fusion_parametric_reference_runner_tests"
        spec = importlib.util.spec_from_file_location(module_name, RUNNER)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        _REFERENCE_RUNNER_MODULE = module
    return _REFERENCE_RUNNER_MODULE


def _passing_phase() -> tuple[dict[str, Any], dict[str, Any], list[dict[str, str]]]:
    fast_path = {
        "status": "applied_verified",
        "declared_mutation_count": 1,
        "mutating_call_count": 1,
        "transport_mutating_dispatch_count": 1,
        "verification": {"passed": True},
    }
    oracle = {
        "passed": True,
        "failed_checks": [],
        "coverage": {"mandatory": 3, "passed": 3, "failed": 0, "unverified": 0},
    }
    images = [
        {"direction": direction, "sha256": f"sha-{direction}"}
        for direction in sorted(REQUIRED_VIEWS)
    ]
    return fast_path, oracle, images


def _passing_case_result(*, has_eco: bool = False) -> dict[str, Any]:
    fast_path, oracle, images = _passing_phase()
    result: dict[str, Any] = {
        "fast_path": fast_path,
        "oracle": oracle,
        "images": images,
        "cleanup": {
            "closed_without_save": True,
            "restored": True,
            "inventory_restored": True,
            "active_document_id": "doc-original",
            "original_document_id": "doc-original",
            "open_document_ids": ["doc-original"],
            "baseline_open_document_ids": ["doc-original"],
        },
        "elapsed_ms": 10,
    }
    if has_eco:
        eco_fast_path, eco_oracle, eco_images = _passing_phase()
        result["eco"] = {
            "fast_path": eco_fast_path,
            "oracle": eco_oracle,
            "images": eco_images,
        }
    return result


def _assert_exact_run_signature(path: Path, tree: ast.Module) -> None:
    run_functions = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "run"
    ]
    assert len(run_functions) == 1, f"{path}: expected exactly one top-level run"
    run_function = run_functions[0]
    assert isinstance(run_function, ast.FunctionDef), f"{path}: run must be synchronous"
    arguments = run_function.args
    assert [argument.arg for argument in arguments.args] == ["_context"], (
        f"{path}: run signature must be exactly def run(_context: str)"
    )
    assert not arguments.posonlyargs
    assert not arguments.kwonlyargs
    assert arguments.vararg is None
    assert arguments.kwarg is None
    annotation = arguments.args[0].annotation
    assert isinstance(annotation, ast.Name) and annotation.id == "str", (
        f"{path}: _context must be annotated as str"
    )


def _build_request(definition: dict[str, Any], script: str) -> dict[str, Any]:
    queries: list[dict[str, Any]] = []
    assertions: list[dict[str, Any]] = []
    target_query_ids: list[str] = []
    for target in definition["future_targets"]:
        queries.append(
            {
                "id": target["id"],
                "entity_type": target["entity_type"],
                "selector": target["selector"],
                "fields": target["fields"],
            }
        )
        if target.get("bind_target", True):
            target_query_ids.append(target["id"])
        for index, assertion in enumerate(target["assertions"], start=1):
            assertions.append(
                {
                    "id": f"{target['id']}_{index}",
                    "query_id": target["id"],
                    **assertion,
                }
            )
    return validate_fast_execute_request(
        {
            "intent": f"Build canonical reference fixture {definition['case_id']}",
            "change_class": "additive",
            "script": script,
            "api_references": definition["api_references"],
            "target_query_ids": target_query_ids,
            "verification": {
                "queries": queries,
                "assertions": assertions,
                "requirements": [
                    {
                        "id": f"{definition['case_id']}_initial_contract",
                        "description": "All reviewed assertions for this canonical benchmark phase must pass.",
                        "required": True,
                        "assertion_ids": [assertion["id"] for assertion in assertions],
                        "oracle": "contract",
                    }
                ],
                "limit_per_query": 20,
                "include_screenshot": False,
            },
        }
    )


def _eco_request(definition: dict[str, Any], script: str) -> dict[str, Any]:
    eco = definition["eco"]
    verification = json.loads(json.dumps(eco["verification"]))
    assertion_ids = []
    for index, assertion in enumerate(verification["assertions"], start=1):
        assertion_id = str(
            assertion.get("id")
            or f"{definition['case_id']}_eco_contract_assertion_{index}"
        )
        assertion["id"] = assertion_id
        assertion_ids.append(assertion_id)
    verification["requirements"] = [
        {
            "id": f"{definition['case_id']}_eco_contract",
            "description": "All reviewed assertions for this canonical benchmark phase must pass.",
            "required": True,
            "assertion_ids": assertion_ids,
            "oracle": "contract",
        }
    ]
    return validate_fast_execute_request(
        {
            "intent": eco["intent"],
            "change_class": "scoped_update",
            "script": script,
            "api_references": eco.get("api_references", []),
            "target_query_ids": eco["target_query_ids"],
            "verification": verification,
        }
    )


def _assert_assertion_contract(
    assertions: list[dict[str, Any]], query_ids: set[str]
) -> None:
    assert len(assertions) <= 100
    explicit_ids = [
        assertion.get("id") for assertion in assertions if assertion.get("id")
    ]
    assert len(explicit_ids) == len(set(explicit_ids)), "assertion ids must be unique"
    for assertion in assertions:
        assert assertion.get("query_id") in query_ids
        assert isinstance(assertion.get("field"), str) and assertion["field"]
        assert assertion.get("operator") in ASSERTION_OPERATORS
        if assertion["operator"] == "unchanged":
            assert "expected" not in assertion or assertion["expected"] is None
        else:
            assert "expected" in assertion
        if assertion["operator"] == "approx":
            assert isinstance(assertion.get("tolerance"), (int, float))
            assert assertion["tolerance"] >= 0


def _assert_oracle_passed(oracle: dict[str, Any], case_id: str) -> None:
    assert oracle.get("schema_version") == "fusion_parametric_oracle.v2"
    assert oracle.get("case_id") == case_id
    assert oracle.get("passed") is True
    assert oracle.get("ok") is True
    assert oracle.get("failed_checks") == []
    coverage = oracle.get("coverage")
    assert isinstance(coverage, dict)
    assert coverage.get("mandatory", 0) > 0
    assert coverage.get("passed") == coverage["mandatory"]
    assert coverage.get("failed") == 0
    assert coverage.get("unverified") == 0


def _assert_fast_path_passed(
    result: dict[str, Any],
    *,
    change_class: str,
    script_sha256: str,
) -> None:
    assert result.get("status") == "applied_verified"
    assert result.get("execution_path") == "native_fast"
    assert result.get("change_class") == change_class
    assert result.get("script_sha256") == script_sha256
    assert result.get("mutating_call_count") == 1
    assert result.get("transport_mutating_dispatch_count") == 1
    assert result.get("declared_mutation_count") == 1
    assert result.get("verification", {}).get("passed") is True


def _assert_png_artifacts(case_root: Path, artifacts: object) -> None:
    assert isinstance(artifacts, list)
    assert len(artifacts) == len(REQUIRED_VIEWS)
    assert {artifact.get("direction") for artifact in artifacts} == REQUIRED_VIEWS
    paths: set[Path] = set()
    for artifact in artifacts:
        assert isinstance(artifact, dict)
        relative_path = Path(str(artifact.get("path") or ""))
        assert not relative_path.is_absolute()
        path = (SUITE_ROOT / relative_path).resolve()
        assert path.is_relative_to((case_root / "images").resolve())
        assert path not in paths
        paths.add(path)
        raw = path.read_bytes()
        assert raw.startswith(PNG_SIGNATURE), f"{path} is not a PNG"
        assert artifact.get("bytes") == len(raw)
        assert artifact.get("bytes", 0) > len(PNG_SIGNATURE)
        assert SHA256_RE.fullmatch(str(artifact.get("sha256") or ""))
        assert artifact["sha256"] == _sha256(raw)


def _runner_default_cases() -> list[str]:
    tree = ast.parse(RUNNER.read_text(encoding="utf-8"), filename=str(RUNNER))
    for statement in tree.body:
        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            targets = (
                statement.targets
                if isinstance(statement, ast.Assign)
                else [statement.target]
            )
            if any(
                isinstance(target, ast.Name) and target.id == "DEFAULT_CASES"
                for target in targets
            ):
                value = ast.literal_eval(statement.value)
                assert isinstance(value, list) and all(
                    isinstance(item, str) for item in value
                )
                return value
    raise AssertionError(f"{RUNNER}: DEFAULT_CASES was not found")


def test_fast_path_import_resolves_to_canonical_local_source() -> None:
    module_path = Path(fast_path_module.__file__).resolve()
    assert module_path.is_relative_to(SOURCE_PACKAGES.resolve())


def test_suite_definition_and_runner_index_every_complete_case() -> None:
    assert CASE_DEFINITIONS, "the parametric benchmark suite has no complete cases"
    discovered = [_case_id(path) for path in CASE_DEFINITIONS]
    assert discovered == sorted(discovered)
    assert all(CASE_ID_RE.fullmatch(case_id) for case_id in discovered)

    suite = _load_json(SUITE_DEFINITION)
    assert suite.get("schema_version") == "fusion_parametric_suite_definition.v1"
    assert suite.get("runner") == RUNNER.name
    assert suite.get("case_order") == discovered
    assert _runner_default_cases() == discovered

    entries = suite.get("cases")
    assert isinstance(entries, list)
    assert [entry.get("case_id") for entry in entries] == discovered
    for entry, definition_path in zip(entries, CASE_DEFINITIONS, strict=True):
        case_id = definition_path.parent.name
        definition = _load_json(definition_path)
        assert entry.get("title") == definition["title"]
        assert entry.get("complexity_tier") == definition["complexity_tier"]
        assert entry.get("case_path") == f"cases/{case_id}"
        expected_paths = {
            "definition": f"cases/{case_id}/definition.json",
            "prompt": f"cases/{case_id}/prompt.txt",
            "build_script": f"cases/{case_id}/build_script.py",
            "oracle_script": f"cases/{case_id}/oracle_script.py",
        }
        for field, expected in expected_paths.items():
            assert entry.get(field) == expected
            assert (SUITE_ROOT / expected).is_file()


@pytest.mark.parametrize("definition_path", CASE_DEFINITIONS, ids=_case_id)
def test_case_definition_and_additive_fast_path_contract(definition_path: Path) -> None:
    case_root = definition_path.parent
    case_id = case_root.name
    definition = _load_json(definition_path)

    assert definition.get("schema_version") in {
        "fusion_parametric_case.v1",
        "fusion_parametric_case.v2",
    }
    assert definition.get("case_id") == case_id
    assert isinstance(definition.get("title"), str) and definition["title"].strip()
    assert isinstance(definition.get("complexity_tier"), int)
    assert definition["complexity_tier"] >= 1
    assert isinstance(definition.get("time_limit_minutes"), int)
    assert definition["time_limit_minutes"] > 0
    assert isinstance(definition.get("maximum_autonomous_repairs"), int)
    assert definition["maximum_autonomous_repairs"] >= 0
    assert (
        isinstance(definition.get("risk_domains"), list) and definition["risk_domains"]
    )
    assert len(definition["risk_domains"]) == len(set(definition["risk_domains"]))
    assert isinstance(definition.get("expected"), dict) and definition["expected"]
    api_references = definition.get("api_references")
    assert isinstance(api_references, list) and api_references
    assert all(
        isinstance(reference, str) and reference.strip() for reference in api_references
    )
    assert len(api_references) == len(set(api_references))

    prompt = (case_root / "prompt.txt").read_text(encoding="utf-8")
    assert prompt.strip()
    targets = definition.get("future_targets")
    assert isinstance(targets, list) and targets
    assert len(targets) <= 50
    target_ids = [target.get("id") for target in targets]
    assert len(target_ids) == len(set(target_ids))
    bind_target_ids = [
        target["id"] for target in targets if target.get("bind_target", True)
    ]
    assert bind_target_ids
    assert len(bind_target_ids) <= 20

    for target in targets:
        assert isinstance(target.get("id"), str) and target["id"]
        assert target.get("entity_type") in SUPPORTED_ENTITY_TYPES
        selector = target.get("selector")
        assert isinstance(selector, dict)
        assert any(
            selector.get(key)
            for key in ("entity_token", "path", "component_path", "name")
        )
        if selector.get("component_path"):
            assert selector.get("name")
        if target.get("bind_target", True):
            assert selector.get("component_path"), (
                f"{case_id}:{target['id']} additive mutation targets require component_path"
            )
        fields = target.get("fields")
        assert isinstance(fields, list) and fields
        assert all(isinstance(field, str) and field for field in fields)
        assert len(fields) == len(set(fields))
        assertions = target.get("assertions")
        assert isinstance(assertions, list) and assertions
        normalized_assertions = [
            {"query_id": target["id"], **assertion} for assertion in assertions
        ]
        _assert_assertion_contract(normalized_assertions, {target["id"]})
        assert all(
            assertion["field"].split(".", 1)[0] in fields for assertion in assertions
        )

    build_path = case_root / "build_script.py"
    script = build_path.read_text(encoding="utf-8")
    assert len(script.encode("utf-8")) <= 64 * 1024
    tree = ast.parse(script, filename=str(build_path))
    _assert_exact_run_signature(build_path, tree)
    request = _build_request(definition, script)
    assert request["change_class"] == "additive"
    assert request["target_query_ids"] == bind_target_ids
    assert len(request["verification"]["queries"]) == len(targets)
    assert len(request["verification"]["assertions"]) == sum(
        len(target["assertions"]) for target in targets
    )
    assert request["verification"]["requirements"] == [
        {
            "id": f"{definition['case_id']}_initial_contract",
            "description": "All reviewed assertions for this canonical benchmark phase must pass.",
            "required": True,
            "assertion_ids": [
                assertion["id"] for assertion in request["verification"]["assertions"]
            ],
            "oracle": "contract",
        }
    ]
    lint = lint_fusion_script(
        script,
        "additive",
        allowed_target_ids=set(),
        allowed_component_paths=set(request["target_component_paths"]),
    )
    assert lint.allowed, lint.as_dict()
    assert lint.detected_change_class == "additive"
    assert lint.mutating_syntax_detected is True


@pytest.mark.parametrize("definition_path", CASE_DEFINITIONS, ids=_case_id)
def test_oracle_and_optional_eco_files_are_complete_and_valid(
    definition_path: Path,
) -> None:
    case_root = definition_path.parent
    definition = _load_json(definition_path)

    oracle_path = case_root / "oracle_script.py"
    oracle_script = oracle_path.read_text(encoding="utf-8")
    _assert_exact_run_signature(
        oracle_path,
        ast.parse(oracle_script, filename=str(oracle_path)),
    )

    eco = definition.get("eco")
    eco_script_path = case_root / "eco_script.py"
    eco_oracle_path = case_root / "eco_oracle_script.py"
    assert bool(eco) == eco_script_path.exists() == eco_oracle_path.exists(), (
        f"{case_root.name}: definition.eco, eco_script.py and eco_oracle_script.py "
        "must be added or removed together"
    )
    if not eco:
        return

    assert isinstance(eco.get("id"), str) and eco["id"].strip()
    assert isinstance(eco.get("intent"), str) and eco["intent"].strip()
    api_references = eco.get("api_references", [])
    assert isinstance(api_references, list)
    assert all(
        isinstance(reference, str) and reference.strip() for reference in api_references
    )
    assert len(api_references) == len(set(api_references))
    target_query_ids = eco.get("target_query_ids")
    assert isinstance(target_query_ids, list) and target_query_ids
    assert len(target_query_ids) <= 20
    assert len(target_query_ids) == len(set(target_query_ids))

    verification = eco.get("verification")
    assert isinstance(verification, dict)
    queries = verification.get("queries")
    assertions = verification.get("assertions")
    assert isinstance(queries, list) and 1 <= len(queries) <= 50
    assert isinstance(assertions, list) and assertions
    query_ids = [query.get("id") for query in queries]
    assert len(query_ids) == len(set(query_ids))
    assert set(target_query_ids).issubset(query_ids)
    for query in queries:
        assert query.get("entity_type") in SUPPORTED_ENTITY_TYPES
        assert isinstance(query.get("selector"), dict)
        assert isinstance(query.get("fields"), list) and query["fields"]
        assert len(query["fields"]) == len(set(query["fields"]))
    _assert_assertion_contract(assertions, set(query_ids))
    asserted_query_ids = {assertion["query_id"] for assertion in assertions}
    assert set(target_query_ids).issubset(asserted_query_ids)

    eco_script = eco_script_path.read_text(encoding="utf-8")
    assert len(eco_script.encode("utf-8")) <= 64 * 1024
    _assert_exact_run_signature(
        eco_script_path,
        ast.parse(eco_script, filename=str(eco_script_path)),
    )
    _assert_exact_run_signature(
        eco_oracle_path,
        ast.parse(
            eco_oracle_path.read_text(encoding="utf-8"),
            filename=str(eco_oracle_path),
        ),
    )
    request = _eco_request(definition, eco_script)
    assert request["change_class"] == "scoped_update"
    assert request["target_query_ids"] == target_query_ids
    requirement = request["verification"]["requirements"][0]
    assert requirement["id"] == f"{definition['case_id']}_eco_contract"
    assert requirement["assertion_ids"] == [
        assertion["id"] for assertion in request["verification"]["assertions"]
    ]
    lint = lint_fusion_script(
        eco_script,
        "scoped_update",
        allowed_target_ids=set(target_query_ids),
        allowed_component_paths=set(),
    )
    assert lint.allowed, lint.as_dict()
    assert lint.detected_change_class == "scoped_update"
    assert lint.mutating_syntax_detected is True


def test_runner_case_summary_requires_every_phase_once_and_cleanup() -> None:
    runner = _reference_runner_module()

    initial = _passing_case_result()
    summary = runner._summarize_case_result("simple", initial, has_eco=False)
    assert summary["passed"] is True
    assert summary["initial_passed"] is True
    assert summary["eco_passed"] is None
    assert summary["cleanup_passed"] is True

    compound = _passing_case_result(has_eco=True)
    compound_summary = runner._summarize_case_result(
        "compound",
        compound,
        has_eco=True,
    )
    assert compound_summary["passed"] is True
    assert compound_summary["eco_passed"] is True

    duplicate_dispatch = json.loads(json.dumps(compound))
    duplicate_dispatch["eco"]["fast_path"]["transport_mutating_dispatch_count"] = 2
    assert (
        runner._summarize_case_result(
            "duplicate",
            duplicate_dispatch,
            has_eco=True,
        )["passed"]
        is False
    )

    missing_eco = _passing_case_result()
    assert (
        runner._summarize_case_result(
            "missing_eco",
            missing_eco,
            has_eco=True,
        )["passed"]
        is False
    )

    missing_cleanup = _passing_case_result()
    missing_cleanup.pop("cleanup")
    assert (
        runner._summarize_case_result(
            "missing_cleanup",
            missing_cleanup,
            has_eco=False,
        )["passed"]
        is False
    )

    repeated_camera = _passing_case_result()
    for artifact in repeated_camera["images"]:
        artifact["sha256"] = "same-camera"
    assert (
        runner._summarize_case_result(
            "repeated_camera",
            repeated_camera,
            has_eco=False,
        )["passed"]
        is False
    )


def test_runner_ignores_stale_case_result_from_an_older_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _reference_runner_module()
    cases_root = tmp_path / "cases"
    result_path = cases_root / "stale_case" / "reference_result.json"
    result_path.parent.mkdir(parents=True)
    result_path.write_text(
        json.dumps({"run_id": "ref_older", "oracle": {"passed": True}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "CASES_ROOT", cases_root)

    assert runner._load_current_case_result("stale_case", "ref_current") is None
    assert runner._load_current_case_result("stale_case", "ref_older") == {
        "run_id": "ref_older",
        "oracle": {"passed": True},
    }


def test_runner_restoration_failure_is_recorded_best_effort() -> None:
    runner = _reference_runner_module()

    class BrokenLifecycle:
        async def read_active_document_id(self) -> str:
            raise RuntimeError("endpoint unavailable")

        async def list_open_document_ids(self) -> list[str]:
            raise AssertionError("inventory read must not follow failed identity read")

    suite_result = {
        "original_document_id": "doc-original",
        "original_open_document_ids": ["doc-original"],
    }
    restored = asyncio.run(
        runner._capture_suite_restoration(BrokenLifecycle(), suite_result)
    )

    assert restored is False
    assert suite_result["restored"] is False
    assert suite_result["restoration_error"] == ("RuntimeError: endpoint unavailable")


def test_capture_images_fits_then_keeps_directional_capture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _reference_runner_module()
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    case_root = tmp_path / "cases" / "camera_case"
    screenshot_directions: list[str] = []
    raw_png = PNG_SIGNATURE + b"synthetic-png-evidence"
    encoded_png = base64.b64encode(raw_png).decode("ascii")

    async def fake_execute_tool_response(
        tool_name: str,
        arguments: dict[str, Any],
        *,
        runtime: object,
    ) -> SimpleNamespace:
        del runtime
        assert tool_name == "fusion_agent_native_read"
        screenshot_directions.append(arguments["direction"])
        return SimpleNamespace(
            is_error=False,
            payload={},
            content=[{"type": "image", "data": encoded_png}],
        )

    class FakeRuntime:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def _call_trusted_native_real(
            self,
            tool_name: str,
            arguments: dict[str, Any],
            **options: Any,
        ) -> dict[str, Any]:
            self.calls.append(
                {"tool_name": tool_name, "arguments": arguments, "options": options}
            )
            return {}

    runtime = FakeRuntime()
    monkeypatch.setattr(runner, "execute_tool_response", fake_execute_tool_response)
    artifacts = asyncio.run(
        runner._capture_images(
            runtime,
            case_root,
            "camera_case",
            "reference_initial",
        )
    )

    assert screenshot_directions == ["iso-top-right", "front", "top", "right"]
    fit_calls = [
        call
        for call in runtime.calls
        if ".activeViewport.fit()" in call["arguments"]["object"]["script"]
    ]
    assert len(runtime.calls) == 5  # one selection clear plus one fit per view
    assert len(fit_calls) == 4
    assert all(call["options"]["semantics"] == "read_only" for call in fit_calls)
    assert {artifact["direction"] for artifact in artifacts} == REQUIRED_VIEWS
    assert [artifact["native_direction"] for artifact in artifacts] == [
        "iso-top-right",
        "front",
        "top",
        "right",
    ]
    assert all(
        (tmp_path / artifact["path"]).read_bytes() == raw_png for artifact in artifacts
    )


def test_partial_runner_writes_run_specific_result_without_overwriting_canonical(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _reference_runner_module()
    cases_root = tmp_path / "cases"
    case_root = cases_root / "partial_case"
    case_root.mkdir(parents=True)
    (case_root / "definition.json").write_text('{"eco": null}', encoding="utf-8")

    class FakeRuntime:
        def __init__(self) -> None:
            self.closed = False

        async def close(self, *, timeout_seconds: float) -> None:
            assert timeout_seconds == 2.0
            self.closed = True

    class FakeLifecycle:
        def __init__(self, runtime: FakeRuntime) -> None:
            self.runtime = runtime

        async def read_active_document_id(self) -> str:
            return "doc-original"

        async def list_open_document_ids(self) -> list[str]:
            return ["doc-original"]

    runtime = FakeRuntime()

    async def fake_run_case(*_: Any) -> dict[str, Any]:
        return _passing_case_result()

    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner, "CASES_ROOT", cases_root)
    monkeypatch.setattr(runner, "DEFAULT_CASES", ["full_case"])
    monkeypatch.setattr(runner, "FusionAgentRuntime", lambda **_: runtime)
    monkeypatch.setattr(runner, "FusionRuntimeLifecycleBackend", FakeLifecycle)
    monkeypatch.setattr(runner, "_run_case", fake_run_case)

    asyncio.run(
        runner._main(
            ["partial_case"],
            git_commit="a" * 40,
            nightly_run_identity="12345-2",
        )
    )

    assert runtime.closed is True
    assert not (tmp_path / "reference_suite_result.json").exists()
    partial_results = list(tmp_path.glob("reference_suite_result_ref_*.json"))
    assert len(partial_results) == 1
    aggregate = _load_json(partial_results[0])
    assert aggregate["status"] == "passed"
    assert aggregate["requested_case_ids"] == ["partial_case"]
    assert aggregate["tested_commit"] == "a" * 40
    assert aggregate["nightly_run_identity"] == "12345-2"
    assert aggregate["restored"] is True
    assert aggregate["cases"][0]["passed"] is True
    assert aggregate["result_file"] == partial_results[0].name


def test_failed_case_is_captured_and_restoration_is_attempted_before_reraise(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _reference_runner_module()
    cases_root = tmp_path / "cases"
    case_root = cases_root / "broken_case"
    case_root.mkdir(parents=True)
    (case_root / "definition.json").write_text('{"eco": null}', encoding="utf-8")
    lifecycle_instances: list[Any] = []

    class FakeRuntime:
        def __init__(self) -> None:
            self.closed = False

        async def close(self, *, timeout_seconds: float) -> None:
            assert timeout_seconds == 2.0
            self.closed = True

    class FakeLifecycle:
        def __init__(self, runtime: FakeRuntime) -> None:
            self.runtime = runtime
            self.active_reads = 0
            self.inventory_reads = 0
            lifecycle_instances.append(self)

        async def read_active_document_id(self) -> str:
            self.active_reads += 1
            return "doc-original"

        async def list_open_document_ids(self) -> list[str]:
            self.inventory_reads += 1
            return ["doc-original"]

    runtime = FakeRuntime()

    async def fake_run_case(
        _runtime: object,
        _lifecycle: object,
        case_id: str,
        run_id: str,
    ) -> dict[str, Any]:
        result = _passing_case_result()
        result["run_id"] = run_id
        result.pop("cleanup")
        result["cleanup_error"] = "BenchmarkExecutionError: teardown failed"
        (cases_root / case_id / "reference_result.json").write_text(
            json.dumps(result),
            encoding="utf-8",
        )
        raise RuntimeError("case execution failed")

    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner, "CASES_ROOT", cases_root)
    monkeypatch.setattr(runner, "DEFAULT_CASES", ["broken_case"])
    monkeypatch.setattr(runner, "FusionAgentRuntime", lambda **_: runtime)
    monkeypatch.setattr(runner, "FusionRuntimeLifecycleBackend", FakeLifecycle)
    monkeypatch.setattr(runner, "_run_case", fake_run_case)

    with pytest.raises(RuntimeError, match="case execution failed"):
        asyncio.run(runner._main(["broken_case"]))

    aggregate = _load_json(tmp_path / "reference_suite_result.json")
    assert aggregate["status"] == "failed"
    assert aggregate["requested_case_ids"] == ["broken_case"]
    assert aggregate["failed_case_id"] == "broken_case"
    assert aggregate["restored"] is True
    assert len(aggregate["cases"]) == 1
    assert aggregate["cases"][0]["passed"] is False
    assert aggregate["cases"][0]["cleanup_passed"] is False
    assert aggregate["cases"][0]["error"] == (
        "BenchmarkExecutionError: teardown failed"
    )
    assert lifecycle_instances[0].active_reads == 2
    assert lifecycle_instances[0].inventory_reads == 2
    assert runtime.closed is True


@pytest.mark.parametrize("definition_path", CASE_DEFINITIONS, ids=_case_id)
def test_existing_reference_result_is_current_verified_and_reproducible(
    definition_path: Path,
) -> None:
    case_root = definition_path.parent
    case_id = case_root.name
    result_path = case_root / "reference_result.json"
    if not result_path.exists():
        pytest.skip(f"{case_id} has no captured real-Fusion reference result yet")

    definition = _load_json(definition_path)
    result = _load_json(result_path)
    build_script = (case_root / "build_script.py").read_text(encoding="utf-8")
    oracle_script = (case_root / "oracle_script.py").read_text(encoding="utf-8")
    build_sha256 = _sha256(build_script.encode("utf-8"))
    oracle_sha256 = _sha256(oracle_script.encode("utf-8"))
    definition_sha256 = _sha256(
        json.dumps(definition, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )

    assert result.get("schema_version") == "fusion_parametric_reference_result.v1"
    assert result.get("case_id") == case_id
    assert "error" not in result
    assert "cleanup_error" not in result
    assert result.get("definition_sha256") == definition_sha256
    assert result.get("build_script_sha256") == build_sha256
    assert result.get("oracle_script_sha256") == oracle_sha256
    assert result.get("linter", {}).get("allowed") is True
    assert result.get("linter", {}).get("script_sha256") == build_sha256
    _assert_fast_path_passed(
        result.get("fast_path", {}),
        change_class="additive",
        script_sha256=build_sha256,
    )
    _assert_oracle_passed(result.get("oracle", {}), case_id)
    _assert_png_artifacts(case_root, result.get("images"))

    cleanup = result.get("cleanup")
    assert isinstance(cleanup, dict)
    assert cleanup.get("closed_without_save") is True
    assert cleanup.get("restored") is True
    assert cleanup.get("inventory_restored") is True
    assert cleanup.get("active_document_id") == cleanup.get("original_document_id")
    assert sorted(cleanup.get("open_document_ids", [])) == sorted(
        cleanup.get("baseline_open_document_ids", [])
    )

    eco = definition.get("eco")
    if not eco:
        assert "eco" not in result
        return

    eco_script = (case_root / "eco_script.py").read_text(encoding="utf-8")
    eco_oracle_script = (case_root / "eco_oracle_script.py").read_text(encoding="utf-8")
    eco_script_sha256 = _sha256(eco_script.encode("utf-8"))
    eco_oracle_sha256 = _sha256(eco_oracle_script.encode("utf-8"))
    assert result.get("eco_script_sha256") == eco_script_sha256
    assert result.get("eco_oracle_script_sha256") == eco_oracle_sha256
    assert result.get("eco_linter", {}).get("allowed") is True
    assert result.get("eco_linter", {}).get("script_sha256") == eco_script_sha256
    eco_result = result.get("eco")
    assert isinstance(eco_result, dict)
    assert eco_result.get("id") == eco["id"]
    _assert_fast_path_passed(
        eco_result.get("fast_path", {}),
        change_class="scoped_update",
        script_sha256=eco_script_sha256,
    )
    _assert_oracle_passed(eco_result.get("oracle", {}), case_id)
    _assert_png_artifacts(case_root, eco_result.get("images"))
