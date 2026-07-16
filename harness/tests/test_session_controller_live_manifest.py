from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_core.session_controller import SessionController, SessionOptions
from fusion_mcp_adapter.manifest_store import ManifestStore
from fusion_mcp_adapter.semantics import ConnectionState
from fusion_mcp_adapter.tool_result import ToolDefinition, ToolManifest
from telemetry.trace import JsonlTraceLogger


class _LiveClient:
    def __init__(self, manifest: ToolManifest, *, drift: bool = False) -> None:
        self._manifest = manifest
        self._drift = drift
        self.ping_count = 0
        self.list_count = 0

    @property
    def current_manifest(self) -> ToolManifest:
        return self._manifest.model_copy(deep=True)

    @property
    def diagnostics(self) -> dict[str, Any]:
        return {
            "state": "READY",
            "fingerprint": self._manifest.fingerprint,
            "manifest_drift": self._drift,
        }

    async def ping(self) -> None:
        self.ping_count += 1

    async def list_tools(self) -> ToolManifest:
        self.list_count += 1
        self._drift = False
        return self._manifest.model_copy(deep=True)


def _manifest(*names: str) -> ToolManifest:
    return ToolManifest(
        source="real",
        tools=[ToolDefinition(name=name) for name in names],
        server={"name": "test", "version": "1"},
        protocol_version="2025-06-18",
    )


@pytest.mark.asyncio
async def test_real_facade_is_built_from_live_manifest_not_stale_disk(
    tmp_path: Path,
) -> None:
    store = ManifestStore(tmp_path / "manifests")
    stale = _manifest("stale_disk_tool")
    store.save_if_changed(stale)
    live = _manifest("fusion_mcp_read", "fusion_mcp_execute")
    client = _LiveClient(live)
    controller = SessionController(real_client=client, manifest_store=store)  # type: ignore[arg-type]

    facade = await controller._build_facade(
        "real",
        options=SessionOptions(mode="real", manifest_dir=store.root),
        trace_logger=JsonlTraceLogger(tmp_path / "trace.jsonl"),
        session_id="live-manifest-test",
    )

    assert client.list_count == 1
    assert facade.adapter.manifest is not None
    assert facade.adapter.manifest.fingerprint == live.fingerprint
    assert facade.adapter.manifest.names() == live.names()
    assert store.load_latest("real").fingerprint == live.fingerprint  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_real_health_pings_without_implicitly_accepting_manifest_drift(
    tmp_path: Path,
) -> None:
    store = ManifestStore(tmp_path / "manifests")
    cached = _manifest("fusion_mcp_read", "fusion_mcp_execute")
    store.save_if_changed(cached)
    live = _manifest("fusion_mcp_read", "fusion_mcp_execute", "new_native_tool")
    client = _LiveClient(live, drift=True)
    controller = SessionController(real_client=client, manifest_store=store)  # type: ignore[arg-type]

    health = await controller.session_health(
        mode="real",
        options=SessionOptions(mode="real", manifest_dir=store.root),
    )

    assert client.ping_count == 1
    assert client.list_count == 0
    assert health["mcp_server_ok"] is True
    assert health["manifest_drift"] is True
    assert health["healthy"] is False
    assert health["cached_manifest_fingerprint"] == cached.fingerprint
    assert health["live_manifest_fingerprint"] == live.fingerprint


@pytest.mark.asyncio
async def test_controller_closes_the_lazy_client_it_owns() -> None:
    controller = SessionController()

    await controller.aclose()

    assert controller.real_client.state == ConnectionState.CLOSED


@pytest.mark.asyncio
async def test_health_reports_corrupt_manifest_instead_of_raising(
    tmp_path: Path,
) -> None:
    store = ManifestStore(tmp_path / "manifests")
    store.root.mkdir(parents=True)
    (store.root / "fusion_mcp_tools_latest_real.json").write_text(
        "{broken", encoding="utf-8"
    )
    live = _manifest("fusion_mcp_read", "fusion_mcp_execute")
    controller = SessionController(
        real_client=_LiveClient(live),  # type: ignore[arg-type]
        manifest_store=store,
    )

    health = await controller.session_health(
        mode="real",
        options=SessionOptions(mode="real", manifest_dir=store.root),
    )

    assert health["mcp_server_ok"] is True
    assert health["manifest_ok"] is False
    assert health["healthy"] is False
    assert "JSONDecodeError" in health["manifest_error"]
    assert "error" in health["manifest_status"]["real"]


def test_manifest_persistence_error_survives_health_reads(tmp_path: Path) -> None:
    store = ManifestStore(tmp_path / "manifests")
    store.last_persistence_error = "PermissionError: OneDrive lock"

    assert store.load_latest("real") is None
    status = store.latest_status()

    assert status["persistence"]["error"] == "PermissionError: OneDrive lock"
