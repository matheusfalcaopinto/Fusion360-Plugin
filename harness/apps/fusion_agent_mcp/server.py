"""Local MCP server that exposes only the safe ``fusion_agent_*`` surface."""

from __future__ import annotations

import json
import os
import hashlib
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import anyio
import mcp.types as types
from mcp.server.lowlevel import Server

from agent_core.guardrails import PlannerUnsupportedError
from agent_core.fast_path import FastPathResponse
from agent_core.planner import PlanningRequest, RuleBasedPlanner
from agent_core.session_controller import SessionOptions
from benchmark.loader import BenchmarkSuiteError, load_benchmark_suite
from benchmark.models import BenchmarkRunConfig
from benchmark.runner import BenchmarkRunner
from cad_spec.models import CadSpec
from cli.main import _doctor, _tools_probe, _tools_propose_mapping
from fusion_agent_mcp.runtime import FusionAgentRuntime
from fusion_agent_mcp.benchmark_bridge import FusionRuntimeBenchmarkBridge
from fusion_agent_mcp import __version__
from fusion_agent_assets import asset_root
from memory.gate import MemoryGate
from memory.retriever import MemoryRetriever
from telemetry.trace import redact_sensitive
from memory.store import MemoryStore
from skills.loader import SkillLoader
from skills.router import SkillRouter


JsonDict = dict[str, Any]
HandlerResult = JsonDict | FastPathResponse
Handler = Callable[[JsonDict], Awaitable[HandlerResult]]

WORKSPACE_ROOT = Path("workspace")
OUTPUTS_ROOT = Path("outputs")
MANIFEST_ROOT = Path("manifests")
BENCHMARK_ROOT = Path("benchmarks")
FAST_PATH_OUTPUT_ROOT = OUTPUTS_ROOT / "fast_path"
SESSION_ARTIFACTS = {
    "cad_spec.json",
    "prompt.md",
    "verification.json",
    "tool_trace.jsonl",
    "session_journal.json",
    "final_summary.md",
    "memory_summary.md",
    "capture.json",
}


@dataclass(frozen=True)
class ToolSpec:
    """MCP tool metadata and handler."""

    name: str
    description: str
    input_schema: JsonDict
    handler: Handler
    output_schema: JsonDict | None = None
    annotations: types.ToolAnnotations | None = None


_RUNTIME_OVERRIDE: ContextVar[FusionAgentRuntime | None] = ContextVar(
    "fusion_agent_runtime_override",
    default=None,
)
_DEFAULT_RUNTIME: FusionAgentRuntime | None = None


def get_runtime() -> FusionAgentRuntime:
    """Return the process-scoped lazy runtime used by direct tool calls."""

    override = _RUNTIME_OVERRIDE.get()
    if override is not None:
        return override
    global _DEFAULT_RUNTIME
    if _DEFAULT_RUNTIME is None:
        _DEFAULT_RUNTIME = FusionAgentRuntime(
            manifest_root=MANIFEST_ROOT,
            outputs_root=OUTPUTS_ROOT,
        )
    return _DEFAULT_RUNTIME


def list_tool_definitions() -> list[types.Tool]:
    """Return MCP tool definitions for the safe harness wrapper."""

    return [
        types.Tool(
            name=spec.name,
            description=spec.description,
            inputSchema=spec.input_schema,
            outputSchema=spec.output_schema or _open_output_schema(),
            annotations=spec.annotations,
        )
        for spec in tool_specs()
    ]


async def execute_tool(
    name: str,
    arguments: JsonDict | None = None,
    *,
    runtime: FusionAgentRuntime | None = None,
) -> JsonDict:
    """Execute one wrapper tool by name and return a JSON-serializable payload."""

    response = await execute_tool_response(name, arguments, runtime=runtime)
    return response.payload


async def execute_tool_response(
    name: str,
    arguments: JsonDict | None = None,
    *,
    runtime: FusionAgentRuntime | None = None,
) -> FastPathResponse:
    """Execute one tool while preserving structured and binary MCP channels."""

    spec = _tool_spec_map().get(name)
    if spec is None:
        raise ValueError(f"unknown fusion agent MCP tool: {name}")
    token = _RUNTIME_OVERRIDE.set(runtime) if runtime is not None else None
    try:
        result = await spec.handler(arguments or {})
    finally:
        if token is not None:
            _RUNTIME_OVERRIDE.reset(token)
    if isinstance(result, FastPathResponse):
        return result
    return FastPathResponse(payload=result)


def build_server(runtime: FusionAgentRuntime | None = None) -> Server:
    """Build the stdio MCP server used by Codex and other MCP clients."""

    app = Server("fusion-agent-harness", version=__version__)
    server_runtime = runtime or get_runtime()

    @app.list_tools()
    async def list_tools() -> list[types.Tool]:
        return list_tool_definitions()

    @app.call_tool()
    async def call_tool(name: str, arguments: dict | None) -> list[types.ContentBlock] | types.CallToolResult:
        try:
            response = await execute_tool_response(
                name,
                dict(arguments or {}),
                runtime=server_runtime,
            )
        except Exception as exc:  # noqa: BLE001 - MCP boundary normalizes all failures
            payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=_compact_summary(name, payload, False))],
                structuredContent=payload,
                isError=True,
            )
        return _as_call_tool_result(name, response)

    return app


def main() -> int:
    """Run the MCP server over stdio."""

    from mcp.server.stdio import stdio_server

    async def arun() -> None:
        runtime = FusionAgentRuntime(manifest_root=MANIFEST_ROOT, outputs_root=OUTPUTS_ROOT)
        app = build_server(runtime)
        try:
            async with stdio_server() as streams:
                await app.run(streams[0], streams[1], app.create_initialization_options())
        finally:
            with anyio.move_on_after(2.0):
                await runtime.close(timeout_seconds=2.0)

    anyio.run(arun)
    return 0


