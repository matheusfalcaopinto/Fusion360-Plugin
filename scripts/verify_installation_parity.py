"""Verify personal source, wheel, installed runtime, and Codex cache parity."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

try:
    from scripts.bundle_integrity import (
        BundleIntegrityError,
        collect_source_files,
        expected_version_from_checkout,
        valid_codex_cachebuster_version,
        verify_wheel,
    )
except (
    ModuleNotFoundError
):  # Executed as ``python scripts/verify_installation_parity.py``.
    from bundle_integrity import (  # type: ignore[no-redef]
        BundleIntegrityError,
        collect_source_files,
        expected_version_from_checkout,
        valid_codex_cachebuster_version,
        verify_wheel,
    )


SECURITY_ANCHORS = (
    ".gitattributes",
    "LICENSE",
    "harness/README.md",
    "harness/pyproject.toml",
    "harness/requirements/build.in",
    "harness/requirements/build.lock",
    "harness/requirements/faust.lock",
    "harness/requirements/quality.lock",
    "harness/requirements/runtime.lock",
    "harness/requirements/test.lock",
    "harness/source-files.txt",
    "harness/uv.lock",
    "scripts/build-distribution.py",
    "scripts/fusion_agent_codex_mcp_launcher.py",
    "scripts/preinstall_verify.py",
    "scripts/bundle_integrity.py",
    "scripts/configure_mcp.py",
    "scripts/check-ci-release-gate.py",
    "scripts/run-isolated-pip.py",
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


def _require_source_runtime(source: Path, runtime_python: Path | str) -> Path:
    runtime = Path(os.path.abspath(runtime_python))
    runtime_root = source / ".venv"
    scripts_root = runtime_root / ("Scripts" if os.name == "nt" else "bin")
    expected = scripts_root / ("python.exe" if os.name == "nt" else "python")
    if (
        os.path.normcase(str(runtime)) != os.path.normcase(str(expected))
        or not runtime_root.is_dir()
        or not scripts_root.is_dir()
        or _path_is_reparse(runtime_root)
        or _path_is_reparse(scripts_root)
        or not runtime.is_file()
        or (os.name == "nt" and _path_is_reparse(runtime))
    ):
        raise InstallationParityError(
            "runtime Python must use the exact non-reparse personal-source .venv"
        )
    return runtime


def verify_installation_parity(
    source_root: Path | str,
    cache_root: Path | str,
    runtime_python: Path | str,
    *,
    verify_installed: bool = True,
) -> dict[str, Any]:
    source = Path(source_root).resolve(strict=True)
    cache = Path(cache_root).resolve(strict=True)
    if not source.is_dir() or not cache.is_dir():
        raise InstallationParityError("source, cache, and runtime Python must exist")
    runtime = _require_source_runtime(source, runtime_python)

    source_manifest = _read_object(source / ".codex-plugin" / "plugin.json")
    cache_manifest = _read_object(cache / ".codex-plugin" / "plugin.json")
    if source_manifest != cache_manifest:
        raise InstallationParityError(
            "cache plugin manifest differs from personal source"
        )
    version = str(source_manifest.get("version") or "")
    expected_base_version = expected_version_from_checkout(source)
    if not valid_codex_cachebuster_version(
        version, expected_base_version=expected_base_version
    ):
        raise InstallationParityError(
            "cachebuster version must match "
            f"{expected_base_version}+codex.<14-digit valid UTC timestamp>"
        )

    source_wheel = _single_wheel(source)
    cache_wheel = _single_wheel(cache)
    if source_wheel.read_bytes() != cache_wheel.read_bytes():
        raise InstallationParityError("cache wheel differs from personal source wheel")
    report = verify_wheel(
        cache_wheel,
        plugin_root=source,
        expected_version=expected_base_version,
        require_source_parity=True,
    )
    backend = _verify_rewritten_mcp(
        source / ".mcp.json",
        cache / ".mcp.json",
        source,
        runtime,
    )
    if verify_installed:
        dependency_lock = "faust.lock" if backend == "faust_stdio" else "runtime.lock"
        _verify_installed_with_runtime(
            runtime,
            source,
            cache_wheel,
            dependency_lock=dependency_lock,
        )

    source_files = collect_source_files(source)
    cache_files = collect_source_files(cache)
    if source_files != cache_files:
        raise InstallationParityError(
            "cache first-party source differs from personal source"
        )
    for relative in SECURITY_ANCHORS:
        if _contained_bytes(source, relative) != _contained_bytes(cache, relative):
            raise InstallationParityError(f"cache security anchor differs: {relative}")
    _verify_skill_tree(source / "skills", cache / "skills")

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
    *,
    dependency_lock: str,
) -> None:
    """Run the stdlib verifier under the exact configured MCP interpreter."""

    verifier = (source / "scripts" / "preinstall_verify.py").resolve(strict=True)
    runtime_parent = runtime.parent
    environment_root = (
        runtime_parent.parent
        if runtime_parent.name.lower() in {"bin", "scripts"}
        else runtime_parent
    )
    dependency_wheelhouse = environment_root / ".fusion-agent-wheelhouse"
    command = [
        str(runtime),
        "-I",
        "-S",
        "-B",
        str(verifier),
        "--plugin-root",
        str(source),
        "--wheel",
        str(wheel),
        "--verify-installed",
        "--dependency-lock",
        dependency_lock,
        "--dependency-wheelhouse",
        str(dependency_wheelhouse),
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
) -> str:
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
    _normalize_source_launcher(source_server, source, runtime)
    _normalize_cache_launcher(cache_server, source, runtime)
    backend = _verify_environment(cache_server)
    if normalized_source != normalized_cache:
        raise InstallationParityError(
            "cache .mcp.json differs outside approved command/args rewrites"
        )
    return backend


def _server_map(payload: dict[str, Any], label: str) -> dict[str, Any]:
    servers = payload.get("mcpServers")
    if not isinstance(servers, dict):
        raise InstallationParityError(f"{label} .mcp.json has no mcpServers object")
    return servers


def _normalize_source_launcher(
    server: dict[str, Any], source: Path, runtime: Path
) -> None:
    command = server.get("command")
    arguments = server.get("args")
    if command in _PORTABLE_COMMANDS:
        if arguments != ["-I", "-B", _LAUNCHER_ARGUMENT]:
            raise InstallationParityError(
                "personal source .mcp.json launcher argument is invalid"
            )
    elif isinstance(command, str) and Path(command).is_absolute():
        configured_runtime = Path(os.path.abspath(command))
        if not configured_runtime.is_file() or os.path.normcase(
            str(configured_runtime)
        ) != os.path.normcase(str(runtime)):
            raise InstallationParityError(
                "personal source .mcp.json runtime Python mismatch"
            )
        if (
            not isinstance(arguments, list)
            or len(arguments) != 3
            or arguments[:2] != ["-I", "-B"]
            or not isinstance(arguments[2], str)
            or not Path(arguments[2]).is_absolute()
        ):
            raise InstallationParityError(
                "personal source .mcp.json absolute launcher argument is invalid"
            )
        configured_launcher = Path(os.path.abspath(arguments[2]))
        expected_launcher = (
            source / "scripts" / "fusion_agent_codex_mcp_launcher.py"
        ).resolve(strict=True)
        if (
            not configured_launcher.is_file()
            or _path_is_reparse(configured_launcher)
            or os.path.normcase(str(configured_launcher))
            != os.path.normcase(str(expected_launcher))
        ):
            raise InstallationParityError(
                "personal source .mcp.json launcher does not match personal source"
            )
    else:
        raise InstallationParityError(
            "personal source .mcp.json command must be portable Python or the "
            "exact personal-source runtime"
        )
    server["command"] = "<runtime-python>"
    server["args"] = ["-I", "-B", "<personal-source-launcher>"]


def _normalize_cache_launcher(
    server: dict[str, Any], source: Path, runtime: Path
) -> None:
    command_value = server.get("command")
    if not isinstance(command_value, str) or not Path(command_value).is_absolute():
        raise InstallationParityError("cache .mcp.json runtime Python is not absolute")
    command = Path(os.path.abspath(command_value))
    if not command.is_file() or os.path.normcase(str(command)) != os.path.normcase(
        str(runtime)
    ):
        raise InstallationParityError("cache .mcp.json runtime Python mismatch")
    arguments = server.get("args")
    if (
        not isinstance(arguments, list)
        or len(arguments) != 3
        or arguments[:2] != ["-I", "-B"]
        or not isinstance(arguments[2], str)
        or not Path(arguments[2]).is_absolute()
    ):
        raise InstallationParityError(
            "cache .mcp.json must contain -I, -B, and one absolute launcher argument"
        )
    try:
        launcher = Path(arguments[2]).resolve(strict=True)
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
    server["args"] = ["-I", "-B", "<personal-source-launcher>"]


def _verify_environment(server: dict[str, Any]) -> str:
    environment = server.get("env")
    if not isinstance(environment, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in environment.items()
    ):
        raise InstallationParityError("cache .mcp.json env must map strings to strings")
    if environment.get("FUSION_AGENT_TOOL_PROFILE") != "normal":
        raise InstallationParityError("cache must install the normal tool profile")
    backend = environment.get("FUSION_AGENT_BACKEND")
    if not isinstance(backend, str) or backend not in {
        "autodesk_http",
        "faust_stdio",
    }:
        raise InstallationParityError("cache backend selection is invalid")
    return backend


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
    source_files = _regular_tree(source, "personal source skills")
    cache_files = _regular_tree(cache, "cache skills")
    if source_files != cache_files:
        raise InstallationParityError("cache skills differ from personal source")


def _single_wheel(root: Path) -> Path:
    wheels = sorted((root / "wheels").glob("fusion_agent_harness-*.whl"))
    if len(wheels) != 1:
        raise InstallationParityError(
            f"expected exactly one wheel in bundle, found {len(wheels)}"
        )
    wheel = wheels[0]
    if wheel.is_symlink():
        raise InstallationParityError("bundled wheel must not be a symlink")
    resolved = wheel.resolve(strict=True)
    if root != resolved and root not in resolved.parents:
        raise InstallationParityError("bundled wheel escapes its root")
    return wheel


def _read_object(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise InstallationParityError(
            f"required JSON must not be a symlink: {path.name}"
        )
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_json_object,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise InstallationParityError(f"invalid required JSON: {path.name}") from exc
    if not isinstance(payload, dict):
        raise InstallationParityError(f"required JSON is not an object: {path.name}")
    return payload


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError(f"duplicate JSON key: {key}")
        payload[key] = value
    return payload


def _contained_bytes(root: Path, relative: str) -> bytes:
    path = root / relative
    if path.is_symlink():
        raise InstallationParityError(
            f"required bundle file must not be a symlink: {relative}"
        )
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise InstallationParityError(
            f"required bundle file is missing: {path.name}"
        ) from exc
    if root != resolved and root not in resolved.parents:
        raise InstallationParityError(
            f"required bundle file escapes its root: {relative}"
        )
    return resolved.read_bytes()


def _regular_tree(root: Path, label: str) -> dict[str, bytes]:
    if root.is_symlink():
        raise InstallationParityError(f"{label} root must not be a symlink")
    files: dict[str, bytes] = {}
    for path in root.rglob("*"):
        if path.is_symlink():
            raise InstallationParityError(f"{label} contains a symlink")
        if path.is_file():
            files[path.relative_to(root).as_posix()] = path.read_bytes()
    return files


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
