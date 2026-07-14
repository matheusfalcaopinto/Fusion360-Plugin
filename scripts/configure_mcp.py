"""Write a machine-local absolute MCP launcher configuration after setup."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path


def configure(plugin_root: Path, python: Path) -> Path:
    plugin_root = plugin_root.resolve()
    python = python.resolve()
    config_path = plugin_root / ".mcp.json"
    launcher = (plugin_root / "scripts" / "fusion_agent_codex_mcp_launcher.py").resolve()
    if not python.is_file():
        raise FileNotFoundError(f"Python interpreter does not exist: {python}")
    if not launcher.is_file():
        raise FileNotFoundError(f"Fusion Agent launcher does not exist: {launcher}")
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    server = payload.setdefault("mcpServers", {}).setdefault("fusion_agent", {})
    server["command"] = str(python)
    server["args"] = [str(launcher)]
    serialized = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        dir=config_path.parent,
        prefix=f".{config_path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, config_path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return config_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plugin-root", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    arguments = parser.parse_args()
    path = configure(arguments.plugin_root, arguments.python)
    print(f"configured_mcp={path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
