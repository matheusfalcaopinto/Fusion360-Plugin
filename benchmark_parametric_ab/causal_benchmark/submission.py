"""Validate and freeze a structured external planner submission without executing it."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from benchmark.filesystem import (
    atomic_write_bytes,
    atomic_write_text,
    path_exists,
    read_text,
    unlink,
)

from .loader import CausalSuiteError, PLAN_SCHEMA_PATH, _verify_build_graph


SUBMISSION_SCHEMA_PATH = Path(__file__).parents[1] / "planner_submission.schema.json"


def freeze_planner_submission(
    submission_path: Path | str,
    output_dir: Path | str,
) -> dict[str, str]:
    """Produce a schema-valid plan JSON and opaque frozen Python script."""

    source = Path(submission_path)
    try:
        payload = json.loads(read_text(source))
        submission_schema = json.loads(read_text(SUBMISSION_SCHEMA_PATH))
        plan_schema = json.loads(read_text(PLAN_SCHEMA_PATH))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CausalSuiteError("cannot read planner submission/schema") from exc
    _validate(payload, submission_schema, label=source.name)
    script = payload["script"]["content"]
    script_bytes = script.encode("utf-8")
    if len(script_bytes) > 65_536:
        raise CausalSuiteError(
            "planner submission script exceeds 64 KiB after UTF-8 encoding"
        )
    if "\x00" in script:
        raise CausalSuiteError("planner submission script contains a NUL byte")

    destination = Path(output_dir)
    stem = f"{payload['case_id']}__{payload['arm_id']}"
    script_path = destination / f"{stem}_script.py"
    plan_path = destination / f"{stem}_plan.json"
    if path_exists(script_path) or path_exists(plan_path):
        raise CausalSuiteError(f"frozen planner artifact already exists for {stem}")
    script_sha256 = hashlib.sha256(script_bytes).hexdigest()
    plan = {
        "schema_version": "fusion_planner_artifact.v1",
        "arm_id": payload["arm_id"],
        "case_id": payload["case_id"],
        "planner": payload["planner"],
        "intent": payload["intent"],
        "assumptions": payload["assumptions"],
        "parameters": payload["parameters"],
        "build_graph": payload["build_graph"],
        "verification_assertions": payload["verification_assertions"],
        "script_sha256": script_sha256,
    }
    _validate(plan, plan_schema, label=plan_path.name)
    _verify_build_graph(plan, plan_path.name)
    plan_text = json.dumps(plan, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    atomic_write_bytes(script_path, script_bytes)
    try:
        atomic_write_text(plan_path, plan_text)
    except Exception:
        unlink(script_path, missing_ok=True)
        raise
    return {
        "arm_id": payload["arm_id"],
        "case_id": payload["case_id"],
        "plan_path": str(plan_path.resolve()),
        "plan_sha256": hashlib.sha256(plan_text.encode("utf-8")).hexdigest(),
        "script_path": str(script_path.resolve()),
        "script_sha256": script_sha256,
    }


def _validate(payload: Any, schema: dict[str, Any], *, label: str) -> None:
    errors = sorted(
        Draft202012Validator(schema).iter_errors(payload),
        key=lambda item: list(item.path),
    )
    if errors:
        first = errors[0]
        location = ".".join(str(part) for part in first.absolute_path) or "$"
        raise CausalSuiteError(
            f"schema violation in {label} at {location}: {first.message}"
        )
