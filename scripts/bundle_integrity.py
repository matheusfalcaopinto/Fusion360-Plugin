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
import os
import platform
import re
import stat
import struct
import subprocess
import sys
import tomllib
import zipfile
from dataclasses import dataclass
from datetime import datetime
from email.parser import BytesParser
from importlib import metadata
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


SOURCE_MANIFEST_SCHEMA = "fusion_agent.source_manifest.v1"
SOURCE_MANIFEST_NAME = "SOURCE-MANIFEST.json"
SOURCE_FILE_INDEX = "harness/source-files.txt"
FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
CANONICAL_WHEEL_METADATA = (
    "Wheel-Version: 1.0\n"
    "Generator: fusion-agent-codex deterministic builder\n"
    "Root-Is-Purelib: true\n"
    "Tag: py3-none-any\n"
).encode("utf-8")
CANONICAL_ENTRY_POINTS = (
    "[console_scripts]\n"
    "fusion-agent = cli.main:app\n"
    "fusion-agent-mcp = fusion_agent_mcp.server:main\n"
).encode("utf-8")
MAX_WHEEL_BYTES = 64 * 1024 * 1024
MAX_WHEEL_MEMBER_BYTES = 8 * 1024 * 1024
MAX_DEPENDENCY_WHEEL_BYTES = 128 * 1024 * 1024
MAX_DEPENDENCY_MEMBER_BYTES = 96 * 1024 * 1024
MAX_DEPENDENCY_EXPANDED_BYTES = 256 * 1024 * 1024
SECURITY_INPUT_PATHS = (
    ".gitattributes",
    "harness/pyproject.toml",
    "harness/requirements/build.in",
    "harness/requirements/build.lock",
    "harness/requirements/faust.lock",
    "harness/requirements/quality.lock",
    "harness/requirements/runtime.lock",
    "harness/requirements/test.lock",
    "harness/uv.lock",
)
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
CODEX_CACHEBUSTER_TIMESTAMP_PATTERN = re.compile(r"^[0-9]{14}$")
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
INSTALLER_OWNED_DIST_INFO_MEMBERS = frozenset(
    {
        "INSTALLER",
        "REQUESTED",
        "direct_url.json",
    }
)
REVIEWED_STARTUP_HOOKS = {
    # ``pywin32`` is a runtime dependency on Windows and needs this bootstrap
    # hook so its extension directories are importable.  Bind the exception to
    # the reviewed package version, path, bytes, and size instead of trusting an
    # editable installed RECORD.
    (
        "pywin32",
        "312",
        "pywin32.pth",
    ): (
        "e7cd73df98b91c407dfd96d1f4dd18b7f9a60f29902b92cf5ece79b6eb637b81",
        185,
    ),
    # The quality environment supports subprocess coverage collection.  It is
    # not installed by the runtime/test locks, but keeping the exact reviewed
    # hook makes ``quality.lock`` independently verifiable.
    (
        "coverage",
        "7.15.2",
        "a1_coverage.pth",
    ): (
        "f1498191b7f52180654ccdb6195233612805e26344100c093058343ea04afd36",
        206,
    ),
    # CPython 3.11 venvs can retain their ensurepip-provided setuptools.  The
    # bootstrap package version is interpreter-owned, so only the invariant
    # reviewed hook bytes (not an open-ended package/path exception) are fixed.
    (
        "setuptools",
        "*",
        "distutils-precedence.pth",
    ): (
        "2638ce9e2500e572a5e0de7faed6661eb569d1b696fcba07b0dd223da5f5d224",
        151,
    ),
}


def valid_codex_cachebuster_version(
    version: str, *, expected_base_version: str
) -> bool:
    """Return whether a plugin version has an exact, valid UTC cachebuster."""

    prefix = f"{expected_base_version}+codex."
    if not version.startswith(prefix):
        return False
    timestamp = version.removeprefix(prefix)
    if CODEX_CACHEBUSTER_TIMESTAMP_PATTERN.fullmatch(timestamp) is None:
        return False
    try:
        parsed = datetime.strptime(timestamp, "%Y%m%d%H%M%S")
    except ValueError:
        return False
    return parsed.strftime("%Y%m%d%H%M%S") == timestamp


REVIEWED_NATIVE_SCRIPT_WRAPPERS = {
    # These projects ship native executables through wheel ``.data/scripts``
    # rather than declaring Python entry points.  Keep the exception bound to
    # the exact versions selected by ``quality.lock``.
    ("ruff", "0.15.21"): {"ruff"},
    ("uv", "0.11.29"): {"uv", "uvw", "uvx"},
}
BUILD_BACKEND_HASHES = {
    ("setuptools", "80.9.0"): {
        "062d34222ad13e0cc312a4c02d73f059e86a4acbfbdea8f8f76b28c99f306922"
    },
    ("wheel", "0.45.1"): {
        "708e7481cc80179af0e556bbf0cc00b8444c7321e2700b8d8580231d13017248"
    },
}


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


@dataclass(frozen=True, slots=True)
class _TrustedDependencyWheel:
    name: str
    version: str
    wheel: Path
    members: dict[str, bytes]
    record_name: str
    entry_points: dict[str, tuple[str, bool]]
    wheel_scripts: dict[str, bytes]


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
        if plugin_root != source_root and plugin_root not in source_root.parents:
            raise BundleIntegrityError(
                f"canonical source root escapes plugin root: {prefix}"
            )
        indexed_path = plugin_root / Path(entry)
        if indexed_path.is_symlink():
            raise BundleIntegrityError(
                f"indexed canonical source must not be a symlink: {entry}"
            )
        path = indexed_path.resolve()
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


def collect_security_inputs(plugin_root: Path) -> dict[str, bytes]:
    root = plugin_root.resolve()
    inputs: dict[str, bytes] = {}
    for relative in SECURITY_INPUT_PATHS:
        input_path = root / relative
        if input_path.is_symlink():
            raise BundleIntegrityError(
                f"security input must not be a symlink: {relative}"
            )
        path = input_path.resolve()
        if root != path and root not in path.parents:
            raise BundleIntegrityError(
                f"security input escapes plugin root: {relative}"
            )
        if not path.is_file():
            raise BundleIntegrityError(
                f"required security input is missing: {relative}"
            )
        inputs[relative] = path.read_bytes()
    return inputs


def _normalize_package_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _requirement_name(value: str) -> str:
    lowered = value.lower()
    if (
        any(token in lowered for token in (" @ ", "://", "git+", "file:"))
        or "/" in value
        or "\\" in value
        or any(character.isspace() for character in value)
    ):
        raise BundleIntegrityError(
            f"dependency requirement uses an unreviewed source: {value!r}"
        )
    match = re.match(r"[A-Za-z0-9_.-]+", value)
    if match is None:
        raise BundleIntegrityError(f"invalid dependency requirement: {value!r}")
    return _normalize_package_name(match.group(0))


def _locked_requirements(path: Path) -> dict[tuple[str, str], set[str]]:
    try:
        raw_lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise BundleIntegrityError(
            f"dependency lock is unreadable: {path.name}"
        ) from exc
    rendered = "\n".join(raw_lines)
    forbidden = (" @ ", "git+", "--trusted-host", "--extra-index-url", "-e ")
    if any(value in rendered.lower() for value in forbidden):
        raise BundleIntegrityError(
            f"dependency lock contains an unsafe source: {path.name}"
        )
    logical: list[str] = []
    pending = ""
    for raw in raw_lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        continuation = stripped.endswith("\\")
        value = stripped[:-1].strip() if continuation else stripped
        pending = f"{pending} {value}".strip()
        if not continuation:
            logical.append(pending)
            pending = ""
    if pending:
        raise BundleIntegrityError(
            f"dependency lock has an unterminated line: {path.name}"
        )
    requirements: dict[tuple[str, str], set[str]] = {}
    options: set[str] = set()
    for value in logical:
        if value.startswith("-"):
            if value != "--only-binary=:all:" or value in options:
                raise BundleIntegrityError(
                    f"dependency lock contains an unsupported option: {path.name}"
                )
            options.add(value)
            continue
        match = re.match(
            r"^([A-Za-z0-9_.-]+)==([^\s;]+)(?:\s*;[^-]+)?\s+(.*)$",
            value,
        )
        if match is None:
            raise BundleIntegrityError(
                f"dependency lock entry is not exact and hash-pinned: {path.name}"
            )
        name = _normalize_package_name(match.group(1))
        version = match.group(2)
        hash_tokens = match.group(3).split()
        if any(
            re.fullmatch(r"--hash=sha256:[0-9a-f]{64}", token) is None
            for token in hash_tokens
        ):
            raise BundleIntegrityError(
                f"dependency lock entry contains an unsupported option: {path.name}"
            )
        hashes = {token.removeprefix("--hash=sha256:") for token in hash_tokens}
        if not hashes or (name, version) in requirements:
            raise BundleIntegrityError(
                f"dependency lock entry is missing hashes or duplicated: {path.name}"
            )
        requirements[(name, version)] = hashes
    if not requirements:
        raise BundleIntegrityError(f"dependency lock is empty: {path.name}")
    return requirements


