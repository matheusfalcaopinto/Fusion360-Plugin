"""Verify personal source, wheel, installed runtime, and Codex cache parity."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

try:
    from scripts.bundle_integrity import (
        BundleIntegrityError,
        collect_source_files,
        verify_wheel,
    )
except (
    ModuleNotFoundError
):  # Executed as ``python scripts/verify_installation_parity.py``.
    from bundle_integrity import (  # type: ignore[no-redef]
        BundleIntegrityError,
        collect_source_files,
        verify_wheel,
    )


SECURITY_ANCHORS = (
    "scripts/fusion_agent_codex_mcp_launcher.py",
    "scripts/preinstall_verify.py",
    "scripts/bundle_integrity.py",
    "scripts/configure_mcp.py",
    "scripts/setup.ps1",
    "scripts/setup.sh",
    "scripts/validate_plugin.py",
    "scripts/verify_installation_parity.py",
)
_PORTABLE_COMMANDS = {"python", "python3"}
_LAUNCHER_ARGUMENT = "scripts/fusion_agent_codex_mcp_launcher.py"
_FUSION_DATA_KEYS = {
    "auth",
    "default_tools_approval_mode",
    "enabled",
    "required",
    "url",
}
_SENSITIVE_QUERY_NAMES = {
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "client_secret",
    "secret",
    "token",
}


class InstallationParityError(RuntimeError):
    """The installed/cache bundle is not the reviewed personal source."""


def verify_installation_parity(
    source_root: Path | str,
    cache_root: Path | str,
    runtime_python: Path | str,
    *,
    verify_installed: bool = True,
) -> dict[str, Any]:
    source = Path(source_root).resolve(strict=True)
    cache = Path(cache_root).resolve(strict=True)
    runtime = Path(runtime_python).resolve(strict=True)
    if not source.is_dir() or not cache.is_dir() or not runtime.is_file():
        raise InstallationParityError("source, cache, and runtime Python must exist")

    source_manifest = _read_object(source / ".codex-plugin" / "plugin.json")
    cache_manifest = _read_object(cache / ".codex-plugin" / "plugin.json")
    if source_manifest != cache_manifest:
        raise InstallationParityError(
            "cache plugin manifest differs from personal source"
        )
    version = str(source_manifest.get("version") or "")
    if not version.startswith("0.4.1+codex."):
        raise InstallationParityError("0.4.1 cachebuster version is missing")

    source_wheel = _single_wheel(source)
    cache_wheel = _single_wheel(cache)
    if source_wheel.read_bytes() != cache_wheel.read_bytes():
        raise InstallationParityError("cache wheel differs from personal source wheel")
    base_version = version.split("+", 1)[0]
    report = verify_wheel(
        cache_wheel,
        plugin_root=source,
        expected_version=base_version,
        require_source_parity=True,
    )
    if verify_installed:
        _verify_installed_with_runtime(runtime, source, cache_wheel)

    source_files = collect_source_files(source)
    cache_files = collect_source_files(cache)
    if source_files != cache_files:
        raise InstallationParityError(
            "cache first-party source differs from personal source"
        )
    for relative in SECURITY_ANCHORS:
        if _bytes(source / relative) != _bytes(cache / relative):
            raise InstallationParityError(f"cache security anchor differs: {relative}")
    _verify_skill_tree(source / "skills", cache / "skills")
    _verify_rewritten_mcp(
        source / ".mcp.json",
        cache / ".mcp.json",
        source,
        runtime,
    )

    return {
        "ok": True,
        "version": version,
        "wheel_sha256": report.sha256,
        "source_file_count": report.source_file_count,
        "runtime_verified": verify_installed,
        "cache_manifest_sha256": hashlib.sha256(
            (cache / ".codex-plugin" / "plugin.json").read_bytes()
        ).hexdigest(),
    }


def _verify_installed_with_runtime(
    runtime: Path,
    source: Path,
    wheel: Path,
) -> None:
    """Run the stdlib verifier under the exact configured MCP interpreter."""

    verifier = (source / "scripts" / "preinstall_verify.py").resolve(strict=True)
    command = [
        str(runtime),
        "-I",
        str(verifier),
        "--plugin-root",
        str(source),
        "--wheel",
        str(wheel),
        "--verify-installed",
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=source,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise InstallationParityError(
            "configured runtime installation verification could not run"
        ) from exc
    try:
        payload = json.loads(completed.stdout.strip())
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise InstallationParityError(
            "configured runtime returned an invalid verification result"
        ) from exc
    if (
        completed.returncode != 0
        or not isinstance(payload, dict)
        or payload.get("ok") is not True
        or payload.get("installed_verified") is not True
    ):
        raise InstallationParityError(
            "configured runtime installation verification failed"
        )


def _verify_rewritten_mcp(
    source_path: Path,
    cache_path: Path,
    source: Path,
    runtime: Path,
) -> None:
    source_payload = _read_object(source_path)
    cache_payload = _read_object(cache_path)
    normalized_source = copy.deepcopy(source_payload)
    normalized_cache = copy.deepcopy(cache_payload)
    source_servers = _server_map(normalized_source, "personal source")
    cache_servers = _server_map(normalized_cache, "cache")

    unexpected_source = set(source_servers) - {"fusion_agent", "fusion_data"}
    unexpected_cache = set(cache_servers) - {"fusion_agent", "fusion_data"}
    if unexpected_source or unexpected_cache:
        raise InstallationParityError(".mcp.json contains an unapproved MCP server")

    source_fusion_data = source_servers.pop("fusion_data", None)
    cache_fusion_data = cache_servers.pop("fusion_data", None)
    if source_fusion_data is not None:
        _verify_fusion_data(source_fusion_data)
        if cache_fusion_data != source_fusion_data:
            raise InstallationParityError(
                "cache fusion_data configuration differs from personal source"
            )
    elif cache_fusion_data is not None:
        # setup may add this one optional server, but its shape is closed and
        # credentials remain owned by Codex OAuth.
        _verify_fusion_data(cache_fusion_data)

    source_server = source_servers.get("fusion_agent")
    cache_server = cache_servers.get("fusion_agent")
    if not isinstance(source_server, dict) or not isinstance(cache_server, dict):
        raise InstallationParityError(".mcp.json has no fusion_agent server")
    _normalize_source_launcher(source_server)
    _normalize_cache_launcher(cache_server, source, runtime)
    _verify_environment(cache_server)
    if normalized_source != normalized_cache:
        raise InstallationParityError(
            "cache .mcp.json differs outside approved command/args rewrites"
        )


def _server_map(payload: dict[str, Any], label: str) -> dict[str, Any]:
    servers = payload.get("mcpServers")
    if not isinstance(servers, dict):
        raise InstallationParityError(f"{label} .mcp.json has no mcpServers object")
    return servers


def _normalize_source_launcher(server: dict[str, Any]) -> None:
    command = server.get("command")
    arguments = server.get("args")
    if command not in _PORTABLE_COMMANDS:
        raise InstallationParityError(
            "personal source .mcp.json command is not portable Python"
        )
    if arguments != [_LAUNCHER_ARGUMENT]:
        raise InstallationParityError(
            "personal source .mcp.json launcher argument is invalid"
        )
    server["command"] = "<runtime-python>"
    server["args"] = ["<personal-source-launcher>"]


def _normalize_cache_launcher(
    server: dict[str, Any], source: Path, runtime: Path
) -> None:
    command_value = server.get("command")
    if not isinstance(command_value, str) or not Path(command_value).is_absolute():
        raise InstallationParityError("cache .mcp.json runtime Python is not absolute")
    try:
        command = Path(command_value).resolve(strict=True)
    except OSError as exc:
        raise InstallationParityError(
            "cache .mcp.json runtime Python is unavailable"
        ) from exc
    if command != runtime:
        raise InstallationParityError("cache .mcp.json runtime Python mismatch")
    arguments = server.get("args")
    if (
        not isinstance(arguments, list)
        or len(arguments) != 1
        or not isinstance(arguments[0], str)
        or not Path(arguments[0]).is_absolute()
    ):
        raise InstallationParityError(
            "cache .mcp.json must contain one absolute launcher argument"
        )
    try:
        launcher = Path(arguments[0]).resolve(strict=True)
    except OSError as exc:
        raise InstallationParityError(
            "cache .mcp.json launcher is unavailable"
        ) from exc
    expected_launcher = (
        source / "scripts" / "fusion_agent_codex_mcp_launcher.py"
    ).resolve(strict=True)
    if launcher != expected_launcher:
        raise InstallationParityError(
            "cache .mcp.json launcher escapes personal source"
        )
    server["command"] = "<runtime-python>"
    server["args"] = ["<personal-source-launcher>"]


def _verify_environment(server: dict[str, Any]) -> None:
    environment = server.get("env")
    if not isinstance(environment, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in environment.items()
    ):
        raise InstallationParityError("cache .mcp.json env must map strings to strings")
    if environment.get("FUSION_AGENT_TOOL_PROFILE") != "normal":
        raise InstallationParityError("cache must install the normal tool profile")
    if environment.get("FUSION_AGENT_BACKEND") not in {
        "autodesk_http",
        "faust_stdio",
    }:
        raise InstallationParityError("cache backend selection is invalid")


def _verify_fusion_data(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != _FUSION_DATA_KEYS:
        raise InstallationParityError("fusion_data configuration shape is invalid")
    if (
        value.get("auth") != "oauth"
        or value.get("enabled") is not True
        or value.get("required") is not False
        or value.get("default_tools_approval_mode") != "writes"
    ):
        raise InstallationParityError("fusion_data security policy is invalid")
    url = value.get("url")
    if not isinstance(url, str):
        raise InstallationParityError("fusion_data URL is invalid")
    parsed = urlsplit(url)
    query_names = {
        name.lower() for name, _ in parse_qsl(parsed.query, keep_blank_values=True)
    }
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or query_names & _SENSITIVE_QUERY_NAMES
    ):
        raise InstallationParityError("fusion_data URL is unsafe")


def _verify_skill_tree(source: Path, cache: Path) -> None:
    source_files = {
        path.relative_to(source).as_posix(): path.read_bytes()
        for path in source.rglob("*")
        if path.is_file()
    }
    cache_files = {
        path.relative_to(cache).as_posix(): path.read_bytes()
        for path in cache.rglob("*")
        if path.is_file()
    }
    if source_files != cache_files:
        raise InstallationParityError("cache skills differ from personal source")


def _single_wheel(root: Path) -> Path:
    wheels = sorted((root / "wheels").glob("fusion_agent_harness-*.whl"))
    if len(wheels) != 1:
        raise InstallationParityError(
            f"expected exactly one wheel in bundle, found {len(wheels)}"
        )
    return wheels[0]


def _read_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InstallationParityError(f"invalid required JSON: {path.name}") from exc
    if not isinstance(payload, dict):
        raise InstallationParityError(f"required JSON is not an object: {path.name}")
    return payload


def _bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise InstallationParityError(
            f"required bundle file is missing: {path.name}"
        ) from exc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--cache-root", type=Path, required=True)
    parser.add_argument("--runtime-python", type=Path, default=Path(sys.executable))
    parser.add_argument("--skip-installed", action="store_true")
    args = parser.parse_args()
    try:
        report = verify_installation_parity(
            args.source_root,
            args.cache_root,
            args.runtime_python,
            verify_installed=not args.skip_installed,
        )
    except (BundleIntegrityError, InstallationParityError, OSError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error_code": "INSTALLATION_PARITY_FAILED",
                    "message": str(exc),
                },
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
