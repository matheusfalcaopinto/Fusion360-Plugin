from __future__ import annotations

import csv
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest

from scripts.bundle_integrity import (
    BundleIntegrityError,
    collect_source_files,
    record_digest,
    validate_source_file_index,
    verify_wheel,
)
from scripts.verify_installation_parity import (
    InstallationParityError,
    verify_installation_parity,
)


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def _workflow_security_violations(text: str) -> list[str]:
    violations: list[str] = []
    uses_pattern = re.compile(r"(?m)^\s*-?\s*uses:\s*([^\s#]+)")
    for reference in uses_pattern.findall(text):
        if not re.fullmatch(r"[^@]+@[0-9a-f]{40}", reference):
            violations.append(f"mutable_action:{reference}")
    if re.search(r"(?m)^permissions:\r?\n\s{2}contents:\s*write\s*$", text):
        violations.append("top_level_contents_write")
    return violations


def _build_wheel() -> Path:
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "build-distribution.py")],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    wheels = list((ROOT / "wheels").glob("fusion_agent_harness-*.whl"))
    assert len(wheels) == 1
    return wheels[0]


def _load_ci_release_gate() -> object:
    script = ROOT / "scripts" / "check-ci-release-gate.py"
    spec = importlib.util.spec_from_file_location("check_ci_release_gate", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rewrite_wheel(
    source: Path,
    target: Path,
    mutate: Callable[[dict[str, bytes]], None],
) -> None:
    with zipfile.ZipFile(source) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    mutate(files)
    with zipfile.ZipFile(target, "w") as archive:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data, compress_type=zipfile.ZIP_STORED)


def _refresh_record(files: dict[str, bytes]) -> None:
    record_name = next(name for name in files if name.endswith(".dist-info/RECORD"))
    rows = list(csv.reader(io.StringIO(files[record_name].decode("utf-8"))))
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    for name, digest, size in rows:
        if name != record_name:
            data = files[name]
            digest, size = record_digest(data), str(len(data))
        writer.writerow([name, digest, size])
    files[record_name] = output.getvalue().encode("utf-8")


def _parity_fixture(tmp_path: Path) -> tuple[Path, Path]:
    wheel = _build_wheel()
    source = tmp_path / "personal-source"
    cache = tmp_path / "codex-cache"
    for root in (source, cache):
        for relative in ("harness/apps", "harness/packages", "skills"):
            shutil.copytree(ROOT / relative, root / relative)
        (root / "harness" / "source-files.txt").parent.mkdir(
            parents=True, exist_ok=True
        )
        shutil.copy2(
            ROOT / "harness" / "source-files.txt",
            root / "harness" / "source-files.txt",
        )
        shutil.copy2(
            ROOT / "harness" / "pyproject.toml",
            root / "harness" / "pyproject.toml",
        )
        shutil.copy2(
            ROOT / "harness" / "README.md",
            root / "harness" / "README.md",
        )
        shutil.copy2(
            ROOT / "harness" / "uv.lock",
            root / "harness" / "uv.lock",
        )
        shutil.copytree(
            ROOT / "harness" / "requirements",
            root / "harness" / "requirements",
        )
        shutil.copy2(ROOT / "LICENSE", root / "LICENSE")
        shutil.copy2(ROOT / ".gitattributes", root / ".gitattributes")
        (root / ".codex-plugin").mkdir(parents=True)
        manifest = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text())
        manifest["version"] = "0.4.1+codex.20260716120000"
        (root / ".codex-plugin" / "plugin.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        (root / "wheels").mkdir()
        shutil.copy2(wheel, root / "wheels" / wheel.name)
        for relative in (
            "scripts/build-distribution.py",
            "scripts/fusion_agent_codex_mcp_launcher.py",
            "scripts/preinstall_verify.py",
            "scripts/bundle_integrity.py",
            "scripts/check-ci-release-gate.py",
            "scripts/configure_mcp.py",
            "scripts/run-isolated-pip.py",
            "scripts/setup.ps1",
            "scripts/setup.sh",
            "scripts/validate_plugin.py",
            "scripts/verify_installation_parity.py",
        ):
            destination = root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, destination)
    source_mcp = {
        "mcpServers": {
            "fusion_agent": {
                "command": "python",
                "args": ["-I", "-B", "scripts/fusion_agent_codex_mcp_launcher.py"],
                "env": {
                    "FUSION_AGENT_TOOL_PROFILE": "normal",
                    "FUSION_AGENT_BACKEND": "autodesk_http",
                },
            }
        }
    }
    (source / ".mcp.json").write_text(json.dumps(source_mcp), encoding="utf-8")
    runtime = _parity_runtime(source)
    cache_mcp = json.loads(json.dumps(source_mcp))
    cache_server = cache_mcp["mcpServers"]["fusion_agent"]
    cache_server["command"] = str(runtime)
    cache_server["args"] = [
        "-I",
        "-B",
        str(source / "scripts" / "fusion_agent_codex_mcp_launcher.py"),
    ]
    (cache / ".mcp.json").write_text(json.dumps(cache_mcp), encoding="utf-8")
    return source, cache


def _parity_runtime(source: Path) -> Path:
    runtime = (
        source / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    )
    runtime.parent.mkdir(parents=True, exist_ok=True)
    if not runtime.exists():
        runtime.write_bytes(b"runtime placeholder")
    return runtime


