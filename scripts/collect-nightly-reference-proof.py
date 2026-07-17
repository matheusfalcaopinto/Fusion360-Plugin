"""Validate and atomically collect a reference-suite proof from the current nightly."""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any


REFERENCE_SCHEMA = "fusion_parametric_reference_suite_result.v2"
DEFAULT_CASES = (
    "b02_vented_enclosure",
    "b03_split_pillow_block",
    "b04_offset_duct_adapter",
    "b05_spherical_lattice_radome",
    "b06_robot_arm_assembly",
    "b07_packaging_machine",
)
GIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SOURCE_MANIFEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
NIGHTLY_RUN_IDENTITY_PATTERN = re.compile(r"^[1-9][0-9]*-[1-9][0-9]*$")
REFERENCE_RUN_ID_PATTERN = re.compile(r"^ref_[0-9]{8}T[0-9]{6}Z_[0-9a-f]{8}$")
COMPLETED_AT_PATTERN = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
)


class ReferenceProofError(ValueError):
    """Raised when a reference result is absent, stale, or malformed."""


def _read_proof(source: Path) -> tuple[bytes, dict[str, Any]]:
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise ReferenceProofError("current reference proof is absent") from exc
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReferenceProofError("current reference proof is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ReferenceProofError("current reference proof must contain an object")
    return raw, payload


def _validate_proof(
    payload: dict[str, Any],
    *,
    expected_commit: str,
    expected_source_manifest_sha256: str,
    expected_run_identity: str,
) -> None:
    if not GIT_SHA_PATTERN.fullmatch(expected_commit):
        raise ReferenceProofError("expected commit must be a full lowercase Git SHA")
    if not NIGHTLY_RUN_IDENTITY_PATTERN.fullmatch(expected_run_identity):
        raise ReferenceProofError("expected nightly run identity is invalid")
    if not SOURCE_MANIFEST_PATTERN.fullmatch(expected_source_manifest_sha256):
        raise ReferenceProofError("expected source manifest digest is invalid")
    if payload.get("schema_version") != REFERENCE_SCHEMA:
        raise ReferenceProofError("current reference proof schema is invalid")
    if payload.get("tested_commit") != expected_commit:
        raise ReferenceProofError("current reference proof commit does not match")
    if payload.get("nightly_run_identity") != expected_run_identity:
        raise ReferenceProofError("current reference proof run identity does not match")
    if payload.get("source_manifest_sha256") != expected_source_manifest_sha256:
        raise ReferenceProofError(
            "current reference proof source manifest does not match"
        )
    revision = payload.get("revision_identity")
    expected_revision = {
        "scheme": "source-manifest-v1",
        "expected_git_commit": expected_commit,
        "observed_git_commit": expected_commit,
        "expected_source_manifest_sha256": expected_source_manifest_sha256,
        "observed_source_manifest_sha256": expected_source_manifest_sha256,
        "tracked_state": "clean",
    }
    if not isinstance(revision, dict) or any(
        revision.get(key) != value for key, value in expected_revision.items()
    ):
        raise ReferenceProofError(
            "current reference proof revision identity is invalid"
        )
    if payload.get("status") not in {"passed", "failed"}:
        raise ReferenceProofError("current reference proof status is invalid")
    if not REFERENCE_RUN_ID_PATTERN.fullmatch(str(payload.get("run_id") or "")):
        raise ReferenceProofError("current reference proof suite run ID is invalid")
    if not COMPLETED_AT_PATTERN.fullmatch(str(payload.get("completed_at_utc") or "")):
        raise ReferenceProofError("current reference proof completion time is invalid")
    if payload.get("result_file") != "reference_suite_result.json":
        raise ReferenceProofError("current reference proof result filename is invalid")
    if payload.get("requested_case_ids") != list(DEFAULT_CASES):
        raise ReferenceProofError("current reference proof case set is invalid")
    cases = payload.get("cases")
    if not isinstance(cases, list) or len(cases) != len(DEFAULT_CASES):
        raise ReferenceProofError("current reference proof case results are invalid")
    if any(not isinstance(case, dict) for case in cases):
        raise ReferenceProofError("current reference proof case results are invalid")
    case_ids = [case.get("case_id") for case in cases]
    if (
        any(not isinstance(case_id, str) for case_id in case_ids)
        or case_ids != list(DEFAULT_CASES)
        or len(set(case_ids)) != len(case_ids)
    ):
        raise ReferenceProofError("current reference proof case results are invalid")
    if any(type(case.get("passed")) is not bool for case in cases):
        raise ReferenceProofError("current reference proof case status is invalid")
    if payload["status"] == "passed":
        if not all(case["passed"] is True for case in cases):
            raise ReferenceProofError("passed reference proof contains a failed case")
        if payload.get("restored") is not True:
            raise ReferenceProofError("passed reference proof lacks restoration proof")


def collect_reference_proof(
    source: Path | str,
    destination: Path | str,
    *,
    expected_commit: str,
    expected_source_manifest_sha256: str,
    expected_run_identity: str,
) -> None:
    source_path = Path(source)
    destination_path = Path(destination)
    destination_path.unlink(missing_ok=True)
    raw, payload = _read_proof(source_path)
    _validate_proof(
        payload,
        expected_commit=expected_commit,
        expected_source_manifest_sha256=expected_source_manifest_sha256,
        expected_run_identity=expected_run_identity,
    )
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination_path.parent,
        prefix=f".{destination_path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, destination_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--expected-source-manifest-sha256", required=True)
    parser.add_argument("--expected-run-identity", required=True)
    args = parser.parse_args()
    collect_reference_proof(
        args.source,
        args.destination,
        expected_commit=args.expected_commit,
        expected_source_manifest_sha256=args.expected_source_manifest_sha256,
        expected_run_identity=args.expected_run_identity,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
