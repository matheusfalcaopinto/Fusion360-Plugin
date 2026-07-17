from __future__ import annotations

import warnings

import pytest

from agent_core.session_controller import SessionController
from fusion_mcp_adapter import backend as backend_module
from fusion_mcp_adapter.backend import create_fusion_client, selected_backend
from fusion_mcp_adapter.real_client import RealMcpClient
from fusion_mcp_adapter.stdio_client import StdioMcpClient


def test_autodesk_backend_is_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FUSION_AGENT_BACKEND", raising=False)
    assert selected_backend() == "autodesk_http"
    assert isinstance(create_fusion_client(), RealMcpClient)


def test_faust_backend_uses_persistent_stdio_command() -> None:
    client = create_fusion_client(
        backend="faust_stdio",
        faust_command="python -m fusion360_mcp_server",
    )
    assert isinstance(client, StdioMcpClient)
    assert client.command == "python"
    assert client.args == ["-m", "fusion360_mcp_server"]
    assert client.diagnostics["transport_mode"] == "persistent_stdio"


def test_standalone_controller_accepts_explicit_faust_client() -> None:
    client = create_fusion_client(
        backend="faust_stdio",
        faust_command="python -m fusion360_mcp_server",
    )
    controller = SessionController(real_client=client)

    assert isinstance(controller.real_client, StdioMcpClient)
    assert controller.real_client.command == "python"
    assert controller.real_client.args == ["-m", "fusion360_mcp_server"]


def test_legacy_command_is_only_an_explicit_faust_alias() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        client = create_fusion_client(
            backend="faust_stdio",
            command="faust-server --stdio",
        )
    assert isinstance(client, StdioMcpClient)
    assert client.command == "faust-server"
    assert any("deprecated" in str(item.message) for item in caught)


def test_invalid_backend_fails_instead_of_falling_back() -> None:
    with pytest.raises(ValueError, match="autodesk_http or faust_stdio"):
        create_fusion_client(backend="automatic")


def test_backend_and_controller_constructors_never_read_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_getenv(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("constructor must use explicit startup configuration")

    monkeypatch.setattr(backend_module.os, "getenv", forbidden_getenv)

    client = create_fusion_client()
    controller = SessionController()

    assert isinstance(client, RealMcpClient)
    assert isinstance(controller.real_client, RealMcpClient)
    assert dict(controller._environment_snapshot) == {}
