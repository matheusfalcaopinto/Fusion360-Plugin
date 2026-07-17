"""Require a completed successful branch CI run for an exact release SHA."""

from __future__ import annotations

import argparse
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any


API_VERSION = "2022-11-28"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_PAGES = 10
RUNS_PER_PAGE = 100
REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
WORKFLOW_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+\.ya?ml$")
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9._/-]+$")


class CiReleaseGateError(RuntimeError):
    """The exact candidate SHA has no qualifying branch CI proof."""


def require_successful_branch_ci(
    repository: str,
    workflow: str,
    commit: str,
    branch: str,
    token: str,
    *,
    api_root: str = "https://api.github.com",
    opener: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Return the newest qualifying CI run or fail closed."""

    _validate_inputs(repository, workflow, commit, branch, token)
    owner, name = repository.split("/", 1)
    endpoint_root = (
        f"{api_root.rstrip('/')}/repos/{urllib.parse.quote(owner, safe='')}"
        f"/{urllib.parse.quote(name, safe='')}/actions/workflows/"
        f"{urllib.parse.quote(workflow, safe='')}/runs"
    )
    open_request = opener or urllib.request.urlopen
    qualifying: list[dict[str, Any]] = []
    exhausted = False
    for page in range(1, MAX_PAGES + 1):
        runs = _read_workflow_runs_page(
            endpoint_root,
            branch=branch,
            commit=commit,
            token=token,
            page=page,
            opener=open_request,
        )
        qualifying.extend(
            run for run in runs if _is_qualifying_run(run, commit=commit, branch=branch)
        )
        if qualifying:
            break
        if len(runs) < RUNS_PER_PAGE:
            exhausted = True
            break
    if not qualifying and not exhausted:
        raise CiReleaseGateError(
            "GitHub Actions CI proof exceeds the bounded pagination limit"
        )
    if not qualifying:
        raise CiReleaseGateError(
            "candidate SHA has no completed successful push CI run on the required branch"
        )
    qualifying.sort(
        key=lambda run: (int(run.get("run_attempt", 0)), int(run.get("id", 0))),
        reverse=True,
    )
    selected = qualifying[0]
    return {
        "ok": True,
        "branch": branch,
        "commit": commit,
        "run_attempt": int(selected["run_attempt"]),
        "run_id": int(selected["id"]),
        "workflow": workflow,
    }


def _read_workflow_runs_page(
    endpoint_root: str,
    *,
    branch: str,
    commit: str,
    token: str,
    page: int,
    opener: Callable[..., Any],
) -> list[Any]:
    query = urllib.parse.urlencode(
        {
            "branch": branch,
            "event": "push",
            "head_sha": commit,
            "page": str(page),
            "per_page": str(RUNS_PER_PAGE),
            "status": "completed",
        }
    )
    request = urllib.request.Request(
        f"{endpoint_root}?{query}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "fusion-agent-codex-release-gate",
            "X-GitHub-Api-Version": API_VERSION,
        },
        method="GET",
    )
    try:
        with opener(request, timeout=30) as response:
            if int(getattr(response, "status", 200)) != 200:
                raise CiReleaseGateError("GitHub Actions API returned a non-200 status")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
        raise CiReleaseGateError(
            "GitHub Actions CI proof could not be queried"
        ) from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise CiReleaseGateError("GitHub Actions CI response exceeds the size limit")
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object)
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise CiReleaseGateError("GitHub Actions CI response is invalid JSON") from exc
    if not isinstance(payload, dict) or not isinstance(
        payload.get("workflow_runs"), list
    ):
        raise CiReleaseGateError("GitHub Actions CI response has an invalid shape")
    return payload["workflow_runs"]


def _validate_inputs(
    repository: str,
    workflow: str,
    commit: str,
    branch: str,
    token: str,
) -> None:
    if not REPOSITORY_PATTERN.fullmatch(repository):
        raise CiReleaseGateError("repository must be owner/name")
    if not WORKFLOW_PATTERN.fullmatch(workflow):
        raise CiReleaseGateError("workflow must be a workflow filename")
    if not SHA_PATTERN.fullmatch(commit):
        raise CiReleaseGateError("candidate commit must be a full lowercase Git SHA")
    if (
        not BRANCH_PATTERN.fullmatch(branch)
        or branch.startswith("/")
        or branch.endswith("/")
        or "//" in branch
        or ".." in branch.split("/")
    ):
        raise CiReleaseGateError("required branch name is invalid")
    if not token.strip():
        raise CiReleaseGateError("GitHub Actions read token is unavailable")


def _is_qualifying_run(run: object, *, commit: str, branch: str) -> bool:
    return bool(
        isinstance(run, dict)
        and type(run.get("id")) is int
        and run["id"] > 0
        and type(run.get("run_attempt")) is int
        and run["run_attempt"] > 0
        and run.get("event") == "push"
        and run.get("status") == "completed"
        and run.get("conclusion") == "success"
        and run.get("head_sha") == commit
        and run.get("head_branch") == branch
    )


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError(f"duplicate JSON key: {key}")
        payload[key] = value
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--workflow", default="ci.yml")
    parser.add_argument("--commit", required=True)
    parser.add_argument("--branch", required=True)
    args = parser.parse_args()
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    try:
        report = require_successful_branch_ci(
            args.repository,
            args.workflow,
            args.commit,
            args.branch,
            token,
        )
    except CiReleaseGateError as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error_code": "RELEASE_CI_GATE_FAILED",
                    "message": str(exc),
                },
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
