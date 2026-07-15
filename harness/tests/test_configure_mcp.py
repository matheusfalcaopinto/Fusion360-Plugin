from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "configure_mcp.py"
    spec = importlib.util.spec_from_file_location("configure_mcp_script", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _plugin(tmp_path: Path) -> tuple[Path, Path]:
    plugin = tmp_path / "plugin"
    (plugin / "scripts").mkdir(parents=True)
    launcher = plugin / "scripts" / "fusion_agent_codex_mcp_launcher.py"
    launcher.write_text("print('ok')\n", encoding="utf-8")
    python = tmp_path / "python.exe"
    python.write_text("", encoding="utf-8")
    (plugin / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"fusion_agent": {"env": {}}}}),
        encoding="utf-8",
    )
    return plugin, python


def test_configure_sets_profile_and_backend(tmp_path: Path) -> None:
    plugin, python = _plugin(tmp_path)
    module = _module()
    module.configure(plugin, python, tool_profile="advanced", backend="faust_stdio")
    payload = json.loads((plugin / ".mcp.json").read_text(encoding="utf-8"))
    env = payload["mcpServers"]["fusion_agent"]["env"]
    assert env["FUSION_AGENT_TOOL_PROFILE"] == "advanced"
    assert env["FUSION_AGENT_BACKEND"] == "faust_stdio"
    assert "--mode socket" in env["FUSION_FAUST_COMMAND"]


def test_fusion_data_is_optional_oauth_and_write_prompted(tmp_path: Path) -> None:
    plugin, python = _plugin(tmp_path)
    module = _module()
    module.configure(
        plugin,
        python,
        fusion_data_url="https://fusion-data.example.test/mcp",
        enable_fusion_data=True,
    )
    servers = json.loads((plugin / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    assert set(servers) == {"fusion_agent", "fusion_data"}
    server = servers["fusion_data"]
    assert server["auth"] == "oauth"
    assert server["enabled"] is True
    assert server["required"] is False
    assert server["default_tools_approval_mode"] == "writes"
    assert "env" not in server
    assert "headers" not in server


def test_fusion_data_rejects_guessed_insecure_or_missing_url(tmp_path: Path) -> None:
    plugin, python = _plugin(tmp_path)
    module = _module()
    with pytest.raises(ValueError, match="requires --fusion-data-url"):
        module.configure(plugin, python, enable_fusion_data=True)
    with pytest.raises(ValueError, match="official HTTPS"):
        module.configure(
            plugin,
            python,
            fusion_data_url="http://example.test/mcp",
            enable_fusion_data=True,
        )
    with pytest.raises(ValueError, match="requires --enable-fusion-data"):
        module.configure(plugin, python, fusion_data_url="https://example.test/mcp")
    with pytest.raises(ValueError, match="credentials or fragments"):
        module.configure(
            plugin,
            python,
            fusion_data_url="https://user:secret@example.test/mcp#token",
            enable_fusion_data=True,
        )


def test_fusion_data_rejects_secret_query_before_persisting(tmp_path: Path) -> None:
    plugin, python = _plugin(tmp_path)
    module = _module()
    config = plugin / ".mcp.json"
    original = config.read_text(encoding="utf-8")

    with pytest.raises(ValueError, match="token or secret query"):
        module.configure(
            plugin,
            python,
            fusion_data_url="https://fusion-data.example.test/mcp?access_token=do-not-write",
            enable_fusion_data=True,
        )

    assert config.read_text(encoding="utf-8") == original
    assert "do-not-write" not in config.read_text(encoding="utf-8")