def tool_specs() -> list[ToolSpec]:
    """Return all safe MCP wrapper tool specs."""

    return [
        ToolSpec("fusion_agent_doctor", "Show harness configuration and paths.", _schema(), _doctor_tool),
        ToolSpec("fusion_agent_readiness_report", "Summarize environment, cache, endpoint, and manifest readiness.", _schema(), _readiness_report_tool),
        ToolSpec("fusion_agent_probe", "Probe candidate real Fusion MCP endpoints.", _schema({"endpoint": _string()}), _probe_tool),
        ToolSpec("fusion_agent_session_health", "Differentiate launcher, MCP server, real endpoint, manifest, and native tool-surface health.", _mode_schema(default="real"), _session_health_tool),
        ToolSpec(
            "fusion_agent_inspect",
            "Inspect selected design sections with hard entity, time, and response budgets.",
            _inspect_schema(),
            _inspect_tool,
        ),
        ToolSpec(
            "fusion_agent_native_read",
            "Read Autodesk API documentation, projects, documents, active-command state, or a PNG screenshot through a bounded safe wrapper.",
            _native_read_schema(),
            _native_read_tool,
            output_schema=_fast_path_output_schema(),
            annotations=_tool_annotations(read_only=True, idempotent=True),
        ),
        ToolSpec(
            "fusion_agent_targeted_inspect",
            "Inspect up to 50 explicitly selected Fusion document/entities in one audited read-only native script; ambiguous names are never selected silently.",
            _targeted_inspect_schema(),
            _targeted_inspect_tool,
            output_schema=_fast_path_output_schema(),
            annotations=_tool_annotations(read_only=True, idempotent=True),
        ),
        ToolSpec(
            "fusion_agent_fast_execute",
            "Lint, bind declared targets, dispatch at most once without automatic post-dispatch replay, and programmatically verify a bounded native Fusion script. Scoped updates must mutate targets[query_id]; additive scripts must create only through target_components[component_path]. Delete, move, visibility, componentize, bulk, hidden/shared, and ambiguous work remains Safe Harness only.",
            _fast_execute_schema(),
            _fast_execute_tool,
            output_schema=_fast_path_output_schema(),
            annotations=_tool_annotations(read_only=False, idempotent=False),
        ),
        ToolSpec(
            "fusion_agent_recover_change",
            "Explicitly undo or redo only the latest verified Fast Path mutation in this runtime after a no-drift check; never invoked automatically.",
            _recover_change_schema(),
            _recover_change_tool,
            output_schema=_fast_path_output_schema(),
            annotations=_tool_annotations(read_only=False, idempotent=False, destructive=True),
        ),
        ToolSpec("fusion_agent_compact_snapshot", "Capture a capped component-scoped programmatic snapshot for large designs.", _compact_snapshot_schema(), _compact_snapshot_tool),
        ToolSpec("fusion_agent_hub_inventory", "Inventory Fusion Personal Library metadata using metadata search and findFileById enrichment.", _hub_inventory_schema(), _hub_inventory_tool),
        ToolSpec("fusion_agent_safe_change_preview", "Classify intended Fusion changes and create a baseline-backed preview.", _safe_change_preview_schema(), _safe_change_preview_tool),
        ToolSpec("fusion_agent_safe_change_apply", "Apply one small previewed reversible batch and abort on visible regressions.", _safe_change_apply_schema(), _safe_change_apply_tool),
        ToolSpec("fusion_agent_verify_active_design", "Verify the active design against a planned CadSpec without executing geometry.", _verify_schema(), _verify_active_design_tool),
        ToolSpec("fusion_agent_capture_viewport", "Capture the active Fusion viewport through the safe facade.", _capture_schema(), _capture_viewport_tool),
        ToolSpec("fusion_agent_run_session", "Run one full modeling session through the harness.", _run_schema(), _run_session_tool),
        ToolSpec("fusion_agent_dry_run_session", "Plan and simulate one modeling session without MCP calls.", _dry_run_schema(), _dry_run_session_tool),
        ToolSpec("fusion_agent_list_sessions", "List saved session journals.", _schema({"project": _string(), "limit": _integer(1, 100)}), _list_sessions_tool),
        ToolSpec("fusion_agent_read_session_artifact", "Read an allowlisted session artifact.", _read_artifact_schema(), _read_session_artifact_tool),
        ToolSpec("fusion_agent_read_trace", "Read parsed events from a session tool trace.", _read_trace_schema(), _read_trace_tool),
        ToolSpec("fusion_agent_plan_spec", "Create a CAD Spec JSON document for a prompt.", _plan_schema(), _plan_spec_tool),
        ToolSpec("fusion_agent_validate_spec", "Validate a CAD Spec JSON string.", _schema({"spec_json": _string()}, ["spec_json"]), _validate_spec_tool),
        ToolSpec("fusion_agent_export_spec_json", "Plan a CAD Spec and optionally save it under outputs/.", _export_spec_schema(), _export_spec_json_tool),
        ToolSpec("fusion_agent_list_benchmarks", "List benchmark suites and case counts.", _schema(), _list_benchmarks_tool),
        ToolSpec("fusion_agent_run_benchmark", "Run a strict benchmark_suite.v2 A/B trial set with internal or isolated Codex driver and immutable run artifacts.", _benchmark_schema(), _run_benchmark_tool),
        ToolSpec("fusion_agent_read_benchmark_report", "Read a paginated benchmark run view by run_id, or an explicitly selected legacy report.", _benchmark_report_schema(), _read_benchmark_report_tool),
        ToolSpec("fusion_agent_discover_tools", "Discover MCP tools through mock or real client and save manifest.", _mode_schema(default="real"), _discover_tools_tool),
        ToolSpec("fusion_agent_propose_mapping", "Propose safe facade/native mappings from latest manifest.", _schema(), _propose_mapping_tool),
        ToolSpec("fusion_agent_read_manifest", "Read the latest real/mock or named tool manifest.", _read_manifest_schema(), _read_manifest_tool),
        ToolSpec("fusion_agent_memory_search", "Search gated global/project memory.", _memory_search_schema(), _memory_search_tool),
        ToolSpec("fusion_agent_memory_write", "Write project Markdown memory.", _memory_write_schema(), _memory_write_tool),
        ToolSpec("fusion_agent_memory_list_project", "List global and project memory records.", _schema({"project": _string()}), _memory_list_project_tool),
        ToolSpec("fusion_agent_skills_list", "List all filesystem-backed harness skills.", _schema(), _skills_list_tool),
        ToolSpec("fusion_agent_skills_get", "Read one harness skill by name.", _schema({"name": _string()}, ["name"]), _skills_get_tool),
        ToolSpec("fusion_agent_skills_rank", "Rank harness skills for a request.", _schema({"query": _string(), "limit": _integer(1, 12)}, ["query"]), _skills_rank_tool),
    ]


async def _doctor_tool(_: JsonDict) -> JsonDict:
    return _doctor()


async def _readiness_report_tool(_: JsonDict) -> JsonDict:
    doctor = _doctor()
    safe_tools = sorted(spec.name for spec in tool_specs())
    return {
        "doctor": doctor,
        "safe_facade_tool_count": len(safe_tools),
        "safe_facade_tools": safe_tools,
        "manifest_status": get_runtime().manifest_store.latest_status(),
        "persistent_runtime": get_runtime().diagnostics(),
        "recommended_startup_sequence": [
            "Call fusion_agent_native_read(query_type=api_documentation) for only the APIs needed.",
            "Call fusion_agent_targeted_inspect for a bounded baseline.",
            "Call fusion_agent_fast_execute only when the route and feature flag allow it.",
            "Use doctor, probe, health, or broad inspection only on request or after a readiness failure.",
        ],
    }


