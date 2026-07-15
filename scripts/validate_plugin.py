"""Validate the Fusion Agent Codex plugin bundle."""

from __future__ import annotations

import json
import os
import platform
import sys
import tomllib
import zipfile
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit


REQUIRED_TOOLS = {
    "fusion_agent_session_health",
    "fusion_agent_compact_snapshot",
    "fusion_agent_safe_change_preview",
    "fusion_agent_safe_change_apply",
    "fusion_agent_hub_inventory",
    "fusion_agent_readiness_report",
    "fusion_agent_native_read",
    "fusion_agent_targeted_inspect",
    "fusion_agent_fast_execute",
    "fusion_agent_recover_change",
}
EXPECTED_TOOL_COUNT = 35
EXPECTED_NORMAL_TOOL_COUNT = 12


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
        environment = server.get("env") or {}
        if not command:
            errors.append(".mcp.json fusion_agent.command is missing")
        if not any(_is_launcher_arg(arg) for arg in args):
            errors.append(".mcp.json must launch scripts/fusion_agent_codex_mcp_launcher.py")
        if environment.get("FUSION_MCP_TRANSPORT_MODE") != "legacy":
            errors.append("installed .mcp.json must default FUSION_MCP_TRANSPORT_MODE to legacy")
        expected_profile = os.getenv("FUSION_AGENT_EXPECTED_TOOL_PROFILE", "normal")
        if environment.get("FUSION_AGENT_TOOL_PROFILE") != expected_profile:
            errors.append(
                "installed .mcp.json FUSION_AGENT_TOOL_PROFILE must be "
                f"{expected_profile}"
            )
        expected_backend = os.getenv("FUSION_AGENT_EXPECTED_BACKEND", "autodesk_http")
        if environment.get("FUSION_AGENT_BACKEND") != expected_backend:
            errors.append(
                "installed .mcp.json FUSION_AGENT_BACKEND must be "
                f"{expected_backend}"
            )
        if environment.get("FUSION_AGENT_REMOTE_POLICY") != "loopback_only":
            errors.append("installed .mcp.json must default FUSION_AGENT_REMOTE_POLICY to loopback_only")
        if platform.system().lower() == "windows" and command.lower() == "python":
            venv_python = root / ".venv" / "Scripts" / "python.exe"
            warnings.append(
                "Windows installed configs should prefer explicit .venv Python: "
                f"{venv_python}"
            )
        _check_fusion_data(mcp_json, environment, errors)
    wheels = sorted((root / "wheels").glob("fusion_agent_harness-*.whl"))
    if not wheels:
        errors.append("missing bundled fusion_agent_harness wheel")
    elif len(wheels) != 1:
        errors.append(f"expected exactly one bundled harness wheel, found {len(wheels)}")
    else:
        _check_wheel(root, wheels[0], plugin_json, errors)
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
    source_roots = [root / "harness" / "apps", root / "harness" / "packages"]
    if not all(path.exists() for path in source_roots):
        errors.append("canonical harness source is missing under harness/apps or harness/packages")
        return
    sys.path[:0] = [str(path) for path in source_roots]
    try:
        from fusion_agent_mcp.server import list_tool_definitions, tool_specs
    except Exception as exc:  # noqa: BLE001 - installed validator may not include unpacked source
        warnings.append(f"could not import fusion_agent_mcp.server from working tree: {type(exc).__name__}: {exc}")
        return
    names = {spec.name for spec in tool_specs()}
    missing = sorted(REQUIRED_TOOLS - names)
    if missing:
        errors.append(f"missing required MCP tools: {', '.join(missing)}")
    if len(names) != EXPECTED_TOOL_COUNT:
        errors.append(f"expected {EXPECTED_TOOL_COUNT} MCP tools, found {len(names)}")
    raw = sorted(name for name in names if not name.startswith("fusion_agent_"))
    if raw:
        errors.append(f"raw/non-facade MCP tools exposed: {', '.join(raw)}")
    definitions = tool_specs()
    if any(not spec.input_schema for spec in definitions):
        errors.append("every MCP tool must have an input schema")
    all_public_definitions = list_tool_definitions("all")
    normal_definitions = list_tool_definitions("normal")
    benchmark_definitions = list_tool_definitions("benchmark")
    if len(normal_definitions) != EXPECTED_NORMAL_TOOL_COUNT:
        errors.append(
            f"expected {EXPECTED_NORMAL_TOOL_COUNT} normal-profile MCP tools, "
            f"found {len(normal_definitions)}"
        )
    if any("script" in tool.inputSchema.get("properties", {}) for tool in normal_definitions):
        errors.append("normal MCP profile must not expose arbitrary script input")
    benchmark_names = {tool.name for tool in benchmark_definitions}
    benchmark_direct_mutators = {
        "fusion_agent_fast_execute",
        "fusion_agent_run_session",
        "fusion_agent_safe_change_apply",
    }
    leaked_mutators = sorted(benchmark_names & benchmark_direct_mutators)
    if leaked_mutators:
        errors.append(
            "benchmark profile must mutate only through its isolated runner: "
            + ", ".join(leaked_mutators)
        )
    if any(tool.outputSchema is None for tool in all_public_definitions):
        errors.append("every public MCP tool must have an output schema")
    annotations_by_name = {tool.name: tool.annotations for tool in all_public_definitions}
    for name in REQUIRED_TOOLS & {
        "fusion_agent_native_read",
        "fusion_agent_targeted_inspect",
        "fusion_agent_fast_execute",
        "fusion_agent_recover_change",
    }:
        if annotations_by_name.get(name) is None:
            errors.append(f"new Fast Path tool is missing MCP annotations: {name}")


