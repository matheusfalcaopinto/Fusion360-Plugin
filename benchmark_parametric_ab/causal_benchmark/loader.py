"""Fail-closed loader and artifact-integrity checks for causal suites."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from jsonschema import Draft202012Validator
from pydantic import ValidationError

from .models import ArtifactRef, CausalSuite


SCHEMA_PATH = Path(__file__).parents[1] / "causal_suite.schema.json"
PLAN_SCHEMA_PATH = Path(__file__).parents[1] / "planner_artifact.schema.json"


class CausalSuiteError(ValueError):
    """Raised before dispatch when a suite or frozen artifact is invalid."""


def load_causal_suite(path: Path | str) -> CausalSuite:
    suite_path = Path(path).expanduser()
    if not suite_path.is_file():
        raise CausalSuiteError(f"causal suite does not exist: {suite_path}")
    if suite_path.suffix.lower() != ".json":
        raise CausalSuiteError("causal suite must be a JSON file")
    try:
        payload = json.loads(suite_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CausalSuiteError(f"cannot read causal suite: {exc}") from exc
    if not isinstance(payload, dict):
        raise CausalSuiteError("causal suite root must be an object")
    try:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CausalSuiteError(f"bundled causal schema is unavailable: {exc}") from exc
    errors = sorted(Draft202012Validator(schema).iter_errors(payload), key=lambda item: list(item.path))
    if errors:
        first = errors[0]
        location = ".".join(str(part) for part in first.absolute_path) or "$"
        raise CausalSuiteError(f"causal suite schema violation at {location}: {first.message}")
    try:
        suite = CausalSuite.model_validate(payload)
    except ValidationError as exc:
        raise CausalSuiteError(f"causal suite semantic validation failed: {exc}") from exc
    _verify_artifacts(suite, suite_path.parent.resolve())
    return suite


def suite_fingerprint(suite: CausalSuite) -> str:
    canonical = json.dumps(
        suite.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def resolve_artifact(suite_path: Path | str, artifact: ArtifactRef) -> Path:
    root = Path(suite_path).expanduser().resolve().parent
    relative = Path(artifact.path)
    if relative.is_absolute() or relative.drive:
        raise CausalSuiteError(f"artifact path must be relative: {artifact.path}")
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise CausalSuiteError(f"artifact escapes suite directory: {artifact.path}") from exc
    return candidate


def _verify_artifacts(suite: CausalSuite, root: Path) -> None:
    try:
        plan_schema = json.loads(PLAN_SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CausalSuiteError(f"bundled planner artifact schema is unavailable: {exc}") from exc
    arm_by_id = {arm.id: arm for arm in suite.arms}
    for case in suite.cases:
        refs: Iterable[ArtifactRef] = [
            case.transport_replay.script,
            *(item.plan for item in case.planner_isolated.artifacts),
            *(item.script for item in case.planner_isolated.artifacts),
        ]
        for artifact in refs:
            relative = Path(artifact.path)
            if relative.is_absolute() or relative.drive:
                raise CausalSuiteError(
                    f"case {case.id}: artifact path must be relative: {artifact.path}"
                )
            candidate = (root / relative).resolve()
            try:
                candidate.relative_to(root)
            except ValueError as exc:
                raise CausalSuiteError(
                    f"case {case.id}: artifact escapes suite directory: {artifact.path}"
                ) from exc
            if not candidate.is_file():
                raise CausalSuiteError(f"case {case.id}: artifact is missing: {artifact.path}")
            digest = _sha256_file(candidate)
            if digest != artifact.sha256:
                raise CausalSuiteError(
                    f"case {case.id}: artifact hash mismatch for {artifact.path}: "
                    f"expected {artifact.sha256}, got {digest}"
                )
        planner = case.planner_isolated.artifacts
        signatures = {(item.plan.sha256, item.script.sha256) for item in planner}
        if len(signatures) != 2:
            raise CausalSuiteError(
                f"case {case.id}: planner_isolated requires distinct frozen plan/script artifacts per arm"
            )
        for item in planner:
            _verify_plan_artifact(
                path=(root / item.plan.path).resolve(),
                schema=plan_schema,
                expected_arm=arm_by_id[item.arm_id],
                expected_case_id=case.id,
                expected_script_sha256=item.script.sha256,
            )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_plan_artifact(
    *,
    path: Path,
    schema: dict[str, Any],
    expected_arm: Any,
    expected_case_id: str,
    expected_script_sha256: str,
) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CausalSuiteError(f"planner artifact must be valid JSON: {path.name}: {exc}") from exc
    errors = sorted(Draft202012Validator(schema).iter_errors(payload), key=lambda item: list(item.path))
    if errors:
        first = errors[0]
        location = ".".join(str(part) for part in first.absolute_path) or "$"
        raise CausalSuiteError(
            f"planner artifact schema violation in {path.name} at {location}: {first.message}"
        )
    if payload["arm_id"] != expected_arm.id or payload["case_id"] != expected_case_id:
        raise CausalSuiteError(
            f"planner artifact identity mismatch in {path.name}: "
            f"expected arm={expected_arm.id}, case={expected_case_id}"
        )
    planner = payload["planner"]
    frozen_identity = (
        expected_arm.provider,
        expected_arm.model,
        expected_arm.reasoning_profile,
    )
    artifact_identity = (
        planner["provider"],
        planner["model"],
        planner["reasoning_profile"],
    )
    if artifact_identity != frozen_identity:
        raise CausalSuiteError(
            f"planner identity mismatch in {path.name}: expected {frozen_identity}, got {artifact_identity}"
        )
    if payload["script_sha256"] != expected_script_sha256:
        raise CausalSuiteError(
            f"planner artifact {path.name} is not bound to its frozen script SHA-256"
        )
    _verify_build_graph(payload, path.name)


def _verify_build_graph(payload: dict[str, Any], filename: str) -> None:
    nodes = payload["build_graph"]
    ids = [node["id"] for node in nodes]
    if len(set(ids)) != len(ids):
        raise CausalSuiteError(f"planner artifact {filename} contains duplicate build_graph ids")
    known = set(ids)
    dependencies = {node["id"]: set(node["depends_on"]) for node in nodes}
    for node_id, required in dependencies.items():
        unknown = sorted(required - known)
        if unknown:
            raise CausalSuiteError(
                f"planner artifact {filename}: node {node_id} has unknown dependencies {unknown}"
            )
        if node_id in required:
            raise CausalSuiteError(
                f"planner artifact {filename}: node {node_id} depends on itself"
            )
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in visited:
            return
        if node_id in visiting:
            raise CausalSuiteError(f"planner artifact {filename}: build_graph contains a cycle")
        visiting.add(node_id)
        for dependency in dependencies[node_id]:
            visit(dependency)
        visiting.remove(node_id)
        visited.add(node_id)

    for node_id in ids:
        visit(node_id)
    assertion_ids = [item["id"] for item in payload["verification_assertions"]]
    if len(set(assertion_ids)) != len(assertion_ids):
        raise CausalSuiteError(
            f"planner artifact {filename} contains duplicate verification assertion ids"
        )