def test_bundled_wheel_has_exact_record_and_source_manifest() -> None:
    wheel = _build_wheel()

    report = verify_wheel(wheel, plugin_root=ROOT, require_source_parity=True)

    assert report.project_name == "fusion-agent-harness"
    assert report.version == "0.4.1"
    assert report.source_file_count > 0
    with zipfile.ZipFile(wheel) as archive:
        infos = archive.infolist()
        assert [info.filename for info in infos] == sorted(
            info.filename for info in infos
        )
        assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in infos)
        assert all(info.create_system == 3 for info in infos)
        assert all(info.compress_type == zipfile.ZIP_STORED for info in infos)
        assert all((info.external_attr >> 16) == 0o100644 for info in infos)
        manifest_name = next(
            name
            for name in archive.namelist()
            if name.endswith(".dist-info/SOURCE-MANIFEST.json")
        )
        manifest = json.loads(archive.read(manifest_name))
        paths = {entry["path"] for entry in manifest["files"]}
        assert not any("duct_" in path or "print_oriented" in path for path in paths)
        assert {entry["path"] for entry in manifest["security_inputs"]} == {
            ".gitattributes",
            "harness/pyproject.toml",
            "harness/requirements/build.in",
            "harness/requirements/build.lock",
            "harness/requirements/faust.lock",
            "harness/requirements/quality.lock",
            "harness/requirements/runtime.lock",
            "harness/requirements/test.lock",
            "harness/uv.lock",
        }


def test_wheel_build_validates_same_directory_temporary_before_atomic_replace() -> None:
    source = (ROOT / "scripts" / "build-distribution.py").read_text(encoding="utf-8")

    assert "dir=OUTPUT_ROOT" in source
    assert source.index("validate(temporary, version)") < source.index(
        "os.replace(temporary, target)"
    )


def test_wheel_verifier_rejects_unrecorded_and_unsafe_members(tmp_path: Path) -> None:
    wheel = _build_wheel()
    tampered = tmp_path / wheel.name
    shutil.copy2(wheel, tampered)
    with zipfile.ZipFile(tampered, "a") as archive:
        archive.writestr("../escape.py", b"sentinel")

    with pytest.raises(BundleIntegrityError, match="unsafe|RECORD|extra"):
        verify_wheel(tampered, plugin_root=ROOT)


def test_wheel_verifier_rejects_duplicate_and_safe_extra_members(
    tmp_path: Path,
) -> None:
    wheel = _build_wheel()
    duplicate = tmp_path / "duplicate.whl"
    shutil.copy2(wheel, duplicate)
    with zipfile.ZipFile(duplicate, "a") as archive:
        first = archive.namelist()[0]
        archive.writestr(first, archive.read(first))
    with pytest.raises(BundleIntegrityError, match="duplicate"):
        verify_wheel(duplicate)

    extra = tmp_path / "extra.whl"
    shutil.copy2(wheel, extra)
    with zipfile.ZipFile(extra, "a") as archive:
        archive.writestr("safe-extra.txt", b"not recorded")
    with pytest.raises(
        BundleIntegrityError,
        match="canonical order|RECORD.*bijective|metadata.*canonical",
    ):
        verify_wheel(extra)

    recorded_extra = tmp_path / "recorded-extra.whl"

    def add_recorded_extra(files: dict[str, bytes]) -> None:
        member_name = "unmanifested_payload.py"
        files[member_name] = b"PAYLOAD = True\n"
        record_name = next(name for name in files if name.endswith(".dist-info/RECORD"))
        rows = list(csv.reader(io.StringIO(files[record_name].decode("utf-8"))))
        rows.insert(
            -1,
            [
                member_name,
                record_digest(files[member_name]),
                str(len(files[member_name])),
            ],
        )
        output = io.StringIO(newline="")
        csv.writer(output, lineterminator="\n").writerows(rows)
        files[record_name] = output.getvalue().encode("utf-8")

    _rewrite_wheel(wheel, recorded_extra, add_recorded_extra)
    with pytest.raises(
        BundleIntegrityError,
        match="RECORD serialization.*canonical|allowlist.*extra",
    ):
        verify_wheel(recorded_extra)


def test_wheel_verifier_rejects_incomplete_record_metadata_and_source_drift(
    tmp_path: Path,
) -> None:
    wheel = _build_wheel()

    incomplete = tmp_path / "incomplete-record.whl"

    def remove_record_row(files: dict[str, bytes]) -> None:
        record_name = next(name for name in files if name.endswith(".dist-info/RECORD"))
        rows = list(csv.reader(io.StringIO(files[record_name].decode("utf-8"))))
        omitted = next(name for name, _digest, _size in rows if name.endswith(".py"))
        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\n")
        writer.writerows(row for row in rows if row[0] != omitted)
        files[record_name] = output.getvalue().encode("utf-8")

    _rewrite_wheel(wheel, incomplete, remove_record_row)
    with pytest.raises(BundleIntegrityError, match="RECORD.*bijective"):
        verify_wheel(incomplete)

    metadata_drift = tmp_path / "metadata-drift.whl"

    def change_metadata(files: dict[str, bytes]) -> None:
        metadata_name = next(
            name for name in files if name.endswith(".dist-info/METADATA")
        )
        files[metadata_name] = files[metadata_name].replace(
            b"Version: 0.4.1", b"Version: 9.9.9", 1
        )
        _refresh_record(files)

    _rewrite_wheel(wheel, metadata_drift, change_metadata)
    with pytest.raises(BundleIntegrityError, match="version"):
        verify_wheel(metadata_drift, expected_version="0.4.1")

    source_drift = tmp_path / "source-drift.whl"

    def change_source(files: dict[str, bytes]) -> None:
        source_name = next(name for name in files if name.endswith("authority.py"))
        files[source_name] += b"\n# unmanifested drift\n"
        _refresh_record(files)

    _rewrite_wheel(wheel, source_drift, change_source)
    with pytest.raises(BundleIntegrityError, match="source manifest mismatch"):
        verify_wheel(source_drift)


