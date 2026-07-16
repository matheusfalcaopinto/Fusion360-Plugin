"""Fail a 0.4.x release unless the last scheduled real nightlies passed."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import tempfile
from pathlib import Path


PUBLIC_ARTIFACT_FILES = {
    "SHA256SUMS",
    "nightly-status.json",
    "summary.json",
}
SHA256_LINE = re.compile(r"^([0-9a-f]{64})  ([A-Za-z0-9._-]+)$")


def _gh(*args: str) -> str:
    completed = subprocess.run(
        ["gh", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def validate_nightlies(
    repository: str,
    workflow: str,
    count: int,
    expected_commit: str | None = None,
) -> list[int]:
    """Return qualifying run IDs or raise with an actionable gate reason."""

    runs = json.loads(
        _gh(
            "run",
            "list",
            "--repo",
            repository,
            "--workflow",
            workflow,
            "--event",
            "schedule",
            "--limit",
            str(count),
            "--json",
            "databaseId,status,conclusion,headSha",
        )
    )
    if len(runs) != count:
        raise RuntimeError(f"need {count} scheduled nightly runs, found {len(runs)}")
    qualified: list[int] = []
    for run in runs:
        run_id = int(run["databaseId"])
        if run.get("status") != "completed" or run.get("conclusion") != "success":
            raise RuntimeError(f"nightly {run_id} did not complete successfully")
        if expected_commit and run.get("headSha") != expected_commit:
            raise RuntimeError(
                f"nightly {run_id} tested {run.get('headSha') or 'unknown'}, expected {expected_commit}"
            )
        with tempfile.TemporaryDirectory(prefix=f"nightly-{run_id}-") as temporary:
            _gh(
                "run",
                "download",
                str(run_id),
                "--repo",
                repository,
                "--name",
                "fusion-real-nightly-status",
                "--dir",
                temporary,
            )
            _validate_public_artifact_bundle(
                Path(temporary),
                run_id=run_id,
                expected_commit=expected_commit,
            )
        qualified.append(run_id)
    return qualified


def _validate_public_artifact_bundle(
    artifact_root: Path,
    *,
    run_id: int,
    expected_commit: str | None,
) -> None:
    """Require the exact sanitized, checksummed nightly release evidence."""

    files = [path for path in artifact_root.rglob("*") if path.is_file()]
    relative_names = {path.relative_to(artifact_root).as_posix() for path in files}
    if relative_names != PUBLIC_ARTIFACT_FILES:
        raise RuntimeError(
            f"nightly {run_id} public artifact set is invalid: "
            f"expected {sorted(PUBLIC_ARTIFACT_FILES)}, found {sorted(relative_names)}"
        )

    checksum_path = artifact_root / "SHA256SUMS"
    checksum_lines = checksum_path.read_text(encoding="utf-8").splitlines()
    recorded: dict[str, str] = {}
    for line in checksum_lines:
        match = SHA256_LINE.fullmatch(line)
        if match is None or match.group(2) in recorded:
            raise RuntimeError(f"nightly {run_id} SHA256SUMS is invalid")
        recorded[match.group(2)] = match.group(1)
    expected_checksum_members = PUBLIC_ARTIFACT_FILES - {"SHA256SUMS"}
    if set(recorded) != expected_checksum_members:
        raise RuntimeError(f"nightly {run_id} SHA256SUMS coverage is invalid")
    for name, expected_digest in recorded.items():
        observed_digest = hashlib.sha256(
            (artifact_root / name).read_bytes()
        ).hexdigest()
        if observed_digest != expected_digest:
            raise RuntimeError(f"nightly {run_id} checksum mismatch for {name}")

    status = _read_json_object(artifact_root / "nightly-status.json", run_id)
    summary = _read_json_object(artifact_root / "summary.json", run_id)
    if status.get("schema_version") != "fusion_real_nightly.v1":
        raise RuntimeError(f"nightly {run_id} status schema is invalid")
    if summary.get("schema_version") != "fusion_real_nightly_public.v1":
        raise RuntimeError(f"nightly {run_id} summary schema is invalid")
    for payload_name, payload in (("status", status), ("summary", summary)):
        if payload.get("status") != "passed":
            raise RuntimeError(
                f"nightly {run_id} {payload_name} is "
                f"{payload.get('status', 'unknown')}, not passed"
            )
        if payload.get("fixture_policy") != "disposable_unsaved_only":
            raise RuntimeError(
                f"nightly {run_id} {payload_name} lacks disposable fixture proof"
            )
        if payload.get("save_user_documents") is not False:
            raise RuntimeError(
                f"nightly {run_id} {payload_name} does not prove save_user_documents=false"
            )
        if expected_commit and payload.get("tested_commit") != expected_commit:
            raise RuntimeError(
                f"nightly {run_id} artifact tested "
                f"{payload.get('tested_commit') or 'unknown'}, expected {expected_commit}"
            )

    checks = summary.get("checks")
    if not isinstance(checks, dict) or checks != {
        "capability_packs": "passed",
        "reference_suite": "passed",
    }:
        raise RuntimeError(f"nightly {run_id} public checks are not all passed")


def _read_json_object(path: Path, run_id: int) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"nightly {run_id} has invalid {path.name}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"nightly {run_id} {path.name} must contain an object")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--workflow", default="fusion-real-nightly.yml")
    parser.add_argument("--count", type=int, default=3)
    parser.add_argument("--commit")
    args = parser.parse_args()
    if args.count < 1:
        raise SystemExit("--count must be positive")
    run_ids = validate_nightlies(
        args.repository, args.workflow, args.count, args.commit
    )
    print(json.dumps({"ok": True, "qualified_run_ids": run_ids}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
