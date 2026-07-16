"""Stdlib-only integrity checks for the bundled Fusion Agent wheel.

This module deliberately does not import the harness.  It is used before a
virtual environment exists and before any wheel member is trusted or imported.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import re
import subprocess
import tomllib
import zipfile
from dataclasses import dataclass
from email.parser import BytesParser
from importlib import metadata
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


SOURCE_MANIFEST_SCHEMA = "fusion_agent.source_manifest.v1"
SOURCE_MANIFEST_NAME = "SOURCE-MANIFEST.json"
SOURCE_FILE_INDEX = "harness/source-files.txt"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
CANONICAL_DIST_INFO_MEMBERS = frozenset(
    {
        "METADATA",
        "RECORD",
        SOURCE_MANIFEST_NAME,
        "WHEEL",
        "entry_points.txt",
        "licenses/LICENSE",
        "top_level.txt",
    }
)


class BundleIntegrityError(RuntimeError):
    """The plugin bundle cannot be trusted or does not match its source."""


@dataclass(frozen=True, slots=True)
class BundleIntegrityReport:
    wheel: Path
    project_name: str
    version: str
    member_count: int
    source_file_count: int
    sha256: str


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def record_digest(data: bytes) -> str:
    encoded = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
    return "sha256=" + encoded.decode("ascii")


def collect_source_files(plugin_root: Path) -> dict[str, bytes]:
    """Return only explicitly indexed first-party files flattened into the wheel."""

    plugin_root = plugin_root.resolve()
    entries = _source_index_entries(plugin_root)
    files: dict[str, bytes] = {}
    for entry in entries:
        prefix = next(
            (
                value
                for value in ("harness/packages/", "harness/apps/")
                if entry.startswith(value)
            ),
            None,
        )
        if prefix is None:
            raise BundleIntegrityError(
                f"source index entry is outside canonical roots: {entry}"
            )
        relative = entry.removeprefix(prefix)
        source_root = (plugin_root / prefix.rstrip("/")).resolve()
        path = (plugin_root / Path(entry)).resolve()
        if source_root != path and source_root not in path.parents:
            raise BundleIntegrityError(
                f"source index entry escapes canonical root: {entry}"
            )
        if not path.is_file():
            raise BundleIntegrityError(f"indexed canonical source is missing: {entry}")
        if relative in files:
            raise BundleIntegrityError(f"duplicate canonical source path: {relative}")
        files[relative] = path.read_bytes()
    return files


def validate_source_file_index(plugin_root: Path) -> None:
    """Prove the checked-in index is exactly the Git-tracked source set."""

    root = plugin_root.resolve()
    indexed = set(_source_index_entries(root))
    tracked = _git_tracked_sources(root)
    if tracked is None:
        tracked = {
            path.relative_to(root).as_posix()
            for source_root in (
                root / "harness" / "packages",
                root / "harness" / "apps",
            )
            for path in source_root.rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix != ".pyc"
        }
    if indexed != tracked:
        missing = sorted(tracked - indexed)
        extra = sorted(indexed - tracked)
        raise BundleIntegrityError(
            f"source file index diverges from tracked sources; missing={missing}, extra={extra}"
        )


def _source_index_entries(plugin_root: Path) -> tuple[str, ...]:
    index = plugin_root / SOURCE_FILE_INDEX
    if not index.is_file():
        raise BundleIntegrityError(f"source file index is missing: {index}")
    entries: list[str] = []
    seen: set[str] = set()
    for line_number, raw in enumerate(
        index.read_text(encoding="utf-8").splitlines(), start=1
    ):
        entry = raw.strip()
        if not entry or entry.startswith("#"):
            continue
        if not _safe_member_name(entry) or not entry.startswith(
            ("harness/packages/", "harness/apps/")
        ):
            raise BundleIntegrityError(
                f"unsafe source file index entry at line {line_number}: {entry!r}"
            )
        if entry in seen:
            raise BundleIntegrityError(f"duplicate source file index entry: {entry}")
        seen.add(entry)
        entries.append(entry)
    if not entries or entries != sorted(entries):
        raise BundleIntegrityError("source file index must be non-empty and sorted")
    return tuple(entries)


def _git_tracked_sources(plugin_root: Path) -> set[str] | None:
    try:
        top_level = subprocess.run(
            ["git", "-C", str(plugin_root), "rev-parse", "--show-toplevel"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=10,
        )
        if top_level.returncode != 0:
            return None
        discovered_root = Path(
            top_level.stdout.decode("utf-8", errors="strict").strip()
        ).resolve()
        if discovered_root != plugin_root:
            return None
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(plugin_root),
                "ls-files",
                "-z",
                "--",
                "harness/packages",
                "harness/apps",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None
    if completed.returncode != 0:
        return None
    try:
        return {
            value.decode("utf-8", errors="strict").replace("\\", "/")
            for value in completed.stdout.split(b"\0")
            if value
        }
    except UnicodeError as exc:
        raise BundleIntegrityError(
            "Git returned a non-UTF-8 tracked source path"
        ) from exc


def source_manifest_bytes(source_files: dict[str, bytes], version: str) -> bytes:
    payload = {
        "schema_version": SOURCE_MANIFEST_SCHEMA,
        "project": "fusion-agent-harness",
        "version": version,
        "files": [
            {
                "path": path,
                "sha256": sha256_hex(data),
                "size": len(data),
            }
            for path, data in sorted(source_files.items())
        ],
    }
    return (
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")


def _safe_member_name(name: str) -> bool:
    if not name or "\x00" in name or "\\" in name or name.endswith("/"):
        return False
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return False
    return not bool(re.match(r"^[A-Za-z]:", name))


def _single_member(names: Iterable[str], suffix: str) -> str:
    matches = [name for name in names if name.endswith(suffix)]
    if len(matches) != 1:
        raise BundleIntegrityError(
            f"wheel must contain exactly one {suffix}, found {len(matches)}"
        )
    return matches[0]


def _metadata_value(raw: bytes, key: str) -> str:
    value = BytesParser().parsebytes(raw).get(key)
    if not value:
        raise BundleIntegrityError(f"wheel METADATA is missing {key}")
    return str(value)


def _read_record(raw: bytes) -> dict[str, tuple[str, str]]:
    rows: dict[str, tuple[str, str]] = {}
    for index, row in enumerate(csv.reader(io.StringIO(raw.decode("utf-8"))), start=1):
        if len(row) != 3 or not row[0]:
            raise BundleIntegrityError(f"invalid RECORD row {index}")
        if row[0] in rows:
            raise BundleIntegrityError(f"duplicate RECORD member: {row[0]}")
        rows[row[0]] = (row[1], row[2])
    return rows


def _read_source_manifest(
    raw: bytes, names: set[str], archive: zipfile.ZipFile
) -> tuple[str, list[dict[str, Any]]]:
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BundleIntegrityError(f"invalid source manifest: {exc}") from exc
    if payload.get("schema_version") != SOURCE_MANIFEST_SCHEMA:
        raise BundleIntegrityError("unsupported source manifest schema")
    version = str(payload.get("version") or "")
    entries = payload.get("files")
    if not isinstance(entries, list) or not entries:
        raise BundleIntegrityError("source manifest has no files")
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise BundleIntegrityError("source manifest entry must be an object")
        path = str(entry.get("path") or "")
        digest = str(entry.get("sha256") or "")
        size = entry.get("size")
        if not _safe_member_name(path) or path in seen:
            raise BundleIntegrityError(
                f"unsafe or duplicate source manifest path: {path!r}"
            )
        if path not in names:
            raise BundleIntegrityError(
                f"source manifest member is absent from wheel: {path}"
            )
        if (
            not SHA256_PATTERN.fullmatch(digest)
            or not isinstance(size, int)
            or size < 0
        ):
            raise BundleIntegrityError(
                f"invalid source manifest digest/size for {path}"
            )
        data = archive.read(path)
        if sha256_hex(data) != digest or len(data) != size:
            raise BundleIntegrityError(f"source manifest mismatch for {path}")
        seen.add(path)
    return version, entries


def verify_wheel(
    wheel_path: Path | str,
    *,
    plugin_root: Path | str | None = None,
    expected_version: str | None = None,
    require_source_parity: bool = False,
) -> BundleIntegrityReport:
    """Verify member safety, exact RECORD coverage, metadata and source parity."""

    wheel = Path(wheel_path).resolve()
    if not wheel.is_file():
        raise BundleIntegrityError(f"wheel does not exist: {wheel}")
    try:
        with zipfile.ZipFile(wheel) as archive:
            infos = archive.infolist()
            names_list = [info.filename for info in infos]
            if len(names_list) != len(set(names_list)):
                raise BundleIntegrityError("wheel contains duplicate member names")
            unsafe = sorted(name for name in names_list if not _safe_member_name(name))
            if unsafe:
                raise BundleIntegrityError(f"wheel contains unsafe members: {unsafe}")
            names = set(names_list)
            record_name = _single_member(names, ".dist-info/RECORD")
            metadata_name = _single_member(names, ".dist-info/METADATA")
            manifest_name = _single_member(names, f".dist-info/{SOURCE_MANIFEST_NAME}")
            records = _read_record(archive.read(record_name))
            if set(records) != names:
                missing = sorted(names - set(records))
                extra = sorted(set(records) - names)
                raise BundleIntegrityError(
                    f"wheel RECORD is not bijective; missing={missing}, extra={extra}"
                )
            for name in sorted(names):
                digest, size = records[name]
                if name == record_name:
                    if digest or size:
                        raise BundleIntegrityError(
                            "RECORD self-entry must have empty digest and size"
                        )
                    continue
                if not digest.startswith("sha256="):
                    raise BundleIntegrityError(f"RECORD must use sha256 for {name}")
                data = archive.read(name)
                if digest != record_digest(data) or size != str(len(data)):
                    raise BundleIntegrityError(f"wheel RECORD mismatch for {name}")

            metadata_raw = archive.read(metadata_name)
            project_name = _metadata_value(metadata_raw, "Name")
            version = _metadata_value(metadata_raw, "Version")
            if project_name.replace("_", "-").lower() != "fusion-agent-harness":
                raise BundleIntegrityError(f"unexpected project name: {project_name}")
            if expected_version is not None and version != expected_version:
                raise BundleIntegrityError(
                    f"wheel version mismatch: expected {expected_version}, found {version}"
                )
            manifest_version, entries = _read_source_manifest(
                archive.read(manifest_name), names, archive
            )
            if manifest_version != version:
                raise BundleIntegrityError(
                    f"source manifest version {manifest_version!r} does not match wheel {version!r}"
                )

            canonical_dist_info = f"fusion_agent_harness-{version}.dist-info"
            canonical_generated_members = {
                f"{canonical_dist_info}/{relative}"
                for relative in CANONICAL_DIST_INFO_MEMBERS
            }
            manifest_members = {str(entry["path"]) for entry in entries}
            expected_members = manifest_members | canonical_generated_members
            if names != expected_members:
                missing = sorted(expected_members - names)
                extra = sorted(names - expected_members)
                raise BundleIntegrityError(
                    "wheel members diverge from the canonical source-manifest/dist-info "
                    f"allowlist; missing={missing}, extra={extra}"
                )
            if {
                record_name,
                metadata_name,
                manifest_name,
            } != {
                f"{canonical_dist_info}/RECORD",
                f"{canonical_dist_info}/METADATA",
                f"{canonical_dist_info}/{SOURCE_MANIFEST_NAME}",
            }:
                raise BundleIntegrityError(
                    "wheel metadata members must share the canonical dist-info directory"
                )

            if require_source_parity:
                if plugin_root is None:
                    raise BundleIntegrityError("source parity requires plugin_root")
                source_files = collect_source_files(Path(plugin_root))
                manifest_by_path = {str(entry["path"]): entry for entry in entries}
                if set(source_files) != set(manifest_by_path):
                    missing = sorted(set(source_files) - set(manifest_by_path))
                    extra = sorted(set(manifest_by_path) - set(source_files))
                    raise BundleIntegrityError(
                        f"source manifest is not bijective with checkout; missing={missing}, extra={extra}"
                    )
                for path, data in source_files.items():
                    entry = manifest_by_path[path]
                    if (
                        sha256_hex(data) != entry["sha256"]
                        or len(data) != entry["size"]
                    ):
                        raise BundleIntegrityError(
                            f"checkout source mismatch for {path}"
                        )
    except zipfile.BadZipFile as exc:
        raise BundleIntegrityError(f"invalid wheel zip: {exc}") from exc

    return BundleIntegrityReport(
        wheel=wheel,
        project_name=project_name,
        version=version,
        member_count=len(names),
        source_file_count=len(entries),
        sha256=sha256_hex(wheel.read_bytes()),
    )


def expected_version_from_checkout(plugin_root: Path | str) -> str:
    pyproject = Path(plugin_root) / "harness" / "pyproject.toml"
    return str(
        tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
    )


def verify_installed_distribution(
    wheel_path: Path | str,
    *,
    distribution_name: str = "fusion-agent-harness",
) -> None:
    """Compare installed files with the already verified wheel without importing it."""

    wheel = Path(wheel_path).resolve()
    dist = metadata.distribution(distribution_name)
    with zipfile.ZipFile(wheel) as archive:
        record_name = _single_member(archive.namelist(), ".dist-info/RECORD")
        for name in archive.namelist():
            if name == record_name:
                continue
            installed = Path(str(dist.locate_file(name)))
            if not installed.is_file():
                raise BundleIntegrityError(f"installed wheel member is missing: {name}")
            if installed.read_bytes() != archive.read(name):
                raise BundleIntegrityError(f"installed wheel member mismatch: {name}")