async def _probe_tool(args: JsonDict) -> JsonDict:
    return await _tools_probe(_optional_str(args, "endpoint"))


async def _session_health_tool(args: JsonDict) -> JsonDict:
    mode = _mode(args, default="real")
    health = await get_runtime().controller.session_health(mode=mode, options=_session_options(mode=mode))
    if mode == "real":
        health["persistent_runtime"] = get_runtime().diagnostics()
    return health


async def _inspect_tool(args: JsonDict) -> JsonDict:
    mode = _mode(args, default="mock")
    inspection_options = {
        key: args[key]
        for key in ("sections", "max_entities_visited", "deadline_ms", "max_response_bytes")
        if key in args
    }
    return await get_runtime().controller.inspect(
        mode=mode,
        options=_session_options(mode=mode),
        inspection_options=inspection_options,
    )


async def _native_read_tool(args: JsonDict) -> FastPathResponse:
    blocked = _fast_path_block("fusion_agent_native_read", args)
    if blocked:
        return blocked
    mode = _mode(args, default="real")
    return await get_runtime().fast_path(mode).native_read(args)


async def _targeted_inspect_tool(args: JsonDict) -> FastPathResponse:
    blocked = _fast_path_block("fusion_agent_targeted_inspect", args)
    if blocked:
        return blocked
    mode = _mode(args, default="real")
    return await get_runtime().fast_path(mode).targeted_inspect(args)


async def _fast_execute_tool(args: JsonDict) -> FastPathResponse:
    blocked = _fast_path_block("fusion_agent_fast_execute", args)
    if blocked:
        return blocked
    fast_mode = _fast_path_mode()
    change_class = str(args.get("change_class") or "")
    if fast_mode == "read_only" and change_class != "read_only":
        return FastPathResponse(
            {
                "status": "blocked_before_apply",
                "reason": "fast_path_read_only",
                "recommended_path": "safe_harness",
                "message": "Fusion Agent 0.2.2 enables mutating Fast Execute only when FUSION_AGENT_FAST_PATH_MODE=enabled.",
            }
        )
    mode = _mode(args, default="real")
    response = await get_runtime().fast_path(mode).fast_execute(args)
    script = str(args.get("script") or "")
    operation_id = str(response.payload.get("operation_id") or f"audit_{hashlib.sha256(script.encode('utf-8')).hexdigest()[:16]}")
    artifacts = _write_fast_path_audit(operation_id, args, response)
    response.payload["artifacts"] = artifacts
    return response


async def _recover_change_tool(args: JsonDict) -> FastPathResponse:
    blocked = _fast_path_block("fusion_agent_recover_change", args)
    if blocked:
        return blocked
    if _fast_path_mode() != "enabled":
        return FastPathResponse(
            {
                "status": "blocked_before_apply",
                "reason": "fast_path_read_only",
                "recommended_path": "manual_fusion_undo_or_safe_harness",
            }
        )
    mode = _mode(args, default="real")
    return await get_runtime().fast_path(mode).recover_change(args)


async def _compact_snapshot_tool(args: JsonDict) -> JsonDict:
    project = _optional_str(args, "project") or "opencode"
    _safe_name(project, "project")
    mode = _mode(args, default="real")
    return await get_runtime().controller.compact_snapshot(
        project=project,
        mode=mode,
        options=_session_options(mode=mode, project=project),
        max_occurrences=int(args.get("max_occurrences", 500)),
        max_bodies=int(args.get("max_bodies", 500)),
        include_transforms=bool(args.get("include_transforms", False)),
        max_entities_visited=int(
            args.get("max_entities_visited", os.getenv("FUSION_AGENT_INSPECTION_MAX_ENTITIES", "1000"))
        ),
        deadline_ms=int(
            args.get("deadline_ms", os.getenv("FUSION_AGENT_INSPECTION_DEADLINE_MS", "1500"))
        ),
        max_response_bytes=int(
            args.get(
                "max_response_bytes",
                os.getenv("FUSION_AGENT_INSPECTION_MAX_RESPONSE_BYTES", "1048576"),
            )
        ),
    )


async def _hub_inventory_tool(args: JsonDict) -> JsonDict:
    mode = _mode(args, default="real")
    return await get_runtime().controller.hub_inventory(
        mode=mode,
        query=_optional_str(args, "query") or "",
        max_results=int(args.get("max_results", 50)),
        enrich=bool(args.get("enrich", True)),
        options=_session_options(mode=mode),
    )


async def _safe_change_preview_tool(args: JsonDict) -> JsonDict:
    project = _required_str(args, "project")
    _safe_name(project, "project")
    mode = _mode(args, default="real")
    targets = args.get("targets")
    if not isinstance(targets, list):
        raise ValueError("targets must be an array")
    policy = args.get("policy") or {}
    if not isinstance(policy, dict):
        raise ValueError("policy must be an object")
    return await get_runtime().controller.safe_change_preview(
        project=project,
        mode=mode,
        operation=_required_str(args, "operation"),
        targets=[dict(item) for item in targets],
        policy=policy,
        options=_session_options(mode=mode, project=project),
    )


async def _safe_change_apply_tool(args: JsonDict) -> JsonDict:
    _ensure_safe_harness_route("fusion_agent_safe_change_apply")
    project = _required_str(args, "project")
    _safe_name(project, "project")
    mode = _mode(args, default="real")
    return await get_runtime().controller.safe_change_apply(
        project=project,
        mode=mode,
        preview_id=_required_str(args, "preview_id"),
        batch_size=int(args.get("batch_size", 5)),
        confirm_destructive=bool(args.get("confirm_destructive", False)),
        save_after=bool(args.get("save_after", False)),
        options=_session_options(mode=mode, project=project),
    )


async def _verify_active_design_tool(args: JsonDict) -> JsonDict:
    prompt = _required_str(args, "prompt")
    project = _optional_str(args, "project") or "opencode"
    _safe_name(project, "project")
    mode = _mode(args, default="mock")
    result = await get_runtime().controller.verify_active(
        prompt,
        project=project,
        mode=mode,
        options=_session_options(mode=mode, project=project),
    )
    return result.model_dump(mode="json")


async def _capture_viewport_tool(args: JsonDict) -> JsonDict:
    project = _optional_str(args, "project") or "opencode"
    _safe_name(project, "project")
    mode = _mode(args, default="mock")
    output_dir_arg = _optional_str(args, "output_dir") or ""
    output_dir = _safe_relative_path(OUTPUTS_ROOT, output_dir_arg) if output_dir_arg else OUTPUTS_ROOT
    name = _optional_str(args, "name") or "active_design_capture"
    if Path(name).is_absolute() or ".." in Path(name).parts or "/" in name or "\\" in name:
        raise ValueError("name must be a simple relative PNG filename")
    view = _optional_str(args, "view") or "isometric"
    if view not in {"isometric", "front", "top", "right"}:
        raise ValueError("view must be one of: isometric, front, top, right")
    result = await get_runtime().controller.capture_viewport(
        project=project,
        mode=mode,
        options=_session_options(mode=mode, project=project),
        output_dir=output_dir,
        name=name,
        view=view,
        isolate_prefix=_optional_str(args, "isolate_prefix"),
        width=int(args.get("width", 1600)),
        height=int(args.get("height", 1100)),
    )
    return result.model_dump(mode="json")


