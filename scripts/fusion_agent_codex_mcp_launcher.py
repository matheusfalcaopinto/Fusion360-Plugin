"""Portable Codex launcher for the Fusion Agent MCP server."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path


def plugin_root() -> Path:
    """Return the Codex plugin root."""

    return Path(__file__).resolve().parents[1]


def is_harness_root(path: Path) -> bool:
    """Return whether a path looks like the Fusion Agent harness root."""

    return (
        (path / "pyproject.toml").is_file()
        and (path / "apps" / "fusion_agent_mcp" / "server.py").is_file()
        and (path / "packages" / "agent_core").is_dir()
    )


def resolve_dev_harness_root(root: Path | None = None) -> Path | None:
    """Resolve an explicit development source checkout override."""

    root = root or plugin_root()
    configured = os.getenv("FUSION_AGENT_HARNESS_ROOT")
    if not configured:
        return None
    candidate = Path(configured).resolve()
    if is_harness_root(candidate):
        return candidate
    raise FileNotFoundError(
        f"FUSION_AGENT_HARNESS_ROOT is not a harness source checkout: {candidate}"
    )


def resolve_harness_root(root: Path | None = None) -> Path | None:
    """Backward-compatible alias for development source checkout resolution."""

    return resolve_dev_harness_root(root)


def build_pythonpath(harness_root: Path, current: str | None = None) -> str:
    """Build a platform-correct PYTHONPATH for the harness packages."""

    entries = [str(harness_root / "packages"), str(harness_root / "apps")]
    if current:
        entries.append(current)
    return os.pathsep.join(entries)


def resolve_python(plugin: Path, harness_root: Path | None = None) -> Path:
    """Resolve the Python interpreter that should host the MCP server."""

    configured = os.getenv("FUSION_AGENT_PYTHON")
    candidates = [
        Path(configured) if configured else None,
        plugin / ".venv" / "bin" / "python",
        plugin / ".venv" / "Scripts" / "python.exe",
        harness_root / ".venv" / "bin" / "python" if harness_root else None,
        harness_root / ".venv" / "Scripts" / "python.exe" if harness_root else None,
        Path(sys.executable),
    ]
    for candidate in candidates:
        if candidate and candidate.exists():
            return candidate.resolve()
    return Path(sys.executable).resolve()


def bundled_wheels(plugin: Path) -> list[Path]:
    """Return bundled harness wheels sorted newest first."""

    wheels_dir = plugin / "wheels"
    if not wheels_dir.is_dir():
        return []
    return sorted(
        wheels_dir.glob("fusion_agent_harness-*.whl"),
        key=lambda path: path.name,
        reverse=True,
    )


def plugin_version(plugin: Path) -> str:
    """Read the installed plugin/cachebuster version without importing the harness."""

    manifest = plugin / ".codex-plugin" / "plugin.json"
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return "unknown"
    value = payload.get("version") if isinstance(payload, dict) else None
    return str(value or "unknown")


def installed_server_available(python: Path) -> bool:
    """Return whether the target interpreter can import the MCP server."""

    if python == Path(sys.executable).resolve():
        return importlib.util.find_spec("fusion_agent_mcp.server") is not None
    completed = subprocess.run(
        [str(python), "-c", "import fusion_agent_mcp.server"],
        text=True,
        capture_output=True,
        check=False,
    )
    return completed.returncode == 0


def main(argv: list[str] | None = None) -> int:
    """Run the MCP server or print launcher diagnostics."""

    parser = argparse.ArgumentParser(
        description="Launch the Fusion Agent Codex MCP server."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Print resolved launcher settings and exit.",
    )
    args = parser.parse_args(argv)

    root = plugin_root()
    harness_root = resolve_dev_harness_root(root)
    python = resolve_python(root, harness_root)
    os.environ.setdefault("FUSION_AGENT_CODEX", "1")
    os.environ.setdefault("FUSION_AGENT_PLUGIN_VERSION", plugin_version(root))
    if harness_root:
        os.environ["PYTHONPATH"] = build_pythonpath(
            harness_root, os.environ.get("PYTHONPATH")
        )

    if args.check:
        print(f"plugin_root={root}")
        print(f"harness_root={harness_root or '<installed-package>'}")
        print(f"python={python}")
        if harness_root:
            print(f"pythonpath={os.environ['PYTHONPATH']}")
        print(f"bundled_wheels={len(bundled_wheels(root))}")
        server_available = installed_server_available(python)
        print(f"installed_server_available={server_available}")
        print(f"fusion_agent_codex={os.environ['FUSION_AGENT_CODEX']}")
        print(
            f"fusion_agent_plugin_version={os.environ['FUSION_AGENT_PLUGIN_VERSION']}"
        )
        return 0 if server_available else 1

    if python == Path(sys.executable).resolve():
        os.chdir(harness_root or root)
        if harness_root:
            sys.path[:0] = [str(harness_root / "packages"), str(harness_root / "apps")]
        runpy.run_module("fusion_agent_mcp.server", run_name="__main__")
        return 0

    return subprocess.call(
        [str(python), "-m", "fusion_agent_mcp.server"],
        cwd=str(harness_root or root),
        env=os.environ,
    )


if __name__ == "__main__":
    raise SystemExit(main())