def _uv_locked_artifacts(plugin_root: Path) -> dict[tuple[str, str], set[str]]:
    try:
        payload = tomllib.loads(
            (plugin_root / "harness" / "uv.lock").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise BundleIntegrityError("uv.lock is unreadable") from exc
    artifacts: dict[tuple[str, str], set[str]] = {}
    for package in payload.get("package", []):
        source = package.get("source")
        if not isinstance(source, dict) or "registry" not in source:
            continue
        key = (
            _normalize_package_name(str(package.get("name") or "")),
            str(package.get("version") or ""),
        )
        hashes = artifacts.setdefault(key, set())
        candidates = [package.get("sdist"), *package.get("wheels", [])]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            digest = str(candidate.get("hash") or "")
            if digest.startswith("sha256:") and SHA256_PATTERN.fullmatch(digest[7:]):
                hashes.add(digest[7:])
    return artifacts


def verify_dependency_locks(plugin_root: Path | str) -> None:
    root = Path(plugin_root).resolve()
    try:
        project = tomllib.loads(
            (root / "harness" / "pyproject.toml").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise BundleIntegrityError("pyproject.toml is unreadable") from exc
    build_system = project.get("build-system", {})
    if (
        set(build_system) != {"requires", "build-backend"}
        or build_system.get("build-backend") != "setuptools.build_meta"
    ):
        raise BundleIntegrityError("PEP 517 build backend is not exact")
    build_requires = tuple(build_system.get("requires", ()))
    if build_requires != ("setuptools==80.9.0", "wheel==0.45.1"):
        raise BundleIntegrityError("PEP 517 build requirements are not exact")
    try:
        build_inputs = tuple(
            line.strip()
            for line in (root / "harness" / "requirements" / "build.in")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    except (OSError, UnicodeError) as exc:
        raise BundleIntegrityError("PEP 517 build input is unreadable") from exc
    if build_inputs != build_requires:
        raise BundleIntegrityError(
            "PEP 517 build input diverges from project requirements"
        )
    build_lock = _locked_requirements(root / "harness" / "requirements" / "build.lock")
    if build_lock != BUILD_BACKEND_HASHES:
        raise BundleIntegrityError(
            "PEP 517 build lock does not match reviewed backends"
        )

    project_table = project.get("project", {})
    if project_table.get("scripts") != {
        "fusion-agent": "cli.main:app",
        "fusion-agent-mcp": "fusion_agent_mcp.server:main",
    }:
        raise BundleIntegrityError(
            "project console scripts diverge from the reviewed entry points"
        )
    runtime_names = {
        _requirement_name(str(value)) for value in project_table.get("dependencies", [])
    }
    optional = project_table.get("optional-dependencies", {})
    required_by_lock = {
        "runtime.lock": runtime_names,
        "test.lock": runtime_names
        | {_requirement_name(str(value)) for value in optional.get("test", [])},
        "quality.lock": runtime_names
        | {
            _requirement_name(str(value))
            for extra in ("test", "dev")
            for value in optional.get(extra, [])
        },
        "faust.lock": runtime_names
        | {_requirement_name(str(value)) for value in optional.get("faust", [])},
    }
    uv_artifacts = _uv_locked_artifacts(root)
    for name, required_names in required_by_lock.items():
        locked = _locked_requirements(root / "harness" / "requirements" / name)
        locked_names = {key[0] for key in locked}
        if not required_names <= locked_names:
            raise BundleIntegrityError(
                f"dependency lock omits required packages: {name}"
            )
        for key, hashes in locked.items():
            if key not in uv_artifacts or hashes != uv_artifacts[key]:
                raise BundleIntegrityError(
                    f"dependency lock diverges from uv.lock artifacts: {name}"
                )


def _marker_applies(marker: str) -> bool:
    environment = {
        "implementation_name": sys.implementation.name,
        "platform_python_implementation": platform.python_implementation(),
        "sys_platform": sys.platform,
    }
    for clause in (value.strip() for value in marker.split(" and ")):
        match = re.fullmatch(
            r"(implementation_name|platform_python_implementation|sys_platform)\s*(==|!=)\s*'([^']+)'",
            clause,
        )
        if match is None:
            raise BundleIntegrityError(
                f"dependency lock contains an unsupported environment marker: {marker}"
            )
        actual = environment[match.group(1)]
        expected = match.group(3)
        if (match.group(2) == "==" and actual != expected) or (
            match.group(2) == "!=" and actual == expected
        ):
            return False
    return True


def _applicable_locked_requirements(
    path: Path,
) -> dict[tuple[str, str], set[str]]:
    locked = _locked_requirements(path)
    logical: list[str] = []
    pending = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        continuation = stripped.endswith("\\")
        value = stripped[:-1].strip() if continuation else stripped
        pending = f"{pending} {value}".strip()
        if not continuation:
            logical.append(pending)
            pending = ""
    applicable: dict[tuple[str, str], set[str]] = {}
    for value in logical:
        if value.startswith("--"):
            continue
        requirement = value.split(" --hash=", 1)[0].strip()
        requirement_text, separator, marker = requirement.partition(";")
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^\s]+)", requirement_text.strip())
        if match is None:
            raise BundleIntegrityError(
                f"dependency lock entry cannot be evaluated: {path.name}"
            )
        if separator and not _marker_applies(marker.strip()):
            continue
        name = _normalize_package_name(match.group(1))
        version = match.group(2)
        key = (name, version)
        if any(existing_name == name for existing_name, _version in applicable):
            raise BundleIntegrityError(
                f"dependency lock has duplicate applicable package: {path.name}"
            )
        try:
            applicable[key] = locked[key]
        except KeyError as exc:
            raise BundleIntegrityError(
                f"dependency lock entry cannot be hash-bound: {path.name}"
            ) from exc
    return applicable


def _applicable_lock_versions(path: Path) -> dict[str, str]:
    return {name: version for name, version in _applicable_locked_requirements(path)}


def verify_installed_dependency_set(
    plugin_root: Path | str,
    *,
    dependency_wheelhouse: Path | str,
    lock_name: str = "runtime.lock",
    site_packages: Path | str | None = None,
) -> None:
    if lock_name not in {"runtime.lock", "test.lock", "quality.lock", "faust.lock"}:
        raise BundleIntegrityError("installed dependency lock selection is invalid")
    root = Path(plugin_root).resolve()
    lock_path = root / "harness" / "requirements" / lock_name
    applicable = _applicable_locked_requirements(lock_path)
    expected = {name: version for name, version in applicable}
    trusted_wheels = _verify_dependency_wheelhouse(
        dependency_wheelhouse,
        applicable=applicable,
    )
    project = tomllib.loads(
        (root / "harness" / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]
    expected[_normalize_package_name(str(project["name"]))] = str(project["version"])

    installed_distributions = _installed_distributions(site_packages)
    observed: dict[str, str] = {}
    for distribution in installed_distributions:
        try:
            raw_name = distribution.metadata["Name"]
        except KeyError:
            raw_name = ""
        name = _normalize_package_name(str(raw_name or ""))
        if not name:
            raise BundleIntegrityError(
                "installed environment contains a distribution without a name"
            )
        if name in observed:
            raise BundleIntegrityError(
                f"installed environment has duplicate distributions: {name}"
            )
        observed[name] = str(distribution.version)
    bootstrap_allowlist = {"pip", "setuptools", "wheel"}
    missing = sorted(set(expected) - set(observed))
    extra = sorted(set(observed) - set(expected) - bootstrap_allowlist)
    mismatched = sorted(
        name
        for name in set(expected) & set(observed)
        if observed[name] != expected[name]
    )
    if missing or extra or mismatched:
        raise BundleIntegrityError(
            "installed dependency set diverges from the selected hash lock; "
            f"missing={missing}, extra={extra}, mismatched={mismatched}"
        )
    _verify_site_package_ownership(
        installed_distributions,
        trusted_wheels=trusted_wheels,
        locked_names=set(expected) - {_normalize_package_name(str(project["name"]))},
    )


def _verify_dependency_wheelhouse(
    dependency_wheelhouse: Path | str,
    *,
    applicable: dict[tuple[str, str], set[str]],
) -> dict[str, _TrustedDependencyWheel]:
    supplied = Path(dependency_wheelhouse)
    _reject_reparse(supplied, "dependency wheelhouse")
    try:
        wheelhouse = supplied.resolve(strict=True)
    except OSError as exc:
        raise BundleIntegrityError("dependency wheelhouse is unavailable") from exc
    if not wheelhouse.is_dir():
        raise BundleIntegrityError("dependency wheelhouse is not a directory")

    wheels: list[Path] = []
    try:
        entries = sorted(os.scandir(wheelhouse), key=lambda entry: entry.name)
    except OSError as exc:
        raise BundleIntegrityError("dependency wheelhouse is unreadable") from exc
    for entry in entries:
        path = Path(entry.path)
        _reject_reparse(path, "dependency wheelhouse member")
        if not entry.is_file(follow_symlinks=False) or path.suffix.lower() != ".whl":
            raise BundleIntegrityError(
                f"dependency wheelhouse contains an unsupported member: {entry.name}"
            )
        wheels.append(path)

    trusted: dict[str, _TrustedDependencyWheel] = {}
    observed_keys: set[tuple[str, str]] = set()
    selected_hashes = {digest for hashes in applicable.values() for digest in hashes}
    for wheel in wheels:
        if wheel.stat().st_size > MAX_DEPENDENCY_WHEEL_BYTES:
            raise BundleIntegrityError(
                f"dependency wheel exceeds the archive size limit: {wheel.name}"
            )
        raw_wheel = _read_bound_regular_file(
            wheel,
            label="dependency wheel",
            max_bytes=MAX_DEPENDENCY_WHEEL_BYTES,
        )
        digest = sha256_hex(raw_wheel)
        if digest not in selected_hashes:
            raise BundleIntegrityError(
                f"dependency wheel is not selected by the hash lock: {wheel.name}"
            )
        parsed = _read_trusted_dependency_wheel(wheel, raw_wheel=raw_wheel)
        key = (parsed.name, parsed.version)
        hashes = applicable.get(key)
        if hashes is None or digest not in hashes:
            raise BundleIntegrityError(
                f"dependency wheel is not selected by the hash lock: {wheel.name}"
            )
        if key in observed_keys:
            raise BundleIntegrityError(
                f"dependency wheelhouse contains duplicate distribution: {parsed.name}"
            )
        observed_keys.add(key)
        trusted[parsed.name] = parsed

    missing = sorted(set(applicable) - observed_keys)
    extra = sorted(observed_keys - set(applicable))
    if missing or extra:
        raise BundleIntegrityError(
            "dependency wheelhouse diverges from the applicable hash lock; "
            f"missing={missing}, extra={extra}"
        )
    return trusted


def _read_trusted_dependency_wheel(
    wheel: Path,
    *,
    raw_wheel: bytes | None = None,
) -> _TrustedDependencyWheel:
    try:
        trusted_bytes = (
            raw_wheel
            if raw_wheel is not None
            else _read_bound_regular_file(
                wheel,
                label="dependency wheel",
                max_bytes=MAX_DEPENDENCY_WHEEL_BYTES,
            )
        )
        with zipfile.ZipFile(io.BytesIO(trusted_bytes)) as archive:
            infos = archive.infolist()
            names_list = [info.filename for info in infos]
            if len(names_list) != len(set(names_list)):
                raise BundleIntegrityError(
                    f"dependency wheel has duplicate members: {wheel.name}"
                )
            file_names = {info.filename for info in infos if not info.is_dir()}
            directory_names = {
                info.filename.removesuffix("/") for info in infos if info.is_dir()
            }
            if (
                not file_names
                or any(not _safe_member_name(name) for name in file_names)
                or any(not _safe_member_name(name) for name in directory_names)
                or bool(file_names & directory_names)
                or any(
                    not any(member.startswith(directory + "/") for member in file_names)
                    for directory in directory_names
                )
            ):
                raise BundleIntegrityError(
                    f"dependency wheel has unsafe members: {wheel.name}"
                )
            expanded = 0
            for info in infos:
                mode = (info.external_attr >> 16) & 0xFFFF
                if info.flag_bits & 0x1 or stat.S_ISLNK(mode):
                    raise BundleIntegrityError(
                        f"dependency wheel has an unsafe ZIP member: {info.filename}"
                    )
                if info.is_dir() and (info.file_size or info.compress_size):
                    raise BundleIntegrityError(
                        f"dependency wheel has a nonempty directory member: {info.filename}"
                    )
                if info.file_size > MAX_DEPENDENCY_MEMBER_BYTES:
                    raise BundleIntegrityError(
                        f"dependency wheel member exceeds the size limit: {info.filename}"
                    )
                expanded += info.file_size
            if expanded > MAX_DEPENDENCY_EXPANDED_BYTES:
                raise BundleIntegrityError(
                    f"dependency wheel exceeds the expanded size limit: {wheel.name}"
                )

            names = file_names
            record_name = _single_member(names, ".dist-info/RECORD")
            metadata_name = _single_member(names, ".dist-info/METADATA")
            wheel_metadata_name = _single_member(names, ".dist-info/WHEEL")
            dist_info = record_name.rsplit("/", 1)[0]
            if {
                metadata_name.rsplit("/", 1)[0],
                wheel_metadata_name.rsplit("/", 1)[0],
            } != {dist_info}:
                raise BundleIntegrityError(
                    f"dependency wheel metadata directories diverge: {wheel.name}"
                )
            if any(
                part.endswith(".dist-info") and part != dist_info
                for name in names
                for part in (name.split("/", 1)[0],)
            ):
                raise BundleIntegrityError(
                    f"dependency wheel contains multiple dist-info roots: {wheel.name}"
                )

            records = _read_record(archive.read(record_name))
            if any(not _safe_member_name(name) for name in records):
                raise BundleIntegrityError(
                    f"dependency wheel RECORD contains a noncanonical path: {wheel.name}"
                )
            if set(records) != names:
                raise BundleIntegrityError(
                    f"dependency wheel RECORD is not bijective: {wheel.name}"
                )
            for name in sorted(names):
                digest, size = records[name]
                if name == record_name:
                    if digest or size:
                        raise BundleIntegrityError(
                            f"dependency wheel RECORD self-entry is invalid: {wheel.name}"
                        )
                    continue
                data = archive.read(name)
                if digest != record_digest(data) or size != str(len(data)):
                    raise BundleIntegrityError(
                        f"dependency wheel RECORD mismatch for {name}"
                    )

            metadata_raw = archive.read(metadata_name)
            message = BytesParser().parsebytes(metadata_raw)
            names_header = message.get_all("Name") or []
            versions_header = message.get_all("Version") or []
            if len(names_header) != 1 or len(versions_header) != 1:
                raise BundleIntegrityError(
                    f"dependency wheel METADATA identity is not unique: {wheel.name}"
                )
            name = _normalize_package_name(str(names_header[0]))
            version = str(versions_header[0])
            expected_dist_info = (
                f"{name.replace('-', '_')}-"
                f"{re.sub(r'[^A-Za-z0-9.]+', '_', version)}.dist-info"
            )
            if dist_info.casefold() != expected_dist_info.casefold():
                raise BundleIntegrityError(
                    f"dependency wheel dist-info identity is not canonical: {wheel.name}"
                )

            wheel_message = BytesParser().parsebytes(archive.read(wheel_metadata_name))
            if (
                len(wheel_message.get_all("Wheel-Version") or []) != 1
                or len(wheel_message.get_all("Root-Is-Purelib") or []) != 1
                or str(wheel_message.get("Root-Is-Purelib")).lower()
                not in {"true", "false"}
            ):
                raise BundleIntegrityError(
                    f"dependency wheel WHEEL metadata is invalid: {wheel.name}"
                )

            data_root = dist_info.removesuffix(".dist-info") + ".data/"
            members: dict[str, bytes] = {}
            wheel_scripts: dict[str, bytes] = {}
            for member_name in sorted(names - {record_name}):
                installed_name = member_name
                if member_name.startswith(data_root):
                    remainder = member_name.removeprefix(data_root)
                    category, separator, relative = remainder.partition("/")
                    if separator != "/" or not _safe_member_name(relative):
                        raise BundleIntegrityError(
                            f"dependency wheel has invalid .data mapping: {member_name}"
                        )
                    if category in {"purelib", "platlib"}:
                        installed_name = relative
                    elif category == "scripts":
                        if "/" in relative or relative in wheel_scripts:
                            raise BundleIntegrityError(
                                f"dependency wheel has ambiguous script mapping: {member_name}"
                            )
                        wheel_scripts[relative] = archive.read(member_name)
                        continue
                    else:
                        raise BundleIntegrityError(
                            f"dependency wheel uses an unsupported .data scheme: {member_name}"
                        )
                if installed_name in members:
                    raise BundleIntegrityError(
                        f"dependency wheel has colliding installed paths: {installed_name}"
                    )
                data = archive.read(member_name)
                _reject_locked_startup_member(installed_name)
                if PurePosixPath(installed_name).suffix.lower() == ".pyc":
                    raise BundleIntegrityError(
                        f"dependency wheel contains precompiled bytecode: {member_name}"
                    )
                members[installed_name] = data

            entry_points_name = f"{dist_info}/entry_points.txt"
            entry_points = (
                _script_entry_points(members[entry_points_name])
                if entry_points_name in members
                else {}
            )
            return _TrustedDependencyWheel(
                name=name,
                version=version,
                wheel=wheel,
                members=members,
                record_name=record_name,
                entry_points=entry_points,
                wheel_scripts=wheel_scripts,
            )
    except (OSError, UnicodeError, zipfile.BadZipFile) as exc:
        raise BundleIntegrityError(
            f"dependency wheel is unreadable: {wheel.name}"
        ) from exc


def _read_bound_regular_file(
    path: Path,
    *,
    label: str,
    max_bytes: int,
) -> bytes:
    try:
        before = path.lstat()
    except OSError as exc:
        raise BundleIntegrityError(f"{label} is unavailable") from exc
    if _is_reparse(before) or not stat.S_ISREG(before.st_mode):
        raise BundleIntegrityError(f"{label} must be a non-reparse regular file")
    if before.st_size > max_bytes:
        raise BundleIntegrityError(f"{label} exceeds the size limit")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise BundleIntegrityError(f"{label} cannot be opened safely") from exc
    try:
        opened = os.fstat(descriptor)
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
        opened_identity = (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        )
        if (
            before_identity != opened_identity
            or _is_reparse(opened)
            or not stat.S_ISREG(opened.st_mode)
        ):
            raise BundleIntegrityError(f"{label} changed before it could be pinned")
        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(descriptor, 1024 * 1024):
            total += len(chunk)
            if total > max_bytes:
                raise BundleIntegrityError(f"{label} exceeds the size limit")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        )
        if after_identity != opened_identity or total != after.st_size:
            raise BundleIntegrityError(f"{label} changed while it was read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _is_reparse(stat_result: os.stat_result) -> bool:
    return stat.S_ISLNK(stat_result.st_mode) or bool(
        getattr(stat_result, "st_file_attributes", 0) & 0x400
    )


def _reject_reparse(path: Path, label: str) -> None:
    try:
        result = path.lstat()
    except OSError as exc:
        raise BundleIntegrityError(f"{label} is unavailable") from exc
    if _is_reparse(result):
        raise BundleIntegrityError(f"{label} must not be a symlink or reparse point")


def _reject_reparse_chain(path: Path, *, anchor: Path) -> None:
    anchor_absolute = Path(os.path.abspath(anchor))
    path_absolute = Path(os.path.abspath(path))
    try:
        relative = path_absolute.relative_to(anchor_absolute)
    except ValueError as exc:
        raise BundleIntegrityError(
            "installed path escapes its reviewed filesystem anchor"
        ) from exc
    current = anchor_absolute
    _reject_reparse(current, "installed path")
    for part in relative.parts:
        current /= part
        _reject_reparse(current, "installed path")


def _walk_regular_files(root: Path) -> Iterable[Path]:
    _reject_reparse(root, "installed site-packages")
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            raise BundleIntegrityError(
                "installed site-packages cannot be enumerated"
            ) from exc
        for entry in entries:
            path = Path(entry.path)
            try:
                result = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise BundleIntegrityError(
                    "installed site-packages member cannot be inspected"
                ) from exc
            if _is_reparse(result):
                raise BundleIntegrityError(
                    f"installed site-packages contains a symlink or reparse point: "
                    f"{path.relative_to(root).as_posix()}"
                )
            if stat.S_ISDIR(result.st_mode):
                pending.append(path)
            elif stat.S_ISREG(result.st_mode):
                yield path
            else:
                raise BundleIntegrityError(
                    f"installed site-packages contains a non-regular member: "
                    f"{path.relative_to(root).as_posix()}"
                )


def _reject_locked_startup_member(relative: str) -> None:
    path = PurePosixPath(relative)
    parts = path.parts
    if not parts:
        return
    customizers = {"sitecustomize", "usercustomize"}
    first = parts[0].casefold()
    first_stem = first.split(".", 1)[0]
    cache_customizer = (
        first == "__pycache__"
        and len(parts) == 2
        and parts[1].casefold().split(".", 1)[0] in customizers
        and parts[1].casefold().endswith(".pyc")
    )
    if first in customizers or first_stem in customizers or cache_customizer:
        raise BundleIntegrityError(
            f"installed startup customizer is not permitted: {relative}"
        )


def _installed_distributions(site_packages: Path | str | None) -> list[Any]:
    if site_packages is None:
        return list(metadata.distributions())
    root = Path(site_packages)
    _reject_reparse(root, "installed site-packages")
    try:
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise BundleIntegrityError("installed site-packages is unavailable") from exc
    if not resolved.is_dir():
        raise BundleIntegrityError("installed site-packages is not a directory")
    return list(metadata.distributions(path=[str(resolved)]))


def _verify_site_package_ownership(
    distributions: list[Any],
    *,
    trusted_wheels: dict[str, _TrustedDependencyWheel] | None = None,
    locked_names: set[str] | None = None,
) -> None:
    trusted_wheels = trusted_wheels or {}
    locked_names = locked_names or set()
    owned_by_root: dict[Path, set[str]] = {}
    locked_owned_by_root: dict[Path, set[str]] = {}
    for distribution in distributions:
        lexical_root = Path(str(distribution.locate_file("")))
        _reject_reparse(lexical_root, "installed distribution root")
        try:
            root = lexical_root.resolve(strict=True)
        except (AttributeError, OSError) as exc:
            raise BundleIntegrityError(
                "installed distribution root cannot be verified"
            ) from exc
        if not root.is_dir():
            raise BundleIntegrityError("installed distribution root is not a directory")
        name, version = _installed_distribution_identity(distribution)
        owned = owned_by_root.setdefault(root, set())
        if name in locked_names:
            try:
                trusted = trusted_wheels[name]
            except KeyError as exc:
                raise BundleIntegrityError(
                    f"locked dependency has no trusted wheel: {name}"
                ) from exc
            if version != trusted.version:
                raise BundleIntegrityError(
                    f"locked dependency version diverges from trusted wheel: {name}"
                )
            distribution_owned = _verify_locked_installed_record(
                distribution,
                root,
                trusted,
                distributions=distributions,
            )
            locked_owned_by_root.setdefault(root, set()).update(distribution_owned)
        else:
            distribution_owned = _verify_installed_record(distribution, root)
        collision = owned & distribution_owned
        if collision:
            raise BundleIntegrityError(
                f"installed distributions claim the same files: {sorted(collision)}"
            )
        owned.update(distribution_owned)

    for root, owned in owned_by_root.items():
        unexpected: list[str] = []
        locked_owned = locked_owned_by_root.get(root, set())
        for path in _walk_regular_files(root):
            relative = path.relative_to(root).as_posix()
            _reject_locked_startup_member(relative)
            if relative in owned:
                continue
            if _is_generated_bytecode(relative, locked_owned):
                raise BundleIntegrityError(
                    f"locked dependency bytecode is not permitted: {relative}"
                )
            if _is_generated_bytecode(relative, owned):
                continue
            unexpected.append(relative)
        if unexpected:
            raise BundleIntegrityError(
                f"installed site-packages contains unowned files: {sorted(unexpected)}"
            )


def _verify_locked_installed_record(
    distribution: Any,
    root: Path,
    trusted: _TrustedDependencyWheel,
    *,
    distributions: list[Any],
) -> set[str]:
    declared = getattr(distribution, "files", None)
    if declared is None:
        raise BundleIntegrityError("locked dependency has no verifiable file inventory")
    declared_names = [str(value).replace("\\", "/") for value in declared]
    if len(declared_names) != len(set(declared_names)):
        raise BundleIntegrityError("locked dependency file inventory is duplicated")
    if any(not _safe_installed_record_name(name) for name in declared_names):
        raise BundleIntegrityError("locked dependency file inventory is noncanonical")

    record_names = [
        name
        for name in declared_names
        if PurePosixPath(name).name == "RECORD"
        and PurePosixPath(name).parent.name.endswith(".dist-info")
    ]
    if record_names != [trusted.record_name]:
        raise BundleIntegrityError(
            "locked dependency RECORD identity diverges from trusted wheel"
        )
    record_path, record_relative = _locate_installed_record_file(
        distribution,
        root,
        trusted.record_name,
        allow_external_script=False,
        script_names=set(),
    )
    if record_relative != trusted.record_name:
        raise BundleIntegrityError("installed RECORD path is not canonical")
    rows = _read_installed_record(record_path)
    if set(rows) != set(declared_names):
        raise BundleIntegrityError(
            "locked dependency metadata inventory diverges from RECORD"
        )
    if rows.get(trusted.record_name) != ("", ""):
        raise BundleIntegrityError(
            "installed RECORD self-entry must have empty digest and size"
        )

    name, version = _installed_distribution_identity(distribution)
    if name != trusted.name or version != trusted.version:
        raise BundleIntegrityError(
            "locked dependency metadata identity diverges from trusted wheel"
        )
    expected = trusted.members
    allowed_installer = {
        f"{trusted.record_name.rsplit('/', 1)[0]}/INSTALLER": b"pip\n",
        f"{trusted.record_name.rsplit('/', 1)[0]}/REQUESTED": b"",
    }
    owned: set[str] = {trusted.record_name}
    external: dict[str, bytes] = {}
    for row_name, (digest, size) in rows.items():
        if row_name == trusted.record_name:
            continue
        if PurePosixPath(row_name).suffix.lower() == ".pyc":
            raise BundleIntegrityError(
                f"locked dependency bytecode is not permitted: {row_name}"
            )
        path, relative = _locate_installed_record_file(
            distribution,
            root,
            row_name,
            allow_external_script=True,
            script_names=None,
        )
        if not digest or not size:
            raise BundleIntegrityError(
                f"locked dependency RECORD entry lacks hash/size: {row_name}"
            )
        data = _read_bound_regular_file(
            path,
            label="installed dependency file",
            max_bytes=MAX_DEPENDENCY_MEMBER_BYTES,
        )
        if digest != record_digest(data) or size != str(len(data)):
            raise BundleIntegrityError(
                f"installed RECORD hash/size mismatch for {row_name}"
            )
        if relative is None:
            external[row_name] = data
            continue
        _reject_locked_startup_member(relative)
        _verify_startup_hook(name, version, relative, data)
        trusted_data = expected.get(relative)
        if trusted_data is not None:
            if data != trusted_data:
                raise BundleIntegrityError(
                    f"installed dependency diverges from trusted wheel: {relative}"
                )
        elif relative in allowed_installer:
            if data != allowed_installer[relative]:
                raise BundleIntegrityError(
                    f"installer-owned dependency metadata is invalid: {relative}"
                )
        else:
            raise BundleIntegrityError(
                f"installed dependency file is absent from trusted wheel: {relative}"
            )
        owned.add(relative)

    missing = sorted(set(expected) - owned)
    if missing:
        raise BundleIntegrityError(
            f"installed dependency omits trusted wheel members: {missing}"
        )
    _verify_dependency_wrappers(
        external,
        trusted,
        root=root,
        distributions=distributions,
    )
    return owned


def _installed_distribution_identity(distribution: Any) -> tuple[str, str]:
    try:
        raw_name = distribution.metadata["Name"]
    except KeyError:
        raw_name = ""
    name = _normalize_package_name(str(raw_name or ""))
    version = str(getattr(distribution, "version", "") or "")
    if not name or not version:
        raise BundleIntegrityError("installed distribution identity is incomplete")
    return name, version


def _verify_dependency_wrappers(
    external: dict[str, bytes],
    trusted: _TrustedDependencyWheel,
    *,
    root: Path,
    distributions: list[Any],
) -> None:
    observed_entry_points: set[str] = set()
    for record_name, data in external.items():
        filename = PurePosixPath(record_name).name
        if filename in trusted.wheel_scripts:
            if data != trusted.wheel_scripts[filename]:
                raise BundleIntegrityError(
                    f"installed dependency script diverges from trusted wheel: {filename}"
                )
            continue

        if os.name == "nt":
            if not filename.lower().endswith(".exe"):
                raise BundleIntegrityError(
                    f"dependency wrapper has an unsupported Windows name: {filename}"
                )
            entry_name = filename[:-4]
        else:
            if Path(filename).suffix:
                raise BundleIntegrityError(
                    f"dependency wrapper has an unsupported POSIX name: {filename}"
                )
            entry_name = filename
        lookup_name = entry_name
        if (
            trusted.name == "pip"
            and entry_name == f"pip{sys.version_info.major}.{sys.version_info.minor}"
        ):
            lookup_name = "pip3"
        entry = trusted.entry_points.get(lookup_name)
        if entry is None or entry_name in observed_entry_points:
            raise BundleIntegrityError(
                f"dependency wrapper is not uniquely bound to entry_points.txt: {filename}"
            )
        target, gui = entry
        _verify_entrypoint_wrapper(
            data,
            target=target,
            gui=gui,
            root=root,
            distributions=distributions,
        )
        observed_entry_points.add(entry_name)


def _verify_entrypoint_wrapper(
    data: bytes,
    *,
    target: str,
    gui: bool,
    root: Path,
    distributions: list[Any],
) -> None:
    expected_script = _entrypoint_script_bytes(target)
    executable = Path(sys.executable).resolve(strict=True)
    shebangs = {
        f"#!{executable}\n".encode(),
        f'#!"{executable}"\n'.encode(),
    }
    if os.name != "nt":
        if not any(data == shebang + expected_script for shebang in shebangs):
            raise BundleIntegrityError(
                "dependency console wrapper diverges from the strict POSIX projection"
            )
        return

    launcher = _trusted_distlib_launcher(
        gui=gui,
        root=root,
        distributions=distributions,
    )
    if not data.startswith(launcher):
        raise BundleIntegrityError(
            "dependency console wrapper has an untrusted Windows launcher"
        )
    remainder = data[len(launcher) :]
    shebang = next((value for value in shebangs if remainder.startswith(value)), None)
    if shebang is None:
        raise BundleIntegrityError(
            "dependency console wrapper targets an unexpected interpreter"
        )
    raw_zip = remainder[len(shebang) :]
    if (
        not raw_zip.startswith(b"PK\x03\x04")
        or len(raw_zip) < 22
        or raw_zip[-22:-18] != b"PK\x05\x06"
        or raw_zip[-2:] != b"\x00\x00"
    ):
        raise BundleIntegrityError(
            "dependency console wrapper has a noncanonical ZIP projection"
        )
    try:
        with zipfile.ZipFile(io.BytesIO(raw_zip)) as archive:
            infos = archive.infolist()
            if (
                len(infos) != 1
                or infos[0].filename != "__main__.py"
                or infos[0].flag_bits & 0x1
                or archive.read("__main__.py") != expected_script
            ):
                raise BundleIntegrityError(
                    "dependency console wrapper payload diverges from entry_points.txt"
                )
    except zipfile.BadZipFile as exc:
        raise BundleIntegrityError(
            "dependency console wrapper payload is not a ZIP archive"
        ) from exc


def _trusted_distlib_launcher(
    *,
    gui: bool,
    root: Path,
    distributions: list[Any],
) -> bytes:
    pip_distributions = [
        distribution
        for distribution in distributions
        if _installed_distribution_identity(distribution)[0] == "pip"
    ]
    if len(pip_distributions) != 1:
        raise BundleIntegrityError(
            "Windows dependency wrappers require one fresh-venv pip bootstrap"
        )
    machine = platform.machine().lower()
    architecture = (
        "64-arm"
        if "arm" in machine and struct.calcsize("P") == 8
        else ("64" if struct.calcsize("P") == 8 else "32")
    )
    prefix = "w" if gui else "t"
    launcher_name = f"{prefix}{architecture}.exe"
    launcher = root / "pip" / "_vendor" / "distlib" / launcher_name
    _reject_reparse_chain(launcher, anchor=root)
    try:
        data = _read_bound_regular_file(
            launcher,
            label="fresh-venv pip launcher",
            max_bytes=4 * 1024 * 1024,
        )
    except OSError as exc:
        raise BundleIntegrityError(
            f"fresh-venv pip launcher is unavailable: {launcher_name}"
        ) from exc
    if not data.startswith(b"MZ"):
        raise BundleIntegrityError("fresh-venv pip launcher is invalid")
    return data


def _entrypoint_script_bytes(target: str) -> bytes:
    module, separator, function = target.partition(":")
    if (
        separator != ":"
        or re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", module) is None
        or re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", function) is None
    ):
        raise BundleIntegrityError("entry_points.txt contains an unsafe target")
    import_name = function.split(".", 1)[0]
    return (
        "# -*- coding: utf-8 -*-\n"
        "import re\n"
        "import sys\n"
        f"from {module} import {import_name}\n"
        "if __name__ == '__main__':\n"
        "    sys.argv[0] = re.sub(r'(-script\\.pyw|\\.exe)?$', '', sys.argv[0])\n"
        f"    sys.exit({function}())\n"
    ).encode("utf-8")


def _verify_installed_record(distribution: Any, root: Path) -> set[str]:
    """Verify one installed distribution through its exact PEP 376 RECORD."""

    declared = getattr(distribution, "files", None)
    if declared is None:
        raise BundleIntegrityError(
            "installed distribution has no verifiable file inventory"
        )
    declared_names = [str(value).replace("\\", "/") for value in declared]
    record_names = [
        name
        for name in declared_names
        if PurePosixPath(name).name == "RECORD"
        and PurePosixPath(name).parent.name.endswith(".dist-info")
    ]
    if len(record_names) != 1:
        raise BundleIntegrityError(
            "installed distribution must declare exactly one dist-info/RECORD"
        )
    record_name = record_names[0]
    record_path, record_relative = _locate_installed_record_file(
        distribution,
        root,
        record_name,
        allow_external_script=False,
        script_names=set(),
    )
    if record_relative != record_name:
        raise BundleIntegrityError("installed RECORD path is not canonical")
    rows = _read_installed_record(record_path)
    if set(rows) != set(declared_names):
        missing = sorted(set(rows) - set(declared_names))
        extra = sorted(set(declared_names) - set(rows))
        raise BundleIntegrityError(
            "installed metadata file inventory diverges from RECORD; "
            f"missing={missing}, extra={extra}"
        )
    if rows.get(record_name) != ("", ""):
        raise BundleIntegrityError(
            "installed RECORD self-entry must have empty digest and size"
        )

    distribution_name, distribution_version = _installed_distribution_identity(
        distribution
    )

    located: dict[str, tuple[Path, str | None, bytes | None]] = {}
    entry_points_raw: bytes | None = None
    for name, (digest, size) in rows.items():
        if name == record_name:
            continue
        path, relative = _locate_installed_record_file(
            distribution,
            root,
            name,
            allow_external_script=True,
            script_names=None,
        )
        data = (
            None
            if not digest and not size
            else _read_bound_regular_file(
                path,
                label="installed dependency file",
                max_bytes=MAX_DEPENDENCY_MEMBER_BYTES,
            )
        )
        located[name] = (path, relative, data)
        if not digest and not size:
            continue
        if not digest or not size:
            raise BundleIntegrityError(
                f"installed RECORD digest/size is incomplete for {name}"
            )
        if data is None or digest != record_digest(data) or size != str(len(data)):
            raise BundleIntegrityError(
                f"installed RECORD hash/size mismatch for {name}"
            )
        if relative is not None:
            _verify_startup_hook(
                distribution_name,
                distribution_version,
                relative,
                data,
            )
        if name.endswith(".dist-info/entry_points.txt"):
            if entry_points_raw is not None:
                raise BundleIntegrityError(
                    "installed distribution declares multiple entry_points.txt files"
                )
            entry_points_raw = data

    script_names = (
        _script_entry_point_names(entry_points_raw)
        if entry_points_raw is not None
        else set()
    )
    if distribution_name == "pip" and "pip3" in script_names:
        # pip installs one interpreter-minor alias in addition to its declared
        # ``pip`` and ``pip3`` entry points.
        script_names.add(f"pip{sys.version_info.major}.{sys.version_info.minor}")
    script_names.update(
        REVIEWED_NATIVE_SCRIPT_WRAPPERS.get(
            (distribution_name, distribution_version), set()
        )
    )
    row_names = set(rows)
    for name, (digest, size) in rows.items():
        if name == record_name:
            continue
        _path, relative, _data = located[name]
        if not digest and not size:
            source_name = _generated_bytecode_source(name, row_names)
            if source_name is None:
                raise BundleIntegrityError(
                    f"installed RECORD entry lacks hash/size: {name}"
                )
            source_digest, source_size = rows[source_name]
            if not source_digest or not source_size:
                raise BundleIntegrityError(
                    f"installed bytecode source is not hash-bound: {name}"
                )
        if relative is None and not _is_generated_console_script(name, script_names):
            source_name = _generated_bytecode_source(name, row_names)
            if source_name is None or not _is_generated_console_script(
                source_name, script_names
            ):
                raise BundleIntegrityError(
                    f"installed RECORD path escapes site-packages: {name}"
                )

    return {
        relative for _path, relative, _data in located.values() if relative is not None
    } | {record_name}


def _read_installed_record(path: Path) -> dict[str, tuple[str, str]]:
    try:
        raw = _read_bound_regular_file(
            path,
            label="installed RECORD",
            max_bytes=16 * 1024 * 1024,
        ).decode("utf-8")
        parsed = csv.reader(io.StringIO(raw), strict=True)
        rows: dict[str, tuple[str, str]] = {}
        for index, row in enumerate(parsed, start=1):
            if len(row) != 3 or not _safe_installed_record_name(row[0]):
                raise BundleIntegrityError(f"invalid installed RECORD row {index}")
            if row[0] in rows:
                raise BundleIntegrityError(
                    f"duplicate installed RECORD member: {row[0]}"
                )
            rows[row[0]] = (row[1], row[2])
    except (OSError, UnicodeError, csv.Error) as exc:
        raise BundleIntegrityError("installed RECORD is unreadable") from exc
    if not rows:
        raise BundleIntegrityError("installed RECORD is empty")
    return rows


def _safe_installed_record_name(name: str) -> bool:
    if not name or "\x00" in name or "\\" in name or "\r" in name or "\n" in name:
        return False
    raw_parts = name.split("/")
    if any(part in {"", "."} for part in raw_parts):
        return False
    dotdot_count = 0
    for part in raw_parts:
        if part != "..":
            break
        dotdot_count += 1
    if ".." in raw_parts[dotdot_count:]:
        return False
    if dotdot_count and (
        len(raw_parts[dotdot_count:]) != 2
        or raw_parts[dotdot_count].lower() not in {"bin", "scripts"}
    ):
        return False
    path = PurePosixPath(name)
    if path.is_absolute() or path.as_posix() != name:
        return False
    return not bool(re.match(r"^[A-Za-z]:", name))


def _locate_installed_record_file(
    distribution: Any,
    root: Path,
    name: str,
    *,
    allow_external_script: bool,
    script_names: set[str] | None,
) -> tuple[Path, str | None]:
    if not _safe_installed_record_name(name):
        raise BundleIntegrityError(f"unsafe installed RECORD path: {name!r}")
    lexical = Path(str(distribution.locate_file(name)))
    parts = PurePosixPath(name).parts
    external = bool(parts and parts[0] == "..")
    if external:
        if not allow_external_script:
            raise BundleIntegrityError(
                f"installed RECORD path escapes site-packages: {name}"
            )
        scripts_root = _installed_scripts_root(root)
        filename = parts[-1]
        expected = scripts_root / filename
        canonical = os.path.relpath(expected, root).replace(os.sep, "/")
        if canonical != name:
            raise BundleIntegrityError(
                f"installed RECORD script path is not canonical: {name}"
            )
        anchor = scripts_root
    else:
        expected = root.joinpath(*parts)
        anchor = root
    lexical_absolute = Path(os.path.abspath(lexical))
    expected_absolute = Path(os.path.abspath(expected))
    if os.path.normcase(str(lexical_absolute)) != os.path.normcase(
        str(expected_absolute)
    ):
        raise BundleIntegrityError(
            f"installed RECORD path does not map canonically: {name}"
        )
    _reject_reparse_chain(expected_absolute, anchor=anchor)
    try:
        resolved = expected_absolute.resolve(strict=True)
    except OSError as exc:
        raise BundleIntegrityError(
            f"installed dependency file is missing: {name}"
        ) from exc
    if not resolved.is_file():
        raise BundleIntegrityError(f"installed RECORD member is not a file: {name}")
    try:
        return resolved, resolved.relative_to(root).as_posix()
    except ValueError:
        if not external:
            raise BundleIntegrityError(
                f"installed RECORD path escapes site-packages: {name}"
            )
        scripts_root = _installed_scripts_root(root)
        if scripts_root != resolved.parent and scripts_root not in resolved.parents:
            raise BundleIntegrityError(
                f"installed RECORD path escapes the environment scripts directory: {name}"
            )
        if script_names is not None and not _is_generated_console_script(
            name, script_names
        ):
            raise BundleIntegrityError(
                f"installed RECORD path is not an owned console wrapper: {name}"
            )
        return resolved, None


def _installed_scripts_root(site_packages: Path) -> Path:
    if site_packages.name.lower() != "site-packages":
        raise BundleIntegrityError("installed site-packages layout is unsupported")
    if site_packages.parent.name.lower() == "lib":
        candidate = site_packages.parent.parent / "Scripts"
    elif site_packages.parent.name.lower().startswith("python"):
        candidate = site_packages.parent.parent.parent / "bin"
    else:
        raise BundleIntegrityError("installed site-packages layout is unsupported")
    _reject_reparse(candidate, "installed environment scripts directory")
    try:
        return candidate.resolve(strict=True)
    except OSError as exc:
        raise BundleIntegrityError(
            "installed environment scripts directory is unavailable"
        ) from exc


def _generated_bytecode_source(name: str, expected_names: set[str]) -> str | None:
    path = PurePosixPath(name)
    if path.suffix != ".pyc" or "__pycache__" not in path.parts:
        return None
    cache_index = len(path.parts) - 2
    if cache_index < 0 or path.parts[cache_index] != "__pycache__":
        return None
    source_parent = PurePosixPath(*path.parts[:cache_index])
    possible_stem = path.name.removesuffix(".pyc")
    while "." in possible_stem:
        possible_stem = possible_stem.rsplit(".", 1)[0]
        source = (source_parent / f"{possible_stem}.py").as_posix()
        if source in expected_names:
            return source
    return None


def _verify_startup_hook(
    distribution_name: str,
    version: str,
    relative: str,
    data: bytes,
) -> None:
    _reject_locked_startup_member(relative)
    path = PurePosixPath(relative)
    if path.suffix.lower() != ".pth":
        return
    keys = (
        (distribution_name, version, relative),
        (distribution_name, "*", relative),
    )
    expected = next(
        (REVIEWED_STARTUP_HOOKS[key] for key in keys if key in REVIEWED_STARTUP_HOOKS),
        None,
    )
    if expected is None or expected != (sha256_hex(data), len(data)):
        raise BundleIntegrityError(
            f"installed startup hook is not explicitly reviewed: {relative}"
        )


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
    if index.is_symlink() or not index.is_file():
        raise BundleIntegrityError(f"source file index is missing: {index}")
    resolved_index = index.resolve()
    if plugin_root != resolved_index and plugin_root not in resolved_index.parents:
        raise BundleIntegrityError("source file index escapes plugin root")
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


def source_manifest_bytes(
    source_files: dict[str, bytes],
    version: str,
    *,
    security_inputs: dict[str, bytes],
) -> bytes:
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
        "security_inputs": [
            {
                "path": path,
                "sha256": sha256_hex(data),
                "size": len(data),
            }
            for path, data in sorted(security_inputs.items())
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


def _expected_metadata_contract(plugin_root: Path) -> dict[str, object]:
    pyproject = plugin_root / "harness" / "pyproject.toml"
    try:
        project = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, KeyError) as exc:
        raise BundleIntegrityError(
            "reviewed source project metadata is unavailable"
        ) from exc
    dependencies = [str(value) for value in project.get("dependencies", [])]
    provides_extra: list[str] = []
    for extra, values in sorted(project.get("optional-dependencies", {}).items()):
        provides_extra.append(str(extra))
        dependencies.extend(f'{value}; extra == "{extra}"' for value in values)
    return {
        "Name": str(project.get("name") or ""),
        "Version": str(project.get("version") or ""),
        "Summary": str(project.get("description") or ""),
        "Requires-Python": str(project.get("requires-python") or ""),
        "Requires-Dist": tuple(dependencies),
        "Provides-Extra": tuple(provides_extra),
    }


def _verify_metadata_contract(raw: bytes, plugin_root: Path) -> None:
    message = BytesParser().parsebytes(raw)
    expected = _expected_metadata_contract(plugin_root)
    for key in ("Name", "Version", "Summary", "Requires-Python"):
        if str(message.get(key) or "") != expected[key]:
            raise BundleIntegrityError(
                f"wheel METADATA diverges from reviewed source for {key}"
            )
    for key in ("Requires-Dist", "Provides-Extra"):
        actual = tuple(str(value) for value in (message.get_all(key) or ()))
        if actual != expected[key]:
            raise BundleIntegrityError(
                f"wheel METADATA diverges from reviewed source for {key}"
            )


def _canonical_metadata_bytes(plugin_root: Path) -> bytes:
    try:
        project = tomllib.loads(
            (plugin_root / "harness" / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]
        readme = (plugin_root / "harness" / "README.md").read_text(encoding="utf-8")
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, KeyError) as exc:
        raise BundleIntegrityError(
            "reviewed source metadata inputs are unavailable"
        ) from exc
    lines = [
        "Metadata-Version: 2.1",
        f"Name: {project['name']}",
        f"Version: {project['version']}",
        f"Summary: {project['description']}",
        f"Requires-Python: {project['requires-python']}",
        "Description-Content-Type: text/markdown",
    ]
    for dependency in project.get("dependencies", []):
        lines.append(f"Requires-Dist: {dependency}")
    for extra, dependencies in sorted(project.get("optional-dependencies", {}).items()):
        lines.append(f"Provides-Extra: {extra}")
        for dependency in dependencies:
            lines.append(f'Requires-Dist: {dependency}; extra == "{extra}"')
    lines.extend(["", readme, ""])
    return "\n".join(lines).encode("utf-8")


def _verify_canonical_archive_layout(
    archive: zipfile.ZipFile, infos: list[zipfile.ZipInfo]
) -> None:
    if archive.comment:
        raise BundleIntegrityError("wheel archive comment must be empty")
    if [info.filename for info in infos] != sorted(info.filename for info in infos):
        raise BundleIntegrityError("wheel members are not in canonical order")
    if sum(info.file_size for info in infos) > MAX_WHEEL_BYTES:
        raise BundleIntegrityError("wheel uncompressed content exceeds the size limit")
    for info in infos:
        if info.file_size > MAX_WHEEL_MEMBER_BYTES:
            raise BundleIntegrityError(
                f"wheel member exceeds the size limit: {info.filename}"
            )
        if (
            info.date_time != FIXED_ZIP_TIME
            or info.create_system != 3
            or info.compress_type != zipfile.ZIP_STORED
            or (info.external_attr >> 16) != 0o100644
            or info.flag_bits != 0
            or info.extra
            or info.comment
        ):
            raise BundleIntegrityError(
                f"wheel member metadata is not canonical: {info.filename}"
            )


def _canonical_record_bytes(
    archive: zipfile.ZipFile, names: set[str], record_name: str
) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    for name in sorted(names - {record_name}):
        data = archive.read(name)
        writer.writerow([name, record_digest(data), len(data)])
    writer.writerow([record_name, "", ""])
    return stream.getvalue().encode("utf-8")


def _canonical_archive_bytes(archive: zipfile.ZipFile, names: set[str]) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as canonical:
        for name in sorted(names):
            info = zipfile.ZipInfo(name, FIXED_ZIP_TIME)
            info.create_system = 3
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = 0o100644 << 16
            canonical.writestr(
                info,
                archive.read(name),
                compress_type=zipfile.ZIP_STORED,
            )
    return stream.getvalue()


def _verify_generated_member_contract(
    archive: zipfile.ZipFile,
    *,
    canonical_dist_info: str,
    manifest_members: set[str],
    plugin_root: Path | None,
) -> None:
    wheel_name = f"{canonical_dist_info}/WHEEL"
    entry_points_name = f"{canonical_dist_info}/entry_points.txt"
    top_level_name = f"{canonical_dist_info}/top_level.txt"
    license_name = f"{canonical_dist_info}/licenses/LICENSE"
    metadata_name = f"{canonical_dist_info}/METADATA"
    if archive.read(wheel_name) != CANONICAL_WHEEL_METADATA:
        raise BundleIntegrityError("wheel WHEEL metadata is not canonical")
    if archive.read(entry_points_name) != CANONICAL_ENTRY_POINTS:
        raise BundleIntegrityError(
            "wheel entry_points.txt diverges from the reviewed console scripts"
        )
    top_levels = sorted(
        {path.split("/", 1)[0] for path in manifest_members if "/" in path}
    )
    expected_top_level = ("\n".join(top_levels) + "\n").encode("utf-8")
    if archive.read(top_level_name) != expected_top_level:
        raise BundleIntegrityError("wheel top_level.txt is not canonical")
    if plugin_root is None:
        return
    try:
        license_text = (plugin_root / "LICENSE").read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise BundleIntegrityError("reviewed LICENSE is unavailable") from exc
    expected_license = (
        license_text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    )
    if archive.read(license_name) != expected_license:
        raise BundleIntegrityError("wheel LICENSE diverges from reviewed source")
    expected_metadata = _canonical_metadata_bytes(plugin_root)
    if archive.read(metadata_name) != expected_metadata:
        raise BundleIntegrityError("wheel METADATA diverges from reviewed source")


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
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        payload = json.loads(raw, object_pairs_hook=_strict_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BundleIntegrityError(f"invalid source manifest: {exc}") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "project",
        "version",
        "files",
        "security_inputs",
    }:
        raise BundleIntegrityError("source manifest shape is invalid")
    if payload.get("schema_version") != SOURCE_MANIFEST_SCHEMA:
        raise BundleIntegrityError("unsupported source manifest schema")
    if payload.get("project") != "fusion-agent-harness":
        raise BundleIntegrityError("source manifest project identity is invalid")
    version = str(payload.get("version") or "")
    entries = payload.get("files")
    if not isinstance(entries, list) or not entries:
        raise BundleIntegrityError("source manifest has no files")
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256", "size"}:
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
            or isinstance(size, bool)
            or size < 0
        ):
            raise BundleIntegrityError(
                f"invalid source manifest digest/size for {path}"
            )
        data = archive.read(path)
        if sha256_hex(data) != digest or len(data) != size:
            raise BundleIntegrityError(f"source manifest mismatch for {path}")
        seen.add(path)
    security_inputs = payload.get("security_inputs")
    if not isinstance(security_inputs, list) or not security_inputs:
        raise BundleIntegrityError("source manifest has no security inputs")
    security_seen: set[str] = set()
    for entry in security_inputs:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256", "size"}:
            raise BundleIntegrityError("security input entry must be an object")
        path = str(entry.get("path") or "")
        digest = str(entry.get("sha256") or "")
        size = entry.get("size")
        if (
            not _safe_member_name(path)
            or path in security_seen
            or not SHA256_PATTERN.fullmatch(digest)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
        ):
            raise BundleIntegrityError(
                f"invalid or duplicate security input manifest entry: {path!r}"
            )
        security_seen.add(path)
    if security_seen != set(SECURITY_INPUT_PATHS):
        raise BundleIntegrityError(
            "source manifest security inputs diverge from the canonical allowlist"
        )
    return version, entries, security_inputs


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise BundleIntegrityError(f"duplicate JSON key: {key}")
        payload[key] = value
    return payload


def verify_wheel(
    wheel_path: Path | str,
    *,
    plugin_root: Path | str | None = None,
    expected_version: str | None = None,
    require_source_parity: bool = False,
) -> BundleIntegrityReport:
    """Verify member safety, exact RECORD coverage, metadata and source parity."""

    supplied_wheel = Path(wheel_path)
    if supplied_wheel.is_symlink():
        raise BundleIntegrityError("wheel path must not be a symlink")
    wheel = supplied_wheel.resolve()
    if not wheel.is_file():
        raise BundleIntegrityError(f"wheel does not exist: {wheel}")
    if wheel.stat().st_size > MAX_WHEEL_BYTES:
        raise BundleIntegrityError("wheel exceeds the archive size limit")
    try:
        with zipfile.ZipFile(wheel) as archive:
            infos = archive.infolist()
            names_list = [info.filename for info in infos]
            if len(names_list) != len(set(names_list)):
                raise BundleIntegrityError("wheel contains duplicate member names")
            unsafe = sorted(name for name in names_list if not _safe_member_name(name))
            if unsafe:
                raise BundleIntegrityError(f"wheel contains unsafe members: {unsafe}")
            _verify_canonical_archive_layout(archive, infos)
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
            if archive.read(record_name) != _canonical_record_bytes(
                archive, names, record_name
            ):
                raise BundleIntegrityError(
                    "wheel RECORD serialization is not canonical"
                )

            metadata_raw = archive.read(metadata_name)
            project_name = _metadata_value(metadata_raw, "Name")
            version = _metadata_value(metadata_raw, "Version")
            if project_name.replace("_", "-").lower() != "fusion-agent-harness":
                raise BundleIntegrityError(f"unexpected project name: {project_name}")
            if expected_version is not None and version != expected_version:
                raise BundleIntegrityError(
                    f"wheel version mismatch: expected {expected_version}, found {version}"
                )
            resolved_root = (
                Path(plugin_root).resolve() if plugin_root is not None else None
            )
            if resolved_root is not None:
                verify_dependency_locks(resolved_root)
                _verify_metadata_contract(metadata_raw, resolved_root)
            manifest_version, entries, security_inputs = _read_source_manifest(
                archive.read(manifest_name), names, archive
            )
            if manifest_version != version:
                raise BundleIntegrityError(
                    f"source manifest version {manifest_version!r} does not match wheel {version!r}"
                )
            if resolved_root is not None:
                actual_security_inputs = collect_security_inputs(resolved_root)
                expected_security_inputs = {
                    str(entry["path"]): entry for entry in security_inputs
                }
                for path, data in actual_security_inputs.items():
                    entry = expected_security_inputs[path]
                    if (
                        sha256_hex(data) != entry["sha256"]
                        or len(data) != entry["size"]
                    ):
                        raise BundleIntegrityError(
                            f"checkout security input mismatch for {path}"
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
            _verify_generated_member_contract(
                archive,
                canonical_dist_info=canonical_dist_info,
                manifest_members=manifest_members,
                plugin_root=resolved_root,
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
                expected_manifest = source_manifest_bytes(
                    source_files,
                    version,
                    security_inputs=collect_security_inputs(Path(plugin_root)),
                )
                if archive.read(manifest_name) != expected_manifest:
                    raise BundleIntegrityError(
                        "source manifest serialization diverges from reviewed checkout"
                    )
            if wheel.read_bytes() != _canonical_archive_bytes(archive, names):
                raise BundleIntegrityError(
                    "wheel bytes diverge from the canonical deterministic archive"
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
    site_packages: Path | str | None = None,
) -> None:
    """Compare installed files with the already verified wheel without importing it."""

    wheel = Path(wheel_path).resolve()
    if site_packages is None:
        try:
            dist = metadata.distribution(distribution_name)
        except metadata.PackageNotFoundError as exc:
            raise BundleIntegrityError(
                f"installed distribution is missing: {distribution_name}"
            ) from exc
    else:
        expected_name = _normalize_package_name(distribution_name)
        matches = []
        for candidate in _installed_distributions(site_packages):
            try:
                candidate_name = candidate.metadata["Name"]
            except KeyError:
                candidate_name = ""
            if _normalize_package_name(str(candidate_name or "")) == expected_name:
                matches.append(candidate)
        if len(matches) != 1:
            raise BundleIntegrityError(
                f"installed distribution must be unique: {distribution_name}"
            )
        dist = matches[0]
    with zipfile.ZipFile(wheel) as archive:
        record_name = _single_member(archive.namelist(), ".dist-info/RECORD")
        names = set(archive.namelist())
        for name in archive.namelist():
            if name == record_name:
                continue
            installed = Path(str(dist.locate_file(name)))
            if not installed.is_file():
                raise BundleIntegrityError(f"installed wheel member is missing: {name}")
            if installed.read_bytes() != archive.read(name):
                raise BundleIntegrityError(f"installed wheel member mismatch: {name}")

        top_level_name = _single_member(names, ".dist-info/top_level.txt")
        top_levels = {
            value.strip()
            for value in archive.read(top_level_name).decode("utf-8").splitlines()
            if value.strip()
        }
        entry_points_name = _single_member(names, ".dist-info/entry_points.txt")
        console_scripts = _console_script_names(archive.read(entry_points_name))
        dist_info = record_name.rsplit("/", 1)[0]
        allowed_generated = {
            f"{dist_info}/{name}" for name in INSTALLER_OWNED_DIST_INFO_MEMBERS
        }
        unexpected: set[str] = set(
            _unexpected_first_party_files(
                dist,
                names=names,
                top_levels=top_levels,
                dist_info=dist_info,
                allowed_generated=allowed_generated,
            )
        )
        declared = getattr(dist, "files", None)
        if declared is not None:
            installed_names = {str(value).replace("\\", "/") for value in declared}
            unexpected.update(
                name
                for name in installed_names - names - allowed_generated
                if not _is_generated_console_script(name, console_scripts)
            )
        if unexpected:
            raise BundleIntegrityError(
                "installed distribution contains unexpected first-party files: "
                f"{sorted(unexpected)}"
            )


def _is_generated_bytecode(name: str, expected_names: set[str]) -> bool:
    return _generated_bytecode_source(name, expected_names) is not None


def _console_script_names(raw: bytes) -> set[str]:
    names = _script_entry_point_names(raw, sections={"console_scripts"})
    if not names:
        raise BundleIntegrityError("entry_points.txt has no console scripts")
    return names


def _script_entry_point_names(
    raw: bytes,
    *,
    sections: set[str] | None = None,
) -> set[str]:
    accepted = sections or {"console_scripts", "gui_scripts"}
    return {
        name
        for name, (_target, gui) in _script_entry_points(raw).items()
        if (gui and "gui_scripts" in accepted)
        or (not gui and "console_scripts" in accepted)
    }


def _script_entry_points(raw: bytes) -> dict[str, tuple[str, bool]]:
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeError as exc:
        raise BundleIntegrityError("entry_points.txt is not UTF-8") from exc
    entries: dict[str, tuple[str, bool]] = {}
    active: bool | None = None
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            active = (
                False
                if section == "console_scripts"
                else True
                if section == "gui_scripts"
                else None
            )
            continue
        if active is None:
            continue
        name, separator, target = line.partition("=")
        name = name.strip()
        target = target.strip()
        target_base, extras_separator, extras = target.partition(" ")
        if (
            extras_separator
            and re.fullmatch(
                r"\[[A-Za-z0-9_.-]+(?:\s*,\s*[A-Za-z0-9_.-]+)*\]",
                extras.strip(),
            )
            is None
        ):
            raise BundleIntegrityError("entry_points.txt contains invalid extras")
        if (
            separator != "="
            or not re.fullmatch(r"[A-Za-z0-9_.-]+", name)
            or re.fullmatch(
                r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*",
                target_base,
            )
            is None
        ):
            raise BundleIntegrityError("entry_points.txt contains an invalid script")
        if name in entries:
            raise BundleIntegrityError("entry_points.txt contains a duplicate script")
        entries[name] = (target_base, active)
    return entries


def _is_generated_console_script(name: str, scripts: set[str]) -> bool:
    parts = PurePosixPath(name).parts
    if len(parts) < 2 or not all(part == ".." for part in parts[:-2]):
        return False
    directory, filename = parts[-2:]
    if directory.lower() not in {"bin", "scripts"}:
        return False
    allowed_names = {
        generated
        for script in scripts
        for generated in (
            script,
            f"{script}.exe",
            f"{script}-script.py",
            f"{script}.py",
        )
    }
    return filename in allowed_names


def _unexpected_first_party_files(
    dist: Any,
    *,
    names: set[str],
    top_levels: set[str],
    dist_info: str,
    allowed_generated: set[str],
) -> list[str]:
    observed: set[str] = set()
    for relative_root in sorted(top_levels | {dist_info}):
        root = Path(str(dist.locate_file(relative_root)))
        if root.is_file():
            observed.add(relative_root)
            continue
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.is_file():
                observed.add(
                    PurePosixPath(
                        relative_root, *path.relative_to(root).parts
                    ).as_posix()
                )
    return sorted(name for name in observed - names - allowed_generated)