async def _run_session_tool(args: JsonDict) -> JsonDict:
    dry_run = bool(args.get("dry_run", False))
    _ensure_dry_run_allowed(dry_run)
    return await _run_session(args, dry_run=dry_run)


async def _dry_run_session_tool(args: JsonDict) -> JsonDict:
    _ensure_dry_run_allowed(True)
    return await _run_session(args, dry_run=True)


async def _run_session(args: JsonDict, *, dry_run: bool) -> JsonDict:
    _ensure_safe_harness_route("fusion_agent_run_session")
    _ensure_dry_run_allowed(dry_run)
    prompt = _required_str(args, "prompt")
    project = _optional_str(args, "project") or "opencode"
    _safe_name(project, "project")
    mode = _mode(args, default="mock")
    max_repairs = int(args.get("max_repairs", 5))
    result = await get_runtime().controller.run(
        prompt,
        project=project,
        mode=mode,
        options=_session_options(
            mode=mode,
            project=project,
            max_repairs=max_repairs,
            dry_run=dry_run,
        ),
    )
    return result.model_dump(mode="json")


async def _list_sessions_tool(args: JsonDict) -> JsonDict:
    project = _optional_str(args, "project")
    if project:
        _safe_name(project, "project")
    limit = int(args.get("limit", 20))
    sessions: list[JsonDict] = []
    projects = [project] if project else [path.name for path in sorted((WORKSPACE_ROOT / "projects").glob("*")) if path.is_dir()]
    for project_name in projects:
        root = WORKSPACE_ROOT / "projects" / str(project_name) / "sessions"
        if not root.exists():
            continue
        for session_dir in sorted(root.iterdir(), key=lambda item: item.name, reverse=True):
            if not session_dir.is_dir():
                continue
            journal_path = session_dir / "session_journal.json"
            journal = _read_json(journal_path) if journal_path.exists() else {}
            sessions.append(
                {
                    "project": project_name,
                    "session_id": session_dir.name,
                    "path": str(session_dir),
                    "final_status": journal.get("final_status"),
                    "summary": journal.get("summary"),
                    "artifacts": sorted(path.name for path in session_dir.iterdir() if path.is_file()),
                }
            )
    return {"sessions": sessions[:limit]}


async def _read_session_artifact_tool(args: JsonDict) -> JsonDict:
    project = _required_str(args, "project")
    session_id = _required_str(args, "session_id")
    artifact = _required_str(args, "artifact")
    if artifact not in SESSION_ARTIFACTS:
        raise ValueError(f"artifact is not allowlisted: {artifact}")
    path = _session_dir(project, session_id) / artifact
    if not path.exists():
        raise FileNotFoundError(path)
    content = path.read_text(encoding="utf-8")
    return {"path": str(path), "artifact": artifact, "content": content, "json": _try_json(content)}


