"""Exact, typed workspace revision provenance for public benchmarks."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from benchmark.filesystem import path_is_file, read_bytes


class RevisionIdentity(BaseModel):
    """Expected and observed identity of one tracked source workspace."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    scheme: Literal["source-manifest-v1"] = "source-manifest-v1"
    expected_git_commit: str | None = Field(default=None, pattern=r"^[a-f0-9]{40}$")
    observed_git_commit: str | None = Field(default=None, pattern=r"^[a-f0-9]{40}$")
    expected_source_manifest_sha256: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )
    observed_source_manifest_sha256: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )
    tracked_state: Literal["clean", "dirty", "unavailable"]
    tracked_changes_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @property
    def expected_complete(self) -> bool:
        return bool(self.expected_git_commit and self.expected_source_manifest_sha256)

    @property
    def explicit_mismatch(self) -> bool:
        return bool(
            (
                self.expected_git_commit is not None
                and self.expected_git_commit != self.observed_git_commit
            )
            or (
                self.expected_source_manifest_sha256 is not None
                and self.expected_source_manifest_sha256
                != self.observed_source_manifest_sha256
            )
        )

    @property
    def exact(self) -> bool:
        return bool(
            self.expected_complete
            and not self.explicit_mismatch
            and self.tracked_state == "clean"
        )


def collect_workspace_revision(
    root_hint: Path | str,
    *,
    expected_git_commit: str | None = None,
    expected_source_manifest_sha256: str | None = None,
) -> RevisionIdentity:
    """Measure Git HEAD, tracked bytes, and tracked working-tree state."""

    hint = Path(root_hint).resolve()
    try:
        root = Path(_git(hint, "rev-parse", "--show-toplevel").strip()).resolve()
        observed_commit = _git(root, "rev-parse", "HEAD").strip().lower()
        tracked_bytes = _git_bytes(root, "ls-files", "-z")
        tracked_files = [
            item.decode("utf-8", errors="strict")
            for item in tracked_bytes.split(b"\0")
            if item
        ]
        observed_manifest = source_manifest_digest(root, tracked_files)
        status = _git_bytes(root, "status", "--porcelain=v1", "--untracked-files=no")
        tracked_state: Literal["clean", "dirty", "unavailable"] = (
            "dirty" if status.strip() else "clean"
        )
        changes_digest = hashlib.sha256(status).hexdigest()
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError):
        return RevisionIdentity(
            expected_git_commit=_normalized_commit(expected_git_commit),
            expected_source_manifest_sha256=_normalized_digest(
                expected_source_manifest_sha256
            ),
            tracked_state="unavailable",
        )

    return RevisionIdentity(
        expected_git_commit=_normalized_commit(expected_git_commit),
        observed_git_commit=_normalized_commit(observed_commit),
        expected_source_manifest_sha256=_normalized_digest(
            expected_source_manifest_sha256
        ),
        observed_source_manifest_sha256=observed_manifest,
        tracked_state=tracked_state,
        tracked_changes_sha256=changes_digest,
    )


def source_manifest_digest(root: Path | str, tracked_files: list[str]) -> str:
    """Hash the exact path/content mapping for an explicit tracked file set."""

    base = Path(root).resolve()
    digest = hashlib.sha256()
    for relative in sorted(set(tracked_files)):
        normalized = relative.replace("\\", "/")
        candidate = (base / Path(normalized)).resolve()
        if candidate != base and base not in candidate.parents:
            raise ValueError(f"tracked path escapes workspace: {relative}")
        path_bytes = normalized.encode("utf-8")
        digest.update(len(path_bytes).to_bytes(8, "big"))
        digest.update(path_bytes)
        if not path_is_file(candidate):
            digest.update(b"\0missing\0")
            continue
        content = read_bytes(candidate)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(hashlib.sha256(content).digest())
    return digest.hexdigest()


def _git(root: Path, *arguments: str) -> str:
    return _git_bytes(root, *arguments).decode("utf-8", errors="strict")


def _git_bytes(root: Path, *arguments: str) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=10,
    )
    if completed.returncode != 0:
        raise subprocess.SubprocessError("git provenance query failed")
    return completed.stdout


def _normalized_commit(value: str | None) -> str | None:
    normalized = (value or "").strip().lower()
    return (
        normalized
        if len(normalized) == 40
        and all(character in "0123456789abcdef" for character in normalized)
        else None
    )


def _normalized_digest(value: str | None) -> str | None:
    normalized = (value or "").strip().lower()
    return (
        normalized
        if len(normalized) == 64
        and all(character in "0123456789abcdef" for character in normalized)
        else None
    )