def test_setup_verifies_bundle_before_venv_or_pip_install() -> None:
    powershell = (ROOT / "scripts" / "setup.ps1").read_text(encoding="utf-8")
    shell = (ROOT / "scripts" / "setup.sh").read_text(encoding="utf-8")

    assert powershell.index("preinstall_verify.py") < powershell.index("-m venv")
    assert powershell.index("preinstall_verify.py") < powershell.index(
        "run-isolated-pip.py"
    )
    assert shell.index("preinstall_verify.py") < shell.index("-m venv")
    assert shell.index("preinstall_verify.py") < shell.index("run-isolated-pip.py")
    assert "$VerifierPython -I -S -B" in powershell
    assert '"$VERIFY_PYTHON" -I -S -B' in shell
    assert "$Wheels.Count -ne 1" in powershell
    assert '"${#WHEELS[@]}" -ne 1' in shell
    assert "FUSION_AGENT_HARNESS_ROOT" in powershell
    assert "FUSION_AGENT_HARNESS_ROOT" in shell
    assert "FUSION_AGENT_HARNESS_ROOT is forbidden" in powershell
    assert "FUSION_AGENT_HARNESS_ROOT is forbidden" in shell
    assert "Get-Command python -All" in powershell
    assert "type -a -p python3" in shell
    assert "must not point into the pre-existing plugin .venv" in powershell
    assert "must not point into the pre-existing plugin .venv" in shell
    for setup in (powershell, shell):
        assert "-m pip" not in setup
        assert "--no-compile" in setup
        assert "--no-index" in setup
        assert "--find-links" in setup
        assert "--dependency-wheelhouse" in setup
        assert "-I -B -m venv" in setup
    powershell_reparse_guard = (
        "$VenvItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint"
    )
    assert powershell.index(powershell_reparse_guard) < powershell.index(
        "Remove-Item -LiteralPath $ResolvedVenvRoot -Recurse -Force"
    )
    assert shell.index('[[ -L "$VENV_ROOT" ]]') < shell.index(
        'rm -rf -- "$RESOLVED_VENV_ROOT"'
    )
    assert "Remove-Item -LiteralPath $ResolvedVenvRoot -Recurse -Force" in powershell
    assert 'rm -rf -- "$RESOLVED_VENV_ROOT"' in shell


def _isolated_setup_fixture(tmp_path: Path) -> tuple[Path, list[str], dict[str, str]]:
    plugin = tmp_path / "plugin"
    scripts = plugin / "scripts"
    scripts.mkdir(parents=True)
    (plugin / "wheels").mkdir()
    development_source = tmp_path / "development-source"
    development_source.mkdir()
    (development_source / "pyproject.toml").write_text(
        "[build-system]\nrequires = []\n",
        encoding="utf-8",
    )

    environment = dict(os.environ)
    environment["FUSION_AGENT_HARNESS_ROOT"] = str(development_source)
    if os.name == "nt":
        setup = scripts / "setup.ps1"
        shutil.copy2(ROOT / "scripts" / "setup.ps1", setup)
        failing_python = tmp_path / "fail-python.cmd"
        failing_python.write_text("@exit /b 17\r\n", encoding="utf-8")
        command = [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(setup),
        ]
    else:
        setup = scripts / "setup.sh"
        shutil.copy2(ROOT / "scripts" / "setup.sh", setup)
        failing_python = tmp_path / "fail-python"
        failing_python.write_text("#!/usr/bin/env sh\nexit 17\n", encoding="utf-8")
        failing_python.chmod(0o755)
        command = ["bash", str(setup)]
    environment["FUSION_AGENT_PYTHON"] = str(failing_python)
    return plugin, command, environment


