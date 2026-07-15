from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any

from agent_core.fast_path import lint_fusion_script, validate_fast_execute_request
from benchmark.fixtures import FixtureDefinition
from benchmark.models import BenchmarkCase
from benchmark.runner import TrialContext
from fusion_agent_mcp.benchmark_bridge import (
    FusionRuntimeLifecycleBackend,
    _decode_script_payload,
)
from fusion_agent_mcp.runtime import FusionAgentRuntime
from fusion_agent_mcp.server import execute_tool, execute_tool_response


ROOT = Path(__file__).resolve().parent
CASES_ROOT = ROOT / "cases"
DEFAULT_CASES = [
    "b02_vented_enclosure",
    "b03_split_pillow_block",
    "b04_offset_duct_adapter",
    "b05_spherical_lattice_radome",
    "b06_robot_arm_assembly",
    "b07_packaging_machine",
]
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
REQUIRED_IMAGE_DIRECTIONS = {"isometric", "front", "top", "right"}


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _load_case(
    case_id: str,
) -> tuple[dict[str, Any], str, str, str | None, str | None]:
    case_root = CASES_ROOT / case_id
    definition = json.loads((case_root / "definition.json").read_text(encoding="utf-8"))
    build_script = (case_root / "build_script.py").read_text(encoding="utf-8")
    oracle_script = (case_root / "oracle_script.py").read_text(encoding="utf-8")
    eco_script_path = case_root / "eco_script.py"
    eco_oracle_path = case_root / "eco_oracle_script.py"
    eco_script = eco_script_path.read_text(encoding="utf-8") if eco_script_path.exists() else None
    eco_oracle_script = (
        eco_oracle_path.read_text(encoding="utf-8") if eco_oracle_path.exists() else None
    )
    if definition.get("case_id") != case_id:
        raise RuntimeError(f"definition/case mismatch for {case_id}")
    has_eco = bool(definition.get("eco"))
    if has_eco != bool(eco_script and eco_oracle_script):
        raise RuntimeError(
            f"{case_id} must provide definition.eco, eco_script.py and "
            "eco_oracle_script.py together"
        )
    return definition, build_script, oracle_script, eco_script, eco_oracle_script


def _build_request(definition: dict[str, Any], script: str) -> dict[str, Any]:
    queries = []
    assertions = []
    target_ids = []
    for target in definition["future_targets"]:
        query = {
            "id": target["id"],
            "entity_type": target["entity_type"],
            "selector": target["selector"],
            "fields": target["fields"],
        }
        queries.append(query)
        if target.get("bind_target", True):
            target_ids.append(target["id"])
        for index, assertion in enumerate(target["assertions"]):
            assertions.append(
                {
                    "id": f"{target['id']}_{index + 1}",
                    "query_id": target["id"],
                    **assertion,
                }
            )
    return validate_fast_execute_request(
        {
            "mode": "real",
            "intent": f"Build canonical reference fixture {definition['case_id']}: {definition['title']}",
            "change_class": "additive",
            "script": script,
            "api_references": definition.get("api_references", []),
            "target_query_ids": target_ids,
            "verification": _with_contract_requirement(
                {
                    "queries": queries,
                    "assertions": assertions,
                    "limit_per_query": 20,
                    "include_screenshot": False,
                },
                requirement_id=f"{definition['case_id']}_initial_contract",
            ),
        }
    )


def _eco_request(definition: dict[str, Any], script: str) -> dict[str, Any]:
    eco = definition["eco"]
    return validate_fast_execute_request(
        {
            "mode": "real",
            "intent": eco["intent"],
            "change_class": "scoped_update",
            "script": script,
            "api_references": eco.get("api_references", []),
            "target_query_ids": eco["target_query_ids"],
            "verification": _with_contract_requirement(
                eco["verification"],
                requirement_id=f"{definition['case_id']}_eco_contract",
            ),
        }
    )


