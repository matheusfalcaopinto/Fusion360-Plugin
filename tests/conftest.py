from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
UNPACKED_WHEEL = PLUGIN_ROOT / "work_unpacked_wheel"
if UNPACKED_WHEEL.is_dir() and str(UNPACKED_WHEEL) not in sys.path:
    sys.path.insert(0, str(UNPACKED_WHEEL))

from fusion_mcp_adapter.manifest_store import ManifestStore  # noqa: E402


@dataclass(frozen=True)
class IsolatedHarnessPaths:
    workspace: Path
    outputs: Path
    manifests: Path


@pytest.fixture
def plugin_root() -> Path:
    return PLUGIN_ROOT


@pytest.fixture
def unpacked_wheel() -> Path:
    return UNPACKED_WHEEL


@pytest.fixture
def harness_paths(tmp_path: Path) -> IsolatedHarnessPaths:
    return IsolatedHarnessPaths(
        workspace=tmp_path / "workspace",
        outputs=tmp_path / "outputs",
        manifests=tmp_path / "manifests",
    )


@pytest.fixture(autouse=True)
def isolate_fusion_agent_server_paths(monkeypatch: pytest.MonkeyPatch, harness_paths: IsolatedHarnessPaths) -> None:
    """Keep MCP wrapper tests from depending on repo-local session state."""

    import cli.main as cli_main
    import fusion_agent_mcp.server as server

    monkeypatch.setattr(server, "WORKSPACE_ROOT", harness_paths.workspace)
    monkeypatch.setattr(server, "OUTPUTS_ROOT", harness_paths.outputs)
    monkeypatch.setattr(server, "MANIFEST_ROOT", harness_paths.manifests)

    class IsolatedManifestStore(ManifestStore):
        def __init__(self, root: Path | str | None = None) -> None:
            super().__init__(harness_paths.manifests if root is None else root)

    monkeypatch.setattr(cli_main, "ManifestStore", IsolatedManifestStore)


@pytest.fixture
def project_name(request: pytest.FixtureRequest) -> str:
    suffix = "".join(char if char.isalnum() else "_" for char in request.node.name.lower())
    return f"pytest_{suffix}"
