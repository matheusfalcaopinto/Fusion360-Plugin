"""Write a machine-local absolute MCP launcher configuration after setup."""

from __future__ import annotations

import argparse
import json
import os
import stat
import tempfile
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit


TOOL_PROFILES = {"normal", "advanced", "diagnostic", "benchmark", "all"}
BACKENDS = {"autodesk_http", "faust_stdio"}
SENSITIVE_QUERY_NAMES = {
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "client_secret",
    "secret",
    "token",
}


def _path_is_reparse(path: Path) -> bool:
    try:
        junction_check = getattr(path, "is_junction", None)
        return bool(
            path.is_symlink()
            or (callable(junction_check) and junction_check())
            or getattr(path.lstat(), "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        )
    except OSError:
        return True


def _require_contained_runtime(plugin_root: Path, python: Path) -> Path:
    runtime_root = plugin_root / ".venv"
    scripts_root = runtime_root / ("Scripts" if os.name == "nt" else "bin")
    expected = scripts_root / ("python.exe" if os.name == "nt" else "python")
    candidate = Path(os.path.abspath(python))
    if os.path.normcase(str(candidate)) != os.path.normcase(str(expected)):
        raise ValueError("installed MCP runtime must be contained by the plugin .venv")
    if (
        not runtime_root.is_dir()
        or not scripts_root.is_dir()
        or _path_is_reparse(runtime_root)
        or _path_is_reparse(scripts_root)
        or not candidate.is_file()
        or (os.name == "nt" and _path_is_reparse(candidate))
    ):
        raise ValueError(
            "installed MCP runtime must use the exact non-reparse plugin .venv"
        )
    return candidate


def configure(
    plugin_root: Path,
    python: Path,
    *,
    tool_profile: str = "normal",
    backend: str = "autodesk_http",
    faust_command: str | None = None,
    fusion_data_url: str | None = None,
    enable_fusion_data: bool = False,
    require_contained_runtime: bool = False,
) -> Path:
    plugin_root = plugin_root.resolve()
    python = Path(os.path.abspath(python))
    config_path = plugin_root / ".mcp.json"
    launcher = (
        plugin_root / "scripts" / "fusion_agent_codex_mcp_launcher.py"
    ).resolve()
    if not python.is_file():
        raise FileNotFoundError(f"Python interpreter does not exist: {python}")
    if require_contained_runtime:
        python = _require_contained_runtime(plugin_root, python)
    if not launcher.is_file():
        raise FileNotFoundError(f"Fusion Agent launcher does not exist: {launcher}")
    tool_profile = tool_profile.strip().lower()
    backend = backend.strip().lower()
    if tool_profile not in TOOL_PROFILES:
        raise ValueError(
            "tool_profile must be normal, advanced, diagnostic, benchmark, or all"
        )
    if backend not in BACKENDS:
        raise ValueError("backend must be autodesk_http or faust_stdio")
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    server = payload.setdefault("mcpServers", {}).setdefault("fusion_agent", {})
    server["command"] = str(python)
    server["args"] = ["-I", "-B", str(launcher)]
    environment = server.setdefault("env", {})
    environment["FUSION_AGENT_TOOL_PROFILE"] = tool_profile
    environment["FUSION_AGENT_BACKEND"] = backend
    environment.setdefault("FUSION_AGENT_REMOTE_POLICY", "loopback_only")
    if backend == "faust_stdio":
        default_executable = python.parent / (
            "fusion360-mcp-server.exe" if os.name == "nt" else "fusion360-mcp-server"
        )
        environment["FUSION_FAUST_COMMAND"] = (
            faust_command or f'"{default_executable}" --mode socket'
        )
    else:
        environment.pop("FUSION_FAUST_COMMAND", None)

    servers = payload["mcpServers"]
    if enable_fusion_data and not fusion_data_url:
        raise ValueError(
            "--enable-fusion-data requires --fusion-data-url from Autodesk"
        )
    if fusion_data_url and not enable_fusion_data:
        raise ValueError("--fusion-data-url requires --enable-fusion-data")
    if fusion_data_url:
        parsed = urlsplit(fusion_data_url)
        if parsed.scheme.lower() != "https" or not parsed.hostname:
            raise ValueError("Fusion Data MCP URL must be an official HTTPS endpoint")
        if parsed.username or parsed.password or parsed.fragment:
            raise ValueError(
                "Fusion Data MCP URL must not contain credentials or fragments"
            )
        query_names = {
            name.lower() for name, _ in parse_qsl(parsed.query, keep_blank_values=True)
        }
        if query_names & SENSITIVE_QUERY_NAMES:
            raise ValueError(
                "Fusion Data MCP URL must not contain token or secret query parameters"
            )
        servers["fusion_data"] = {
            "url": fusion_data_url,
            "auth": "oauth",
            "enabled": True,
            "required": False,
            "default_tools_approval_mode": "writes",
        }
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
    parser.add_argument(
        "--tool-profile", choices=sorted(TOOL_PROFILES), default="normal"
    )
    parser.add_argument("--backend", choices=sorted(BACKENDS), default="autodesk_http")
    parser.add_argument("--faust-command")
    parser.add_argument("--fusion-data-url")
    parser.add_argument("--enable-fusion-data", action="store_true")
    parser.add_argument("--require-contained-runtime", action="store_true")
    arguments = parser.parse_args()
    path = configure(
        arguments.plugin_root,
        arguments.python,
        tool_profile=arguments.tool_profile,
        backend=arguments.backend,
        faust_command=arguments.faust_command,
        fusion_data_url=arguments.fusion_data_url,
        enable_fusion_data=arguments.enable_fusion_data,
        require_contained_runtime=arguments.require_contained_runtime,
    )
    print(f"configured_mcp={path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
