"""Project private nightly state into a fixed, sanitized public artifact set."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any


STATUS_KEYS = {
    "schema_version",
    "status",
    "reason",
    "git_commit",
    "tested_commit",
    "fixture_policy",
    "save_user_documents",
}
VALID_STATUSES = {"passed", "failed", "not_run"}
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
REASON_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_,.-]{0,199}$")


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def prepare_public_artifacts(private_root: Path | str, public_root: Path | str) -> None:
    private = Path(private_root).resolve()
    public = Path(public_root).resolve()
    if private == public or private in public.parents:
        raise ValueError(
            "public artifact directory must not contain the private directory"
        )
    if public.exists() and any(public.iterdir()):
        raise ValueError("public artifact directory must be empty")
    public.mkdir(parents=True, exist_ok=True)

    raw_status = _read_json(private / "nightly-status.json")
    status = {key: raw_status[key] for key in sorted(STATUS_KEYS) if key in raw_status}
    state = str(status.get("status") or "")
    if state not in VALID_STATUSES:
        raise ValueError(f"invalid nightly status: {state!r}")
    commit = str(status.get("tested_commit") or status.get("git_commit") or "")
    if not SHA_PATTERN.fullmatch(commit):
        raise ValueError("nightly tested commit must be a full lowercase Git SHA")
    if status.get("fixture_policy") != "disposable_unsaved_only":
        raise ValueError("nightly fixture policy must be disposable_unsaved_only")
    if status.get("save_user_documents") is not False:
        raise ValueError("nightly must prove save_user_documents=false")
    reason = str(status.get("reason") or "")
    if reason and not REASON_PATTERN.fullmatch(reason):
        status["reason"] = "details_redacted"
    status["tested_commit"] = commit
    status.pop("git_commit", None)
    _write_json(public / "nightly-status.json", status)

    capability = _read_json(private / "capability-packs.json")
    reference = _read_json(private / "reference_suite_result.json")
    summary = {
        "schema_version": "fusion_real_nightly_public.v1",
        "status": state,
        "tested_commit": commit or None,
        "fixture_policy": status.get("fixture_policy"),
        "save_user_documents": status.get("save_user_documents", False),
        "checks": {
            "capability_packs": _public_check_status(capability.get("status")),
            "reference_suite": _public_check_status(
                reference.get("status") or reference.get("result")
            ),
        },
    }
    _write_json(public / "summary.json", summary)

    checksum_lines = []
    for path in sorted(public.glob("*.json")):
        checksum_lines.append(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}"
        )
    (public / "SHA256SUMS").write_text(
        "\n".join(checksum_lines) + "\n", encoding="utf-8"
    )


def _public_check_status(value: object) -> str:
    status = str(value or "absent").strip().lower()
    return status if status in {"passed", "failed", "not_run", "absent"} else "invalid"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--private-root", type=Path, required=True)
    parser.add_argument("--public-root", type=Path, required=True)
    args = parser.parse_args()
    prepare_public_artifacts(args.private_root, args.public_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