async def _read_trace_tool(args: JsonDict) -> JsonDict:
    project = _required_str(args, "project")
    session_id = _required_str(args, "session_id")
    limit = int(args.get("limit", 100))
    path = _session_dir(project, session_id) / "tool_trace.jsonl"
    if not path.exists():
        raise FileNotFoundError(path)
    events = [_try_json(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return {"path": str(path), "events": events[-limit:], "event_count": len(events)}


async def _plan_spec_tool(args: JsonDict) -> JsonDict:
    project = _optional_str(args, "project") or "opencode"
    _safe_name(project, "project")
    prompt = _required_str(args, "prompt")
    try:
        spec, metadata = await _plan_spec(prompt, project)
    except PlannerUnsupportedError as exc:
        return exc.payload()
    except ValueError as exc:
        return _planner_route_payload(prompt, str(exc))
    return {"cad_spec": spec.model_dump(mode="json"), "cad_spec_json": spec.to_json_text(), **metadata}


async def _validate_spec_tool(args: JsonDict) -> JsonDict:
    try:
        spec = CadSpec.model_validate_json(_required_str(args, "spec_json"))
    except Exception as exc:  # noqa: BLE001 - validator diagnostics
        return {"valid": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"valid": True, "cad_spec": spec.model_dump(mode="json")}


async def _export_spec_json_tool(args: JsonDict) -> JsonDict:
    project = _optional_str(args, "project") or "opencode"
    _safe_name(project, "project")
    prompt = _required_str(args, "prompt")
    try:
        spec, metadata = await _plan_spec(prompt, project)
    except PlannerUnsupportedError as exc:
        return exc.payload()
    except ValueError as exc:
        return _planner_route_payload(prompt, str(exc))
    output_path = _optional_str(args, "output_path")
    payload: JsonDict = {"cad_spec": spec.model_dump(mode="json"), "cad_spec_json": spec.to_json_text(), **metadata}
    if output_path:
        path = _safe_relative_path(OUTPUTS_ROOT, output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(spec.to_json_text(), encoding="utf-8")
        payload["path"] = str(path)
    return payload


async def _list_benchmarks_tool(_: JsonDict) -> JsonDict:
    candidates = [_default_benchmark_suite(), *sorted(BENCHMARK_ROOT.glob("*.json"))]
    suites = []
    seen: set[Path] = set()
    for path in candidates:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            suite = load_benchmark_suite(path)
            suites.append(
                {
                    "name": path.name,
                    "path": str(path),
                    "schema_version": suite.schema_version,
                    "suite_id": suite.suite_id,
                    "case_count": len(suite.cases),
                    "valid": True,
                }
            )
        except (BenchmarkSuiteError, FileNotFoundError, ValueError) as exc:
            suites.append({"name": path.name, "path": str(path), "valid": False, "error": str(exc)})
    return {"suites": suites}


async def _run_benchmark_tool(args: JsonDict) -> JsonDict:
    suite = _resolve_benchmark_suite(_optional_str(args, "suite"))
    mode = _mode(args, default="mock")
    project = _optional_str(args, "project") or "opencode_benchmarks"
    _safe_name(project, "project")
    execution_paths = args.get("execution_paths") or ["safe_harness", "native_fast"]
    config = BenchmarkRunConfig.model_validate(
        {
            "driver": str(args.get("driver") or "internal"),
            "mode": mode,
            "execution_paths": execution_paths,
            "repetitions": int(args.get("repetitions", 1)),
            "warmups": int(args.get("warmups", 0)),
            "seed": int(args.get("seed", 42)),
            "model": args.get("model"),
            "reasoning_effort": str(args.get("reasoning_effort") or "high"),
            "confirm_real_benchmark": bool(args.get("confirm_real_benchmark", False)),
            "baseline_run_id": args.get("baseline_run_id"),
            "project": project,
            "dry_run": bool(args.get("dry_run", False)),
        }
    )
    if config.dry_run:
        _ensure_dry_run_allowed(True)
    runtime = get_runtime()
    diagnostics = runtime.diagnostics()
    bridge = FusionRuntimeBenchmarkBridge(runtime) if config.mode == "real" else None
    runner = BenchmarkRunner(
        controller=runtime.controller,
        workspace_root=WORKSPACE_ROOT,
        output_dir=OUTPUTS_ROOT,
        manifest_dir=MANIFEST_ROOT,
        route_executors=bridge.route_executors if bridge is not None else None,
        oracle_observer=bridge if bridge is not None else None,
        real_lifecycle=bridge,
        environment_metadata={
            "plugin_version": os.getenv("FUSION_AGENT_PLUGIN_VERSION", "0.2.2"),
            "mcp_fingerprint": diagnostics.get("fingerprint"),
            "connection_generation": diagnostics.get("connection_generation"),
        },
    )
    run = await runner.run_suite(suite, config=config)
    return {
        "schema_version": run.report.schema_version,
        "run_id": run.report.run_id,
        "suite_id": run.report.suite_id,
        "trial_count": len(run.report.trials),
        "summary": run.report.summary,
        "report_path": str(run.report_path),
        "summary_path": str(run.summary_path),
        "trials_path": str(run.trials_path),
        "environment_path": str(run.environment_path),
    }


async def _read_benchmark_report_tool(args: JsonDict) -> JsonDict:
    path_arg = _optional_str(args, "path")
    legacy_path = _safe_relative_path(OUTPUTS_ROOT, path_arg) if path_arg else None
    runner = BenchmarkRunner(
        controller=get_runtime().controller,
        workspace_root=WORKSPACE_ROOT,
        output_dir=OUTPUTS_ROOT,
        manifest_dir=MANIFEST_ROOT,
    )
    return runner.read_report(
        run_id=_optional_str(args, "run_id"),
        view=_optional_str(args, "view") or "report",
        offset=int(args.get("offset", 0)),
        limit=int(args.get("limit", 100)),
        legacy_path=legacy_path,
    )


async def _discover_tools_tool(args: JsonDict) -> JsonDict:
    mode = _mode(args, default="real")
    manifest = await get_runtime().controller.discover_tools(mode=mode, options=_session_options(mode=mode))
    return manifest.model_dump(mode="json")


async def _propose_mapping_tool(_: JsonDict) -> JsonDict:
    return _tools_propose_mapping()


async def _read_manifest_tool(args: JsonDict) -> JsonDict:
    path_arg = _optional_str(args, "path")
    if path_arg:
        path = _safe_relative_path(MANIFEST_ROOT, path_arg)
    else:
        source = _optional_str(args, "source") or "real"
        if source not in {"real", "mock"}:
            raise ValueError("source must be 'real' or 'mock'")
        path = MANIFEST_ROOT / f"fusion_mcp_tools_latest_{source}.json"
    if not path.exists():
        return {"loaded": False, "path": str(path), "manifest": None, "manifest_source": None}
    manifest = _read_json(path)
    return {"loaded": True, "path": str(path), "manifest": manifest, "manifest_source": manifest.get("source")}


async def _memory_search_tool(args: JsonDict) -> JsonDict:
    query = _required_str(args, "query")
    project = _optional_str(args, "project") or "opencode"
    _safe_name(project, "project")
    store = MemoryStore(workspace_root=WORKSPACE_ROOT)
    store.seed_global()
    records = MemoryGate().filter(MemoryRetriever(store).retrieve(query, project=project), query)
    return {"records": [record.model_dump(mode="json") for record in records]}


async def _memory_write_tool(args: JsonDict) -> JsonDict:
    project = _required_str(args, "project")
    _safe_name(project, "project")
    relative_path = _required_str(args, "path")
    content = _required_str(args, "content")
    if Path(relative_path).is_absolute() or ".." in Path(relative_path).parts:
        raise ValueError("memory path must be relative and stay under the project memory root")
    path = MemoryStore(workspace_root=WORKSPACE_ROOT).write_project_markdown(project, relative_path, content)
    return {"path": str(path)}


async def _memory_list_project_tool(args: JsonDict) -> JsonDict:
    project = _optional_str(args, "project") or "opencode"
    _safe_name(project, "project")
    store = MemoryStore(workspace_root=WORKSPACE_ROOT)
    store.seed_global()
    records = store.iter_records(project=project)
    return {"records": [record.model_dump(mode="json") for record in records]}


async def _skills_list_tool(_: JsonDict) -> JsonDict:
    skills = SkillLoader().load().all()
    return {"skills": [_skill_payload(skill, include_content=False) for skill in skills]}


async def _skills_get_tool(args: JsonDict) -> JsonDict:
    skill = SkillLoader().load().get(_required_str(args, "name"))
    if skill is None:
        raise KeyError(args["name"])
    return {"skill": _skill_payload(skill, include_content=True)}


async def _skills_rank_tool(args: JsonDict) -> JsonDict:
    registry = SkillLoader().load()
    ranked = SkillRouter(registry).rank(_required_str(args, "query"), limit=int(args.get("limit", 3)))
    return {"skills": [_skill_payload(skill, include_content=False) for skill in ranked]}


async def _plan_spec(prompt: str, project: str) -> tuple[CadSpec, JsonDict]:
    store = MemoryStore(workspace_root=WORKSPACE_ROOT)
    store.seed_global()
    retrieved = MemoryRetriever(store).retrieve(prompt, project=project)
    gated_memory = MemoryGate().filter(retrieved, prompt)
    ranked_skills = SkillRouter(SkillLoader().load()).rank(prompt)
    spec = await RuleBasedPlanner().plan(
        PlanningRequest(
            user_prompt=prompt,
            project=project,
            memory=gated_memory,
            skills=[skill.name for skill in ranked_skills],
        )
    )
    return (
        spec,
        {
            "project": project,
            "memory_records": [record.model_dump(mode="json") for record in gated_memory],
            "skills": [_skill_payload(skill, include_content=False) for skill in ranked_skills],
        },
    )


def _tool_spec_map() -> dict[str, ToolSpec]:
    return {spec.name: spec for spec in tool_specs()}


def _planner_route_payload(prompt: str, reason: str) -> JsonDict:
    lowered = prompt.lower()
    destructive = any(
        term in lowered
        for term in (
            "delete",
            "remove",
            "cleanup",
            "reorgan",
            "move",
            "visibility",
            "hidden",
            "shared",
            "componentize",
            "apagar",
            "remover",
            "mover",
            "ocult",
        )
    )
    return {
        "supported": False,
        "code": "unsupported_for_legacy_cadspec_recipe",
        "reason": reason,
        "recommended_path": "safe_harness" if destructive else "api_documentation_then_native_fast",
        "recommended_tools": (
            ["fusion_agent_compact_snapshot", "fusion_agent_safe_change_preview"]
            if destructive
            else [
                "fusion_agent_native_read",
                "fusion_agent_targeted_inspect",
                "fusion_agent_fast_execute",
            ]
        ),
    }


def _default_benchmark_suite() -> Path:
    return asset_root("benchmarks") / "benchmark_suite_v2.json"


def _resolve_benchmark_suite(value: str | None) -> Path:
    if value is None or value == "benchmark_suite_v2.json":
        path = _default_benchmark_suite()
    else:
        path = _safe_relative_path(BENCHMARK_ROOT, value)
    if path.suffix.lower() != ".json":
        raise ValueError("benchmark suite must be a benchmark_suite.v2 JSON file")
    if not path.exists():
        raise FileNotFoundError(path)
    return path


def _session_options(
    *,
    mode: str,
    project: str = "opencode",
    max_repairs: int = 5,
    dry_run: bool = False,
) -> SessionOptions:
    return SessionOptions(
        mode=mode,
        project=project,
        max_repairs=max_repairs,
        workspace_root=WORKSPACE_ROOT,
        output_dir=OUTPUTS_ROOT,
        manifest_dir=MANIFEST_ROOT,
        dry_run=dry_run,
    )


def _skill_payload(skill: Any, *, include_content: bool) -> JsonDict:
    payload = skill.model_dump(mode="json")
    if not include_content:
        payload.pop("content", None)
    return payload


def _mode(args: JsonDict, *, default: str) -> str:
    value = str(args.get("mode", _default_mode(default)))
    if value not in {"mock", "real"}:
        raise ValueError("mode must be 'mock' or 'real'")
    if _env_bool("FUSION_AGENT_REQUIRE_REAL", False) and value != "real":
        raise ValueError("Fusion Agent is configured for real-only mode; mode must be 'real'")
    return value


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


def _required_str(args: JsonDict, key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} is required")
    return value


def _optional_str(args: JsonDict, key: str) -> str | None:
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


def _session_dir(project: str, session_id: str) -> Path:
    _safe_name(project, "project")
    _safe_name(session_id, "session_id")
    return WORKSPACE_ROOT / "projects" / project / "sessions" / session_id


def _safe_name(value: str, label: str) -> None:
    if not value or any(part in value for part in ("/", "\\", "..")):
        raise ValueError(f"{label} must be a simple path segment")


def _safe_relative_path(root: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("path must be relative and must not contain '..'")
    if path.parts and path.parts[0] == root.name:
        path = Path(*path.parts[1:]) if len(path.parts) > 1 else Path()
    return root / path


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _try_json(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def _json_text(payload: Any) -> str:
    return json.dumps(_jsonable(payload), indent=2, sort_keys=True)


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: _jsonable(child) for key, child in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(child) for child in value]
    return value


def _compact_summary(name: str, payload: JsonDict, ok: bool) -> str:
    summary: JsonDict = {"ok": ok, "tool": name}
    for key in (
        "status",
        "query_type",
        "operation_id",
        "execution_path",
        "duration_ms",
        "error",
        "reason",
        "recommended_path",
    ):
        if key in payload:
            summary[key] = payload[key]
    if len(summary) == 2:
        summary["result_keys"] = sorted(payload)[:24]
    return json.dumps(_jsonable(summary), ensure_ascii=False, separators=(",", ":"))


def _as_call_tool_result(name: str, response: FastPathResponse) -> types.CallToolResult:
    structured = {"ok": not response.is_error, "result": response.payload}
    content: list[types.ContentBlock] = [
        types.TextContent(
            type="text",
            text=_compact_summary(name, response.payload, not response.is_error),
        )
    ]
    content.extend(_mcp_content_blocks(response.content))
    return types.CallToolResult(
        meta=response.meta or None,
        content=content,
        structuredContent=structured,
        isError=response.is_error,
    )


def _mcp_content_blocks(content: list[dict[str, Any]]) -> list[types.ContentBlock]:
    blocks: list[types.ContentBlock] = []
    for raw in content:
        if not isinstance(raw, dict):
            continue
        if raw.get("type") == "image" and isinstance(raw.get("data"), str):
            blocks.append(
                types.ImageContent(
                    type="image",
                    data=raw["data"],
                    mimeType=str(raw.get("mimeType") or raw.get("mime_type") or "image/png"),
                )
            )
        elif raw.get("type") == "text" and isinstance(raw.get("text"), str):
            blocks.append(types.TextContent(type="text", text=raw["text"]))
    return blocks


def _open_output_schema() -> JsonDict:
    return {"type": "object", "additionalProperties": True}


def _fast_path_output_schema() -> JsonDict:
    return {
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "result": {"type": "object", "additionalProperties": True},
        },
        "required": ["ok", "result"],
        "additionalProperties": False,
    }


def _tool_annotations(
    *,
    read_only: bool,
    idempotent: bool,
    destructive: bool = False,
) -> types.ToolAnnotations:
    return types.ToolAnnotations(
        readOnlyHint=read_only,
        destructiveHint=destructive,
        idempotentHint=idempotent,
        openWorldHint=False,
    )


def _fast_path_mode() -> str:
    value = os.getenv("FUSION_AGENT_FAST_PATH_MODE", "read_only").strip().lower()
    if value not in {"off", "read_only", "enabled"}:
        raise ValueError("FUSION_AGENT_FAST_PATH_MODE must be off, read_only, or enabled")
    if (
        os.getenv("FUSION_AGENT_BENCHMARK_ROUTE_LOCK", "").strip().lower() == "native_fast"
        and os.getenv("FUSION_AGENT_BENCHMARK_TRIAL_ID")
        and (
            os.getenv("FUSION_AGENT_BENCHMARK_MODE", "mock").strip().lower() == "mock"
            or _env_bool("FUSION_AGENT_BENCHMARK_CONFIRM_REAL", False)
        )
    ):
        return "enabled"
    return value


def _execution_path() -> str:
    value = os.getenv("FUSION_AGENT_EXECUTION_PATH", "auto").strip().lower()
    if value not in {"auto", "native_fast", "safe_harness"}:
        raise ValueError("FUSION_AGENT_EXECUTION_PATH must be auto, native_fast, or safe_harness")
    route_lock = os.getenv("FUSION_AGENT_BENCHMARK_ROUTE_LOCK")
    if route_lock:
        route_lock = route_lock.strip().lower()
        if route_lock not in {"native_fast", "safe_harness"} or value != route_lock:
            raise ValueError(
                f"benchmark route lock mismatch: locked={route_lock!r}, execution_path={value!r}"
            )
    return value


def _fast_path_block(tool_name: str, _: JsonDict) -> FastPathResponse | None:
    if _execution_path() == "safe_harness":
        return FastPathResponse(
            {
                "status": "blocked_before_apply",
                "reason": "route_lock_safe_harness",
                "recommended_path": "safe_harness",
                "tool": tool_name,
            }
        )
    if _fast_path_mode() == "off":
        return FastPathResponse(
            {
                "status": "blocked_before_apply",
                "reason": "fast_path_disabled",
                "recommended_path": "safe_harness",
                "tool": tool_name,
            }
        )
    return None


def _ensure_safe_harness_route(tool_name: str) -> None:
    if _execution_path() == "native_fast":
        raise ValueError(f"{tool_name} blocked by native_fast route lock")


def _write_fast_path_audit(
    operation_id: str,
    arguments: JsonDict,
    response: FastPathResponse,
) -> JsonDict:
    _safe_name(operation_id, "operation_id")
    root = FAST_PATH_OUTPUT_ROOT / operation_id
    root.mkdir(parents=True, exist_ok=True)
    script = str(arguments.get("script") or "")
    audit_path = root / "audit.json"
    audit = {
        "operation_id": operation_id,
        "script": redact_sensitive(script, key="script"),
        "request": redact_sensitive(arguments),
        "response": redact_sensitive(response.payload),
        "is_error": response.is_error,
    }
    audit_path.write_text(_json_text(audit) + "\n", encoding="utf-8", newline="\n")
    return {"audit_path": str(audit_path)}


def _schema(properties: JsonDict | None = None, required: list[str] | None = None) -> JsonDict:
    return {
        "type": "object",
        "properties": properties or {},
        "required": required or [],
        "additionalProperties": False,
    }


def _string() -> JsonDict:
    return {"type": "string"}


def _boolean(default: bool | None = None) -> JsonDict:
    schema: JsonDict = {"type": "boolean"}
    if default is not None:
        schema["default"] = default
    return schema


def _integer(minimum: int | None = None, maximum: int | None = None, default: int | None = None) -> JsonDict:
    schema: JsonDict = {"type": "integer"}
    if minimum is not None:
        schema["minimum"] = minimum
    if maximum is not None:
        schema["maximum"] = maximum
    if default is not None:
        schema["default"] = default
    return schema


def _mode_property(default: str = "mock") -> JsonDict:
    return {"type": "string", "enum": ["mock", "real"], "default": _default_mode(default)}


def _mode_schema(default: str = "mock") -> JsonDict:
    return _schema({"mode": _mode_property(default)})


def _inspection_budget_properties() -> JsonDict:
    return {
        "max_entities_visited": _integer(1, 5000, 1000),
        "deadline_ms": _integer(50, 5000, 1500),
        "max_response_bytes": _integer(4096, 1_048_576, 1_048_576),
    }


def _inspect_schema() -> JsonDict:
    return _schema(
        {
            "mode": _mode_property("mock"),
            "sections": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": [
                        "document",
                        "counts",
                        "geometry",
                        "parameters",
                        "assembly",
                        "physical_properties",
                        "legacy_recipe_metrics",
                    ],
                },
                "minItems": 1,
                "uniqueItems": True,
                "default": ["document", "counts"],
            },
            **_inspection_budget_properties(),
        }
    )


