"""Fail a 0.4.x release unless the last scheduled real nightlies passed."""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from pathlib import Path


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
            status_paths = list(Path(temporary).rglob("nightly-status.json"))
            if len(status_paths) != 1:
                raise RuntimeError(f"nightly {run_id} has no real status artifact")
            status_path = status_paths[0]
            status = json.loads(status_path.read_text(encoding="utf-8-sig"))
            if status.get("status") != "passed":
                raise RuntimeError(f"nightly {run_id} is {status.get('status', 'unknown')}, not passed")
        qualified.append(run_id)
    return qualified


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--workflow", default="fusion-real-nightly.yml")
    parser.add_argument("--count", type=int, default=3)
    parser.add_argument("--commit")
    args = parser.parse_args()
    if args.count < 1:
        raise SystemExit("--count must be positive")
    run_ids = validate_nightlies(args.repository, args.workflow, args.count, args.commit)
    print(json.dumps({"ok": True, "qualified_run_ids": run_ids}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