@pytest.mark.parametrize("dangling", (False, True), ids=("external", "dangling"))
def test_setup_rejects_linked_venv_without_touching_external_target(
    tmp_path: Path,
    dangling: bool,
) -> None:
    plugin, command, environment = _isolated_setup_fixture(tmp_path)
    external = tmp_path / "external-venv"
    external.mkdir()
    sentinel = external / "must-survive.txt"
    sentinel.write_text("external", encoding="utf-8")
    venv = plugin / ".venv"
    if os.name == "nt":
        linked = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(venv), str(external)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert linked.returncode == 0, (linked.stdout, linked.stderr)
        expected_link_kind = "reparse point"
    else:
        venv.symlink_to(external, target_is_directory=True)
        expected_link_kind = "symbolic link"
    if dangling:
        sentinel.unlink()
        external.rmdir()

    try:
        completed = subprocess.run(
            command,
            cwd=plugin,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        output = completed.stdout + completed.stderr
        assert completed.returncode != 0
        assert "Refusing to replace a virtual environment" in output
        assert expected_link_kind in output
        if dangling:
            assert not external.exists()
        else:
            assert sentinel.read_text(encoding="utf-8") == "external"
    finally:
        if os.path.lexists(venv):
            if os.name == "nt":
                os.rmdir(venv)
            else:
                venv.unlink()


def test_setup_still_replaces_regular_venv_directory(tmp_path: Path) -> None:
    plugin, command, environment = _isolated_setup_fixture(tmp_path)
    sentinel = plugin / ".venv" / "replace-me.txt"
    sentinel.parent.mkdir()
    sentinel.write_text("regular", encoding="utf-8")

    completed = subprocess.run(
        command,
        cwd=plugin,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert not sentinel.exists()
    assert "Refusing to replace a virtual environment" not in (
        completed.stdout + completed.stderr
    )


def test_setup_without_wheel_fails_before_creating_venv(tmp_path: Path) -> None:
    plugin = tmp_path / "plugin"
    scripts = plugin / "scripts"
    scripts.mkdir(parents=True)
    (plugin / "wheels").mkdir()
    environment = dict(os.environ)
    environment.pop("FUSION_AGENT_HARNESS_ROOT", None)
    if os.name == "nt":
        setup = scripts / "setup.ps1"
        shutil.copy2(ROOT / "scripts" / "setup.ps1", setup)
        command = [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(setup),
        ]
    else:
        setup = scripts / "setup.sh"
        shutil.copy2(ROOT / "scripts" / "setup.sh", setup)
        command = ["bash", str(setup)]

    completed = subprocess.run(
        command,
        cwd=plugin,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "Expected exactly one bundled harness wheel before setup" in (
        completed.stdout + completed.stderr
    )
    assert not (plugin / ".venv").exists()


def test_isolated_pip_targets_exact_venv_without_startup_hooks(
    tmp_path: Path,
) -> None:
    environment_root = tmp_path / "isolated-runtime"
    subprocess.run(
        [sys.executable, "-I", "-B", "-m", "venv", str(environment_root)],
        check=True,
        capture_output=True,
        text=True,
    )
    if os.name == "nt":
        runtime = environment_root / "Scripts" / "python.exe"
        site_packages = environment_root / "Lib" / "site-packages"
    else:
        runtime = environment_root / "bin" / "python"
        site_packages = (
            environment_root
            / "lib"
            / f"python{sys.version_info.major}.{sys.version_info.minor}"
            / "site-packages"
        )
    startup_sentinel = tmp_path / "startup-hook-executed"
    (site_packages / "untrusted.pth").write_text(
        "import pathlib; "
        f"pathlib.Path({str(startup_sentinel)!r}).write_text('executed')\n",
        encoding="utf-8",
    )
    pythonpath_root = tmp_path / "pythonpath"
    pythonpath_root.mkdir()
    (pythonpath_root / "sitecustomize.py").write_text(
        "import pathlib; "
        f"pathlib.Path({str(startup_sentinel)!r}).write_text('executed')\n",
        encoding="utf-8",
    )
    redirect = tmp_path / "pip-target-redirect"
    wheel = next((ROOT / "wheels").glob("fusion_agent_harness-*.whl"))
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(pythonpath_root)
    environment["PIP_TARGET"] = str(redirect)

    subprocess.run(
        [
            str(runtime),
            "-I",
            "-S",
            "-B",
            str(ROOT / "scripts" / "run-isolated-pip.py"),
            "install",
            "--no-compile",
            "--no-index",
            "--no-deps",
            str(wheel),
        ],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    assert not startup_sentinel.exists()
    assert not redirect.exists()
    assert (site_packages / "fusion_agent_mcp").is_dir()
    assert not list((site_packages / "fusion_agent_mcp").rglob("*.pyc"))


def test_all_github_actions_are_immutable_and_ci_covers_supported_matrix() -> None:
    uses_pattern = re.compile(r"(?m)^\s*-?\s*uses:\s*([^\s#]+)")
    for workflow in WORKFLOWS.glob("*.yml"):
        text = workflow.read_text(encoding="utf-8")
        assert _workflow_security_violations(text) == []
        for reference in uses_pattern.findall(text):
            assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", reference), (
                workflow.name,
                reference,
            )

    ci = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    assert 'python-version: ["3.11", "3.12"]' in ci
    assert "windows-latest" in ci and "ubuntu-latest" in ci and "macos-latest" in ci
    assert "pip-audit" in ci
    assert "actionlint" in ci
    assert "harness/requirements/test.lock" in ci
    assert "harness/requirements/quality.lock" in ci
    assert "--require-hashes" in ci
    assert "pip install -e" not in ci
    assert "python -m pip" not in ci
    assert "scripts/run-isolated-pip.py" in ci
    assert "--no-compile" in ci
    assert "--no-index" in ci
    assert "--dependency-wheelhouse" in ci
    assert (
        "compileall -q harness/apps harness/packages harness/tests scripts tests" in ci
    )
    assert "compileall -q harness scripts tests" not in ci
    assert "scripts/measure-performance.py" in ci
    assert "scripts/check-performance-gate.py" in ci
    assert "a148a741bbe7fc89cd1db62df3414db84aff41bd" in ci

    actionlint = (WORKFLOWS.parent / "actionlint.yaml").read_text(encoding="utf-8")
    assert "self-hosted-runner:" in actionlint
    assert "labels:" in actionlint
    assert "- fusion-real" in actionlint


@pytest.mark.parametrize(
    ("workflow", "violation"),
    [
        (
            "permissions:\n  contents: read\nsteps:\n  - uses: actions/checkout@v4\n",
            "mutable_action:actions/checkout@v4",
        ),
        (
            "permissions:\n  contents: write\nsteps:\n"
            "  - uses: actions/checkout@"
            "de0fac2e4500dabe0009e67214ff5f5447ce83dd\n",
            "top_level_contents_write",
        ),
    ],
)
def test_workflow_security_policy_rejects_mutable_actions_and_broad_defaults(
    workflow: str,
    violation: str,
) -> None:
    assert violation in _workflow_security_violations(workflow)


def test_release_ci_gate_accepts_prior_successful_branch_run_not_tag_run() -> None:
    module = _load_ci_release_gate()
    commit = "a" * 40
    payload = {
        "workflow_runs": [
            {
                "id": 102,
                "run_attempt": 1,
                "event": "push",
                "status": "completed",
                "conclusion": "success",
                "head_sha": commit,
                "head_branch": "v0.4.1",
            },
            {
                "id": 101,
                "run_attempt": 2,
                "event": "push",
                "status": "completed",
                "conclusion": "success",
                "head_sha": commit,
                "head_branch": "main",
            },
        ]
    }
    observed: list[object] = []

    class Response:
        status = 200

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, _limit: int) -> bytes:
            return json.dumps(payload).encode()

    def opener(request: object, *, timeout: int) -> Response:
        observed.extend([request, timeout])
        return Response()

    report = module.require_successful_branch_ci(
        "owner/repository",
        "ci.yml",
        commit,
        "main",
        "read-token",
        opener=opener,
    )

    assert report["run_id"] == 101
    assert report["run_attempt"] == 2
    assert observed[1] == 30
    request = observed[0]
    assert request.get_header("Authorization") == "Bearer read-token"
    assert "branch=main" in request.full_url
    assert f"head_sha={commit}" in request.full_url


def test_release_ci_gate_paginates_until_exact_success() -> None:
    module = _load_ci_release_gate()
    commit = "a" * 40
    failure = {
        "id": 1,
        "run_attempt": 1,
        "event": "push",
        "status": "completed",
        "conclusion": "failure",
        "head_sha": commit,
        "head_branch": "codex/fusion-agent-0.4.1",
    }
    success = dict(failure, id=1001, conclusion="success")
    pages = [
        {"workflow_runs": [dict(failure, id=index + 1) for index in range(100)]},
        {"workflow_runs": [success]},
    ]
    observed_urls: list[str] = []

    class Response:
        status = 200

        def __init__(self, payload: dict[str, object]) -> None:
            self.payload = payload

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, _limit: int) -> bytes:
            return json.dumps(self.payload).encode()

    def opener(request: object, *, timeout: int) -> Response:
        assert timeout == 30
        observed_urls.append(request.full_url)
        return Response(pages[len(observed_urls) - 1])

    report = module.require_successful_branch_ci(
        "owner/repository",
        "ci.yml",
        commit,
        "codex/fusion-agent-0.4.1",
        "read-token",
        opener=opener,
    )

    assert report["run_id"] == 1001
    assert len(observed_urls) == 2
    assert "page=1" in observed_urls[0]
    assert "page=2" in observed_urls[1]


@pytest.mark.parametrize(
    ("mutation", "expected_message"),
    (
        ({"conclusion": "failure"}, "no completed successful"),
        ({"status": "in_progress", "conclusion": None}, "no completed successful"),
        ({"head_sha": "b" * 40}, "no completed successful"),
        ({"head_branch": "v0.4.1"}, "no completed successful"),
        ({"event": "pull_request"}, "no completed successful"),
    ),
)
def test_release_ci_gate_rejects_nonqualifying_ci_proof(
    mutation: dict[str, object],
    expected_message: str,
) -> None:
    module = _load_ci_release_gate()
    commit = "a" * 40
    run = {
        "id": 101,
        "run_attempt": 1,
        "event": "push",
        "status": "completed",
        "conclusion": "success",
        "head_sha": commit,
        "head_branch": "main",
    }
    run.update(mutation)

    class Response:
        status = 200

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, _limit: int) -> bytes:
            return json.dumps({"workflow_runs": [run]}).encode()

    with pytest.raises(module.CiReleaseGateError, match=expected_message):
        module.require_successful_branch_ci(
            "owner/repository",
            "ci.yml",
            commit,
            "main",
            "read-token",
            opener=lambda *_args, **_kwargs: Response(),
        )