def _inspection_query_schema() -> JsonDict:
    return {
        "type": "object",
        "properties": {
            "id": _string(),
            "entity_type": {
                "type": "string",
                "enum": ["document", "component", "occurrence", "body", "sketch", "feature", "parameter"],
            },
            "selector": {
                "type": "object",
                "properties": {
                    "entity_token": _string(),
                    "path": _string(),
                    "component_path": {
                        "type": "string",
                        "description": "Exact full component occurrence path. It is valid only together with selector.name.",
                    },
                    "name": _string(),
                },
                "additionalProperties": False,
            },
            "fields": {"type": "array", "items": _string(), "maxItems": 40},
        },
        "required": ["id", "entity_type"],
        "additionalProperties": False,
    }


def _verification_contract_schema(*, require_arrays: bool = True) -> JsonDict:
    return {
        "type": "object",
        "properties": {
            "queries": {
                "type": "array",
                "items": _inspection_query_schema(),
                "minItems": 1,
                "maxItems": 50,
            },
            "assertions": {
                "type": "array",
                "maxItems": 100,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": _string(),
                        "query_id": _string(),
                        "field": _string(),
                        "operator": {
                            "type": "string",
                            "enum": [
                                "eq",
                                "ne",
                                "approx",
                                "gte",
                                "lte",
                                "contains",
                                "unchanged",
                                "increased_by",
                                "decreased_by",
                            ],
                        },
                        "expected": {},
                        "tolerance": {"type": "number", "minimum": 0},
                    },
                    "required": ["query_id", "field", "operator"],
                    "additionalProperties": False,
                },
            },
            "limit_per_query": _integer(1, 100, 20),
            "include_screenshot": _boolean(False),
        },
        "required": ["queries", "assertions"] if require_arrays else [],
        "additionalProperties": False,
    }


