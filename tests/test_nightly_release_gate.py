from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "check-nightly-release-gate.py"
SPEC = importlib.util.spec_from_file_location("check_nightly_release_gate", SCRIPT)
assert SPEC and SPEC.loader
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def _write_public_artifact(
    root: Path,
    *,
    status: str = "passed",
    commit: str = "abc123",
    checks: dict[str, str] | None = None,
) -> None:
    status_payload = {
        "schema_version": "fusion_real_nightly.v1",
        "status": status,
        "tested_commit": commit,
        "fixture_policy": "disposable_unsaved_only",
        "save_user_documents": False,
    }
    summary_payload = {
        "schema_version": "fusion_real_nightly_public.v1",
        "status": status,
        "tested_commit": commit,
        "fixture_policy": "disposable_unsaved_only",
        "save_user_documents": False,
        "checks": checks
        if checks is not None
        else {"capability_packs": "passed", "reference_suite": "passed"},
    }
    for name, payload in (
        ("nightly-status.json", status_payload),
        ("summary.json", summary_payload),
    ):
        (root / name).write_text(
            json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8"
        )
    checksum_lines = [
        f"{hashlib.sha256((root / name).read_bytes()).hexdigest()}  {name}"
        for name in ("nightly-status.json", "summary.json")
    ]
    (root / "SHA256SUMS").write_text("\n".join(checksum_lines) + "\n", encoding="utf-8")


def test_three_real_passed_nightlies_qualify(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_gh(*args: str) -> str:
        if args[:2] == ("run", "list"):
            return json.dumps(
                [
                    {
                        "databaseId": run_id,
                        "status": "completed",
                        "conclusion": "success",
                        "headSha": "abc123",
                    }
                    for run_id in (30, 29, 28)
                ]
            )
        output = Path(args[args.index("--dir") + 1])
        _write_public_artifact(output)
        return ""

    monkeypatch.setattr(gate, "_gh", fake_gh)
    assert gate.validate_nightlies(
        "owner/repo", "fusion-real-nightly.yml", 3, "abc123"
    ) == [30, 29, 28]


def test_not_run_nightly_cannot_qualify_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_gh(*args: str) -> str:
        if args[:2] == ("run", "list"):
            return json.dumps(
                [
                    {
                        "databaseId": 30,
                        "status": "completed",
                        "conclusion": "success",
                        "headSha": "abc123",
                    }
                ]
            )
        output = Path(args[args.index("--dir") + 1])
        _write_public_artifact(output, status="not_run")
        return ""

    monkeypatch.setattr(gate, "_gh", fake_gh)
    with pytest.raises(RuntimeError, match="not_run"):
        gate.validate_nightlies("owner/repo", "fusion-real-nightly.yml", 1)


def test_nightly_for_another_commit_cannot_qualify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_gh(*args: str) -> str:
        return json.dumps(
            [
                {
                    "databaseId": 30,
                    "status": "completed",
                    "conclusion": "success",
                    "headSha": "old",
                }
            ]
        )

    monkeypatch.setattr(gate, "_gh", fake_gh)
    with pytest.raises(RuntimeError, match="expected current"):
        gate.validate_nightlies("owner/repo", "fusion-real-nightly.yml", 1, "current")


def test_artifact_for_another_commit_cannot_qualify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_gh(*args: str) -> str:
        if args[:2] == ("run", "list"):
            return json.dumps(
                [
                    {
                        "databaseId": 30,
                        "status": "completed",
                        "conclusion": "success",
                        "headSha": "current",
                    }
                ]
            )
        output = Path(args[args.index("--dir") + 1])
        _write_public_artifact(output, commit="other")
        return ""

    monkeypatch.setattr(gate, "_gh", fake_gh)
    with pytest.raises(RuntimeError, match="artifact tested other"):
        gate.validate_nightlies("owner/repo", "fusion-real-nightly.yml", 1, "current")


def test_checksum_mismatch_cannot_qualify(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_gh(*args: str) -> str:
        if args[:2] == ("run", "list"):
            return json.dumps(
                [
                    {
                        "databaseId": 30,
                        "status": "completed",
                        "conclusion": "success",
                        "headSha": "abc123",
                    }
                ]
            )
        output = Path(args[args.index("--dir") + 1])
        _write_public_artifact(output)
        (output / "summary.json").write_text("{}\n", encoding="utf-8")
        return ""

    monkeypatch.setattr(gate, "_gh", fake_gh)
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        gate.validate_nightlies("owner/repo", "fusion-real-nightly.yml", 1, "abc123")


def test_incomplete_public_checks_cannot_qualify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_gh(*args: str) -> str:
        if args[:2] == ("run", "list"):
            return json.dumps(
                [
                    {
                        "databaseId": 30,
                        "status": "completed",
                        "conclusion": "success",
                        "headSha": "abc123",
                    }
                ]
            )
        output = Path(args[args.index("--dir") + 1])
        _write_public_artifact(
            output,
            checks={"capability_packs": "passed", "reference_suite": "failed"},
        )
        return ""

    monkeypatch.setattr(gate, "_gh", fake_gh)
    with pytest.raises(RuntimeError, match="checks are not all passed"):
        gate.validate_nightlies("owner/repo", "fusion-real-nightly.yml", 1, "abc123")