def test_release_and_nightly_workflows_use_least_privilege_public_artifacts() -> None:
    release = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    nightly = (WORKFLOWS / "fusion-real-nightly.yml").read_text(encoding="utf-8")

    assert re.search(r"(?m)^permissions:\n\s+contents: read", release)
    assert "validate-build:" in release and "publish:" in release
    assert "scripts/measure-performance.py" in release
    assert "scripts/check-performance-gate.py" in release
    assert "a148a741bbe7fc89cd1db62df3414db84aff41bd" in release
    assert "harness/requirements/test.lock" in release
    assert "--require-hashes" in release
    assert "pip install -e" not in release
    assert "python -m pip" not in release
    assert "scripts/run-isolated-pip.py" in release
    assert "--no-compile" in release
    assert "--no-index" in release
    assert "--dependency-wheelhouse" in release
    publish = release.split("publish:", 1)[1]
    assert re.search(r"(?m)^\s+contents: write$", publish)
    assert "git ls-remote" in publish
    assert "CANDIDATE_SHA" in release
    assert "scripts/check-ci-release-gate.py" in release
    assert "Require successful branch CI for candidate SHA" in release
    assert '--branch "codex/fusion-agent-0.4.1"' in release
    assert release.index("check-ci-release-gate.py") < release.index(
        "Require three consecutive real scheduled nightlies"
    )

    assert "fusion-agent inspect --real" not in nightly
    assert "Read-only active-design check" not in nightly
    assert "nightly-public/**" not in nightly
    for public_name in ("nightly-status.json", "summary.json", "SHA256SUMS"):
        assert f"nightly-public/{public_name}" in nightly
    assert "python -I -S -B scripts/prepare-nightly-public.py" in nightly
    assert "if: always() && steps.prepare_public.outcome == 'success'" in nightly
    assert "manifests/**" not in nightly
    assert "logs/**" not in nightly
    assert "fusion_captures/**" not in nightly
    assert "FUSION_AGENT_BENCHMARK_TRIAL_ID" not in nightly
    assert 'id = "nightly-import"' in nightly
    assert 'id = "nightly-export"' not in nightly
    assert "export_roots = @()" in nightly
    assert "allow_overwrite = $false" in nightly
    assert "capability_ttl_seconds = 1800" in nightly
    assert nightly.count("FUSION_AGENT_AUTHORITY_POLICY_PATH") == 1
    assert "environment: fusion-real-nightly" in nightly
    assert "FUSION_AGENT_RELEASE_CANDIDATE_SHA" in nightly
    assert "ref: ${{ needs.preflight.outputs.candidate_sha }}" in nightly
    assert "harness/requirements/test.lock" in nightly
    assert "pip install -e" not in nightly
    assert "python -m pip" not in nightly
    assert "scripts/run-isolated-pip.py" in nightly
    assert "--no-compile" in nightly
    assert "--no-index" in nightly
    assert "--dependency-wheelhouse" in nightly
    assert "python -I -B -m venv" in nightly
    assert "python -I -B -m cli.main doctor" in nightly
    assert "Tee-Object" not in nightly
    assert "doctor *> nightly-private/doctor.json" in nightly
    assert "tools probe *> nightly-private/probe.json" in nightly
    assert "--source-manifest-sha256 $revision" in nightly
    assert "--output-root nightly-private/reference-runs" in nightly
    assert "steps.reference_suite.outputs.proof_path" in nightly
    assert "scripts/collect-nightly-reference-proof.py" in nightly
    assert '--expected-commit "${{ github.sha }}"' in nightly
    assert "--expected-source-manifest-sha256" in nightly
    assert (
        '--expected-run-identity "${{ github.run_id }}-${{ github.run_attempt }}"'
        in nightly
    )
    assert "path: nightly-private" not in nightly
    real_job_header = nightly.split("steps:", 1)[0]
    assert "FUSION_MCP_ENDPOINT:" not in real_job_header
    assert "FUSION_MCP_BEARER_TOKEN:" not in real_job_header


