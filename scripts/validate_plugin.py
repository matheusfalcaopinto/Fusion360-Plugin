"""Validate the Fusion Agent Codex plugin bundle."""

from __future__ import annotations

import json
import os
import platform
import sys
from pathlib import Path


REQUIRED_TOOLS = {
    "fusion_agent_session_health",
    "fusion_agent_compact_snapshot",
    "fusion_agent_safe_change_preview",
    "fusion_agent_safe_change_apply",
    "fusion_agent_hub_inventory",
    "fusion_agent_readiness_report",
}


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    errors: list[str] = []
    warnings: list[str] = []
    plugin_json = _read_json(root / ".codex-plugin" / "plugin.json", errors)
    mcp_json = _read_json(root / ".mcp.json", errors)

    if plugin_json:
        if plugin_json.get("name") != "fusion-agent-codex":
            errors.append("plugin.json name must be fusion-agent-codex")
        if not (root / str(plugin_json.get("skills", "skills"))).exists():
            errors.append("plugin.json skills path does not exist")
    if mcp_json:
        server = (mcp_json.get("mcpServers") or {}).get("fusion_agent") or {}
        command = str(server.get("command") or "")
        args = list(server.get("args") or [])
        if not command:
            errors.append(".mcp.json fusion_agent.command is missing")
        if not any(_is_launcher_arg(arg) for arg in args):
            errors.append(".mcp.json must launch scripts/fusion_agent_codex_mcp_launcher.py")
        if platform.system().lower() == "windows" and command.lower() == "python":
            venv_python = root / ".venv" / "Scripts" / "python.exe"
            warnings.append(
                "Windows installed configs should prefer explicit .venv Python: "
                f"{venv_python}"
            )
    wheels = sorted((root / "wheels").glob("fusion_agent_harness-*.whl"))
    if not wheels:
        errors.append("missing bundled fusion_agent_harness wheel")
    _check_tools(root, errors, warnings)

    payload = {
        "ok": not errors,
        "root": str(root),
        "errors": errors,
        "warnings": warnings,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if not errors else 1


def _read_json(path: Path, errors: list[str]) -> dict:
    if not path.exists():
        errors.append(f"missing {path}")
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - validator reports all parse failures
        errors.append(f"cannot read {path}: {type(exc).__name__}: {exc}")
        return {}


def _check_tools(root: Path, errors: list[str], warnings: list[str]) -> None:
    sys.path.insert(0, str(root / "work_unpacked_wheel"))
    try:
        from fusion_agent_mcp.server import tool_specs
    except Exception as exc:  # noqa: BLE001 - installed validator may not include unpacked source
        warnings.append(f"could not import fusion_agent_mcp.server from working tree: {type(exc).__name__}: {exc}")
        return
    names = {spec.name for spec in tool_specs()}
    missing = sorted(REQUIRED_TOOLS - names)
    if missing:
        errors.append(f"missing required MCP tools: {', '.join(missing)}")


def _is_launcher_arg(value: object) -> bool:
    text = str(value).replace("\\", "/")
    return text.endswith("scripts/fusion_agent_codex_mcp_launcher.py")


if __name__ == "__main__":
    os.chdir(Path(__file__).resolve().parents[1])
    raise SystemExit(main())