def _native_read_schema() -> JsonDict:
    return _schema(
        {
            "mode": _mode_property("real"),
            "query_type": {
                "type": "string",
                "enum": ["api_documentation", "projects", "document", "active_command", "screenshot"],
            },
            "search_pattern": _string(),
            "api_category": _string(),
            "filter": _string(),
            "operation": {"type": "string", "enum": ["search", "list_open", "recent"]},
            "name": _string(),
            "fusion_project": _string(),
            "width": _integer(32, 4096),
            "height": _integer(32, 4096),
            "anti_aliasing": _boolean(),
            "transparent_background": _boolean(),
            "direction": {
                "type": "string",
                "enum": [
                    "current",
                    "front",
                    "back",
                    "bottom",
                    "top",
                    "left",
                    "right",
                    "iso-bottom-left",
                    "iso-bottom-right",
                    "iso-top-left",
                    "iso-top-right",
                ],
            },
        },
        ["query_type"],
    )


def _targeted_inspect_schema() -> JsonDict:
    return _schema(
        {
            "mode": _mode_property("real"),
            "queries": {
                "type": "array",
                "items": _inspection_query_schema(),
                "minItems": 1,
                "maxItems": 50,
            },
            "limit_per_query": _integer(1, 100, 20),
            **_inspection_budget_properties(),
        },
        ["queries"],
    )


