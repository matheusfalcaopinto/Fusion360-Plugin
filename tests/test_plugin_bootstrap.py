from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from scripts import bundle_integrity

ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(
        f"fusion_agent_test_{path.stem}", path
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_plugin_validator_runs_under_isolated_mode() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            str(ROOT / "scripts" / "validate_plugin.py"),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    payload = json.loads(completed.stdout)
    assert payload["ok"] is True


def test_ci_and_release_run_plugin_validator_under_isolated_mode() -> None:
    for workflow_name in ("ci.yml", "release.yml"):
        workflow = (ROOT / ".github" / "workflows" / workflow_name).read_text(
            encoding="utf-8"
        )
        assert "run: python -I -B scripts/validate_plugin.py" in workflow
        assert "run: python -B scripts/validate_plugin.py" not in workflow


@pytest.mark.parametrize(
    "version",
    [
        "0.4.1+codex.20260717192131",
        "0.4.1+codex.20000229000000",
    ],
)
def test_cachebuster_accepts_exact_valid_utc_timestamp(version: str) -> None:
    assert bundle_integrity.valid_codex_cachebuster_version(
        version, expected_base_version="0.4.1"
    )


@pytest.mark.parametrize(
    "version",
    [
        "0.4.1",
        "0.4.1+codex.",
        "0.4.1+codex.not-utc",
        "0.4.1+codex.2026071719213",
        "0.4.1+codex.202607171921310",
        "0.4.1+codex.20260229000000",
        "0.4.1+codex.20261301120000",
        "0.4.2+codex.20260717192131",
    ],
)
def test_cachebuster_rejects_wrong_shape_calendar_or_base(version: str) -> None:
    assert not bundle_integrity.valid_codex_cachebuster_version(
        version, expected_base_version="0.4.1"
    )


def test_plugin_validator_rejects_invalid_cachebuster_at_wheel_boundary() -> None:
    module = _load_script("validate_plugin.py")
    wheel = ROOT / "wheels" / "fusion_agent_harness-0.4.1-py3-none-any.whl"
    errors: list[str] = []

    module._check_wheel(
        ROOT,
        wheel,
        {"version": "0.4.1+codex.20260229000000"},
        errors,
    )

    assert any("14-digit valid UTC timestamp" in error for error in errors)


def test_installation_parity_rejects_invalid_cachebuster_before_wheel_use(
    tmp_path: Path,
) -> None:
    module = _load_script("verify_installation_parity.py")
    source = tmp_path / "source"
    cache = tmp_path / "cache"
    invalid_manifest = {
        "name": "fusion-agent-codex",
        "version": "0.4.1+codex.20260229000000",
    }
    for root in (source, cache):
        manifest = root / ".codex-plugin" / "plugin.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps(invalid_manifest), encoding="utf-8")
    pyproject = source / "harness" / "pyproject.toml"
    pyproject.parent.mkdir()
    pyproject.write_text(
        '[project]\nname = "fusion-agent-harness"\nversion = "0.4.1"\n',
        encoding="utf-8",
    )
    runtime = (
        source / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    )
    runtime.parent.mkdir(parents=True)
    runtime.write_bytes(b"runtime placeholder")

    with pytest.raises(module.InstallationParityError, match="14-digit valid UTC"):
        module.verify_installation_parity(
            source,
            cache,
            runtime,
            verify_installed=False,
        )


def test_configure_mcp_writes_existing_python_and_launcher_as_absolute_paths(
    tmp_path: Path,
) -> None:
    plugin = tmp_path / "plugin"
    scripts = plugin / "scripts"
    scripts.mkdir(parents=True)
    launcher = scripts / "fusion_agent_codex_mcp_launcher.py"
    launcher.write_text("# launcher\n", encoding="utf-8")
    (plugin / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "fusion_agent": {
                        "command": "python",
                        "args": ["scripts/fusion_agent_codex_mcp_launcher.py"],
                        "env": {"FUSION_AGENT_CODEX": "1"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    module = _load_script("configure_mcp.py")

    module.configure(plugin, Path(sys.executable))

    configured = json.loads((plugin / ".mcp.json").read_text(encoding="utf-8"))
    server = configured["mcpServers"]["fusion_agent"]
    assert Path(server["command"]) == Path(sys.executable).resolve()
    assert server["args"][:2] == ["-I", "-B"]
    assert Path(server["args"][2]) == launcher.resolve()
    assert server["env"] == {
        "FUSION_AGENT_CODEX": "1",
        "FUSION_AGENT_TOOL_PROFILE": "normal",
        "FUSION_AGENT_BACKEND": "autodesk_http",
        "FUSION_AGENT_REMOTE_POLICY": "loopback_only",
    }


def test_launcher_reads_full_cachebuster_version(tmp_path: Path) -> None:
    manifest = tmp_path / ".codex-plugin" / "plugin.json"
    manifest.parent.mkdir()
    manifest.write_text(
        json.dumps(
            {"name": "fusion-agent-codex", "version": "0.2.1+codex.20260714123456"}
        ),
        encoding="utf-8",
    )
    module = _load_script("fusion_agent_codex_mcp_launcher.py")

    assert module.plugin_version(tmp_path) == "0.2.1+codex.20260714123456"


def test_release_launcher_rejects_development_source_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_script("fusion_agent_codex_mcp_launcher.py")
    plugin = tmp_path / "plugin"
    (plugin / "wheels").mkdir(parents=True)
    (plugin / "wheels" / "fusion_agent_harness-0.4.1-py3-none-any.whl").write_bytes(
        b"release"
    )
    harness = tmp_path / "harness"
    (harness / "apps" / "fusion_agent_mcp").mkdir(parents=True)
    (harness / "packages" / "agent_core").mkdir(parents=True)
    (harness / "pyproject.toml").write_text("", encoding="utf-8")
    (harness / "apps" / "fusion_agent_mcp" / "server.py").write_text(
        "", encoding="utf-8"
    )
    monkeypatch.setenv("FUSION_AGENT_HARNESS_ROOT", str(harness))

    with pytest.raises(RuntimeError, match="forbidden"):
        module.resolve_dev_harness_root(plugin)


def test_release_launcher_ignores_external_python_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_script("fusion_agent_codex_mcp_launcher.py")
    plugin = tmp_path / "plugin"
    (plugin / "wheels").mkdir(parents=True)
    (plugin / "wheels" / "fusion_agent_harness-0.4.1-py3-none-any.whl").write_bytes(
        b"release"
    )
    runtime = (
        plugin / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    )
    runtime.parent.mkdir(parents=True)
    runtime.write_text("", encoding="utf-8")
    external = tmp_path / "external-python"
    external.write_text("", encoding="utf-8")
    monkeypatch.setenv("FUSION_AGENT_PYTHON", str(external))

    assert module.resolve_python(plugin) == runtime.absolute()
    assert module._server_command(runtime)[:4] == [
        str(runtime),
        "-I",
        "-B",
        "-m",
    ]
    assert module._server_import_command(runtime)[:4] == [
        str(runtime),
        "-I",
        "-B",
        "-c",
    ]


def test_distributed_mcp_config_defaults_to_legacy_transport() -> None:
    payload = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))
    server = payload["mcpServers"]["fusion_agent"]
    environment = server["env"]

    assert server["args"] == [
        "-I",
        "-B",
        "scripts/fusion_agent_codex_mcp_launcher.py",
    ]
    assert environment["FUSION_MCP_TRANSPORT_MODE"] == "legacy"
    assert environment["FUSION_AGENT_TOOL_PROFILE"] == "normal"
    assert environment["FUSION_AGENT_BACKEND"] == "autodesk_http"
    assert environment["FUSION_AGENT_REMOTE_POLICY"] == "loopback_only"


def test_plugin_validator_accepts_absent_fusion_data_and_rejects_credential_leaks() -> (
    None
):
    module = _load_script("validate_plugin.py")
    errors: list[str] = []
    module._check_fusion_data({"mcpServers": {}}, {}, errors)
    assert errors == []

    errors = []
    module._check_fusion_data(
        {
            "mcpServers": {
                "fusion_data": {
                    "url": "https://example.test/mcp?access_token=secret",
                    "auth": "none",
                    "enabled": "yes",
                    "required": True,
                    "default_tools_approval_mode": "never",
                    "headers": {"Authorization": "Bearer secret"},
                }
            }
        },
        {"FUSION_DATA_TOKEN": "secret"},
        errors,
    )
    assert any("secret query" in error for error in errors)
    assert any("Codex OAuth" in error for error in errors)
    assert any("fusion_agent env" in error for error in errors)


def test_plugin_validator_contains_installed_runtime_and_launcher(
    tmp_path: Path,
) -> None:
    module = _load_script("validate_plugin.py")
    plugin = tmp_path / "plugin"
    launcher = plugin / "scripts" / "fusion_agent_codex_mcp_launcher.py"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("# launcher\n", encoding="utf-8")
    runtime = (
        plugin / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    )
    runtime.parent.mkdir(parents=True)
    runtime.write_text("", encoding="utf-8")

    errors: list[str] = []
    module._check_mcp_runtime_paths(
        plugin, str(runtime), ["-I", "-B", str(launcher)], errors
    )
    assert errors == []

    outside = tmp_path / "outside-python.exe"
    outside.write_text("", encoding="utf-8")
    errors = []
    module._check_mcp_runtime_paths(
        plugin, str(outside), ["-I", "-B", str(launcher)], errors
    )
    assert any("contained" in error for error in errors)

    errors = []
    module._check_mcp_runtime_paths(
        plugin, str(runtime), ["-I", "-B", str(tmp_path / "evil.py")], errors
    )
    assert any("launcher" in error for error in errors)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink regression")
def test_plugin_validator_rejects_symlinked_venv_root(tmp_path: Path) -> None:
    module = _load_script("validate_plugin.py")
    plugin = tmp_path / "plugin"
    launcher = plugin / "scripts" / "fusion_agent_codex_mcp_launcher.py"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("", encoding="utf-8")
    external = tmp_path / "external-venv"
    runtime = external / "bin" / "python"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("", encoding="utf-8")
    (plugin / ".venv").symlink_to(external, target_is_directory=True)
    errors: list[str] = []

    module._check_mcp_runtime_paths(
        plugin,
        str(plugin / ".venv" / "bin" / "python"),
        ["-I", "-B", str(launcher)],
        errors,
    )

    assert any("non-reparse" in error for error in errors)
