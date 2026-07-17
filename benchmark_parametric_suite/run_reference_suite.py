from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

from agent_core.request_context import RequestContext
from agent_core.fast_path import lint_fusion_script, validate_fast_execute_request
from benchmark.fixtures import FixtureDefinition
from benchmark.filesystem import (
    atomic_write_bytes,
    atomic_write_text,
    mkdir,
    mkdir_exclusive,
    path_exists,
    path_is_dir,
    read_text,
)
from benchmark.models import BenchmarkCase
from benchmark.provenance import RevisionIdentity, collect_workspace_revision
from benchmark.runner import TrialContext
from fusion_agent_mcp.benchmark_bridge import (
    FusionRuntimeLifecycleBackend,
    _decode_script_payload,
)
from fusion_agent_mcp.runtime import FusionAgentRuntime
from fusion_agent_mcp.server import execute_tool, execute_tool_response


ROOT = Path(__file__).resolve().parent
CASES_ROOT = ROOT / "cases"
DEFAULT_OUTPUT_ROOT = ROOT.parent / "outputs" / "reference-runs"
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
GIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
NIGHTLY_RUN_IDENTITY_PATTERN = re.compile(r"^[1-9][0-9]*-[1-9][0-9]*$")
SOURCE_MANIFEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
FIXTURE_FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _load_case(
    case_id: str,
) -> tuple[dict[str, Any], str, str, str | None, str | None]:
    case_root = CASES_ROOT / case_id
    definition = json.loads(read_text(case_root / "definition.json"))
    build_script = read_text(case_root / "build_script.py")
    oracle_script = read_text(case_root / "oracle_script.py")
    eco_script_path = case_root / "eco_script.py"
    eco_oracle_path = case_root / "eco_oracle_script.py"
    eco_script = read_text(eco_script_path) if path_exists(eco_script_path) else None
    eco_oracle_script = (
        read_text(eco_oracle_path) if path_exists(eco_oracle_path) else None
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


def _trial_context(
    case_id: str, definition: dict[str, Any], run_id: str
) -> TrialContext:
    trial_id = f"{case_id}_{uuid.uuid4().hex[:12]}"
    prompt_path = CASES_ROOT / case_id / "prompt.txt"
    prompt = read_text(prompt_path) if path_exists(prompt_path) else definition["title"]
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


def _fast_path_request_context(
    runtime: FusionAgentRuntime,
    context: TrialContext,
    session: Any,
    request: dict[str, Any],
    *,
    phase: str,
) -> RequestContext:
    """Bind one reviewed Fast Path request to its disposable benchmark fixture."""

    if (
        context.mode != "real"
        or context.dry_run
        or context.execution_path != "native_fast"
    ):
        raise RuntimeError("reference Fast Path requires one real native_fast trial")
    fixture_document_id = getattr(session, "fixture_document_id", None)
    fixture_marker = getattr(session, "fixture_marker", None)
    fixture_fingerprint = getattr(session, "fixture_fingerprint", None)
    if (
        not isinstance(fixture_marker, str)
        or fixture_marker != context.fixture_marker
        or not isinstance(fixture_fingerprint, str)
        or not FIXTURE_FINGERPRINT_PATTERN.fullmatch(fixture_fingerprint)
        or getattr(session, "unsaved", None) is not True
    ):
        raise RuntimeError("reference Fast Path fixture binding is incomplete")
    document_identity = _normalize_fixture_document_identity(
        fixture_document_id,
        fixture_marker=fixture_marker,
    )
    canonical_request = json.dumps(
        request,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return RequestContext(
        request_id=f"reference:{context.trial_id}:{phase}",
        session_id=context.run_id,
        trial_id=context.trial_id,
        profile="advanced",
        mode="real",
        backend=runtime.configuration.backend,
        document_identity=document_identity,
        spec_digest=_sha256_bytes(canonical_request),
        capabilities=(
            "fast_path:enabled",
            "execution_path:native_fast",
            f"benchmark_fixture:{fixture_fingerprint}",
        ),
    )


def _normalize_fixture_document_identity(
    value: Any,
    *,
    fixture_marker: str,
) -> str:
    """Translate lifecycle keys into the Fast Path stable-binding namespace."""

    if (
        not isinstance(value, str)
        or value != value.strip()
        or any(character.isspace() for character in value)
    ):
        raise RuntimeError("reference Fast Path fixture binding is incomplete")
    kind, separator, identifier = value.partition(":")
    if separator != ":" or not identifier:
        raise RuntimeError("reference Fast Path fixture binding is incomplete")
    if kind == "data":
        return value
    if kind == "marker" and identifier == fixture_marker:
        return f"runtime:{value}"
    raise RuntimeError("reference Fast Path fixture binding is incomplete")


async def _run_oracle(
    runtime: FusionAgentRuntime,
    lifecycle: FusionRuntimeLifecycleBackend,
    context: TrialContext,
    session: Any,
    case_id: str,
    oracle_script: str,
    phase: str = "initial",
) -> dict[str, Any]:
    identity = await lifecycle.read_fixture_identity(context, session)
    if not _fixture_identity_matches(identity, context, session):
        return _oracle_identity_failure(case_id, phase)
    bound_script = _bind_oracle_script(
        oracle_script,
        document_id=session.fixture_document_id,
        marker=session.fixture_marker,
        fingerprint=session.fixture_fingerprint,
    )
    result = await runtime._call_trusted_native_real(
        "fusion_mcp_execute",
        {"featureType": "script", "object": {"script": bound_script}},
        semantics="read_only",
        operation_id=f"reference:{context.trial_id}:{phase}:oracle",
    )
    try:
        payload = _decode_script_payload(
            result,
            operation_id=f"reference:{case_id}:{phase}:oracle",
        )
    except BaseException:
        return {
            "schema_version": "fusion_parametric_oracle.v2",
            "oracle_id": f"{case_id}_geometry",
            "case_id": case_id,
            "passed": False,
            "failed_checks": ["oracle.transport_or_decode"],
            "coverage": {"mandatory": 1, "passed": 0, "failed": 1, "unverified": 0},
            "error": _public_error(
                "ORACLE_TRANSPORT_OR_DECODE_FAILED",
                correlation_material=f"{context.trial_id}:{phase}:decode",
            ),
        }
    identity_after = await lifecycle.read_fixture_identity(context, session)
    if not _fixture_identity_matches(identity_after, context, session):
        return _oracle_identity_failure(case_id, phase)
    payload["evidence_envelope"] = {
        "producer": "reference_oracle",
        "provenance": "code_owned_oracle",
        "document_identity_sha256": _sha256_bytes(
            session.fixture_document_id.encode("utf-8")
        ),
        "complete": True,
        "counts_exact": True,
        "truncated": False,
        "stop_reason": "completed",
    }
    return payload


def _fixture_identity_matches(
    identity: Any, context: TrialContext, session: Any
) -> bool:
    return bool(
        identity.document_id == session.fixture_document_id
        and identity.fixture_marker == context.fixture_marker
        and identity.fixture_fingerprint == session.fixture_fingerprint
        and identity.unsaved
    )


def _oracle_identity_failure(case_id: str, phase: str) -> dict[str, Any]:
    return {
        "schema_version": "fusion_parametric_oracle.v2",
        "oracle_id": f"{case_id}_geometry",
        "case_id": case_id,
        "phase": phase,
        "passed": False,
        "failed_checks": ["ORACLE_FIXTURE_IDENTITY_MISMATCH"],
        "coverage": {"mandatory": 1, "passed": 0, "failed": 1, "unverified": 0},
        "evidence_envelope": {
            "producer": "reference_oracle",
            "provenance": "code_owned_oracle",
            "complete": False,
            "counts_exact": False,
            "truncated": False,
            "stop_reason": "fixture_identity_mismatch",
        },
    }


def _bind_oracle_script(
    oracle_script: str,
    *,
    document_id: str,
    marker: str,
    fingerprint: str,
) -> str:
    """Inject an exact document/marker/fingerprint check before oracle reads."""

    signature = re.compile(r"^def run\(_context: str\):\s*$", re.MULTILINE)
    matches = list(signature.finditer(oracle_script))
    if len(matches) != 1:
        raise ValueError("oracle script must expose exactly one typed run entrypoint")
    guard = f"""\n    _fa_expected_document_id = json.loads({json.dumps(document_id)!r})
    _fa_expected_marker = json.loads({json.dumps(marker)!r})
    _fa_expected_fingerprint = json.loads({json.dumps(fingerprint)!r})
    _fa_app = adsk.core.Application.get()
    _fa_document = _fa_app.activeDocument
    _fa_design = adsk.fusion.Design.cast(_fa_app.activeProduct)
    _fa_root = _fa_design.rootComponent if _fa_design is not None else None
    _fa_marker_attribute = (
        _fa_root.attributes.itemByName("fusion_agent_benchmark", "trial_marker")
        if _fa_root is not None else None
    )
    _fa_fingerprint_attribute = (
        _fa_root.attributes.itemByName("fusion_agent_benchmark", "fixture_fingerprint")
        if _fa_root is not None else None
    )
    _fa_marker = _fa_marker_attribute.value if _fa_marker_attribute is not None else None
    _fa_fingerprint = (
        _fa_fingerprint_attribute.value if _fa_fingerprint_attribute is not None else None
    )
    _fa_data_file = _fa_document.dataFile if _fa_document is not None else None
    _fa_document_id = (
        "data:" + str(_fa_data_file.id)
        if _fa_data_file is not None and _fa_data_file.id
        else ("marker:" + str(_fa_marker) if _fa_marker else None)
    )
    if not (
        _fa_document_id == _fa_expected_document_id
        and _fa_marker == _fa_expected_marker
        and _fa_fingerprint == _fa_expected_fingerprint
        and _fa_data_file is None
    ):
        print(json.dumps({{
            "ok": False,
            "schema_version": "fusion_parametric_oracle.v2",
            "passed": False,
            "failed_checks": ["ORACLE_FIXTURE_IDENTITY_MISMATCH"],
            "coverage": {{"mandatory": 1, "passed": 0, "failed": 1, "unverified": 0}},
        }}, sort_keys=True, allow_nan=False))
        return
"""
    insertion = matches[0].end()
    return oracle_script[:insertion] + guard + oracle_script[insertion:]


async def _capture_images(
    runtime: FusionAgentRuntime,
    case_root: Path,
    run_root: Path,
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
            raise RuntimeError(f"{case_id} screenshot capture failed")
        raw = base64.b64decode(image_blocks[0]["data"], validate=True)
        if not raw.startswith(PNG_SIGNATURE):
            raise RuntimeError(f"{case_id} screenshot {direction} is not PNG")
        path = image_root / f"{prefix}_{label}.png"
        atomic_write_bytes(path, raw)
        artifacts.append(
            {
                "direction": label,
                "native_direction": direction,
                "path": str(path.relative_to(run_root)).replace("\\", "/"),
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
    closed = await asyncio.shield(
        lifecycle.close_fixture_without_save(context, session)
    )
    restored = await asyncio.shield(
        lifecycle.restore_original_document(context, session)
    )
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


def _public_error(code: str, *, correlation_material: str) -> dict[str, Any]:
    return {
        "code": code,
        "generic_message": "The benchmark operation failed. Inspect private local diagnostics.",
        "correlation_id": _sha256_bytes(correlation_material.encode("utf-8"))[:16],
        "retryable": False,
    }


def _project_case_result(result: dict[str, Any]) -> dict[str, Any]:
    projected: dict[str, Any] = {
        key: result[key]
        for key in (
            "schema_version",
            "run_id",
            "case_id",
            "definition_sha256",
            "build_script_sha256",
            "oracle_script_sha256",
            "eco_script_sha256",
            "eco_oracle_script_sha256",
            "elapsed_ms",
        )
        if key in result
    }
    if isinstance(result.get("linter"), dict):
        projected["linter"] = _project_linter(result["linter"])
    if isinstance(result.get("eco_linter"), dict):
        projected["eco_linter"] = _project_linter(result["eco_linter"])
    if isinstance(result.get("fixture"), dict):
        fixture = result["fixture"]
        identity_material = "|".join(
            str(fixture.get(key) or "")
            for key in (
                "fixture_document_id",
                "fixture_marker",
                "fixture_fingerprint",
            )
        )
        projected["fixture"] = {
            "identity_sha256": _sha256_bytes(identity_material.encode("utf-8")),
            "document_bound": bool(fixture.get("fixture_document_id")),
            "marker_bound": bool(fixture.get("fixture_marker")),
            "fingerprint_bound": bool(fixture.get("fixture_fingerprint")),
            "unsaved": fixture.get("unsaved") is True,
        }
    if isinstance(result.get("fast_path"), dict):
        projected["fast_path"] = _project_fast_path(result["fast_path"])
    if isinstance(result.get("oracle"), dict):
        projected["oracle"] = _project_oracle(result["oracle"])
    projected["images"] = _project_images(result.get("images"))
    if isinstance(result.get("eco"), dict):
        eco = result["eco"]
        projected["eco"] = {
            "id": str(eco.get("id") or "")[:120],
            "fast_path": _project_fast_path(eco.get("fast_path") or {}),
            "oracle": _project_oracle(eco.get("oracle") or {}),
            "images": _project_images(eco.get("images")),
        }
    if isinstance(result.get("cleanup"), dict):
        cleanup = result["cleanup"]
        projected["cleanup"] = {
            "closed_without_save": cleanup.get("closed_without_save") is True,
            "restored": cleanup.get("restored") is True,
            "identity_restored": (
                cleanup.get("active_document_id") == cleanup.get("original_document_id")
            ),
            "inventory_restored": cleanup.get("inventory_restored") is True,
        }
    for key in ("error", "cleanup_error"):
        if isinstance(result.get(key), dict):
            projected[key] = result[key]
    return projected


def _project_linter(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value[key]
        for key in (
            "allowed",
            "script_sha256",
            "declared_change_class",
            "detected_change_class",
            "mutating_syntax_detected",
        )
        if key in value
    }


def _project_fast_path(value: dict[str, Any]) -> dict[str, Any]:
    projected = {
        key: value[key]
        for key in (
            "declared_mutation_count",
            "mutating_call_count",
            "transport_mutating_dispatch_count",
            "contract_verified",
        )
        if key in value
    }
    if "status" in value:
        projected["status"] = _public_token(
            value["status"], "fast_path_status_redacted"
        )
    if "assertion_status" in value:
        projected["assertion_status"] = _public_token(
            value["assertion_status"], "assertion_status_redacted"
        )
    verification = value.get("verification")
    if isinstance(verification, dict):
        projected["verification"] = {
            key: verification[key]
            for key in (
                "passed",
                "status",
                "assertion_status",
                "contract_verified",
                "readback_complete",
            )
            if key in verification
        }
    # Transient audit paths are never advertised. A future embedded public
    # audit must carry its own digest and size before this projector accepts it.
    audit = value.get("public_audit")
    if isinstance(audit, dict):
        digest = audit.get("sha256")
        size = audit.get("bytes")
        if (
            isinstance(digest, str)
            and re.fullmatch(r"[0-9a-f]{64}", digest)
            and type(size) is int
            and 0 < size <= 1_000_000
        ):
            projected["public_audit"] = {"sha256": digest, "bytes": size}
    return projected


def _project_oracle(value: dict[str, Any]) -> dict[str, Any]:
    coverage = value.get("coverage") if isinstance(value.get("coverage"), dict) else {}
    envelope = (
        value.get("evidence_envelope")
        if isinstance(value.get("evidence_envelope"), dict)
        else {}
    )
    failed_checks = value.get("failed_checks")
    if not isinstance(failed_checks, list):
        failed_checks = []
    return {
        "schema_version": _public_token(
            value.get("schema_version"), "oracle_schema_redacted"
        ),
        "oracle_id": _public_token(value.get("oracle_id"), "oracle_id_redacted"),
        "case_id": _public_token(value.get("case_id"), "case_id_redacted"),
        "phase": _public_token(value.get("phase"), "phase_redacted"),
        "passed": value.get("passed") is True,
        "failed_checks": [
            _public_token(item, "oracle_check_redacted")
            for item in failed_checks[:100]
            if isinstance(item, str)
        ],
        "coverage": {
            key: coverage.get(key)
            for key in ("mandatory", "passed", "failed", "unverified")
        },
        "evidence_envelope": {
            "producer": _public_token(envelope.get("producer"), "producer_redacted"),
            "provenance": _public_token(
                envelope.get("provenance"), "provenance_redacted"
            ),
            "document_identity_sha256": (
                envelope.get("document_identity_sha256")
                if isinstance(envelope.get("document_identity_sha256"), str)
                and re.fullmatch(r"[0-9a-f]{64}", envelope["document_identity_sha256"])
                else None
            ),
            "complete": envelope.get("complete") is True,
            "counts_exact": envelope.get("counts_exact") is True,
            "truncated": envelope.get("truncated") is True,
            "stop_reason": _public_token(
                envelope.get("stop_reason"), "stop_reason_redacted"
            ),
        },
    }


def _public_token(value: Any, fallback: str) -> str:
    normalized = str(value or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", normalized):
        return normalized
    return fallback


def _project_images(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    projected: list[dict[str, Any]] = []
    for item in value[:8]:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").replace("\\", "/")
        digest = item.get("sha256")
        size = item.get("bytes")
        if (
            not path
            or path.startswith("/")
            or ".." in Path(path).parts
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or type(size) is not int
            or not 8 < size <= 25_000_000
        ):
            continue
        projected.append(
            {
                "direction": str(item.get("direction") or "")[:40],
                "native_direction": str(item.get("native_direction") or "")[:40],
                "path": path,
                "bytes": size,
                "sha256": digest,
            }
        )
    return projected


async def _run_case(
    runtime: FusionAgentRuntime,
    lifecycle: FusionRuntimeLifecycleBackend,
    case_id: str,
    run_id: str,
    artifact_run_root: Path,
) -> dict[str, Any]:
    definition, build_script, oracle_script, eco_script, eco_oracle_script = _load_case(
        case_id
    )
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
    case_root = artifact_run_root / "cases" / case_id
    mkdir(case_root)
    result: dict[str, Any] = {
        "schema_version": "fusion_parametric_reference_result.v2",
        "run_id": run_id,
        "case_id": case_id,
        "trial_id": context.trial_id,
        "fixture_marker": context.fixture_marker,
        "definition_sha256": _sha256_bytes(
            json.dumps(definition, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ),
        "build_script_sha256": _sha256_bytes(build_script.encode("utf-8")),
        "oracle_script_sha256": _sha256_bytes(oracle_script.encode("utf-8")),
        "linter": lint.as_dict(),
    }
    if (
        eco_script is not None
        and eco_oracle_script is not None
        and eco_lint is not None
    ):
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
            profile="advanced",
            request_context=_fast_path_request_context(
                runtime,
                context,
                session,
                request,
                phase="initial",
            ),
        )
        result["fast_path"] = fast_result
        result["oracle"] = await _run_oracle(
            runtime,
            lifecycle,
            context,
            session,
            case_id,
            oracle_script,
            "initial",
        )
        result["images"] = await _capture_images(
            runtime,
            case_root,
            artifact_run_root,
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
                    profile="advanced",
                    request_context=_fast_path_request_context(
                        runtime,
                        context,
                        session,
                        eco_request,
                        phase="eco",
                    ),
                ),
            }
            result["eco"]["oracle"] = await _run_oracle(
                runtime,
                lifecycle,
                context,
                session,
                case_id,
                eco_oracle_script,
                "eco",
            )
            result["eco"]["images"] = await _capture_images(
                runtime,
                case_root,
                artifact_run_root,
                case_id,
                "reference_eco",
            )
    except BaseException as exc:
        failure = exc
        result["error"] = _public_error(
            "REFERENCE_CASE_FAILED",
            correlation_material=f"{run_id}:{case_id}:case",
        )
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
                result["cleanup_error"] = _public_error(
                    "REFERENCE_CLEANUP_FAILED",
                    correlation_material=f"{run_id}:{case_id}:cleanup",
                )
                failure = cleanup_error
        result["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        result_path = case_root / "reference_result.json"
        public_result = _project_case_result(result)
        atomic_write_text(
            result_path,
            json.dumps(
                public_result,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n",
        )
    if failure is not None:
        raise RuntimeError("reference case execution failed") from failure
    if result.get("fast_path", {}).get("status") != "applied_verified":
        raise RuntimeError(
            f"{case_id} Fast Path did not verify: {result.get('fast_path', {}).get('status')}"
        )
    if result.get("oracle", {}).get("passed") is not True:
        raise RuntimeError(
            f"{case_id} independent oracle failed: {result.get('oracle', {}).get('failed_checks')}"
        )
    if eco_request is not None:
        if (
            result.get("eco", {}).get("fast_path", {}).get("status")
            != "applied_verified"
        ):
            raise RuntimeError(
                f"{case_id} ECO Fast Path did not verify: "
                f"{result.get('eco', {}).get('fast_path', {}).get('status')}"
            )
        if result.get("eco", {}).get("oracle", {}).get("passed") is not True:
            raise RuntimeError(
                f"{case_id} ECO oracle failed: "
                f"{result.get('eco', {}).get('oracle', {}).get('failed_checks')}"
            )
    return public_result


def _phase_passed(
    fast_path: dict[str, Any],
    oracle: dict[str, Any],
    images: list[dict[str, Any]],
) -> bool:
    coverage = oracle.get("coverage") or {}
    envelope = oracle.get("evidence_envelope") or {}
    image_directions = {
        artifact.get("direction") for artifact in images if isinstance(artifact, dict)
    }
    image_hashes = {
        artifact.get("sha256")
        for artifact in images
        if isinstance(artifact, dict) and artifact.get("sha256")
    }
    return bool(
        fast_path.get("status") == "applied_verified"
        and _exact_int(fast_path.get("declared_mutation_count"), 1)
        and _exact_int(fast_path.get("mutating_call_count"), 1)
        and _exact_int(fast_path.get("transport_mutating_dispatch_count"), 1)
        and (fast_path.get("verification") or {}).get("passed") is True
        and oracle.get("passed") is True
        and oracle.get("failed_checks") == []
        and _positive_exact_int(coverage.get("mandatory"))
        and _exact_int(coverage.get("passed"), coverage.get("mandatory"))
        and _exact_int(coverage.get("failed"), 0)
        and _exact_int(coverage.get("unverified"), 0)
        and envelope.get("producer") == "reference_oracle"
        and envelope.get("provenance") == "code_owned_oracle"
        and isinstance(envelope.get("document_identity_sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", envelope["document_identity_sha256"])
        and envelope.get("complete") is True
        and envelope.get("counts_exact") is True
        and envelope.get("truncated") is False
        and envelope.get("stop_reason") == "completed"
        and len(images) == len(REQUIRED_IMAGE_DIRECTIONS)
        and image_directions == REQUIRED_IMAGE_DIRECTIONS
        # Symmetry can make two orthographic views equal, but four identical
        # captures prove that the requested camera routing did not take effect.
        and len(image_hashes) > 1
    )


def _exact_int(value: Any, expected: Any) -> bool:
    return type(value) is int and type(expected) is int and value == expected


def _positive_exact_int(value: Any) -> bool:
    return type(value) is int and value > 0


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
        and cleanup.get("identity_restored") is True
        and cleanup.get("inventory_restored") is True
    )
    error = result.get("error") or result.get("cleanup_error")
    phase_contract_passed = eco_passed is True if has_eco else not bool(eco_result)
    return {
        "case_id": case_id,
        "passed": bool(
            initial_passed and phase_contract_passed and cleanup_passed and not error
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


def _load_current_case_result(
    case_id: str, run_id: str, artifact_run_root: Path
) -> dict[str, Any] | None:
    path = artifact_run_root / "cases" / case_id / "reference_result.json"
    try:
        result = json.loads(read_text(path))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(result, dict) or result.get("run_id") != run_id:
        return None
    return result


async def _capture_suite_restoration(
    lifecycle: FusionRuntimeLifecycleBackend,
    suite_result: dict[str, Any],
) -> bool:
    original_id = suite_result.get("_original_document_id")
    original_open_ids = suite_result.get("_original_open_document_ids")
    if original_id is None or not isinstance(original_open_ids, list):
        suite_result["restored"] = False
        suite_result["restoration_error"] = _public_error(
            "RESTORATION_BASELINE_MISSING",
            correlation_material=f"{suite_result.get('run_id')}:restoration-baseline",
        )
        return False
    try:
        final_id = await lifecycle.read_active_document_id()
        final_open_ids = await lifecycle.list_open_document_ids()
        if not isinstance(final_open_ids, list):
            raise TypeError("final open document inventory is not a list")
        restored = final_id == original_id and sorted(final_open_ids) == sorted(
            original_open_ids
        )
    except BaseException:
        suite_result["restored"] = False
        suite_result["restoration_error"] = _public_error(
            "RESTORATION_READBACK_FAILED",
            correlation_material=f"{suite_result.get('run_id')}:restoration-readback",
        )
        return False
    suite_result["identity_restored"] = final_id == original_id
    suite_result["inventory_restored"] = sorted(final_open_ids) == sorted(
        original_open_ids
    )
    suite_result["restored"] = restored
    return bool(suite_result["restored"])


def _revision_unchanged(before: RevisionIdentity, after: RevisionIdentity) -> bool:
    return bool(
        before.observed_git_commit is not None
        and before.observed_source_manifest_sha256 is not None
        and before.observed_git_commit == after.observed_git_commit
        and before.observed_source_manifest_sha256
        == after.observed_source_manifest_sha256
        and before.tracked_state == after.tracked_state
        and before.tracked_changes_sha256 == after.tracked_changes_sha256
    )


def _project_suite_result(value: dict[str, Any]) -> dict[str, Any]:
    return {key: child for key, child in value.items() if not str(key).startswith("_")}


def _write_public_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_text(
        path,
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n",
    )


async def _main(
    case_ids: list[str],
    *,
    git_commit: str | None = None,
    source_manifest_sha256: str | None = None,
    nightly_run_identity: str | None = None,
    output_root: Path | None = None,
) -> dict[str, Any]:
    supplied_revision_values = (
        git_commit,
        source_manifest_sha256,
        nightly_run_identity,
    )
    if any(value is not None for value in supplied_revision_values) and not all(
        value is not None for value in supplied_revision_values
    ):
        raise ValueError(
            "git_commit, source_manifest_sha256 and nightly_run_identity "
            "must be provided together"
        )
    if git_commit is not None and not GIT_SHA_PATTERN.fullmatch(git_commit):
        raise ValueError("git_commit must be a full lowercase Git SHA")
    if nightly_run_identity is not None and not NIGHTLY_RUN_IDENTITY_PATTERN.fullmatch(
        nightly_run_identity
    ):
        raise ValueError("nightly_run_identity must be '<run_id>-<run_attempt>'")
    if source_manifest_sha256 is not None and not SOURCE_MANIFEST_PATTERN.fullmatch(
        source_manifest_sha256
    ):
        raise ValueError("source_manifest_sha256 must be a lowercase SHA-256")
    requested_case_ids = list(case_ids)
    run_id = (
        "ref_"
        + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        + "_"
        + uuid.uuid4().hex[:8]
    )
    artifact_run_root = Path(output_root or DEFAULT_OUTPUT_ROOT) / run_id
    mkdir_exclusive(artifact_run_root)
    revision_identity = collect_workspace_revision(
        ROOT,
        expected_git_commit=git_commit,
        expected_source_manifest_sha256=source_manifest_sha256,
    )
    suite_result: dict[str, Any] = {
        "schema_version": "fusion_parametric_reference_suite_result.v2",
        "run_id": run_id,
        "requested_case_ids": requested_case_ids,
        "status": "running",
        "cases": [],
        "revision_identity": revision_identity.model_dump(mode="json"),
        "scoreable": False,
        "scoreability": {
            "own_subject_complete": False,
            "eligible_comparators": [],
            "requires_same_fixture_comparator": True,
            "revision_exact": revision_identity.exact,
        },
    }
    if git_commit is not None and nightly_run_identity is not None:
        suite_result["tested_commit"] = git_commit
        suite_result["source_manifest_sha256"] = source_manifest_sha256
        suite_result["nightly_run_identity"] = nightly_run_identity
    result_path = artifact_run_root / "reference_suite_result.json"
    if git_commit is not None and not revision_identity.exact:
        suite_result["status"] = "aborted"
        suite_result["error"] = _public_error(
            "REVISION_IDENTITY_MISMATCH",
            correlation_material=f"{run_id}:revision",
        )
        suite_result["completed_at_utc"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
        )
        _write_public_json(result_path, _project_suite_result(suite_result))
        raise ValueError("workspace revision identity mismatch")

    runtime = FusionAgentRuntime(manifest_root="manifests", outputs_root="outputs")
    lifecycle = FusionRuntimeLifecycleBackend(runtime)
    try:
        try:
            suite_result[
                "_original_document_id"
            ] = await lifecycle.read_active_document_id()
            suite_result[
                "_original_open_document_ids"
            ] = await lifecycle.list_open_document_ids()
            for case_id in requested_case_ids:
                has_eco: bool | None = None
                try:
                    definition = json.loads(
                        read_text(CASES_ROOT / case_id / "definition.json")
                    )
                    has_eco = bool(definition.get("eco"))
                    case_result = await _run_case(
                        runtime,
                        lifecycle,
                        case_id,
                        run_id,
                        artifact_run_root,
                    )
                except BaseException:
                    current_result = _load_current_case_result(
                        case_id, run_id, artifact_run_root
                    )
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
                            "eco_oracle_passed": (False if effective_has_eco else None),
                            "cleanup_passed": False,
                            "elapsed_ms": None,
                            "error": _public_error(
                                "REFERENCE_CASE_FAILED",
                                correlation_material=f"{run_id}:{case_id}:summary",
                            ),
                        }
                    else:
                        summary = _summarize_case_result(
                            case_id,
                            current_result,
                            has_eco=effective_has_eco,
                        )
                        if not summary["error"]:
                            summary["error"] = _public_error(
                                "REFERENCE_CASE_FAILED",
                                correlation_material=f"{run_id}:{case_id}:summary",
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
                    raise RuntimeError(
                        f"{case_id} failed the aggregate reference gates"
                    )
        except BaseException:
            suite_result["status"] = "failed"
            suite_result["error"] = _public_error(
                "REFERENCE_SUITE_FAILED",
                correlation_material=f"{run_id}:suite",
            )
            raise
    finally:
        await _capture_suite_restoration(lifecycle, suite_result)
        final_revision = collect_workspace_revision(ROOT)
        suite_result["source_unchanged"] = _revision_unchanged(
            revision_identity, final_revision
        )
        suite_result["final_revision_identity"] = final_revision.model_dump(mode="json")
        completed = len(suite_result["cases"]) == len(requested_case_ids) and all(
            item.get("passed") is True for item in suite_result["cases"]
        )
        if suite_result["status"] == "running":
            if (
                completed
                and suite_result.get("restored") is True
                and suite_result["source_unchanged"] is True
            ):
                suite_result["status"] = "passed"
            else:
                suite_result["status"] = "failed"
                suite_result["error"] = _public_error(
                    "REFERENCE_AGGREGATE_GATES_FAILED",
                    correlation_material=f"{run_id}:aggregate",
                )
        try:
            await runtime.close(timeout_seconds=2.0)
        except BaseException:
            suite_result["status"] = "failed"
            suite_result["close_error"] = _public_error(
                "RUNTIME_CLOSE_FAILED",
                correlation_material=f"{run_id}:runtime-close",
            )
        suite_result["completed_at_utc"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
        )
        suite_result["result_file"] = "reference_suite_result.json"
        suite_result["scoreability"]["own_subject_complete"] = bool(
            suite_result["status"] == "passed"
            and requested_case_ids == DEFAULT_CASES
            and revision_identity.exact
            and suite_result.get("source_unchanged") is True
        )
        _write_public_json(result_path, _project_suite_result(suite_result))
    if suite_result["status"] != "passed":
        raise RuntimeError("suite reference run failed")
    public_result = _project_suite_result(suite_result)
    print(
        json.dumps(
            public_result,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
    )
    return public_result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("cases", nargs="*", default=DEFAULT_CASES)
    parser.add_argument("--git-commit")
    parser.add_argument("--source-manifest-sha256")
    parser.add_argument("--nightly-run-identity")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    args = parser.parse_args()
    unknown = [
        case_id for case_id in args.cases if not path_is_dir(CASES_ROOT / case_id)
    ]
    if unknown:
        raise SystemExit(f"unknown cases: {', '.join(unknown)}")
    try:
        asyncio.run(
            _main(
                args.cases,
                git_commit=args.git_commit,
                source_manifest_sha256=args.source_manifest_sha256,
                nightly_run_identity=args.nightly_run_identity,
                output_root=args.output_root,
            )
        )
    except BaseException:
        raise SystemExit(
            "reference suite failed; inspect the sanitized local run report"
        ) from None
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