def _with_contract_requirement(
    verification: dict[str, Any],
    *,
    requirement_id: str,
) -> dict[str, Any]:
    """Attach explicit requirement coverage to one reviewed benchmark phase."""

    normalized = json.loads(json.dumps(verification))
    assertions = normalized.get("assertions")
    if not isinstance(assertions, list) or not assertions:
        raise ValueError("benchmark verification must declare assertions")
    assertion_ids: list[str] = []
    for index, assertion in enumerate(assertions, start=1):
        if not isinstance(assertion, dict):
            raise ValueError("benchmark verification assertions must be objects")
        assertion_id = str(assertion.get("id") or f"{requirement_id}_assertion_{index}")
        assertion["id"] = assertion_id
        assertion_ids.append(assertion_id)
    normalized["requirements"] = [
        {
            "id": requirement_id,
            "description": "All reviewed assertions for this canonical benchmark phase must pass.",
            "required": True,
            "assertion_ids": assertion_ids,
            "oracle": "contract",
        }
    ]
    return normalized


def _trial_context(case_id: str, definition: dict[str, Any], run_id: str) -> TrialContext:
    trial_id = f"{case_id}_{uuid.uuid4().hex[:12]}"
    prompt_path = CASES_ROOT / case_id / "prompt.txt"
    prompt = prompt_path.read_text(encoding="utf-8") if prompt_path.exists() else definition["title"]
    case = BenchmarkCase(
        id=case_id,
        prompt=prompt,
        category="parametric_reference_fixture",
        risk="additive",
        timeout_seconds=900.0,
        fixture_id=f"empty_{case_id}",
        script_id=f"canonical_{case_id}",
        oracle_id=f"{case_id}_geometry",
        execution_paths=["native_fast"],
    )
    fixture = FixtureDefinition(
        id=f"empty_{case_id}",
        state={"saved": False, "bodies": [], "features": []},
    )
    marker = f"fusion_agent_trial_{run_id}_{case_id}_{uuid.uuid4().hex[:10]}"
    return TrialContext(
        run_id=run_id,
        trial_id=trial_id,
        pair_id=trial_id,
        case=case,
        fixture=fixture,
        execution_path="native_fast",
        mode="real",
        repetition=0,
        warmup=False,
        seed=42,
        project="fusion_parametric_reference_suite",
        dry_run=False,
        fixture_marker=marker,
    )


async def _run_oracle(
    runtime: FusionAgentRuntime,
    case_id: str,
    trial_id: str,
    oracle_script: str,
    phase: str = "initial",
) -> dict[str, Any]:
    result = await runtime._call_trusted_native_real(
        "fusion_mcp_execute",
        {"featureType": "script", "object": {"script": oracle_script}},
        semantics="read_only",
        operation_id=f"reference:{trial_id}:{phase}:oracle",
    )
    try:
        return _decode_script_payload(
            result,
            operation_id=f"reference:{case_id}:{phase}:oracle",
        )
    except BaseException as exc:
        return {
            "schema_version": "fusion_parametric_oracle.v2",
            "oracle_id": f"{case_id}_geometry",
            "case_id": case_id,
            "passed": False,
            "failed_checks": ["oracle.transport_or_decode"],
            "decode_error": f"{type(exc).__name__}: {exc}",
            "native_result": result.model_dump(mode="json", by_alias=True),
        }


