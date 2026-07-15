from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "check-nightly-release-gate.py"
SPEC = importlib.util.spec_from_file_location("check_nightly_release_gate", SCRIPT)
assert SPEC and SPEC.loader
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def test_three_real_passed_nightlies_qualify(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_gh(*args: str) -> str:
        if args[:2] == ("run", "list"):
            return json.dumps(
                [
                    {"databaseId": run_id, "status": "completed", "conclusion": "success", "headSha": "abc123"}
                    for run_id in (30, 29, 28)
                ]
            )
        output = Path(args[args.index("--dir") + 1]) / "nightly-status.json"
        output.write_text(json.dumps({"status": "passed"}), encoding="utf-8")
        return ""

    monkeypatch.setattr(gate, "_gh", fake_gh)
    assert gate.validate_nightlies("owner/repo", "fusion-real-nightly.yml", 3, "abc123") == [30, 29, 28]


def test_not_run_nightly_cannot_qualify_release(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_gh(*args: str) -> str:
        if args[:2] == ("run", "list"):
            return json.dumps(
                [{"databaseId": 30, "status": "completed", "conclusion": "success", "headSha": "abc123"}]
            )
        output = Path(args[args.index("--dir") + 1]) / "nightly-status.json"
        output.write_text(json.dumps({"status": "not_run"}), encoding="utf-8")
        return ""

    monkeypatch.setattr(gate, "_gh", fake_gh)
    with pytest.raises(RuntimeError, match="not_run"):
        gate.validate_nightlies("owner/repo", "fusion-real-nightly.yml", 1)


def test_nightly_for_another_commit_cannot_qualify(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_gh(*args: str) -> str:
        return json.dumps(
            [{"databaseId": 30, "status": "completed", "conclusion": "success", "headSha": "old"}]
        )

    monkeypatch.setattr(gate, "_gh", fake_gh)
    with pytest.raises(RuntimeError, match="expected current"):
        gate.validate_nightlies("owner/repo", "fusion-real-nightly.yml", 1, "current")