def test_nightly_reference_collector_rejects_stale_proof(tmp_path: Path) -> None:
    script_path = ROOT / "scripts" / "collect-nightly-reference-proof.py"
    spec = importlib.util.spec_from_file_location(
        "collect_nightly_reference_proof", script_path
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    current_commit = "a" * 40
    current_run = "12345-2"
    current_manifest = "d" * 64
    source = tmp_path / "reference_suite_result.json"
    destination = tmp_path / "nightly-private" / "reference_suite_result.json"
    payload = {
        "schema_version": "fusion_parametric_reference_suite_result.v0",
        "run_id": "ref_20260716T120000Z_1234abcd",
        "nightly_run_identity": current_run,
        "tested_commit": current_commit,
        "source_manifest_sha256": current_manifest,
        "revision_identity": {
            "scheme": "source-manifest-v1",
            "expected_git_commit": current_commit,
            "observed_git_commit": current_commit,
            "expected_source_manifest_sha256": current_manifest,
            "observed_source_manifest_sha256": current_manifest,
            "tracked_state": "clean",
        },
        "requested_case_ids": list(module.DEFAULT_CASES),
        "status": "passed",
        "cases": [
            {"case_id": case_id, "passed": True} for case_id in module.DEFAULT_CASES
        ],
        "completed_at_utc": "2026-07-16T12:00:01Z",
        "result_file": "reference_suite_result.json",
        "restored": True,
    }
    source.write_text(json.dumps(payload), encoding="utf-8")
    destination.parent.mkdir()
    destination.write_text("stale destination", encoding="utf-8")

    with pytest.raises(module.ReferenceProofError, match="schema is invalid"):
        module.collect_reference_proof(
            source,
            destination,
            expected_commit=current_commit,
            expected_source_manifest_sha256=current_manifest,
            expected_run_identity=current_run,
        )
    assert not destination.exists()

    payload["schema_version"] = "fusion_parametric_reference_suite_result.v2"
    payload["tested_commit"] = "b" * 40
    source.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(module.ReferenceProofError, match="commit does not match"):
        module.collect_reference_proof(
            source,
            destination,
            expected_commit=current_commit,
            expected_source_manifest_sha256=current_manifest,
            expected_run_identity=current_run,
        )
    assert not destination.exists()

    payload["tested_commit"] = current_commit
    payload["nightly_run_identity"] = "12345-1"
    source.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(module.ReferenceProofError, match="run identity does not match"):
        module.collect_reference_proof(
            source,
            destination,
            expected_commit=current_commit,
            expected_source_manifest_sha256=current_manifest,
            expected_run_identity=current_run,
        )
    assert not destination.exists()


def test_nightly_reference_collector_accepts_only_bound_current_proof(
    tmp_path: Path,
) -> None:
    script_path = ROOT / "scripts" / "collect-nightly-reference-proof.py"
    spec = importlib.util.spec_from_file_location(
        "collect_nightly_reference_proof_control", script_path
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    expected_commit = "c" * 40
    expected_run = "98765-3"
    expected_manifest = "e" * 64
    source = tmp_path / "reference_suite_result.json"
    destination = tmp_path / "nightly-private" / "reference_suite_result.json"
    payload = {
        "schema_version": "fusion_parametric_reference_suite_result.v2",
        "run_id": "ref_20260716T130000Z_89abcdef",
        "nightly_run_identity": expected_run,
        "tested_commit": expected_commit,
        "source_manifest_sha256": expected_manifest,
        "revision_identity": {
            "scheme": "source-manifest-v1",
            "expected_git_commit": expected_commit,
            "observed_git_commit": expected_commit,
            "expected_source_manifest_sha256": expected_manifest,
            "observed_source_manifest_sha256": expected_manifest,
            "tracked_state": "clean",
        },
        "requested_case_ids": list(module.DEFAULT_CASES),
        "status": "passed",
        "cases": [
            {"case_id": case_id, "passed": True} for case_id in module.DEFAULT_CASES
        ],
        "completed_at_utc": "2026-07-16T13:00:01Z",
        "result_file": "reference_suite_result.json",
        "restored": True,
    }
    source.write_text(json.dumps(payload), encoding="utf-8")

    module.collect_reference_proof(
        source,
        destination,
        expected_commit=expected_commit,
        expected_source_manifest_sha256=expected_manifest,
        expected_run_identity=expected_run,
    )

    assert json.loads(destination.read_text(encoding="utf-8")) == payload

    invalid_cases = [
        [
            {**case, "passed": False} if index == 0 else case
            for index, case in enumerate(payload["cases"])
        ],
        [
            {**case, "passed": "true"} if index == 0 else case
            for index, case in enumerate(payload["cases"])
        ],
        [
            *payload["cases"][:-1],
            {**payload["cases"][-1], "case_id": payload["cases"][0]["case_id"]},
        ],
    ]
    for cases in invalid_cases:
        invalid = {**payload, "cases": cases}
        source.write_text(json.dumps(invalid), encoding="utf-8")
        with pytest.raises(module.ReferenceProofError):
            module.collect_reference_proof(
                source,
                destination,
                expected_commit=expected_commit,
                expected_source_manifest_sha256=expected_manifest,
                expected_run_identity=expected_run,
            )
        assert not destination.exists()


def test_nightly_public_projection_drops_raw_canaries(tmp_path: Path) -> None:
    script_path = ROOT / "scripts" / "prepare-nightly-public.py"
    spec = importlib.util.spec_from_file_location("prepare_nightly_public", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    private = tmp_path / "nightly-private"
    private.mkdir()
    (private / "nightly-status.json").write_text(
        json.dumps(
            {
                "schema_version": "fusion_real_nightly.v1",
                "status": "passed",
                "git_commit": "a" * 40,
                "fixture_policy": "disposable_unsaved_only",
                "save_user_documents": False,
                "reason": "SECRET_CANARY C:\\Users\\private",
                "raw_error": "SECRET_CANARY C:\\Users\\private",
            }
        ),
        encoding="utf-8",
    )
    (private / "probe.json").write_text("SECRET_CANARY", encoding="utf-8")
    public = tmp_path / "nightly-public"

    module.prepare_public_artifacts(private, public)

    rendered = "\n".join(path.read_text(encoding="utf-8") for path in public.iterdir())
    assert "SECRET_CANARY" not in rendered
    assert "C:\\Users\\private" not in rendered
    assert "details_redacted" in rendered
    assert {path.name for path in public.iterdir()} == {
        "SHA256SUMS",
        "nightly-status.json",
        "summary.json",
    }


def test_license_is_canonical_lf_text() -> None:
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert re.search(r"(?m)^\.gitattributes text eol=lf$", attributes)
    assert re.search(r"(?m)^LICENSE text eol=lf$", attributes)
    assert re.search(r"(?m)^\*\.lock text eol=lf$", attributes)
    assert re.search(r"(?m)^\*\.in text eol=lf$", attributes)


def test_source_file_index_excludes_unlisted_files_and_matches_archives(
    tmp_path: Path,
) -> None:
    root = tmp_path / "source-archive"
    package = root / "harness" / "packages" / "example"
    app = root / "harness" / "apps" / "service"
    package.mkdir(parents=True)
    app.mkdir(parents=True)
    (package / "tracked.py").write_text("VALUE = 1\n", encoding="utf-8")
    (package / "untracked.py").write_text("VALUE = 2\n", encoding="utf-8")
    (app / "tracked.py").write_text("VALUE = 3\n", encoding="utf-8")
    index = root / "harness" / "source-files.txt"
    index.write_text(
        "harness/apps/service/tracked.py\nharness/packages/example/tracked.py\n",
        encoding="utf-8",
    )

    files = collect_source_files(root)

    assert set(files) == {"example/tracked.py", "service/tracked.py"}
    with pytest.raises(BundleIntegrityError, match="diverges"):
        validate_source_file_index(root)
    (package / "untracked.py").unlink()
    validate_source_file_index(root)


def test_installation_parity_checks_source_wheel_runtime_and_cache(
    tmp_path: Path,
) -> None:
    source, cache = _parity_fixture(tmp_path)

    report = verify_installation_parity(
        source,
        cache,
        _parity_runtime(source),
        verify_installed=False,
    )

    assert report["ok"] is True
    assert report["version"] == "0.4.1+codex.20260716120000"
    assert report["runtime_verified"] is False

    payload = json.loads((cache / ".mcp.json").read_text(encoding="utf-8"))
    payload["mcpServers"]["fusion_agent"]["args"] = [str(tmp_path / "escape.py")]
    (tmp_path / "escape.py").write_text("# untrusted\n", encoding="utf-8")
    (cache / ".mcp.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(InstallationParityError, match="launcher"):
        verify_installation_parity(
            source,
            cache,
            _parity_runtime(source),
            verify_installed=False,
        )


def test_installation_parity_accepts_setup_rewritten_personal_source_mcp(
    tmp_path: Path,
) -> None:
    source, cache = _parity_fixture(tmp_path)
    runtime = _parity_runtime(source)
    source_path = source / ".mcp.json"
    payload = json.loads(source_path.read_text(encoding="utf-8"))
    server = payload["mcpServers"]["fusion_agent"]
    server["command"] = str(runtime)
    server["args"] = [
        "-I",
        "-B",
        str(source / "scripts" / "fusion_agent_codex_mcp_launcher.py"),
    ]
    source_path.write_text(json.dumps(payload), encoding="utf-8")

    report = verify_installation_parity(
        source,
        cache,
        runtime,
        verify_installed=False,
    )

    assert report["ok"] is True


def test_installation_parity_rejects_setup_rewritten_source_runtime_mismatch(
    tmp_path: Path,
) -> None:
    source, cache = _parity_fixture(tmp_path)
    runtime = _parity_runtime(source)
    outside_runtime = tmp_path / "outside-python"
    outside_runtime.write_bytes(b"runtime placeholder")
    source_path = source / ".mcp.json"
    payload = json.loads(source_path.read_text(encoding="utf-8"))
    server = payload["mcpServers"]["fusion_agent"]
    server["command"] = str(outside_runtime)
    server["args"] = [
        "-I",
        "-B",
        str(source / "scripts" / "fusion_agent_codex_mcp_launcher.py"),
    ]
    source_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(InstallationParityError, match="source.*runtime Python"):
        verify_installation_parity(
            source,
            cache,
            runtime,
            verify_installed=False,
        )


def test_installation_parity_rejects_setup_rewritten_source_launcher_mismatch(
    tmp_path: Path,
) -> None:
    source, cache = _parity_fixture(tmp_path)
    runtime = _parity_runtime(source)
    outside_launcher = tmp_path / "outside-launcher.py"
    outside_launcher.write_text("# untrusted\n", encoding="utf-8")
    source_path = source / ".mcp.json"
    payload = json.loads(source_path.read_text(encoding="utf-8"))
    server = payload["mcpServers"]["fusion_agent"]
    server["command"] = str(runtime)
    server["args"] = ["-I", "-B", str(outside_launcher)]
    source_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(InstallationParityError, match="source.*launcher"):
        verify_installation_parity(
            source,
            cache,
            runtime,
            verify_installed=False,
        )


def test_installation_parity_rejects_runtime_outside_personal_venv(
    tmp_path: Path,
) -> None:
    source, cache = _parity_fixture(tmp_path)
    external_runtime = tmp_path / "external-python"
    external_runtime.write_bytes(b"runtime placeholder")

    with pytest.raises(InstallationParityError, match="personal-source .venv"):
        verify_installation_parity(
            source,
            cache,
            external_runtime,
            verify_installed=False,
        )


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink regression")
def test_installation_parity_rejects_symlinked_personal_venv(
    tmp_path: Path,
) -> None:
    source, cache = _parity_fixture(tmp_path)
    shutil.rmtree(source / ".venv")
    external_venv = tmp_path / "external-venv"
    external_runtime = external_venv / "bin" / "python"
    external_runtime.parent.mkdir(parents=True)
    external_runtime.write_bytes(b"runtime placeholder")
    (source / ".venv").symlink_to(external_venv, target_is_directory=True)

    with pytest.raises(InstallationParityError, match="non-reparse"):
        verify_installation_parity(
            source,
            cache,
            source / ".venv" / "bin" / "python",
            verify_installed=False,
        )


def test_installation_parity_uses_exact_configured_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, cache = _parity_fixture(tmp_path)
    runtime = _parity_runtime(source)
    payload = json.loads((cache / ".mcp.json").read_text(encoding="utf-8"))
    payload["mcpServers"]["fusion_agent"]["command"] = str(runtime)
    (cache / ".mcp.json").write_text(json.dumps(payload), encoding="utf-8")
    observed: list[list[str]] = []

    def _run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        observed.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({"ok": True, "installed_verified": True}),
            stderr="",
        )

    monkeypatch.setattr("scripts.verify_installation_parity.subprocess.run", _run)

    report = verify_installation_parity(source, cache, runtime)

    assert report["runtime_verified"] is True
    assert observed == [
        [
            str(runtime),
            "-I",
            "-S",
            "-B",
            str((source / "scripts" / "preinstall_verify.py").resolve()),
            "--plugin-root",
            str(source.resolve()),
            "--wheel",
            str(next((cache / "wheels").glob("*.whl")).resolve()),
            "--verify-installed",
            "--dependency-lock",
            "runtime.lock",
            "--dependency-wheelhouse",
            str(source / ".venv" / ".fusion-agent-wheelhouse"),
        ]
    ]

    for config in (source / ".mcp.json", cache / ".mcp.json"):
        payload = json.loads(config.read_text(encoding="utf-8"))
        payload["mcpServers"]["fusion_agent"]["env"]["FUSION_AGENT_BACKEND"] = (
            "faust_stdio"
        )
        config.write_text(json.dumps(payload), encoding="utf-8")

    verify_installation_parity(source, cache, runtime)

    assert observed[-1][-4:-2] == ["--dependency-lock", "faust.lock"]
    assert observed[-1][-2:] == [
        "--dependency-wheelhouse",
        str(source / ".venv" / ".fusion-agent-wheelhouse"),
    ]


def test_installation_parity_rejects_failed_exact_runtime_without_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, cache = _parity_fixture(tmp_path)
    runtime = _parity_runtime(source)
    payload = json.loads((cache / ".mcp.json").read_text(encoding="utf-8"))
    payload["mcpServers"]["fusion_agent"]["command"] = str(runtime)
    (cache / ".mcp.json").write_text(json.dumps(payload), encoding="utf-8")

    def _run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        assert command[0] == str(runtime)
        return subprocess.CompletedProcess(
            command,
            1,
            stdout=json.dumps({"ok": False, "installed_verified": False}),
            stderr="current interpreter would have passed",
        )

    monkeypatch.setattr("scripts.verify_installation_parity.subprocess.run", _run)

    with pytest.raises(InstallationParityError, match="configured runtime"):
        verify_installation_parity(source, cache, runtime)


def test_installation_parity_mcp_comparison_is_closed_to_injection(
    tmp_path: Path,
) -> None:
    source, cache = _parity_fixture(tmp_path)
    cache_path = cache / ".mcp.json"
    base = json.loads(cache_path.read_text(encoding="utf-8"))

    def _extra_server(payload: dict[str, object]) -> None:
        payload["mcpServers"]["unreviewed"] = {"command": "malware"}  # type: ignore[index]

    def _extra_server_key(payload: dict[str, object]) -> None:
        payload["mcpServers"]["fusion_agent"]["cwd"] = str(tmp_path)  # type: ignore[index]

    def _extra_environment(payload: dict[str, object]) -> None:
        payload["mcpServers"]["fusion_agent"]["env"]["PYTHONPATH"] = str(  # type: ignore[index]
            tmp_path
        )

    def _extra_top_level(payload: dict[str, object]) -> None:
        payload["injected"] = True

    for mutation, match in (
        (_extra_server, "unapproved MCP server"),
        (_extra_server_key, "differs outside"),
        (_extra_environment, "differs outside"),
        (_extra_top_level, "differs outside"),
    ):
        payload = json.loads(json.dumps(base))
        mutation(payload)
        cache_path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(InstallationParityError, match=match):
            verify_installation_parity(
                source,
                cache,
                _parity_runtime(source),
                verify_installed=False,
            )


def test_installation_parity_allows_only_closed_oauth_fusion_data(
    tmp_path: Path,
) -> None:
    source, cache = _parity_fixture(tmp_path)
    cache_path = cache / ".mcp.json"
    base = json.loads(cache_path.read_text(encoding="utf-8"))
    fusion_data = {
        "url": "https://fusion-data.example.test/mcp",
        "auth": "oauth",
        "enabled": True,
        "required": False,
        "default_tools_approval_mode": "writes",
    }
    base["mcpServers"]["fusion_data"] = fusion_data
    cache_path.write_text(json.dumps(base), encoding="utf-8")

    report = verify_installation_parity(
        source,
        cache,
        _parity_runtime(source),
        verify_installed=False,
    )

    assert report["ok"] is True

    def _inject_environment(value: dict[str, object]) -> None:
        value["env"] = {"TOKEN": "secret"}

    def _inject_secret_url(value: dict[str, object]) -> None:
        value["url"] = "https://fusion-data.example.test/mcp?access_token=secret"

    mutations: tuple[tuple[Callable[[dict[str, object]], None], str], ...] = (
        (_inject_environment, "shape"),
        (_inject_secret_url, "URL is unsafe"),
    )
    for mutation, match in mutations:
        payload = json.loads(json.dumps(base))
        mutation(payload["mcpServers"]["fusion_data"])
        cache_path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(InstallationParityError, match=match):
            verify_installation_parity(
                source,
                cache,
                _parity_runtime(source),
                verify_installed=False,
            )