async def _capture_images(
    runtime: FusionAgentRuntime,
    case_root: Path,
    case_id: str,
    prefix: str = "reference",
) -> list[dict[str, Any]]:
    await runtime._call_trusted_native_real(
        "fusion_mcp_execute",
        {
            "featureType": "script",
            "object": {
                "script": (
                    "import adsk.core\n\n"
                    "def run(_context: str):\n"
                    "    selections = adsk.core.Application.get().userInterface.activeSelections\n"
                    "    cleared = selections.clear()\n"
                    "    return str(bool(cleared))\n"
                )
            },
        },
        semantics="read_only",
        operation_id=f"reference:{case_id}:clear-selection",
    )
    image_root = case_root / "images"
    image_root.mkdir(parents=True, exist_ok=True)
    artifacts = []
    camera_views = (
        ("isometric", "iso-top-right"),
        ("front", "front"),
        ("top", "top"),
        ("right", "right"),
    )
    for label, direction in camera_views:
        # Fusion applies `direction` to the rendered screenshot, but does not
        # promise to persist that camera in the active viewport.  Fit first and
        # retain the directional screenshot itself.  Capturing `current` after
        # the directional call collapses every artifact to the same camera on
        # current Fusion builds.
        await runtime._call_trusted_native_real(
            "fusion_mcp_execute",
            {
                "featureType": "script",
                "object": {
                    "script": (
                        "import adsk.core\n\n"
                        "def run(_context: str):\n"
                        "    app = adsk.core.Application.get()\n"
                        "    app.activeViewport.fit()\n"
                        "    adsk.doEvents()\n"
                        "    return 'fitted'\n"
                    )
                },
            },
            semantics="read_only",
            operation_id=f"reference:{case_id}:{prefix}:{label}:fit-view",
        )
        response = await execute_tool_response(
            "fusion_agent_native_read",
            {
                "mode": "real",
                "query_type": "screenshot",
                "width": 1280,
                "height": 900,
                "anti_aliasing": True,
                "direction": direction,
            },
            runtime=runtime,
        )
        image_blocks = [
            block
            for block in response.content
            if isinstance(block, dict) and block.get("type") == "image"
        ]
        if response.is_error or len(image_blocks) != 1:
            raise RuntimeError(f"{case_id} screenshot {direction} failed: {response.payload}")
        raw = base64.b64decode(image_blocks[0]["data"], validate=True)
        if not raw.startswith(PNG_SIGNATURE):
            raise RuntimeError(f"{case_id} screenshot {direction} is not PNG")
        path = image_root / f"{prefix}_{label}.png"
        path.write_bytes(raw)
        artifacts.append(
            {
                "direction": label,
                "native_direction": direction,
                "path": str(path.relative_to(ROOT)).replace("\\", "/"),
                "bytes": len(raw),
                "sha256": _sha256_bytes(raw),
            }
        )
    return artifacts


async def _safe_cleanup(
    lifecycle: FusionRuntimeLifecycleBackend,
    context: TrialContext,
    session: Any,
    baseline_open_ids: list[str],
) -> dict[str, Any]:
    closed = await asyncio.shield(lifecycle.close_fixture_without_save(context, session))
    restored = await asyncio.shield(lifecycle.restore_original_document(context, session))
    active_id = await lifecycle.read_active_document_id()
    open_ids = await lifecycle.list_open_document_ids()
    cleanup = {
        "closed_without_save": bool(closed),
        "restored": bool(restored),
        "active_document_id": active_id,
        "original_document_id": session.original_document_id,
        "open_document_ids": open_ids,
        "baseline_open_document_ids": baseline_open_ids,
        "inventory_restored": sorted(open_ids) == sorted(baseline_open_ids),
    }
    if not (
        cleanup["closed_without_save"]
        and cleanup["restored"]
        and active_id == session.original_document_id
        and cleanup["inventory_restored"]
    ):
        raise RuntimeError(f"fixture cleanup failed closed: {cleanup}")
    return cleanup