def _fast_execute_schema() -> JsonDict:
    schema = _schema(
        {
            "mode": _mode_property("real"),
            "intent": _string(),
            "change_class": {"type": "string", "enum": ["read_only", "additive", "scoped_update"]},
            "script": {
                "type": "string",
                "minLength": 1,
                "maxLength": 65536,
                "description": "Exactly one run(_context: str). Scoped mutations must use targets[query_id]; additive creation must derive from target_components[exact component_path].",
            },
            "target_query_ids": {
                "type": "array",
                "items": _string(),
                "maxItems": 20,
                "uniqueItems": True,
                "description": "Mutation queries that bind exact entity tokens (scoped_update) or future entities with selector.component_path (additive).",
            },
            "verification": _verification_contract_schema(require_arrays=False),
            "api_references": {"type": "array", "items": _string(), "maxItems": 100},
        },
        ["intent", "change_class", "script"],
    )
    schema["allOf"] = [
        {
            "if": {
                "properties": {"change_class": {"enum": ["additive", "scoped_update"]}},
                "required": ["change_class"],
            },
            "then": {
                "required": ["target_query_ids", "verification"],
                "properties": {
                    "target_query_ids": {"minItems": 1},
                    "verification": {
                        "properties": {
                            "queries": {"minItems": 1},
                            "assertions": {"minItems": 1},
                        }
                    },
                },
            },
        }
    ]
    return schema


def _recover_change_schema() -> JsonDict:
    return _schema(
        {
            "mode": _mode_property("real"),
            "action": {"type": "string", "enum": ["undo", "redo"]},
            "operation_id": _string(),
            "confirm": {"type": "boolean", "const": True},
            "verification": _verification_contract_schema(),
        },
        ["action", "operation_id", "confirm", "verification"],
    )


def _run_schema() -> JsonDict:
    return _schema(
        {
            "prompt": _string(),
            "mode": _mode_property("mock"),
            "project": _string(),
            "max_repairs": _integer(0, 20, 5),
            "dry_run": _boolean(False),
        },
        ["prompt"],
    )


def _dry_run_schema() -> JsonDict:
    return _schema(
        {
            "prompt": _string(),
            "mode": _mode_property("mock"),
            "project": _string(),
            "max_repairs": _integer(0, 20, 5),
        },
        ["prompt"],
    )


def _verify_schema() -> JsonDict:
    return _schema(
        {
            "prompt": _string(),
            "mode": _mode_property("mock"),
            "project": _string(),
        },
        ["prompt"],
    )


def _capture_schema() -> JsonDict:
    return _schema(
        {
            "mode": _mode_property("mock"),
            "project": _string(),
            "output_dir": _string(),
            "name": _string(),
            "view": {"type": "string", "enum": ["isometric", "front", "top", "right"], "default": "isometric"},
            "isolate_prefix": _string(),
            "width": _integer(64, 10000, 1600),
            "height": _integer(64, 10000, 1100),
            "max_capture_retries": _integer(0, 3, 0),
        }
    )


def _compact_snapshot_schema() -> JsonDict:
    return _schema(
        {
            "mode": _mode_property("real"),
            "project": _string(),
            "max_occurrences": _integer(1, 100000, 500),
            "max_bodies": _integer(1, 100000, 500),
            "include_transforms": _boolean(False),
            **_inspection_budget_properties(),
        },
        ["project"],
    )


def _hub_inventory_schema() -> JsonDict:
    return _schema(
        {
            "mode": _mode_property("real"),
            "query": _string(),
            "max_results": _integer(1, 500, 50),
            "enrich": _boolean(True),
        }
    )


def _safe_change_preview_schema() -> JsonDict:
    return _schema(
        {
            "mode": _mode_property("real"),
            "project": _string(),
            "operation": {"type": "string", "enum": ["move", "delete", "visibility", "componentize"]},
            "targets": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
            "policy": {"type": "object", "additionalProperties": True},
        },
        ["project", "operation", "targets"],
    )


def _safe_change_apply_schema() -> JsonDict:
    return _schema(
        {
            "mode": _mode_property("real"),
            "project": _string(),
            "preview_id": _string(),
            "batch_size": _integer(1, 50, 5),
            "confirm_destructive": _boolean(False),
            "save_after": _boolean(False),
        },
        ["project", "preview_id"],
    )


def _read_manifest_schema() -> JsonDict:
    return _schema(
        {
            "path": _string(),
            "source": {"type": "string", "enum": ["real", "mock"], "default": "real"},
        }
    )


def _read_artifact_schema() -> JsonDict:
    return _schema(
        {
            "project": _string(),
            "session_id": _string(),
            "artifact": {"type": "string", "enum": sorted(SESSION_ARTIFACTS)},
        },
        ["project", "session_id", "artifact"],
    )


def _read_trace_schema() -> JsonDict:
    return _schema(
        {"project": _string(), "session_id": _string(), "limit": _integer(1, 1000, 100)},
        ["project", "session_id"],
    )


def _plan_schema() -> JsonDict:
    return _schema({"prompt": _string(), "project": _string()}, ["prompt"])


def _export_spec_schema() -> JsonDict:
    return _schema({"prompt": _string(), "project": _string(), "output_path": _string()}, ["prompt"])


def _benchmark_schema() -> JsonDict:
    return _schema(
        {
            "suite": _string(),
            "driver": {"type": "string", "enum": ["internal", "codex_e2e"], "default": "internal"},
            "mode": _mode_property("mock"),
            "execution_paths": {
                "type": "array",
                "items": {"type": "string", "enum": ["safe_harness", "native_fast"]},
                "minItems": 1,
                "maxItems": 2,
                "uniqueItems": True,
                "default": ["safe_harness", "native_fast"],
            },
            "repetitions": _integer(1, 100, 1),
            "warmups": _integer(0, 20, 0),
            "seed": _integer(0, 2147483647, 42),
            "model": _string(),
            "reasoning_effort": {
                "type": "string",
                "enum": ["none", "minimal", "low", "medium", "high", "xhigh", "ultra"],
                "default": "high",
            },
            "confirm_real_benchmark": _boolean(False),
            "baseline_run_id": _string(),
            "project": _string(),
            "dry_run": _boolean(False),
        }
    )


def _benchmark_report_schema() -> JsonDict:
    return _schema(
        {
            "run_id": _string(),
            "view": {
                "type": "string",
                "enum": ["report", "summary", "trials", "environment", "traces", "oracles"],
                "default": "report",
            },
            "offset": _integer(0, None, 0),
            "limit": _integer(1, 1000, 100),
            "path": _string(),
        }
    )


def _memory_search_schema() -> JsonDict:
    return _schema({"query": _string(), "project": _string()}, ["query"])


def _memory_write_schema() -> JsonDict:
    return _schema({"project": _string(), "path": _string(), "content": _string()}, ["project", "path", "content"])


if __name__ == "__main__":
    raise SystemExit(main())
