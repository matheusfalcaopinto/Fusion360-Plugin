"""fusion-agent command line interface."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from agent_core.session_controller import SessionController, SessionOptions
from benchmark.models import BenchmarkRunConfig
from benchmark.runner import BenchmarkRunner
from fusion_mcp_adapter.manifest_store import ManifestStore
from fusion_mcp_adapter.real_client import RealMcpClient
from fusion_tool_facade.policy import MOCK_FACADE_NATIVE_MAP
from memory.gate import MemoryGate
from memory.retriever import MemoryRetriever
from memory.store import MemoryStore

try:  # pragma: no cover - covered when Typer is installed in the target env
    import typer
except ModuleNotFoundError:  # pragma: no cover - fallback is covered by integration smoke
    typer = None


def _mode(mock: bool, real: bool) -> str:
    if real:
        return "real"
    if mock:
        if _env_bool("FUSION_AGENT_REQUIRE_REAL", False):
            raise ValueError("Fusion Agent is configured for real-only mode; mode must be 'real'")
        return "mock"
    return _default_mode("mock")


async def _inspect(mode: str) -> dict[str, Any]:
    async with SessionController() as controller:
        return await controller.inspect(mode=mode, options=SessionOptions(mode=mode))


async def _run(prompt: str, mode: str, project: str, max_repairs: int, dry_run: bool) -> dict[str, Any]:
    _ensure_dry_run_allowed(dry_run)
    async with SessionController() as controller:
        result = await controller.run(
            prompt,
            project=project,
            mode=mode,
            options=SessionOptions(mode=mode, project=project, max_repairs=max_repairs, dry_run=dry_run),
        )
    return result.model_dump(mode="json")


async def _verify(prompt: str, mode: str, project: str) -> dict[str, Any]:
    async with SessionController() as controller:
        result = await controller.verify_active(
            prompt,
            project=project,
            mode=mode,
            options=SessionOptions(mode=mode, project=project),
        )
    return result.model_dump(mode="json")


async def _capture(
    mode: str,
    project: str,
    output_dir: str,
    name: str,
    view: str,
    isolate_prefix: str | None,
    width: int,
    height: int,
) -> dict[str, Any]:
    async with SessionController() as controller:
        result = await controller.capture_viewport(
            project=project,
            mode=mode,
            options=SessionOptions(mode=mode, project=project, output_dir=Path(output_dir)),
            output_dir=Path(output_dir),
            name=name,
            view=view,
            isolate_prefix=isolate_prefix,
            width=width,
            height=height,
        )
    return result.model_dump(mode="json")


async def _benchmark_run(suite: str, mode: str, dry_run: bool) -> dict[str, Any]:
    _ensure_dry_run_allowed(dry_run)
    runner = BenchmarkRunner()
    run = await runner.run_suite(
        suite,
        config=BenchmarkRunConfig(mode=mode, dry_run=dry_run),
    )
    return {
        "run_id": run.report.run_id,
        "report_path": str(run.report_path),
        "summary_path": str(run.summary_path),
        "trial_count": len(run.report.trials),
        "summary": run.report.summary,
    }


async def _tools_discover(mode: str) -> dict[str, Any]:
    async with SessionController() as controller:
        manifest = await controller.discover_tools(mode=mode, options=SessionOptions(mode=mode))
    return manifest.model_dump(mode="json")


async def _tools_probe(endpoint: str | None = None) -> dict[str, Any]:
    endpoints = [endpoint] if endpoint else _candidate_endpoints()
    probes = []
    for candidate in endpoints:
        if not candidate:
            continue
        health_uri = candidate.removesuffix("/mcp") + "/health"
        health = _http_get_probe(health_uri)
        list_tools: dict[str, Any]
        client = RealMcpClient(endpoint=candidate, timeout_seconds=3)
        try:
            manifest = await client.list_tools()
            list_tools = {
                "ok": True,
                "tool_count": len(manifest.tools),
                "sample_tools": sorted(manifest.names())[:10],
            }
        except Exception as exc:  # noqa: BLE001 - probe command must normalize diagnostics
            list_tools = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        finally:
            await client.close(timeout_seconds=2.0)
        probes.append({"endpoint": candidate, "health_uri": health_uri, "health": health, "tools_list": list_tools})
    return {"probes": probes}


def _tools_propose_mapping() -> dict[str, Any]:
    manifest = ManifestStore().load_latest("real")
    names = manifest.names() if manifest else set()
    if {"fusion_mcp_read", "fusion_mcp_execute"}.issubset(names):
        crud_mapping = {
            "inspect_design": "fusion_mcp_execute",
            "create_named_parameter": "fusion_mcp_execute",
            "update_named_parameter": "fusion_mcp_execute",
            "create_component": "fusion_mcp_execute",
            "activate_component": "local_noop",
            "create_sketch_on_plane": "fusion_mcp_execute",
            "draw_constrained_rectangle": "fusion_mcp_execute",
            "draw_constrained_circle": "fusion_mcp_execute",
            "extrude_profile": "fusion_mcp_execute",
            "measure_bounding_box": "inspection_cache",
            "validate_named_objects": "local_validation",
            "undo_redo": "fusion_mcp_update",
            "create_spacer_plate_assembly": "fusion_mcp_execute",
            "create_hinge_assembly": "fusion_mcp_execute",
            "set_component_metadata": "fusion_mcp_execute",
            "create_assembly_joints": "fusion_mcp_execute",
            "capture_viewport": "fusion_mcp_execute",
            "analyze_interference": "fusion_mcp_execute",
            "measure_physical_properties": "fusion_mcp_execute",
        }
        return {
            "manifest_loaded": manifest is not None,
            "profile": "fusion_mcp_crud",
            "proposals": [
                {
                    "facade_operation": facade_operation,
                    "candidate_native_tool": native_tool,
                    "available": native_tool in names or native_tool in {"local_noop", "inspection_cache", "local_validation"},
                    "status": "allowlisted_via_vendor_facade",
                }
                for facade_operation, native_tool in crud_mapping.items()
            ],
        }
    proposals = []
    for facade_operation, native_tool in MOCK_FACADE_NATIVE_MAP.items():
        proposals.append(
            {
                "facade_operation": facade_operation,
                "candidate_native_tool": native_tool,
                "available": native_tool in names,
                "status": "blocked_until_allowlisted",
            }
        )
    return {"manifest_loaded": manifest is not None, "proposals": proposals}


def _memory_search(query: str, project: str) -> dict[str, Any]:
    store = MemoryStore()
    store.seed_global()
    retrieved = MemoryRetriever(store).retrieve(query, project=project)
    gated = MemoryGate().filter(retrieved, query)
    return {"records": [record.model_dump(mode="json") for record in gated]}


def _memory_write(project: str, path: str, content: str) -> dict[str, Any]:
    store = MemoryStore()
    target = store.write_project_markdown(project, path, content)
    return {"path": str(target)}


def _doctor() -> dict[str, Any]:
    manifest_store = ManifestStore()
    return {
        "project_root": str(Path.cwd()),
        "workspace": str(Path("workspace").resolve()),
        "outputs": str(Path("outputs").resolve()),
        "manifests": str(Path("manifests").resolve()),
        "python_executable": sys.executable,
        "launcher_path": os.getenv("FUSION_AGENT_LAUNCHER") or str((Path.cwd() / "scripts" / "fusion_agent_codex_mcp_launcher.py").resolve()),
        "source_plugin_root": os.getenv("FUSION_AGENT_HARNESS_ROOT") or str(Path.cwd().resolve()),
        "cache_plugin_version": _plugin_version(),
        "fusion_mcp_endpoint": os.getenv("FUSION_MCP_ENDPOINT") or "",
        "fusion_mcp_endpoint_configured": bool(os.getenv("FUSION_MCP_ENDPOINT")),
        "fusion_mcp_command_configured": bool(os.getenv("FUSION_MCP_COMMAND")),
        "fusion_agent_default_mode": _default_mode("mock"),
        "fusion_agent_require_real": _env_bool("FUSION_AGENT_REQUIRE_REAL", False),
        "fusion_agent_allow_dry_run": _env_bool("FUSION_AGENT_ALLOW_DRY_RUN", True),
        "dry_run_policy": "disabled" if not _env_bool("FUSION_AGENT_ALLOW_DRY_RUN", True) else "allowed",
        "manifest_status": manifest_store.latest_status(),
    }


def _default_mode(default: str = "mock") -> str:
    value = os.getenv("FUSION_AGENT_DEFAULT_MODE")
    if _env_bool("FUSION_AGENT_REQUIRE_REAL", False):
        return "real"
    if value:
        normalized = value.strip().lower()
        if normalized not in {"mock", "real"}:
            raise ValueError("FUSION_AGENT_DEFAULT_MODE must be 'mock' or 'real'")
        return normalized
    return default


def _ensure_dry_run_allowed(dry_run: bool) -> None:
    if dry_run and not _env_bool("FUSION_AGENT_ALLOW_DRY_RUN", True):
        raise ValueError("Fusion Agent dry-run is disabled by FUSION_AGENT_ALLOW_DRY_RUN=0")


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _candidate_endpoints() -> list[str]:
    import os

    configured = os.getenv("FUSION_MCP_ENDPOINT")
    candidates = [
        configured,
        "http://127.0.0.1:17182/mcp",
        "http://127.0.0.1:17183/mcp",
        "http://127.0.0.1:27182/mcp",
    ]
    seen: set[str] = set()
    unique: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in seen:
            unique.append(candidate)
            seen.add(candidate)
    return unique


def _plugin_version() -> str:
    manifest_path = Path(".codex-plugin") / "plugin.json"
    if manifest_path.exists():
        try:
            return str(json.loads(manifest_path.read_text(encoding="utf-8")).get("version") or "")
        except Exception:
            return ""
    return ""


def _http_get_probe(uri: str) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(uri, timeout=3) as response:  # noqa: S310 - local MCP probe
            content = response.read(500).decode("utf-8", errors="replace")
            return {"ok": True, "status": response.status, "content": content}
    except urllib.error.URLError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - diagnostics only
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


if typer is not None:  # pragma: no cover - requires Typer dependency
    app = typer.Typer(no_args_is_help=True, help="Fusion CAD automation harness")
    benchmark_app = typer.Typer(no_args_is_help=True)
    tools_app = typer.Typer(no_args_is_help=True)
    memory_app = typer.Typer(no_args_is_help=True)
    app.add_typer(benchmark_app, name="benchmark")
    app.add_typer(tools_app, name="tools")
    app.add_typer(memory_app, name="memory")

    @app.command("inspect")
    def inspect_command(mock: bool = False, real: bool = False) -> None:
        """Inspect the active design state."""

        _print_json(asyncio.run(_inspect(_mode(mock, real))))

    @app.command("run")
    def run_command(
        prompt: str,
        mock: bool = False,
        real: bool = False,
        project: str = "default",
        max_repairs: int = 5,
        dry_run: bool = False,
    ) -> None:
        """Run one modeling session."""

        _print_json(asyncio.run(_run(prompt, _mode(mock, real), project, max_repairs, dry_run)))

    @app.command("verify")
    def verify_command(prompt: str, mock: bool = False, real: bool = False, project: str = "default") -> None:
        """Verify the active design against a planned CadSpec without executing geometry."""

        _print_json(asyncio.run(_verify(prompt, _mode(mock, real), project)))

    @app.command("capture")
    def capture_command(
        mock: bool = False,
        real: bool = False,
        project: str = "default",
        output_dir: str = "outputs",
        name: str = "active_design_capture",
        view: str = "isometric",
        isolate_prefix: str | None = None,
        width: int = 1600,
        height: int = 1100,
    ) -> None:
        """Capture the active Fusion viewport through the safe facade."""

        _print_json(asyncio.run(_capture(_mode(mock, real), project, output_dir, name, view, isolate_prefix, width, height)))

    @benchmark_app.command("run")
    def benchmark_run_command(suite: str, mock: bool = False, real: bool = False, dry_run: bool = False) -> None:
        """Run a benchmark suite."""

        _print_json(asyncio.run(_benchmark_run(suite, _mode(mock, real), dry_run)))

    @tools_app.command("discover")
    def tools_discover_command(mock: bool = False, real: bool = False) -> None:
        """Discover MCP tools and persist a manifest."""

        _print_json(asyncio.run(_tools_discover(_mode(mock, real))))

    @tools_app.command("probe")
    def tools_probe_command(endpoint: str | None = None) -> None:
        """Probe candidate real MCP endpoints without saving manifests."""

        _print_json(asyncio.run(_tools_probe(endpoint)))

    @tools_app.command("propose-mapping")
    def tools_propose_mapping_command() -> None:
        """Propose facade/native mappings from the latest manifest."""

        _print_json(_tools_propose_mapping())

    @memory_app.command("search")
    def memory_search_command(query: str, project: str = "default") -> None:
        """Search gated memory."""

        _print_json(_memory_search(query, project))

    @memory_app.command("write")
    def memory_write_command(project: str, path: str, content: str) -> None:
        """Write project memory."""

        _print_json(_memory_write(project, path, content))

    @app.command("doctor")
    def doctor_command() -> None:
        """Show local configuration."""

        _print_json(_doctor())

else:

    def app() -> None:
        """Argparse fallback used when Typer is not installed."""

        parser = argparse.ArgumentParser(prog="fusion-agent", description="Fusion CAD automation harness")
        subparsers = parser.add_subparsers(dest="command")

        inspect_parser = subparsers.add_parser("inspect")
        inspect_parser.add_argument("--mock", action="store_true")
        inspect_parser.add_argument("--real", action="store_true")

        run_parser = subparsers.add_parser("run")
        run_parser.add_argument("prompt")
        run_parser.add_argument("--mock", action="store_true")
        run_parser.add_argument("--real", action="store_true")
        run_parser.add_argument("--project", default="default")
        run_parser.add_argument("--max-repairs", type=int, default=5)
        run_parser.add_argument("--dry-run", action="store_true")

        verify_parser = subparsers.add_parser("verify")
        verify_parser.add_argument("prompt")
        verify_parser.add_argument("--mock", action="store_true")
        verify_parser.add_argument("--real", action="store_true")
        verify_parser.add_argument("--project", default="default")

        capture_parser = subparsers.add_parser("capture")
        capture_parser.add_argument("--mock", action="store_true")
        capture_parser.add_argument("--real", action="store_true")
        capture_parser.add_argument("--project", default="default")
        capture_parser.add_argument("--output-dir", default="outputs")
        capture_parser.add_argument("--name", default="active_design_capture")
        capture_parser.add_argument("--view", default="isometric")
        capture_parser.add_argument("--isolate-prefix")
        capture_parser.add_argument("--width", type=int, default=1600)
        capture_parser.add_argument("--height", type=int, default=1100)

        benchmark_parser = subparsers.add_parser("benchmark")
        benchmark_sub = benchmark_parser.add_subparsers(dest="benchmark_command")
        benchmark_run = benchmark_sub.add_parser("run")
        benchmark_run.add_argument("suite")
        benchmark_run.add_argument("--mock", action="store_true")
        benchmark_run.add_argument("--real", action="store_true")
        benchmark_run.add_argument("--dry-run", action="store_true")

        tools_parser = subparsers.add_parser("tools")
        tools_sub = tools_parser.add_subparsers(dest="tools_command")
        tools_discover = tools_sub.add_parser("discover")
        tools_discover.add_argument("--mock", action="store_true")
        tools_discover.add_argument("--real", action="store_true")
        tools_probe = tools_sub.add_parser("probe")
        tools_probe.add_argument("--endpoint")
        tools_sub.add_parser("propose-mapping")

        memory_parser = subparsers.add_parser("memory")
        memory_sub = memory_parser.add_subparsers(dest="memory_command")
        memory_search = memory_sub.add_parser("search")
        memory_search.add_argument("query")
        memory_search.add_argument("--project", default="default")
        memory_write = memory_sub.add_parser("write")
        memory_write.add_argument("project")
        memory_write.add_argument("path")
        memory_write.add_argument("content")

        subparsers.add_parser("doctor")

        args = parser.parse_args()
        if args.command == "inspect":
            _print_json(asyncio.run(_inspect(_mode(args.mock, args.real))))
        elif args.command == "run":
            _print_json(asyncio.run(_run(args.prompt, _mode(args.mock, args.real), args.project, args.max_repairs, args.dry_run)))
        elif args.command == "verify":
            _print_json(asyncio.run(_verify(args.prompt, _mode(args.mock, args.real), args.project)))
        elif args.command == "capture":
            _print_json(
                asyncio.run(
                    _capture(
                        _mode(args.mock, args.real),
                        args.project,
                        args.output_dir,
                        args.name,
                        args.view,
                        args.isolate_prefix,
                        args.width,
                        args.height,
                    )
                )
            )
        elif args.command == "benchmark" and args.benchmark_command == "run":
            _print_json(asyncio.run(_benchmark_run(args.suite, _mode(args.mock, args.real), args.dry_run)))
        elif args.command == "tools" and args.tools_command == "discover":
            _print_json(asyncio.run(_tools_discover(_mode(args.mock, args.real))))
        elif args.command == "tools" and args.tools_command == "probe":
            _print_json(asyncio.run(_tools_probe(args.endpoint)))
        elif args.command == "tools" and args.tools_command == "propose-mapping":
            _print_json(_tools_propose_mapping())
        elif args.command == "memory" and args.memory_command == "search":
            _print_json(_memory_search(args.query, args.project))
        elif args.command == "memory" and args.memory_command == "write":
            _print_json(_memory_write(args.project, args.path, args.content))
        elif args.command == "doctor":
            _print_json(_doctor())
        else:
            parser.print_help()


if __name__ == "__main__":
    app()