def _check_wheel(root: Path, wheel: Path, plugin_json: dict, errors: list[str]) -> None:
    pyproject_path = root / "harness" / "pyproject.toml"
    if not pyproject_path.exists():
        errors.append("missing harness/pyproject.toml")
        return
    project = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))["project"]
    version = str(project.get("version") or "")
    expected_name = f"fusion_agent_harness-{version}-py3-none-any.whl"
    if wheel.name != expected_name:
        errors.append(f"wheel name/version mismatch: expected {expected_name}, found {wheel.name}")
    plugin_version = str(plugin_json.get("version") or "")
    if plugin_version.split("+", 1)[0] != version:
        errors.append(f"plugin base version {plugin_version!r} does not match harness {version!r}")
    try:
        with zipfile.ZipFile(wheel) as archive:
            names = set(archive.namelist())
            required = {
                "fusion_agent_mcp/server.py",
                "fusion_agent_mcp/runtime.py",
                "fusion_agent_mcp/benchmark_bridge.py",
                "agent_core/fast_path.py",
                "benchmark/runner.py",
                f"fusion_agent_harness-{version}.dist-info/RECORD",
            }
            missing = sorted(required - names)
            if missing:
                errors.append(f"wheel missing canonical runtime files: {', '.join(missing)}")
            metadata_name = f"fusion_agent_harness-{version}.dist-info/METADATA"
            if metadata_name not in names:
                errors.append("wheel is missing package METADATA")
            else:
                metadata = archive.read(metadata_name).decode("utf-8")
                if "Provides-Extra: faust" not in metadata:
                    errors.append("wheel METADATA is missing the pinned faust extra")
                if 'Requires-Dist: fusion360-mcp-server==0.1.0; extra == "faust"' not in metadata:
                    errors.append("wheel METADATA is missing fusion360-mcp-server==0.1.0 for faust")
            if any("work_unpacked_wheel" in name or ".dist-info/.dist-info" in name for name in names):
                errors.append("wheel contains diagnostic or nested dist-info content")
    except Exception as exc:  # noqa: BLE001 - validator reports artifact failures
        errors.append(f"cannot inspect wheel {wheel}: {type(exc).__name__}: {exc}")


def _check_fusion_data(
    mcp_json: dict,
    fusion_agent_environment: dict,
    errors: list[str],
) -> None:
    """Validate the optional, Codex-managed Fusion Data OAuth server."""

    server = (mcp_json.get("mcpServers") or {}).get("fusion_data")
    if server is None:
        return
    if not isinstance(server, dict):
        errors.append(".mcp.json fusion_data must be an MCP server object")
        return
    parsed = urlsplit(str(server.get("url") or ""))
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        errors.append("fusion_data.url must be an explicit official HTTPS endpoint")
    if parsed.username or parsed.password or parsed.fragment:
        errors.append("fusion_data.url must not contain credentials or a fragment")
    sensitive_query_names = {
        "access_token",
        "api_key",
        "authorization",
        "client_secret",
        "key",
        "token",
    }
    if sensitive_query_names & {name.lower() for name, _ in parse_qsl(parsed.query)}:
        errors.append("fusion_data.url must not contain token or secret query parameters")
    if server.get("auth") != "oauth":
        errors.append("fusion_data.auth must be oauth so Codex owns the token flow")
    if not isinstance(server.get("enabled"), bool):
        errors.append("fusion_data.enabled must be an explicit boolean")
    if server.get("required") is not False:
        errors.append("fusion_data.required must be false")
    if server.get("default_tools_approval_mode") != "writes":
        errors.append("fusion_data.default_tools_approval_mode must be writes")
    if "env" in server or "headers" in server or "bearer_token" in server:
        errors.append("fusion_data credentials must be managed by Codex OAuth, not plugin config")
    leaked = sorted(
        key
        for key in fusion_agent_environment
        if key.upper().startswith("FUSION_DATA_") or "AUTODESK_OAUTH" in key.upper()
    )
    if leaked:
        errors.append(
            "Fusion Data credentials must not pass through fusion_agent env: "
            + ", ".join(leaked)
        )


def _is_launcher_arg(value: object) -> bool:
    text = str(value).replace("\\", "/")
    return text.endswith("scripts/fusion_agent_codex_mcp_launcher.py")


if __name__ == "__main__":
    os.chdir(Path(__file__).resolve().parents[1])
    raise SystemExit(main())
