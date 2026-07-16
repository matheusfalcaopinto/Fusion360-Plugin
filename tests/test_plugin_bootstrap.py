from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

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
    assert Path(server["args"][0]) == launcher.resolve()
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


def test_distributed_mcp_config_defaults_to_legacy_transport() -> None:
    payload = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))
    environment = payload["mcpServers"]["fusion_agent"]["env"]

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
    runtime = plugin / ".venv" / "Scripts" / "python.exe"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("", encoding="utf-8")

    errors: list[str] = []
    module._check_mcp_runtime_paths(plugin, str(runtime), [str(launcher)], errors)
    assert errors == []

    outside = tmp_path / "outside-python.exe"
    outside.write_text("", encoding="utf-8")
    errors = []
    module._check_mcp_runtime_paths(plugin, str(outside), [str(launcher)], errors)
    assert any("contained" in error for error in errors)

    errors = []
    module._check_mcp_runtime_paths(
        plugin, str(runtime), [str(tmp_path / "evil.py")], errors
    )
    assert any("launcher" in error for error in errors)
