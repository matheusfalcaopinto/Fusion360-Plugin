"""fusion-agent command line interface."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from agent_core.session_controller import SessionController, SessionOptions
from benchmark.adapters import AdapterPrerequisites, build_public_adapter_registry
from benchmark.fusion_agent_driver import FusionAgentCodexPublicDriver
from benchmark.models import BenchmarkRunConfig
from benchmark.public import PublicBenchmarkConfig, PublicBenchmarkRunner
from benchmark.runner import BenchmarkRunner
from fusion_agent_assets import asset_root
from fusion_mcp_adapter.endpoint_policy import (
    EndpointDecision,
    EndpointPolicyError,
    open_url_no_redirects,
    revalidate_resolution,
    validate_endpoint,
)
from fusion_mcp_adapter.manifest_store import ManifestStore
from fusion_mcp_adapter.real_client import RealMcpClient
from fusion_tool_facade.policy import MOCK_FACADE_NATIVE_MAP
from memory.gate import MemoryGate
from memory.retriever import MemoryRetriever
from memory.schemas import (
    MemoryRecord,
    MemoryScope,
    MemorySource,
    MemoryType,
    TrustLevel,
)
from memory.store import MemoryStore

try:  # pragma: no cover - covered when Typer is installed in the target env
    import typer
except (
    ModuleNotFoundError
):  # pragma: no cover - fallback is covered by integration smoke
    typer = None


def _mode(mock: bool, real: bool) -> str:
    if real:
        return "real"
    if mock:
        if _env_bool("FUSION_AGENT_REQUIRE_REAL", False):
            raise ValueError(
                "Fusion Agent is configured for real-only mode; mode must be 'real'"
            )
        return "mock"
    return _default_mode("mock")


async def _inspect(
    mode: str, *, runtime_configuration: Any | None = None
) -> dict[str, Any]:
    async with _controller_session(runtime_configuration) as controller:
        return await controller.inspect(mode=mode, options=SessionOptions(mode=mode))


async def _run(
    prompt: str,
    mode: str,
    project: str,
    max_repairs: int,
    dry_run: bool,
    *,
    runtime_configuration: Any | None = None,
) -> dict[str, Any]:
    _ensure_dry_run_allowed(dry_run, runtime_configuration=runtime_configuration)
    async with _controller_session(runtime_configuration) as controller:
        result = await controller.run(
            prompt,
            project=project,
            mode=mode,
            options=SessionOptions(
                mode=mode, project=project, max_repairs=max_repairs, dry_run=dry_run
            ),
        )
    return result.model_dump(mode="json")


async def _verify(
    prompt: str,
    mode: str,
    project: str,
    *,
    runtime_configuration: Any | None = None,
) -> dict[str, Any]:
    async with _controller_session(runtime_configuration) as controller:
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
    *,
    runtime_configuration: Any | None = None,
) -> dict[str, Any]:
    async with _controller_session(runtime_configuration) as controller:
        result = await controller.capture_viewport(
            project=project,
            mode=mode,
            options=SessionOptions(
                mode=mode, project=project, output_dir=Path(output_dir)
            ),
            output_dir=Path(output_dir),
            name=name,
            view=view,
            isolate_prefix=isolate_prefix,
            width=width,
            height=height,
        )
    return result.model_dump(mode="json")


async def _benchmark_run(
    suite: str,
    mode: str,
    dry_run: bool,
    *,
    runtime_configuration: Any | None = None,
) -> dict[str, Any]:
    _ensure_dry_run_allowed(dry_run, runtime_configuration=runtime_configuration)
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


async def _benchmark_public(
    manifest: str,
    output_dir: str,
    mode: str,
    confirm_real_benchmark: bool,
    disposable_fixture_confirmed: bool,
    include_faults: bool,
    *,
    environment_snapshot: Mapping[str, str] | None = None,
    runtime_configuration: Any | None = None,
) -> dict[str, Any]:
    # Only our own code-owned driver is installed by the CLI. Competitor
    # adapters remain policy wrappers with no executable driver and therefore
    # report ``not_run`` until a trusted embedding injects one explicitly.
    own_adapter_id = "fusion_agent_codex"
    startup_environment = dict(environment_snapshot or {})
    own_driver = FusionAgentCodexPublicDriver(
        output_dir=Path(output_dir),
        manifest_dir="manifests",
        runtime_configuration=runtime_configuration,
        environment_snapshot=startup_environment,
    )
    runner = PublicBenchmarkRunner(
        build_public_adapter_registry(
            drivers={own_adapter_id: own_driver},
            prerequisites={
                own_adapter_id: AdapterPrerequisites(
                    subject_id=own_adapter_id,
                    license_id="MIT",
                    license_accepted=True,
                    entitlement_confirmed=(
                        mode == "mock"
                        or (confirm_real_benchmark and disposable_fixture_confirmed)
                    ),
                    isolated_installation_confirmed=(
                        mode == "mock" or disposable_fixture_confirmed
                    ),
                    normal_profile_equivalent_confirmed=True,
                )
            },
        ),
        environment_snapshot=startup_environment,
    )
    report = await runner.run(
        manifest,
        config=PublicBenchmarkConfig(
            mode=mode,
            confirm_real_benchmark=confirm_real_benchmark,
            disposable_fixture_confirmed=disposable_fixture_confirmed,
            include_faults=include_faults,
        ),
    )
    json_path, markdown_path = runner.write(report, output_dir)
    return {
        "run_id": report.run_id,
        "report_path": str(json_path),
        "summary_path": str(markdown_path),
        "summary": report.summary,
    }


def _default_public_manifest() -> str:
    return str(asset_root("benchmarks") / "public_competitors_v1.json")


def _startup_environment_snapshot() -> dict[str, str]:
    """Capture CLI process metadata before starting an event loop."""

    return dict(os.environ)


def _startup_runtime_configuration() -> Any:
    """Create the immutable Fusion runtime configuration at CLI startup."""

    from fusion_agent_mcp.runtime import RuntimeConfiguration

    return RuntimeConfiguration.from_environment()


@asynccontextmanager
async def _controller_session(runtime_configuration: Any | None):
    """Own one CLI controller built from a pre-event-loop configuration snapshot."""

    if runtime_configuration is None:
        async with SessionController() as controller:
            yield controller
        return
    from fusion_agent_mcp.runtime import FusionAgentRuntime

    runtime = FusionAgentRuntime(configuration=runtime_configuration)
    try:
        yield runtime.controller
    finally:
        await runtime.close()


async def _tools_discover(
    mode: str, *, runtime_configuration: Any | None = None
) -> dict[str, Any]:
    async with _controller_session(runtime_configuration) as controller:
        manifest = await controller.discover_tools(
            mode=mode, options=SessionOptions(mode=mode)
        )
    return manifest.model_dump(mode="json")


async def _tools_probe(
    endpoint: str | None = None,
    *,
    remote_policy: str | None = None,
    remote_allowlist: str | None = None,
    bearer_token: str | None = None,
    transport_mode: str | None = None,
    command: str | None = None,
    use_environment: bool = True,
) -> dict[str, Any]:
    endpoints = [endpoint] if endpoint else _candidate_endpoints()
    probes = []
    for candidate in endpoints:
        if not candidate:
            continue
        policy_token = bearer_token if use_environment else (bearer_token or "")
        decision = validate_endpoint(
            candidate,
            policy=remote_policy,
            allowlist=remote_allowlist,
            bearer_token=policy_token,
        )
        revalidate_resolution(decision)
        health_uri = candidate.removesuffix("/mcp") + "/health"
        health = _http_get_probe(
            health_uri,
            decision=decision,
            bearer_token=bearer_token,
            use_environment=use_environment,
        )
        list_tools: dict[str, Any]
        client = RealMcpClient(
            endpoint=candidate,
            command=command,
            timeout_seconds=3,
            transport_mode=transport_mode or "legacy",
            connect_timeout_seconds=3,
            read_timeout_seconds=3,
            mutation_timeout_seconds=3,
            sse_read_timeout_seconds=3,
            remote_policy=remote_policy,
            remote_allowlist=remote_allowlist,
            bearer_token=bearer_token,
        )
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
        probes.append(
            {
                "endpoint": candidate,
                "health_uri": health_uri,
                "health": health,
                "tools_list": list_tools,
            }
        )
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
                    "available": native_tool in names
                    or native_tool
                    in {"local_noop", "inspection_cache", "local_validation"},
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
    return {
        "policy": {
            "treat_as_data": True,
            "embedded_instructions_are_authoritative": False,
            "blocked_record_count": len(retrieved) - len(gated),
        },
        "records": [record.model_dump(mode="json") for record in gated],
    }


def _memory_write(
    project: str,
    path: str,
    content: str,
    source: str = "user",
    citations: list[str] | None = None,
) -> dict[str, Any]:
    store = MemoryStore()
    memory_source = MemorySource(source)
    if memory_source == MemorySource.LEGACY:
        raise ValueError(
            "legacy is assigned only while reading records without v2 metadata"
        )
    relative = Path(path)
    if relative.is_absolute() or relative.suffix.lower() != ".md":
        raise ValueError("memory path must be a relative .md path")
    title = next(
        (
            line.lstrip("# ").strip()
            for line in content.splitlines()
            if line.startswith("#")
        ),
        relative.stem,
    )
    record = MemoryRecord(
        id=f"project:{project}:{relative.as_posix()}",
        scope=MemoryScope.PROJECT,
        type=MemoryType.SKILL_NOTE,
        summary=title,
        content=content,
        content_path=store.project_root(project) / relative,
        project=project,
        tags=[
            part.lower() for part in relative.stem.replace("-", "_").split("_") if part
        ],
        source=memory_source,
        provenance=["cli:memory_write"],
        trust_level=TrustLevel.UNTRUSTED,
        citations=citations or [],
    )
    target = store.write_record(record)
    return {
        "path": str(target),
        "metadata_path": str(target.with_suffix(target.suffix + ".memory.json")),
        "content_sha256": record.content_sha256,
        "source": record.source,
        "trust_level": record.trust_level,
    }


def _doctor(environment: Mapping[str, Any] | None = None) -> dict[str, Any]:
    snapshot = dict(environment) if environment is not None else None
    manifest_store = ManifestStore()
    return {
        "project_root": str(Path.cwd()),
        "workspace": str(Path("workspace").resolve()),
        "outputs": str(Path("outputs").resolve()),
        "manifests": str(Path("manifests").resolve()),
        "python_executable": sys.executable,
        "launcher_path": (
            snapshot.get("launcher_path", "")
            if snapshot is not None
            else os.getenv("FUSION_AGENT_LAUNCHER")
        )
        or str(
            (Path.cwd() / "scripts" / "fusion_agent_codex_mcp_launcher.py").resolve()
        ),
        "source_plugin_root": (
            snapshot.get("source_plugin_root", "")
            if snapshot is not None
            else os.getenv("FUSION_AGENT_HARNESS_ROOT")
        )
        or str(Path.cwd().resolve()),
        "cache_plugin_version": _plugin_version(),
        "fusion_mcp_endpoint": (
            snapshot.get("fusion_mcp_endpoint", "")
            if snapshot is not None
            else os.getenv("FUSION_MCP_ENDPOINT") or ""
        ),
        "fusion_mcp_endpoint_configured": bool(
            snapshot.get("fusion_mcp_endpoint", "")
            if snapshot is not None
            else os.getenv("FUSION_MCP_ENDPOINT")
        ),
        "fusion_mcp_command_configured": bool(
            snapshot.get("fusion_mcp_command", "")
            if snapshot is not None
            else os.getenv("FUSION_MCP_COMMAND")
        ),
        "fusion_agent_default_mode": (
            str(snapshot.get("default_mode") or "mock")
            if snapshot is not None
            else _default_mode("mock")
        ),
        "fusion_agent_require_real": (
            bool(snapshot.get("require_real", False))
            if snapshot is not None
            else _env_bool("FUSION_AGENT_REQUIRE_REAL", False)
        ),
        "fusion_agent_allow_dry_run": (
            bool(snapshot.get("allow_dry_run", True))
            if snapshot is not None
            else _env_bool("FUSION_AGENT_ALLOW_DRY_RUN", True)
        ),
        "dry_run_policy": (
            "allowed"
            if (
                bool(snapshot.get("allow_dry_run", True))
                if snapshot is not None
                else _env_bool("FUSION_AGENT_ALLOW_DRY_RUN", True)
            )
            else "disabled"
        ),
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


def _ensure_dry_run_allowed(
    dry_run: bool, *, runtime_configuration: Any | None = None
) -> None:
    allow_dry_run = (
        bool(runtime_configuration.allow_dry_run)
        if runtime_configuration is not None
        else True
    )
    if dry_run and not allow_dry_run:
        raise ValueError(
            "Fusion Agent dry-run is disabled by FUSION_AGENT_ALLOW_DRY_RUN=0"
        )


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
            return str(
                json.loads(manifest_path.read_text(encoding="utf-8")).get("version")
                or ""
            )
        except Exception:
            return ""
    return ""


def _http_get_probe(
    uri: str,
    *,
    decision: EndpointDecision,
    bearer_token: str | None = None,
    use_environment: bool = True,
) -> dict[str, Any]:
    headers: dict[str, str] = {}
    if decision.requires_bearer_token:
        token = (
            os.getenv("FUSION_MCP_BEARER_TOKEN", "").strip()
            if use_environment
            else (bearer_token or "").strip()
        )
        if not token:
            return {"ok": False, "error": "remote bearer token is unavailable"}
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(uri, headers=headers)
    try:
        # Resolve again at the last possible point before opening the socket.
        # Redirects are disabled so this validated authority cannot be swapped
        # by a 3xx response.
        revalidate_resolution(decision)
        with open_url_no_redirects(request, timeout=3) as response:
            content = response.read(500).decode("utf-8", errors="replace")
            return {"ok": True, "status": response.status, "content": content}
    except EndpointPolicyError as exc:
        return {"ok": False, "error_code": exc.code, "error": str(exc)}
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

        configuration = _startup_runtime_configuration()
        _print_json(
            asyncio.run(
                _inspect(_mode(mock, real), runtime_configuration=configuration)
            )
        )

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

        configuration = _startup_runtime_configuration()
        _print_json(
            asyncio.run(
                _run(
                    prompt,
                    _mode(mock, real),
                    project,
                    max_repairs,
                    dry_run,
                    runtime_configuration=configuration,
                )
            )
        )

    @app.command("verify")
    def verify_command(
        prompt: str, mock: bool = False, real: bool = False, project: str = "default"
    ) -> None:
        """Verify the active design against a planned CadSpec without executing geometry."""

        configuration = _startup_runtime_configuration()
        _print_json(
            asyncio.run(
                _verify(
                    prompt,
                    _mode(mock, real),
                    project,
                    runtime_configuration=configuration,
                )
            )
        )

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

        configuration = _startup_runtime_configuration()
        _print_json(
            asyncio.run(
                _capture(
                    _mode(mock, real),
                    project,
                    output_dir,
                    name,
                    view,
                    isolate_prefix,
                    width,
                    height,
                    runtime_configuration=configuration,
                )
            )
        )

    @benchmark_app.command("run")
    def benchmark_run_command(
        suite: str, mock: bool = False, real: bool = False, dry_run: bool = False
    ) -> None:
        """Run a benchmark suite."""

        configuration = _startup_runtime_configuration()
        _print_json(
            asyncio.run(
                _benchmark_run(
                    suite,
                    _mode(mock, real),
                    dry_run,
                    runtime_configuration=configuration,
                )
            )
        )

    @benchmark_app.command("public")
    def benchmark_public_command(
        manifest: str = _default_public_manifest(),
        output_dir: str = "outputs/public_benchmark",
        mock: bool = False,
        confirm_real_benchmark: bool = False,
        disposable_fixture_confirmed: bool = False,
        include_faults: bool = True,
    ) -> None:
        """Write an honest public comparison report; unavailable adapters remain not_run."""

        environment_snapshot = _startup_environment_snapshot()
        runtime_configuration = _startup_runtime_configuration()
        _print_json(
            asyncio.run(
                _benchmark_public(
                    manifest,
                    output_dir,
                    "mock" if mock else "real",
                    confirm_real_benchmark,
                    disposable_fixture_confirmed,
                    include_faults,
                    environment_snapshot=environment_snapshot,
                    runtime_configuration=runtime_configuration,
                )
            )
        )

    @tools_app.command("discover")
    def tools_discover_command(mock: bool = False, real: bool = False) -> None:
        """Discover MCP tools and persist a manifest."""

        configuration = _startup_runtime_configuration()
        _print_json(
            asyncio.run(
                _tools_discover(_mode(mock, real), runtime_configuration=configuration)
            )
        )

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
    def memory_write_command(
        project: str,
        path: str,
        content: str,
        source: str = "user",
        citation: list[str] = typer.Option([], "--citation"),
    ) -> None:
        """Write project memory."""

        _print_json(_memory_write(project, path, content, source, citation))

    @app.command("doctor")
    def doctor_command() -> None:
        """Show local configuration."""

        _print_json(_doctor())

else:

    def app() -> None:
        """Argparse fallback used when Typer is not installed."""

        parser = argparse.ArgumentParser(
            prog="fusion-agent", description="Fusion CAD automation harness"
        )
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
        benchmark_public = benchmark_sub.add_parser("public")
        benchmark_public.add_argument("--manifest", default=_default_public_manifest())
        benchmark_public.add_argument(
            "--output-dir", default="outputs/public_benchmark"
        )
        benchmark_public.add_argument("--mock", action="store_true")
        benchmark_public.add_argument("--confirm-real-benchmark", action="store_true")
        benchmark_public.add_argument(
            "--disposable-fixture-confirmed", action="store_true"
        )
        benchmark_public.add_argument("--no-faults", action="store_true")

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
        memory_write.add_argument(
            "--source",
            default="user",
            choices=[
                item.value for item in MemorySource if item != MemorySource.LEGACY
            ],
        )
        memory_write.add_argument("--citation", action="append", default=[])

        subparsers.add_parser("doctor")

        args = parser.parse_args()
        if args.command == "inspect":
            configuration = _startup_runtime_configuration()
            _print_json(
                asyncio.run(
                    _inspect(
                        _mode(args.mock, args.real),
                        runtime_configuration=configuration,
                    )
                )
            )
        elif args.command == "run":
            configuration = _startup_runtime_configuration()
            _print_json(
                asyncio.run(
                    _run(
                        args.prompt,
                        _mode(args.mock, args.real),
                        args.project,
                        args.max_repairs,
                        args.dry_run,
                        runtime_configuration=configuration,
                    )
                )
            )
        elif args.command == "verify":
            configuration = _startup_runtime_configuration()
            _print_json(
                asyncio.run(
                    _verify(
                        args.prompt,
                        _mode(args.mock, args.real),
                        args.project,
                        runtime_configuration=configuration,
                    )
                )
            )
        elif args.command == "capture":
            configuration = _startup_runtime_configuration()
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
                        runtime_configuration=configuration,
                    )
                )
            )
        elif args.command == "benchmark" and args.benchmark_command == "run":
            configuration = _startup_runtime_configuration()
            _print_json(
                asyncio.run(
                    _benchmark_run(
                        args.suite,
                        _mode(args.mock, args.real),
                        args.dry_run,
                        runtime_configuration=configuration,
                    )
                )
            )
        elif args.command == "benchmark" and args.benchmark_command == "public":
            environment_snapshot = _startup_environment_snapshot()
            runtime_configuration = _startup_runtime_configuration()
            _print_json(
                asyncio.run(
                    _benchmark_public(
                        args.manifest,
                        args.output_dir,
                        "mock" if args.mock else "real",
                        args.confirm_real_benchmark,
                        args.disposable_fixture_confirmed,
                        not args.no_faults,
                        environment_snapshot=environment_snapshot,
                        runtime_configuration=runtime_configuration,
                    )
                )
            )
        elif args.command == "tools" and args.tools_command == "discover":
            configuration = _startup_runtime_configuration()
            _print_json(
                asyncio.run(
                    _tools_discover(
                        _mode(args.mock, args.real),
                        runtime_configuration=configuration,
                    )
                )
            )
        elif args.command == "tools" and args.tools_command == "probe":
            _print_json(asyncio.run(_tools_probe(args.endpoint)))
        elif args.command == "tools" and args.tools_command == "propose-mapping":
            _print_json(_tools_propose_mapping())
        elif args.command == "memory" and args.memory_command == "search":
            _print_json(_memory_search(args.query, args.project))
        elif args.command == "memory" and args.memory_command == "write":
            _print_json(
                _memory_write(
                    args.project, args.path, args.content, args.source, args.citation
                )
            )
        elif args.command == "doctor":
            _print_json(_doctor())
        else:
            parser.print_help()


if __name__ == "__main__":
    app()
