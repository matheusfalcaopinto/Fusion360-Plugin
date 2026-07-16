from __future__ import annotations

import warnings

import pytest

from agent_core.session_controller import SessionController
from fusion_mcp_adapter.backend import create_fusion_client, selected_backend
from fusion_mcp_adapter.real_client import RealMcpClient
from fusion_mcp_adapter.stdio_client import StdioMcpClient


def test_autodesk_backend_is_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FUSION_AGENT_BACKEND", raising=False)
    assert selected_backend() == "autodesk_http"
    assert isinstance(create_fusion_client(), RealMcpClient)


def test_faust_backend_uses_persistent_stdio_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FUSION_AGENT_BACKEND", "faust_stdio")
    monkeypatch.setenv("FUSION_FAUST_COMMAND", "python -m fusion360_mcp_server")
    client = create_fusion_client()
    assert isinstance(client, StdioMcpClient)
    assert client.command == "python"
    assert client.args == ["-m", "fusion360_mcp_server"]
    assert client.diagnostics["transport_mode"] == "persistent_stdio"


def test_standalone_controller_and_cli_path_honor_faust_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FUSION_AGENT_BACKEND", "faust_stdio")
    monkeypatch.setenv("FUSION_FAUST_COMMAND", "python -m fusion360_mcp_server")

    controller = SessionController()

    assert isinstance(controller.real_client, StdioMcpClient)
    assert controller.real_client.command == "python"
    assert controller.real_client.args == ["-m", "fusion360_mcp_server"]


def test_legacy_command_is_only_an_explicit_faust_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FUSION_AGENT_BACKEND", "faust_stdio")
    monkeypatch.delenv("FUSION_FAUST_COMMAND", raising=False)
    monkeypatch.setenv("FUSION_MCP_COMMAND", "faust-server --stdio")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        client = create_fusion_client()
    assert isinstance(client, StdioMcpClient)
    assert client.command == "faust-server"
    assert any("deprecated" in str(item.message) for item in caught)


def test_invalid_backend_fails_instead_of_falling_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FUSION_AGENT_BACKEND", "automatic")
    with pytest.raises(ValueError, match="autodesk_http or faust_stdio"):
        create_fusion_client()
