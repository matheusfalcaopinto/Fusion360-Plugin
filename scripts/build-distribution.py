"""Build the canonical harness source into a deterministic bundled wheel."""

from __future__ import annotations

import csv
import hashlib
import io
import os
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path

try:
    from scripts.bundle_integrity import (
        SOURCE_MANIFEST_NAME,
        collect_source_files,
        record_digest,
        source_manifest_bytes,
        validate_source_file_index,
        verify_wheel,
    )
except ModuleNotFoundError:  # Executed as ``python scripts/build-distribution.py``.
    from bundle_integrity import (  # type: ignore[no-redef]
        SOURCE_MANIFEST_NAME,
        collect_source_files,
        record_digest,
        source_manifest_bytes,
        validate_source_file_index,
        verify_wheel,
    )


ROOT = Path(__file__).resolve().parents[1]
HARNESS_ROOT = ROOT / "harness"
OUTPUT_ROOT = ROOT / "wheels"
FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)


def _record_digest(data: bytes) -> str:
    return record_digest(data)


def _metadata(project: dict) -> bytes:
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
    lines.extend(["", (HARNESS_ROOT / "README.md").read_text(encoding="utf-8"), ""])
    return "\n".join(lines).encode("utf-8")


def _source_files() -> dict[str, bytes]:
    return collect_source_files(ROOT)


def _wheel_files(project: dict) -> tuple[dict[str, bytes], str]:
    version = project["version"]
    dist_info = f"fusion_agent_harness-{version}.dist-info"
    source_files = _source_files()
    files = dict(source_files)
    files[f"{dist_info}/{SOURCE_MANIFEST_NAME}"] = source_manifest_bytes(
        source_files, version
    )
    files[f"{dist_info}/METADATA"] = _metadata(project)
    files[f"{dist_info}/WHEEL"] = (
        "Wheel-Version: 1.0\n"
        "Generator: fusion-agent-codex deterministic builder\n"
        "Root-Is-Purelib: true\n"
        "Tag: py3-none-any\n"
    ).encode("utf-8")
    files[f"{dist_info}/entry_points.txt"] = (
        "[console_scripts]\n"
        "fusion-agent = cli.main:app\n"
        "fusion-agent-mcp = fusion_agent_mcp.server:main\n"
    ).encode("utf-8")
    top_levels = sorted(
        {
            path.split("/", 1)[0]
            for path in files
            if "/" in path and ".dist-info/" not in path
        }
    )
    files[f"{dist_info}/top_level.txt"] = ("\n".join(top_levels) + "\n").encode("utf-8")
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    files[f"{dist_info}/licenses/LICENSE"] = (
        license_text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    )
    return files, dist_info


def _record(files: dict[str, bytes], record_path: str) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    for path in sorted(files):
        data = files[path]
        writer.writerow([path, _record_digest(data), len(data)])
    writer.writerow([record_path, "", ""])
    return stream.getvalue().encode("utf-8")


def _write_member(archive: zipfile.ZipFile, path: str, data: bytes) -> None:
    info = zipfile.ZipInfo(path, FIXED_ZIP_TIME)
    info.create_system = 3
    # Stored members avoid zlib-version variance across Windows/macOS/Linux;
    # wheel size is secondary to byte-identical release provenance.
    info.compress_type = zipfile.ZIP_STORED
    info.external_attr = 0o100644 << 16
    archive.writestr(info, data, compress_type=zipfile.ZIP_STORED)


def build() -> Path:
    validate_source_file_index(ROOT)
    config = tomllib.loads(
        (HARNESS_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    project = config["project"]
    version = project["version"]
    filename = f"fusion_agent_harness-{version}-py3-none-any.whl"
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

    files, dist_info = _wheel_files(project)
    record_path = f"{dist_info}/RECORD"
    files[record_path] = _record(files, record_path)
    target = OUTPUT_ROOT / filename
    with tempfile.NamedTemporaryFile(
        dir=OUTPUT_ROOT,
        prefix=f".{filename}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
    try:
        with zipfile.ZipFile(temporary, "w") as archive:
            for path in sorted(files):
                _write_member(archive, path, files[path])
        validate(temporary, version)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    for previous in OUTPUT_ROOT.glob("fusion_agent_harness-*.whl"):
        if previous != target:
            previous.unlink()
    return target


def validate(wheel_path: Path, version: str) -> None:
    verify_wheel(
        wheel_path,
        plugin_root=ROOT,
        expected_version=version,
        require_source_parity=True,
    )
    required = {
        "agent_core/__init__.py",
        "benchmark/__init__.py",
        "fusion_agent_mcp/server.py",
        "fusion_agent_mcp/runtime.py",
        "fusion_agent_mcp/benchmark_bridge.py",
        "agent_core/fast_path.py",
        "benchmark/runner.py",
        "fusion_mcp_adapter/real_client.py",
        f"fusion_agent_harness-{version}.dist-info/RECORD",
    }
    with zipfile.ZipFile(wheel_path) as archive:
        names = set(archive.namelist())
        missing = sorted(required - names)
        if missing:
            raise RuntimeError(f"wheel missing required files: {missing}")
        record_name = f"fusion_agent_harness-{version}.dist-info/RECORD"
        rows = list(csv.reader(io.StringIO(archive.read(record_name).decode("utf-8"))))
        recorded = {row[0]: row for row in rows}
        for name in sorted(names - {record_name}):
            row = recorded.get(name)
            if not row:
                raise RuntimeError(f"wheel RECORD missing {name}")
            data = archive.read(name)
            if row[1] != _record_digest(data) or row[2] != str(len(data)):
                raise RuntimeError(f"wheel RECORD mismatch for {name}")

    with tempfile.TemporaryDirectory(prefix="fusion-agent-wheel-") as temporary:
        with zipfile.ZipFile(wheel_path) as archive:
            archive.extractall(temporary)
        sys.path.insert(0, temporary)
        try:
            import fusion_agent_mcp.server as server

            definitions = server.list_tool_definitions("all")
            normal_definitions = server.list_tool_definitions("normal")
            if len(definitions) != 35:
                raise RuntimeError(
                    f"installed wheel all profile must expose exactly 35 tools, found {len(definitions)}"
                )
            if len(normal_definitions) != 12:
                raise RuntimeError(
                    f"installed wheel normal profile must expose exactly 12 tools, found {len(normal_definitions)}"
                )
            if any(not tool.name.startswith("fusion_agent_") for tool in definitions):
                raise RuntimeError(
                    "installed wheel exposes a non-fusion_agent public tool"
                )
            if any(tool.outputSchema is None for tool in definitions):
                raise RuntimeError(
                    "installed wheel contains a tool without output schema"
                )
        finally:
            sys.path.remove(temporary)


def main() -> int:
    target = build()
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    print(f"wheel={target}")
    print(f"sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