async def _run_case(
    runtime: FusionAgentRuntime,
    lifecycle: FusionRuntimeLifecycleBackend,
    case_id: str,
    run_id: str,
) -> dict[str, Any]:
    definition, build_script, oracle_script, eco_script, eco_oracle_script = _load_case(case_id)
    request = _build_request(definition, build_script)
    lint = lint_fusion_script(
        build_script,
        "additive",
        allowed_target_ids=set(),
        allowed_component_paths=set(request["target_component_paths"]),
    )
    if not lint.allowed:
        raise RuntimeError(json.dumps(lint.as_dict(), sort_keys=True))
    eco_request = None
    eco_lint = None
    if eco_script is not None:
        eco_request = _eco_request(definition, eco_script)
        eco_lint = lint_fusion_script(
            eco_script,
            "scoped_update",
            allowed_target_ids=set(eco_request["target_query_ids"]),
            allowed_component_paths=set(),
        )
        if not eco_lint.allowed:
            raise RuntimeError(json.dumps(eco_lint.as_dict(), sort_keys=True))

    context = _trial_context(case_id, definition, run_id)
    baseline_open_ids = await lifecycle.list_open_document_ids()
    session = None
    cleanup = None
    started = time.perf_counter()
    case_root = CASES_ROOT / case_id
    result: dict[str, Any] = {
        "schema_version": "fusion_parametric_reference_result.v1",
        "run_id": run_id,
        "case_id": case_id,
        "trial_id": context.trial_id,
        "fixture_marker": context.fixture_marker,
        "definition_sha256": _sha256_bytes(
            json.dumps(definition, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ),
        "build_script_sha256": _sha256_bytes(build_script.encode("utf-8")),
        "oracle_script_sha256": _sha256_bytes(oracle_script.encode("utf-8")),
        "linter": lint.as_dict(),
    }
    if eco_script is not None and eco_oracle_script is not None and eco_lint is not None:
        result["eco_script_sha256"] = _sha256_bytes(eco_script.encode("utf-8"))
        result["eco_oracle_script_sha256"] = _sha256_bytes(
            eco_oracle_script.encode("utf-8")
        )
        result["eco_linter"] = eco_lint.as_dict()
    failure = None
    try:
        session = await lifecycle.prepare_fixture(context)
        identity = await lifecycle.read_fixture_identity(context, session)
        if not (
            identity.document_id == session.fixture_document_id
            and identity.fixture_marker == context.fixture_marker
            and identity.fixture_fingerprint == session.fixture_fingerprint
            and identity.unsaved
        ):
            raise RuntimeError(f"fixture identity mismatch: {identity}")
        result["fixture"] = {
            "original_document_id": session.original_document_id,
            "fixture_document_id": session.fixture_document_id,
            "fixture_marker": session.fixture_marker,
            "fixture_fingerprint": session.fixture_fingerprint,
            "unsaved": session.unsaved,
        }

        fast_result = await execute_tool(
            "fusion_agent_fast_execute",
            request,
            runtime=runtime,
        )
        result["fast_path"] = fast_result
        result["oracle"] = await _run_oracle(
            runtime,
            case_id,
            context.trial_id,
            oracle_script,
            "initial",
        )
        result["images"] = await _capture_images(
            runtime,
            case_root,
            case_id,
            "reference_initial" if eco_request is not None else "reference",
        )
        if eco_request is not None and eco_oracle_script is not None:
            result["eco"] = {
                "id": definition["eco"]["id"],
                "fast_path": await execute_tool(
                    "fusion_agent_fast_execute",
                    eco_request,
                    runtime=runtime,
                ),
            }
            result["eco"]["oracle"] = await _run_oracle(
                runtime,
                case_id,
                context.trial_id,
                eco_oracle_script,
                "eco",
            )
            result["eco"]["images"] = await _capture_images(
                runtime,
                case_root,
                case_id,
                "reference_eco",
            )
    except BaseException as exc:
        failure = exc
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if session is not None:
            try:
                cleanup = await _safe_cleanup(
                    lifecycle,
                    context,
                    session,
                    baseline_open_ids,
                )
                result["cleanup"] = cleanup
            except BaseException as cleanup_error:
                result["cleanup_error"] = f"{type(cleanup_error).__name__}: {cleanup_error}"
                failure = cleanup_error
        result["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        result_path = case_root / "reference_result.json"
        result_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    if failure is not None:
        raise failure
    if result.get("fast_path", {}).get("status") != "applied_verified":
        raise RuntimeError(f"{case_id} Fast Path did not verify: {result.get('fast_path', {}).get('status')}")
    if result.get("oracle", {}).get("passed") is not True:
        raise RuntimeError(f"{case_id} independent oracle failed: {result.get('oracle', {}).get('failed_checks')}")
    if eco_request is not None:
        if result.get("eco", {}).get("fast_path", {}).get("status") != "applied_verified":
            raise RuntimeError(
                f"{case_id} ECO Fast Path did not verify: "
                f"{result.get('eco', {}).get('fast_path', {}).get('status')}"
            )
        if result.get("eco", {}).get("oracle", {}).get("passed") is not True:
            raise RuntimeError(
                f"{case_id} ECO oracle failed: "
                f"{result.get('eco', {}).get('oracle', {}).get('failed_checks')}"
            )
    return result


def _phase_passed(
    fast_path: dict[str, Any],
    oracle: dict[str, Any],
    images: list[dict[str, Any]],
) -> bool:
    coverage = oracle.get("coverage") or {}
    image_directions = {
        artifact.get("direction")
        for artifact in images
        if isinstance(artifact, dict)
    }
    image_hashes = {
        artifact.get("sha256")
        for artifact in images
        if isinstance(artifact, dict) and artifact.get("sha256")
    }
    return bool(
        fast_path.get("status") == "applied_verified"
        and fast_path.get("declared_mutation_count") == 1
        and fast_path.get("mutating_call_count") == 1
        and fast_path.get("transport_mutating_dispatch_count") == 1
        and (fast_path.get("verification") or {}).get("passed") is True
        and oracle.get("passed") is True
        and oracle.get("failed_checks") == []
        and coverage.get("mandatory", 0) > 0
        and coverage.get("passed") == coverage.get("mandatory")
        and coverage.get("failed") == 0
        and coverage.get("unverified") == 0
        and len(images) == len(REQUIRED_IMAGE_DIRECTIONS)
        and image_directions == REQUIRED_IMAGE_DIRECTIONS
        # Symmetry can make two orthographic views equal, but four identical
        # captures prove that the requested camera routing did not take effect.
        and len(image_hashes) > 1
    )


def _summarize_case_result(
    case_id: str,
    result: dict[str, Any],
    *,
    has_eco: bool,
) -> dict[str, Any]:
    fast_path = result.get("fast_path") or {}
    oracle = result.get("oracle") or {}
    initial_passed = _phase_passed(
        fast_path,
        oracle,
        result.get("images") or [],
    )
    eco_result = result.get("eco") or {}
    eco_fast_path = eco_result.get("fast_path") or {}
    eco_oracle = eco_result.get("oracle") or {}
    eco_passed = (
        _phase_passed(
            eco_fast_path,
            eco_oracle,
            eco_result.get("images") or [],
        )
        if has_eco
        else None
    )
    cleanup = result.get("cleanup") or {}
    cleanup_passed = bool(
        cleanup.get("closed_without_save") is True
        and cleanup.get("restored") is True
        and cleanup.get("inventory_restored") is True
        and cleanup.get("active_document_id") == cleanup.get("original_document_id")
        and sorted(cleanup.get("open_document_ids") or [])
        == sorted(cleanup.get("baseline_open_document_ids") or [])
    )
    error = result.get("error") or result.get("cleanup_error")
    phase_contract_passed = eco_passed is True if has_eco else not bool(eco_result)
    return {
        "case_id": case_id,
        "passed": bool(
            initial_passed
            and phase_contract_passed
            and cleanup_passed
            and not error
        ),
        "initial_passed": initial_passed,
        "fast_path_status": fast_path.get("status"),
        "oracle_passed": oracle.get("passed") is True,
        "eco_required": has_eco,
        "eco_passed": eco_passed,
        "eco_fast_path_status": eco_fast_path.get("status") if has_eco else None,
        "eco_oracle_passed": eco_oracle.get("passed") is True if has_eco else None,
        "cleanup_passed": cleanup_passed,
        "elapsed_ms": result.get("elapsed_ms"),
        "error": error,
    }


def _load_current_case_result(case_id: str, run_id: str) -> dict[str, Any] | None:
    path = CASES_ROOT / case_id / "reference_result.json"
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(result, dict) or result.get("run_id") != run_id:
        return None
    return result


async def _capture_suite_restoration(
    lifecycle: FusionRuntimeLifecycleBackend,
    suite_result: dict[str, Any],
) -> bool:
    original_id = suite_result.get("original_document_id")
    original_open_ids = suite_result.get("original_open_document_ids")
    if original_id is None or not isinstance(original_open_ids, list):
        suite_result["restored"] = False
        suite_result["restoration_error"] = "original document inventory was not captured"
        return False
    try:
        final_id = await lifecycle.read_active_document_id()
        final_open_ids = await lifecycle.list_open_document_ids()
        if not isinstance(final_open_ids, list):
            raise TypeError("final open document inventory is not a list")
        restored = (
            final_id == original_id
            and sorted(final_open_ids) == sorted(original_open_ids)
        )
    except BaseException as exc:
        suite_result["restored"] = False
        suite_result["restoration_error"] = f"{type(exc).__name__}: {exc}"
        return False
    suite_result["final_document_id"] = final_id
    suite_result["final_open_document_ids"] = final_open_ids
    suite_result["restored"] = restored
    return bool(suite_result["restored"])


async def _main(case_ids: list[str]) -> None:
    requested_case_ids = list(case_ids)
    run_id = "ref_" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    runtime = FusionAgentRuntime(manifest_root="manifests", outputs_root="outputs")
    lifecycle = FusionRuntimeLifecycleBackend(runtime)
    suite_result = {
        "schema_version": "fusion_parametric_reference_suite_result.v1",
        "run_id": run_id,
        "requested_case_ids": requested_case_ids,
        "status": "running",
        "cases": [],
    }
    try:
        try:
            suite_result["original_document_id"] = await lifecycle.read_active_document_id()
            suite_result["original_open_document_ids"] = await lifecycle.list_open_document_ids()
            for case_id in requested_case_ids:
                has_eco: bool | None = None
                try:
                    definition = json.loads(
                        (CASES_ROOT / case_id / "definition.json").read_text(
                            encoding="utf-8"
                        )
                    )
                    has_eco = bool(definition.get("eco"))
                    case_result = await _run_case(runtime, lifecycle, case_id, run_id)
                except BaseException as case_error:
                    current_result = _load_current_case_result(case_id, run_id)
                    effective_has_eco = (
                        has_eco
                        if has_eco is not None
                        else bool(
                            current_result
                            and (
                                current_result.get("eco_script_sha256")
                                or current_result.get("eco")
                            )
                        )
                    )
                    if current_result is None:
                        summary = {
                            "case_id": case_id,
                            "passed": False,
                            "initial_passed": False,
                            "fast_path_status": None,
                            "oracle_passed": False,
                            "eco_required": effective_has_eco,
                            "eco_passed": False if effective_has_eco else None,
                            "eco_fast_path_status": None,
                            "eco_oracle_passed": (
                                False if effective_has_eco else None
                            ),
                            "cleanup_passed": False,
                            "elapsed_ms": None,
                            "error": f"{type(case_error).__name__}: {case_error}",
                        }
                    else:
                        summary = _summarize_case_result(
                            case_id,
                            current_result,
                            has_eco=effective_has_eco,
                        )
                        if not summary["error"]:
                            summary["error"] = (
                                f"{type(case_error).__name__}: {case_error}"
                            )
                    suite_result["cases"].append(summary)
                    suite_result["failed_case_id"] = case_id
                    raise
                assert has_eco is not None
                summary = _summarize_case_result(
                    case_id,
                    case_result,
                    has_eco=has_eco,
                )
                suite_result["cases"].append(summary)
                if not summary["passed"]:
                    suite_result["failed_case_id"] = case_id
                    raise RuntimeError(f"{case_id} failed the aggregate reference gates")
        except BaseException as exc:
            suite_result["status"] = "failed"
            suite_result["error"] = f"{type(exc).__name__}: {exc}"
            raise
    finally:
        await _capture_suite_restoration(lifecycle, suite_result)
        completed = (
            len(suite_result["cases"]) == len(requested_case_ids)
            and all(item.get("passed") is True for item in suite_result["cases"])
        )
        if suite_result["status"] == "running":
            if completed and suite_result.get("restored") is True:
                suite_result["status"] = "passed"
            else:
                suite_result["status"] = "failed"
                suite_result["error"] = "suite aggregate gates did not pass"
        try:
            await runtime.close(timeout_seconds=2.0)
        except BaseException as close_error:
            suite_result["status"] = "failed"
            suite_result["close_error"] = f"{type(close_error).__name__}: {close_error}"
        suite_result["completed_at_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        result_name = (
            "reference_suite_result.json"
            if requested_case_ids == DEFAULT_CASES
            else f"reference_suite_result_{run_id}.json"
        )
        suite_result["result_file"] = result_name
        (ROOT / result_name).write_text(
            json.dumps(suite_result, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    if suite_result["status"] != "passed":
        raise RuntimeError(suite_result.get("error") or "suite reference run failed")
    print(json.dumps(suite_result, ensure_ascii=False, indent=2, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("cases", nargs="*", default=DEFAULT_CASES)
    args = parser.parse_args()
    unknown = [case_id for case_id in args.cases if not (CASES_ROOT / case_id).is_dir()]
    if unknown:
        raise SystemExit(f"unknown cases: {', '.join(unknown)}")
    asyncio.run(_main(args.cases))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
