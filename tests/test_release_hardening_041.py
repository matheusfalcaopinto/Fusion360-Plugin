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
            archive.writestr(name, data, compress_type=zipfile.ZIP_STORED)


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
        (root / ".codex-plugin").mkdir(parents=True)
        manifest = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text())
        manifest["version"] = "0.4.1+codex.20260716120000"
        (root / ".codex-plugin" / "plugin.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        (root / "wheels").mkdir()
        shutil.copy2(wheel, root / "wheels" / wheel.name)
        for relative in (
            "scripts/fusion_agent_codex_mcp_launcher.py",
            "scripts/preinstall_verify.py",
            "scripts/bundle_integrity.py",
            "scripts/configure_mcp.py",
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
                "args": ["scripts/fusion_agent_codex_mcp_launcher.py"],
                "env": {
                    "FUSION_AGENT_TOOL_PROFILE": "normal",
                    "FUSION_AGENT_BACKEND": "autodesk_http",
                },
            }
        }
    }
    (source / ".mcp.json").write_text(json.dumps(source_mcp), encoding="utf-8")
    cache_mcp = json.loads(json.dumps(source_mcp))
    cache_server = cache_mcp["mcpServers"]["fusion_agent"]
    cache_server["command"] = sys.executable
    cache_server["args"] = [
        str(source / "scripts" / "fusion_agent_codex_mcp_launcher.py")
    ]
    (cache / ".mcp.json").write_text(json.dumps(cache_mcp), encoding="utf-8")
    return source, cache


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
    with pytest.raises(BundleIntegrityError, match="RECORD.*bijective"):
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
    with pytest.raises(BundleIntegrityError, match="allowlist.*extra"):
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
    assert powershell.index("preinstall_verify.py") < powershell.index("-m pip install")
    assert shell.index("preinstall_verify.py") < shell.index("-m venv")
    assert shell.index("preinstall_verify.py") < shell.index("-m pip install")
    assert "$VerifierPython -E -s -S" in powershell
    assert '"$VERIFY_PYTHON" -E -s -S' in shell
    assert "$Wheels.Count -ne 1" in powershell
    assert '"${#WHEELS[@]}" -ne 1' in shell
    assert "FUSION_AGENT_HARNESS_ROOT" in powershell
    assert "FUSION_AGENT_HARNESS_ROOT" in shell


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


def test_all_github_actions_are_immutable_and_ci_covers_supported_matrix() -> None:
    uses_pattern = re.compile(r"(?m)^\s*-?\s*uses:\s*([^\s#]+)")
    for workflow in WORKFLOWS.glob("*.yml"):
        text = workflow.read_text(encoding="utf-8")
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
    assert "uv sync --frozen --project harness" in ci
    assert "uv run --frozen --project harness" in ci
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


def test_release_and_nightly_workflows_use_least_privilege_public_artifacts() -> None:
    release = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    nightly = (WORKFLOWS / "fusion-real-nightly.yml").read_text(encoding="utf-8")

    assert re.search(r"(?m)^permissions:\n\s+contents: read$", release)
    assert "validate-build:" in release and "publish:" in release
    assert "scripts/measure-performance.py" in release
    assert "scripts/check-performance-gate.py" in release
    assert "a148a741bbe7fc89cd1db62df3414db84aff41bd" in release
    assert "uv sync --frozen --project harness --extra test" in release
    publish = release.split("publish:", 1)[1]
    assert re.search(r"(?m)^\s+contents: write$", publish)

    assert "fusion-agent inspect --real" not in nightly
    assert "Read-only active-design check" not in nightly
    assert "nightly-public/**" in nightly
    assert "manifests/**" not in nightly
    assert "logs/**" not in nightly
    assert "fusion_captures/**" not in nightly
    assert "FUSION_AGENT_BENCHMARK_TRIAL_ID" not in nightly
    assert 'id = "nightly-import"' in nightly
    assert 'id = "nightly-export"' in nightly
    assert "allow_overwrite = $false" in nightly
    assert "capability_ttl_seconds = 1800" in nightly
    assert nightly.count("FUSION_AGENT_AUTHORITY_POLICY_PATH") == 1
    assert (
        "Remove-Item -LiteralPath benchmark_parametric_suite/reference_suite_result.json"
        in nightly
    )
    assert "scripts/collect-nightly-reference-proof.py" in nightly
    assert '--expected-commit "${{ github.sha }}"' in nightly
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
    source = tmp_path / "reference_suite_result.json"
    destination = tmp_path / "nightly-private" / "reference_suite_result.json"
    payload = {
        "schema_version": "fusion_parametric_reference_suite_result.v0",
        "run_id": "ref_20260716T120000Z",
        "nightly_run_identity": current_run,
        "tested_commit": current_commit,
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
            expected_run_identity=current_run,
        )
    assert not destination.exists()

    payload["schema_version"] = "fusion_parametric_reference_suite_result.v1"
    payload["tested_commit"] = "b" * 40
    source.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(module.ReferenceProofError, match="commit does not match"):
        module.collect_reference_proof(
            source,
            destination,
            expected_commit=current_commit,
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
    source = tmp_path / "reference_suite_result.json"
    destination = tmp_path / "nightly-private" / "reference_suite_result.json"
    payload = {
        "schema_version": "fusion_parametric_reference_suite_result.v1",
        "run_id": "ref_20260716T130000Z",
        "nightly_run_identity": expected_run,
        "tested_commit": expected_commit,
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
    assert re.search(r"(?m)^LICENSE text eol=lf$", attributes)


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
        sys.executable,
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
            sys.executable,
            verify_installed=False,
        )


def test_installation_parity_uses_exact_configured_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, cache = _parity_fixture(tmp_path)
    runtime = tmp_path / "exact-runtime.exe"
    runtime.write_bytes(b"runtime placeholder")
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
            str(runtime.resolve()),
            "-I",
            str((source / "scripts" / "preinstall_verify.py").resolve()),
            "--plugin-root",
            str(source.resolve()),
            "--wheel",
            str(next((cache / "wheels").glob("*.whl")).resolve()),
            "--verify-installed",
        ]
    ]


def test_installation_parity_rejects_failed_exact_runtime_without_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, cache = _parity_fixture(tmp_path)
    runtime = tmp_path / "different-runtime.exe"
    runtime.write_bytes(b"runtime placeholder")
    payload = json.loads((cache / ".mcp.json").read_text(encoding="utf-8"))
    payload["mcpServers"]["fusion_agent"]["command"] = str(runtime)
    (cache / ".mcp.json").write_text(json.dumps(payload), encoding="utf-8")

    def _run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        assert command[0] == str(runtime.resolve())
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
                sys.executable,
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
        sys.executable,
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
                sys.executable,
                verify_installed=False,
            )
