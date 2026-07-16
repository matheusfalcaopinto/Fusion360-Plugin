"""Local MCP server that exposes only the safe ``fusion_agent_*`` surface."""

from __future__ import annotations

import hashlib
import json
from contextvars import ContextVar
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import uuid4

import anyio
import mcp.types as types
from jsonschema import Draft202012Validator
from mcp.server.lowlevel import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.shared.exceptions import McpError

from agent_core.guardrails import PlannerUnsupportedError
from agent_core.fast_path import FastPathResponse
from agent_core.planner import PlanningRequest, RuleBasedPlanner
from agent_core.request_context import (
    RequestContext,
    bind_request_context,
    current_request_context,
)
from agent_core.session_controller import SessionOptions
from benchmark.loader import BenchmarkSuiteError, load_benchmark_suite
from benchmark.models import BenchmarkRunConfig
from benchmark.runner import BenchmarkRunner
from cad_spec.models import CadSpec
from cad_spec.v2 import CadSpecV2, parse_cad_spec, upgrade_legacy_plan_to_v2
from cli.main import _doctor, _tools_probe, _tools_propose_mapping
from fusion_agent_mcp.runtime import (
    FusionAgentRuntime,
    MOCK_IMPLEMENTED_CAPABILITIES,
    RuntimeConfiguration,
)
from fusion_agent_mcp.benchmark_bridge import FusionRuntimeBenchmarkBridge
from fusion_agent_mcp import __version__
from fusion_agent_mcp import mcp_surface
from fusion_agent_mcp.profiles import (
    TOOL_PROFILES,
    ToolProfileError,
    profiles_for_tool,
    resolve_tool_profile,
)
from fusion_agent_assets import asset_root
from fusion_mcp_adapter.endpoint_policy import EndpointPolicyError, validate_endpoint
from fusion_mcp_adapter.errors import ErrorCode
from fusion_mcp_adapter.tool_result import PublicError
from fusion_tool_facade.autodesk_typed_backend import AUTODESK_IMPLEMENTED_CAPABILITIES
from fusion_tool_facade.typed_backend import FAUST_IMPLEMENTED_CAPABILITIES
from memory.gate import MemoryGate
from memory.retriever import MemoryRetriever
from memory.schemas import (
    MemoryRecord,
    MemoryScope,
    MemorySource,
    MemoryType,
    TrustLevel,
)
from telemetry.trace import redact_sensitive
from telemetry.journal import SessionJournal
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
    "execution.json",
    "readback.json",
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
    capability_group: str = "orchestration"
    risk: str = "read"
    evidence_role: str = "structured"
    profiles: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.output_schema is None:
            object.__setattr__(self, "output_schema", _tool_output_schema(self.name))
        if self.annotations is None:
            object.__setattr__(self, "annotations", _annotations_for_tool(self.name))
        metadata = _tool_metadata(self.name)
        if self.capability_group == "orchestration":
            object.__setattr__(self, "capability_group", metadata[0])
        if self.risk == "read":
            object.__setattr__(self, "risk", metadata[1])
        if self.evidence_role == "structured":
            object.__setattr__(self, "evidence_role", metadata[2])


def surface_specs() -> list[mcp_surface.SurfaceSpec]:
    """Return the unified declarative MCP surface registry."""

    tool_surfaces = [
        mcp_surface.SurfaceSpec(
            kind="tool",
            name=spec.name,
            profiles=spec.profiles,
            risk=spec.risk,
            data_class=f"tool_{spec.evidence_role}",
            capability_group=spec.capability_group,
            evidence_role=spec.evidence_role,
            description=spec.description,
            input_schema=spec.input_schema,
            output_schema=spec.output_schema,
            annotations=spec.annotations,
            handler=spec.handler,
            projector=_as_call_tool_result,
        )
        for spec in tool_specs()
    ]
    return tool_surfaces + list(mcp_surface.surface_specs())


_RUNTIME_OVERRIDE: ContextVar[FusionAgentRuntime | None] = ContextVar(
    "fusion_agent_runtime_override",
    default=None,
)
_PROFILE_OVERRIDE: ContextVar[str] = ContextVar(
    "fusion_agent_tool_profile_override",
    default="all",
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


def _runtime_configuration() -> RuntimeConfiguration:
    return get_runtime().configuration


def _doctor_environment(configuration: RuntimeConfiguration) -> JsonDict:
    return {
        "launcher_path": "",
        "source_plugin_root": "",
        "fusion_mcp_endpoint": configuration.endpoint or "",
        "fusion_mcp_command": configuration.command or "",
        "default_mode": configuration.default_mode,
        "require_real": configuration.require_real,
        "allow_dry_run": configuration.allow_dry_run,
    }


def _call_context_template(
    profile: str, configuration: RuntimeConfiguration | None = None
) -> RequestContext:
    """Create the immutable security template captured by one MCP server."""

    config = configuration or _runtime_configuration()
    return RequestContext(
        request_id="mcp_server_startup_template",
        profile=profile,
        mode=config.default_mode,
        backend=config.backend,
        timeouts={
            "connect": config.connect_timeout_seconds,
            "read": config.read_timeout_seconds,
            "mutation": config.mutation_timeout_seconds,
            "trusted_read": config.trusted_read_timeout_seconds,
            "inspection_deadline_ms": float(config.inspection_deadline_ms),
        },
        limits={
            "inspection_max_entities": config.inspection_max_entities,
            "inspection_max_response_bytes": config.inspection_max_response_bytes,
            "resource_max_bytes": config.resource_max_bytes,
            "protected_script_bytes": config.protected_script_limit_bytes,
        },
        capabilities=(
            f"fast_path:{config.fast_path_mode}",
            f"execution_path:{config.execution_path}",
        ),
    )


def _context_with_runtime_defaults(
    context: RequestContext | None,
    template: RequestContext,
) -> RequestContext:
    if context is None:
        return template
    capabilities = list(context.capabilities)
    for prefix in ("fast_path:", "execution_path:"):
        if not any(value.startswith(prefix) for value in capabilities):
            capabilities.extend(
                value for value in template.capabilities if value.startswith(prefix)
            )
    return RequestContext(
        request_id=context.request_id,
        profile=context.profile,
        mode=context.mode,
        backend=context.backend,
        session_id=context.session_id,
        trial_id=context.trial_id,
        document_identity=context.document_identity,
        spec_digest=context.spec_digest,
        timeouts={**template.timeouts, **context.timeouts},
        limits={**template.limits, **context.limits},
        capabilities=tuple(capabilities),
    )


def _call_request_context(
    name: str,
    arguments: JsonDict,
    *,
    profile: str,
    parent: RequestContext | None,
) -> RequestContext:
    """Derive one call-owned snapshot without consulting process globals."""

    inherited = parent or _call_context_template(profile)
    requested_mode = arguments.get("mode")
    mode = (
        str(requested_mode).strip().lower()
        if isinstance(requested_mode, str)
        and requested_mode.strip().lower() in {"mock", "real"}
        else inherited.mode
    )
    inherited_capabilities = tuple(
        capability
        for capability in inherited.capabilities
        if not capability.startswith(("tool:", "profile:"))
    )
    capabilities = tuple(
        dict.fromkeys(
            (
                *inherited_capabilities,
                f"tool:{name}",
                f"profile:{profile}",
            )
        )
    )
    canonical_arguments = json.dumps(
        arguments,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return RequestContext(
        request_id=f"mcp_{uuid4().hex}",
        session_id=inherited.session_id,
        trial_id=inherited.trial_id,
        profile=profile,
        mode=mode,
        backend=inherited.backend,
        document_identity=inherited.document_identity,
        spec_digest=hashlib.sha256(canonical_arguments).hexdigest(),
        timeouts=inherited.timeouts,
        limits=inherited.limits,
        capabilities=capabilities,
    )


def _context_capability_value(
    prefix: str,
    *,
    allowed: set[str],
    default: str,
) -> str:
    """Read one unambiguous decision from the current request snapshot."""

    context = current_request_context()
    if context is None:
        return default
    marker = f"{prefix}:"
    values = {
        capability[len(marker) :]
        for capability in context.capabilities
        if capability.startswith(marker)
    }
    if not values:
        return default
    if len(values) != 1:
        raise ValueError(f"conflicting {prefix} capabilities in request context")
    value = next(iter(values))
    if value not in allowed:
        raise ValueError(f"unsupported {prefix} capability: {value}")
    return value


def list_tool_definitions(profile: str | None = None) -> list[types.Tool]:
    """Return MCP tool definitions for the safe harness wrapper."""

    resolved_profile = resolve_tool_profile(
        profile if profile is not None else _runtime_configuration().tool_profile
    )
    specs = _tool_surface_specs()
    return [
        types.Tool(
            name=spec.name,
            description=spec.description,
            inputSchema=spec.input_schema or _schema(),
            outputSchema=spec.output_schema or _open_output_schema(),
            annotations=spec.annotations,
        )
        for spec in specs
        if resolved_profile in spec.profiles
    ]


async def execute_tool(
    name: str,
    arguments: JsonDict | None = None,
    *,
    runtime: FusionAgentRuntime | None = None,
    profile: str = "all",
    request_context: RequestContext | None = None,
) -> JsonDict:
    """Execute one wrapper tool by name and return a JSON-serializable payload."""

    response = await execute_tool_response(
        name,
        arguments,
        runtime=runtime,
        profile=profile,
        request_context=request_context,
    )
    return response.payload


async def execute_tool_response(
    name: str,
    arguments: JsonDict | None = None,
    *,
    runtime: FusionAgentRuntime | None = None,
    profile: str = "all",
    request_context: RequestContext | None = None,
) -> FastPathResponse:
    """Execute one tool while preserving structured and binary MCP channels."""

    active_runtime = runtime or get_runtime()
    resolved_profile = resolve_tool_profile(profile)
    template = _call_context_template(resolved_profile, active_runtime.configuration)
    parent_context = _context_with_runtime_defaults(
        request_context or current_request_context(), template
    )
    call_context = _call_request_context(
        name,
        arguments or {},
        profile=resolved_profile,
        parent=parent_context,
    )
    token = _RUNTIME_OVERRIDE.set(active_runtime)
    profile_token = _PROFILE_OVERRIDE.set(resolved_profile)
    try:
        with bind_request_context(call_context):
            spec_map = _tool_spec_map()
            spec = spec_map.get(name)
            if spec is None:
                raise ValueError(f"unknown fusion agent MCP tool: {name}")
            if resolved_profile not in spec.profiles:
                raise ToolProfileError(
                    tool_name=name,
                    profile=resolved_profile,
                    available_profiles=spec.profiles,
                )
            result = await spec.handler(arguments or {})
    finally:
        _PROFILE_OVERRIDE.reset(profile_token)
        _RUNTIME_OVERRIDE.reset(token)
    if isinstance(result, FastPathResponse):
        return result
    return FastPathResponse(payload=result)


def build_server(
    runtime: FusionAgentRuntime | None = None, *, profile: str | None = None
) -> Server:
    """Build the stdio MCP server used by Codex and other MCP clients."""

    app = Server("fusion-agent-harness", version=__version__)
    server_runtime = runtime or get_runtime()
    server_profile = resolve_tool_profile(
        profile if profile is not None else server_runtime.configuration.tool_profile
    )
    server_context = _call_context_template(
        server_profile, server_runtime.configuration
    )

    @app.list_tools()
    async def list_tools() -> list[types.Tool]:
        return list_tool_definitions(server_profile)

    @app.call_tool()
    async def call_tool(
        name: str, arguments: dict | None
    ) -> list[types.ContentBlock] | types.CallToolResult:
        try:
            response = await execute_tool_response(
                name,
                dict(arguments or {}),
                runtime=server_runtime,
                profile=server_profile,
                request_context=server_context,
            )
        except ToolProfileError as exc:
            return _public_call_tool_error(
                name,
                code=exc.code,
                generic_message="The requested tool is unavailable in this profile.",
                extras={
                    "tool": exc.tool_name,
                    "profile": exc.profile,
                    "available_profiles": list(exc.available_profiles),
                },
            )
        except Exception:  # noqa: BLE001 - raw exception text is private
            return _public_call_tool_error(
                name,
                code="INTERNAL_ERROR",
                generic_message="The operation could not be completed.",
            )
        spec = _tool_spec_map().get(name)
        projector = getattr(spec, "projector", None)
        if not callable(projector):
            projector = _as_call_tool_result
        return projector(name, response)

    @app.list_resources()
    async def list_resources() -> list[types.Resource]:
        return mcp_surface.resources(server_profile)

    @app.list_resource_templates()
    async def list_resource_templates() -> list[types.ResourceTemplate]:
        return mcp_surface.resource_templates(server_profile)

    @app.read_resource()
    async def read_resource(uri: Any) -> list[ReadResourceContents]:
        try:
            payload = await _read_mcp_resource(
                str(uri), runtime=server_runtime, profile=server_profile
            )
        except mcp_surface.SurfaceProfileError:
            raise _public_resource_error(
                code="SURFACE_NOT_AVAILABLE_IN_PROFILE",
                generic_message="The requested resource is unavailable in this profile.",
                protocol_code=types.INVALID_REQUEST,
            ) from None
        except FileNotFoundError:
            raise _public_resource_error(
                code="RESOURCE_NOT_FOUND",
                generic_message="The requested resource is unavailable.",
                protocol_code=types.INVALID_REQUEST,
            ) from None
        except (KeyError, TypeError, ValueError):
            raise _public_resource_error(
                code="INVALID_REQUEST",
                generic_message="The resource request is invalid.",
                protocol_code=types.INVALID_REQUEST,
            ) from None
        except Exception:  # noqa: BLE001 - raw resource diagnostics stay private
            raise _public_resource_error(
                code="INTERNAL_ERROR",
                generic_message="The resource could not be read.",
                protocol_code=types.INTERNAL_ERROR,
            ) from None
        return [
            ReadResourceContents(
                content=_bounded_json_text(payload),
                mime_type=mcp_surface.RESOURCE_MIME_TYPE,
            )
        ]

    @app.list_prompts()
    async def list_prompts() -> list[types.Prompt]:
        return mcp_surface.prompts(server_profile)

    @app.get_prompt()
    async def get_prompt(
        name: str, arguments: dict[str, str] | None
    ) -> types.GetPromptResult:
        return mcp_surface.render_prompt(name, arguments, profile=server_profile)

    return app


def main() -> int:
    """Run the MCP server over stdio."""

    from mcp.server.stdio import stdio_server

    configuration = RuntimeConfiguration.from_environment()
    runtime = FusionAgentRuntime(
        manifest_root=MANIFEST_ROOT,
        outputs_root=OUTPUTS_ROOT,
        configuration=configuration,
    )

    async def arun() -> None:
        app = build_server(runtime)
        try:
            async with stdio_server() as streams:
                await app.run(
                    streams[0], streams[1], app.create_initialization_options()
                )
        finally:
            with anyio.move_on_after(2.0):
                await runtime.close(timeout_seconds=2.0)

    anyio.run(arun)
    return 0


def tool_specs() -> list[ToolSpec]:
    """Return all safe MCP wrapper tool specs."""

    specs = [
        ToolSpec(
            "fusion_agent_doctor",
            "Show harness configuration and paths.",
            _schema(),
            _doctor_tool,
        ),
        ToolSpec(
            "fusion_agent_readiness_report",
            "Summarize environment, cache, endpoint, and manifest readiness.",
            _schema(),
            _readiness_report_tool,
        ),
        ToolSpec(
            "fusion_agent_probe",
            "Probe only the configured real Fusion MCP endpoint.",
            _schema(),
            _probe_tool,
        ),
        ToolSpec(
            "fusion_agent_session_health",
            "Differentiate launcher, MCP server, real endpoint, manifest, and native tool-surface health.",
            _mode_schema(default="real"),
            _session_health_tool,
        ),
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
        ),
        ToolSpec(
            "fusion_agent_targeted_inspect",
            "Inspect up to 50 explicitly selected Fusion document/entities in one audited read-only native script; ambiguous names are never selected silently.",
            _targeted_inspect_schema(),
            _targeted_inspect_tool,
        ),
        ToolSpec(
            "fusion_agent_fast_execute",
            "Lint, bind declared targets, dispatch at most once without automatic post-dispatch replay, and programmatically verify a bounded native Fusion script. Scoped updates must mutate targets[query_id]; additive scripts must create only through target_components[component_path]. Delete, move, visibility, componentize, bulk, hidden/shared, and ambiguous work remains Safe Harness only.",
            _fast_execute_schema(),
            _fast_execute_tool,
        ),
        ToolSpec(
            "fusion_agent_recover_change",
            "Explicitly undo or redo only the latest verified Fast Path mutation in this runtime after a scoped state-fingerprint check; never invoked automatically.",
            _recover_change_schema(),
            _recover_change_tool,
        ),
        ToolSpec(
            "fusion_agent_compact_snapshot",
            "Capture a capped component-scoped programmatic snapshot for large designs.",
            _compact_snapshot_schema(),
            _compact_snapshot_tool,
        ),
        ToolSpec(
            "fusion_agent_hub_inventory",
            "Inventory Fusion Personal Library metadata using metadata search and findFileById enrichment.",
            _hub_inventory_schema(),
            _hub_inventory_tool,
        ),
        ToolSpec(
            "fusion_agent_safe_change_preview",
            "Classify intended Fusion changes and create a baseline-backed preview.",
            _safe_change_preview_schema(),
            _safe_change_preview_tool,
        ),
        ToolSpec(
            "fusion_agent_safe_change_apply",
            "Apply one small previewed reversible batch and abort on visible regressions.",
            _safe_change_apply_schema(),
            _safe_change_apply_tool,
        ),
        ToolSpec(
            "fusion_agent_verify_active_design",
            "Verify the active design against a planned CadSpec without executing geometry.",
            _verify_schema(),
            _verify_active_design_tool,
        ),
        ToolSpec(
            "fusion_agent_capture_viewport",
            "Capture the active Fusion viewport through the safe facade.",
            _capture_schema(),
            _capture_viewport_tool,
        ),
        ToolSpec(
            "fusion_agent_run_session",
            "Run one full modeling session through the harness.",
            _run_schema(),
            _run_session_tool,
        ),
        ToolSpec(
            "fusion_agent_dry_run_session",
            "Plan and simulate one modeling session without MCP calls.",
            _dry_run_schema(),
            _dry_run_session_tool,
        ),
        ToolSpec(
            "fusion_agent_list_sessions",
            "List saved session journals.",
            _schema({"project": _string(), "limit": _integer(1, 100)}),
            _list_sessions_tool,
        ),
        ToolSpec(
            "fusion_agent_read_session_artifact",
            "Read an allowlisted session artifact.",
            _read_artifact_schema(),
            _read_session_artifact_tool,
        ),
        ToolSpec(
            "fusion_agent_read_trace",
            "Read parsed events from a session tool trace.",
            _read_trace_schema(),
            _read_trace_tool,
        ),
        ToolSpec(
            "fusion_agent_plan_spec",
            "Create a CAD Spec JSON document for a prompt.",
            _plan_schema(),
            _plan_spec_tool,
        ),
        ToolSpec(
            "fusion_agent_validate_spec",
            "Validate a CAD Spec JSON string.",
            _schema({"spec_json": _string()}, ["spec_json"]),
            _validate_spec_tool,
        ),
        ToolSpec(
            "fusion_agent_export_spec_json",
            "Plan a CAD Spec and optionally save it under outputs/.",
            _export_spec_schema(),
            _export_spec_json_tool,
        ),
        ToolSpec(
            "fusion_agent_list_benchmarks",
            "List benchmark suites and case counts.",
            _schema(),
            _list_benchmarks_tool,
        ),
        ToolSpec(
            "fusion_agent_run_benchmark",
            "Run a strict benchmark_suite.v2 A/B trial set with internal or isolated Codex driver and immutable run artifacts.",
            _benchmark_schema(),
            _run_benchmark_tool,
        ),
        ToolSpec(
            "fusion_agent_read_benchmark_report",
            "Read a paginated benchmark run view by run_id, or an explicitly selected legacy report.",
            _benchmark_report_schema(),
            _read_benchmark_report_tool,
        ),
        ToolSpec(
            "fusion_agent_discover_tools",
            "Discover MCP tools through mock or real client and save manifest.",
            _mode_schema(default="real"),
            _discover_tools_tool,
        ),
        ToolSpec(
            "fusion_agent_propose_mapping",
            "Propose safe facade/native mappings from latest manifest.",
            _schema(),
            _propose_mapping_tool,
        ),
        ToolSpec(
            "fusion_agent_read_manifest",
            "Read the latest real/mock or named tool manifest.",
            _read_manifest_schema(),
            _read_manifest_tool,
        ),
        ToolSpec(
            "fusion_agent_memory_search",
            "Search gated global/project memory.",
            _memory_search_schema(),
            _memory_search_tool,
        ),
        ToolSpec(
            "fusion_agent_memory_write",
            "Write project Markdown memory.",
            _memory_write_schema(),
            _memory_write_tool,
        ),
        ToolSpec(
            "fusion_agent_memory_list_project",
            "List global and project memory records.",
            _schema({"project": _string()}),
            _memory_list_project_tool,
        ),
        ToolSpec(
            "fusion_agent_skills_list",
            "List all filesystem-backed harness skills.",
            _schema(),
            _skills_list_tool,
        ),
        ToolSpec(
            "fusion_agent_skills_get",
            "Read one harness skill by name.",
            _schema({"name": _string()}, ["name"]),
            _skills_get_tool,
        ),
        ToolSpec(
            "fusion_agent_skills_rank",
            "Rank harness skills for a request.",
            _schema({"query": _string(), "limit": _integer(1, 12)}, ["query"]),
            _skills_rank_tool,
        ),
    ]
    all_names = tuple(spec.name for spec in specs)
    return [
        replace(spec, profiles=profiles_for_tool(spec.name, all_names))
        for spec in specs
    ]


async def _doctor_tool(_: JsonDict) -> JsonDict:
    return _doctor(_doctor_environment(_runtime_configuration()))


async def _readiness_report_tool(_: JsonDict) -> JsonDict:
    doctor = _doctor(_doctor_environment(_runtime_configuration()))
    profile = _PROFILE_OVERRIDE.get()
    runtime = get_runtime()
    safe_tools = sorted(tool.name for tool in list_tool_definitions(profile))
    return {
        "schema_version": "fusion_agent.readiness.v2",
        "mcp_version": __version__,
        "tool_profile": profile,
        "available_tool_profiles": list(TOOL_PROFILES),
        "doctor": _public_doctor_status(doctor),
        "safe_facade_tool_count": len(safe_tools),
        "safe_facade_tools": safe_tools,
        "manifest_status": _public_manifest_status(
            runtime.manifest_store.latest_status()
        ),
        "persistent_runtime": _public_runtime_diagnostics(runtime.diagnostics()),
        "recommended_startup_sequence": [
            "Call fusion_agent_native_read(query_type=api_documentation) for only the APIs needed.",
            "Call fusion_agent_targeted_inspect for a bounded baseline.",
            "Plan and validate a typed CadSpec before any authorized mutation.",
            "Use diagnostic-profile health tools only after a readiness failure.",
        ],
    }


def _public_doctor_status(doctor: JsonDict) -> JsonDict:
    """Project installation diagnostics without paths, endpoints, or commands."""

    allowed = (
        "cache_plugin_version",
        "fusion_mcp_endpoint_configured",
        "fusion_mcp_command_configured",
        "fusion_agent_default_mode",
        "fusion_agent_require_real",
        "fusion_agent_allow_dry_run",
        "dry_run_policy",
    )
    return {key: doctor[key] for key in allowed if key in doctor}


def _public_manifest_status(status: JsonDict) -> JsonDict:
    """Project manifest presence/provenance without local paths or raw errors."""

    public: JsonDict = {}
    for source, raw in status.items():
        if not isinstance(raw, dict):
            continue
        entry = {
            key: raw[key]
            for key in (
                "exists",
                "bytes",
                "schema_version",
                "fingerprint",
                "captured_at",
            )
            if key in raw
        }
        if "error" in raw:
            entry["error_present"] = True
        public[str(source)] = entry
    return public


def _public_runtime_diagnostics(diagnostics: JsonDict) -> JsonDict:
    """Allowlist lifecycle state safe for normal-profile readiness."""

    allowed = (
        "backend",
        "state",
        "transport_mode",
        "requested_transport_mode",
        "effective_transport_mode",
        "frontend_transport",
        "connection_generation",
        "initialize_count",
        "tools_list_count",
        "call_count",
        "reconnect_count",
        "retry_count",
        "fingerprint",
        "manifest_drift",
        "session_established",
        "worker_running",
        "queue_depth",
        "cooldown_remaining_seconds",
        "auto_canary_completed",
        "auto_canary_count",
        "auto_canary_ms",
        "mutation_dispatched",
        "readiness_cached",
        "readiness_ttl_seconds",
        "real_benchmark_backend",
        "closing",
        "command_configured",
        "argument_count",
        "last_error_present",
    )
    public = {key: diagnostics[key] for key in allowed if key in diagnostics}
    endpoint_policy = diagnostics.get("endpoint_policy")
    if isinstance(endpoint_policy, dict):
        public["endpoint_policy"] = {
            key: endpoint_policy[key]
            for key in ("policy", "scheme", "loopback", "authenticated")
            if key in endpoint_policy
        }
    authority_policy = diagnostics.get("authority_policy")
    if isinstance(authority_policy, dict):
        authority_public: JsonDict = {}
        digest = authority_policy.get("digest")
        if isinstance(digest, str) and digest:
            authority_public["digest"] = digest
        io_enabled = authority_policy.get("io_enabled")
        if isinstance(io_enabled, bool):
            authority_public["io_enabled"] = io_enabled
        root_ids = authority_policy.get("root_ids")
        if isinstance(root_ids, dict):
            public_root_ids: JsonDict = {}
            for direction in ("import", "export"):
                values = root_ids.get(direction)
                if isinstance(values, list) and all(
                    isinstance(value, str) for value in values
                ):
                    public_root_ids[direction] = list(values)
            if public_root_ids:
                authority_public["root_ids"] = public_root_ids
        if authority_public:
            public["authority_policy"] = authority_public
    if diagnostics.get("last_error") or diagnostics.get("manifest_persistence_error"):
        public["diagnostic_failure_present"] = True
    if diagnostics.get("fallback_reason"):
        public["fallback_active"] = True
    return public


async def _probe_tool(args: JsonDict) -> JsonDict:
    if "endpoint" in args:
        return {
            "ok": False,
            "error_code": "ENDPOINT_SOURCE_NOT_ALLOWED",
            "error": "public MCP tools cannot supply backend endpoints; configure FUSION_MCP_ENDPOINT at startup",
            "probes": [],
        }
    configuration = _runtime_configuration()
    endpoint = configuration.endpoint
    if endpoint:
        try:
            validate_endpoint(
                endpoint,
                policy=configuration.remote_policy,
                allowlist=configuration.remote_allowlist,
                bearer_token=configuration.bearer_token or "",
            )
        except EndpointPolicyError as exc:
            return {
                "ok": False,
                "error_code": exc.code,
                "error": str(exc),
                "probes": [],
            }
    return _public_probe_result(
        await _tools_probe(
            endpoint,
            remote_policy=configuration.remote_policy,
            remote_allowlist=configuration.remote_allowlist,
            bearer_token=configuration.bearer_token,
            transport_mode=configuration.transport_mode,
            command=configuration.command,
            use_environment=False,
        )
    )


def _public_probe_result(payload: JsonDict) -> JsonDict:
    """Project probe evidence while replacing every downstream error channel."""

    probes = payload.get("probes")
    if not isinstance(probes, list):
        return {"probes": []}
    projected: list[JsonDict] = []
    for raw in probes:
        if not isinstance(raw, dict):
            continue
        item: JsonDict = {
            "endpoint": str(raw.get("endpoint") or "configured"),
            "health_uri": str(raw.get("health_uri") or "configured"),
            "health": _public_probe_stage(raw.get("health")),
            "tools_list": _public_probe_stage(raw.get("tools_list")),
        }
        projected.append(item)
    return {"probes": projected}


def _public_probe_stage(value: Any) -> JsonDict:
    if not isinstance(value, dict):
        return {}
    allowed = {
        key: value[key]
        for key in ("ok", "status", "status_code", "tool_count", "sample_tools")
        if key in value
    }
    if value.get("ok") is False or value.get("error") or value.get("error_message"):
        code = _normalized_public_error_code(value.get("error_code"))
        allowed["ok"] = False
        allowed["error_code"] = code
        allowed["error"] = PublicError.create(
            code=code,
            generic_message=_generic_public_error_message(code),
            retryable=code in _RETRYABLE_PUBLIC_ERROR_CODES,
        ).model_dump(mode="json")
    return allowed


async def _session_health_tool(args: JsonDict) -> JsonDict:
    mode = _mode(args, default="real")
    health = await get_runtime().controller.session_health(
        mode=mode, options=_session_options(mode=mode)
    )
    health = _public_session_health(health)
    if mode == "real":
        health["persistent_runtime"] = _public_runtime_diagnostics(
            get_runtime().diagnostics()
        )
    return health


def _public_session_health(health: JsonDict) -> JsonDict:
    """Remove local paths and free-form downstream errors from health output."""

    allowed = (
        "mode",
        "launcher_ok",
        "default_mode",
        "require_real",
        "allow_dry_run",
        "manifest_ok",
        "manifest_source",
        "manifest_tool_count",
        "mcp_server_ok",
        "real_endpoint_ok",
        "native_tools_attached",
        "native_tool_count",
        "native_tool_sample",
        "live_manifest_fingerprint",
        "cached_manifest_fingerprint",
        "manifest_drift",
        "healthy",
    )
    public = {key: health[key] for key in allowed if key in health}
    manifest_status = health.get("manifest_status")
    if isinstance(manifest_status, dict):
        public["manifest_status"] = _public_manifest_status(manifest_status)
    connection = health.get("connection")
    if isinstance(connection, dict):
        public["connection"] = _public_runtime_diagnostics(connection)
    return public


async def _inspect_tool(args: JsonDict) -> JsonDict:
    mode = _mode(args, default="mock")
    configuration = _runtime_configuration()
    inspection_options = {
        key: args[key]
        for key in (
            "sections",
            "max_entities_visited",
            "deadline_ms",
            "max_response_bytes",
        )
        if key in args
    }
    inspection_options.setdefault(
        "max_entities_visited", configuration.inspection_max_entities
    )
    inspection_options.setdefault("deadline_ms", configuration.inspection_deadline_ms)
    inspection_options.setdefault(
        "max_response_bytes", configuration.inspection_max_response_bytes
    )
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
    response = await get_runtime().fast_path(mode).native_read(args)
    if str(args.get("query_type") or "") == "screenshot":
        response.payload["evidence_role"] = "supplemental_visual"
    return response


async def _targeted_inspect_tool(args: JsonDict) -> FastPathResponse:
    blocked = _fast_path_block("fusion_agent_targeted_inspect", args)
    if blocked:
        return blocked
    mode = _mode(args, default="real")
    configuration = _runtime_configuration()
    request = dict(args)
    request.setdefault("max_entities_visited", configuration.inspection_max_entities)
    request.setdefault("deadline_ms", configuration.inspection_deadline_ms)
    request.setdefault(
        "max_response_bytes", configuration.inspection_max_response_bytes
    )
    return await get_runtime().fast_path(mode).targeted_inspect(request)


async def _fast_execute_tool(args: JsonDict) -> FastPathResponse:
    blocked = _fast_path_block("fusion_agent_fast_execute", args)
    if blocked:
        return blocked
    if (
        _runtime_configuration().backend == "faust_stdio"
        and str(args.get("change_class") or "") != "read_only"
    ):
        return FastPathResponse(
            {
                "status": "blocked_before_apply",
                "error_code": "FAST_PATH_UNAVAILABLE_FOR_BACKEND",
                "reason": "faust_mutable_fast_path_unavailable",
                "backend": "faust_stdio",
                "recommended_path": "fusion_agent_run_session with a typed CadSpec v2",
                "dispatched": False,
                "mutation_status": "not_dispatched",
            },
            is_error=True,
        )
    fast_mode = _fast_path_mode()
    change_class = str(args.get("change_class") or "")
    if fast_mode == "read_only" and change_class != "read_only":
        return FastPathResponse(
            {
                "status": "blocked_before_apply",
                "reason": "fast_path_read_only",
                "recommended_path": "safe_harness",
                "message": "Mutating Fast Execute requires FUSION_AGENT_FAST_PATH_MODE=enabled.",
            }
        )
    mode = _mode(args, default="real")
    response = await get_runtime().fast_path(mode).fast_execute(args)
    script = str(args.get("script") or "")
    operation_id = str(
        response.payload.get("operation_id")
        or f"audit_{hashlib.sha256(script.encode('utf-8')).hexdigest()[:16]}"
    )
    artifacts = _write_fast_path_audit(operation_id, args, response)
    response.payload["artifacts"] = artifacts
    return response


async def _recover_change_tool(args: JsonDict) -> FastPathResponse:
    if _runtime_configuration().backend == "faust_stdio":
        return FastPathResponse(
            {
                "status": "blocked_before_apply",
                "error_code": "FAST_PATH_UNAVAILABLE_FOR_BACKEND",
                "reason": "faust_mutable_fast_path_unavailable",
                "backend": "faust_stdio",
                "dispatched": False,
            },
            is_error=True,
        )
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
    configuration = _runtime_configuration()
    return await get_runtime().controller.compact_snapshot(
        project=project,
        mode=mode,
        options=_session_options(mode=mode, project=project),
        max_occurrences=int(args.get("max_occurrences", 500)),
        max_bodies=int(args.get("max_bodies", 500)),
        include_transforms=bool(args.get("include_transforms", False)),
        max_entities_visited=int(
            args.get(
                "max_entities_visited",
                configuration.inspection_max_entities,
            )
        ),
        deadline_ms=int(args.get("deadline_ms", configuration.inspection_deadline_ms)),
        max_response_bytes=int(
            args.get(
                "max_response_bytes",
                configuration.inspection_max_response_bytes,
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
    output_dir = (
        _safe_relative_path(OUTPUTS_ROOT, output_dir_arg)
        if output_dir_arg
        else OUTPUTS_ROOT
    )
    name = _optional_str(args, "name") or "active_design_capture"
    if (
        Path(name).is_absolute()
        or ".." in Path(name).parts
        or "/" in name
        or "\\" in name
    ):
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
    payload = result.model_dump(mode="json")
    payload["evidence_role"] = "supplemental_visual"
    payload["can_promote_geometry_verification"] = False
    return payload


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
    prompt, spec_json = _session_input(args)
    project = _optional_str(args, "project") or "opencode"
    _safe_name(project, "project")
    mode = _mode(args, default="mock")
    max_repairs = int(args.get("max_repairs", 5))
    options = _session_options(
        mode=mode,
        project=project,
        max_repairs=max_repairs,
        dry_run=dry_run,
    )
    if prompt is not None:
        legacy_plan, planning_metadata = await _plan_spec(prompt, project)
        v2_plan = upgrade_legacy_plan_to_v2(legacy_plan)
        _ensure_experimental_profile(v2_plan)
        payload = await _execute_and_record_v2(
            v2_plan,
            project=project,
            mode=mode,
            dry_run=dry_run,
            warnings=["Deterministic prompt plan normalized to strict CadSpec v2."],
        )
        payload["planning"] = planning_metadata
        return payload

    normalized = parse_cad_spec(spec_json or "")
    if normalized.legacy_spec is not None:
        result = await get_runtime().controller.run_spec(
            normalized.legacy_spec,
            user_prompt=normalized.legacy_spec.intent,
            project=project,
            mode=mode,
            options=options,
        )
        return {
            **result.model_dump(mode="json"),
            "cad_spec_version": normalized.source_version,
            "contract_eligible": normalized.contract_eligible,
            "warnings": normalized.warnings,
        }

    if normalized.spec is None:  # Defensive: NormalizedCadSpec requires one branch.
        raise ValueError("parsed CadSpec did not contain an executable document")
    _ensure_experimental_profile(normalized.spec)
    return await _execute_and_record_v2(
        normalized.spec,
        project=project,
        mode=mode,
        dry_run=dry_run,
        warnings=normalized.warnings,
    )


async def _execute_and_record_v2(
    spec: CadSpecV2,
    *,
    project: str,
    mode: str,
    dry_run: bool,
    warnings: list[str],
) -> JsonDict:
    runtime = get_runtime()
    execution = await runtime.execute_cad_spec_v2(
        spec,
        mode=mode,
        dry_run=dry_run,
    )
    readback: JsonDict | None = None
    readback_error: JsonDict | None = None
    if mode == "real" and not dry_run:
        try:
            readback = await runtime.controller.compact_snapshot(
                project=project,
                mode="real",
                options=_session_options(mode="real", project=project),
                max_entities_visited=runtime.configuration.inspection_max_entities,
                deadline_ms=runtime.configuration.inspection_deadline_ms,
                max_response_bytes=(
                    runtime.configuration.inspection_max_response_bytes
                ),
                label="cadspec_v2_readback",
            )
        except Exception:  # noqa: BLE001 - raw readback diagnostics stay private
            readback_error = PublicError.create(
                code="INCOMPLETE_INSPECTION",
                generic_message="The readback inspection was incomplete.",
                retryable=True,
            ).model_dump(mode="json")
    return _record_v2_session(
        spec,
        execution=execution,
        project=project,
        mode=mode,
        dry_run=dry_run,
        warnings=warnings,
        readback=readback,
        readback_error=readback_error,
    )


def _session_input(args: JsonDict) -> tuple[str | None, str | None]:
    """Return exactly one non-empty session input."""

    prompt = _optional_str(args, "prompt")
    spec_json = _optional_str(args, "spec_json")
    prompt = prompt.strip() if prompt and prompt.strip() else None
    spec_json = spec_json.strip() if spec_json and spec_json.strip() else None
    if (prompt is None) == (spec_json is None):
        raise ValueError("provide exactly one of prompt or spec_json")
    return prompt, spec_json


def _ensure_experimental_profile(spec: CadSpecV2) -> None:
    experimental = {
        capability
        for capability in spec.capabilities
        if capability.startswith("sheet_metal_") or capability.startswith("cam_")
    }
    profile = _PROFILE_OVERRIDE.get()
    if experimental and profile not in {"advanced", "all"}:
        raise ValueError(
            "experimental manufacturing CadSpec operations require the advanced or all "
            f"tool profile; active profile is {profile}"
        )


def _public_execution_payload(payload: JsonDict) -> JsonDict:
    """Project execution evidence before it reaches MCP or durable artifacts."""

    public = dict(payload)
    transactions = public.get("transactions")
    if isinstance(transactions, list):
        public["transactions"] = [
            _public_execution_transaction(item)
            for item in transactions
            if isinstance(item, dict)
        ]
    if public.get("success") is False:
        code = _normalized_public_error_code(public.get("error_code"))
        public_error = _coerce_public_error(public.get("public_error"), code=code)
        public["error_code"] = code
        public["error_message"] = public_error["generic_message"]
        public["public_error"] = public_error
    else:
        public.pop("error_message", None)
        public.pop("public_error", None)
    return public


def _public_execution_transaction(value: JsonDict) -> JsonDict:
    transaction = dict(value)
    transport = transaction.get("transport")
    transaction["transport"] = _public_transport_evidence(transport)
    if transaction.get("status") == "failed":
        code = _normalized_public_error_code(transaction.get("error_code"))
        public_error = _coerce_public_error(transaction.get("public_error"), code=code)
        transaction.pop("error", None)
        transaction.pop("native_result", None)
        transaction["error_code"] = code
        transaction["error_message"] = public_error["generic_message"]
        transaction["public_error"] = public_error
    return transaction


def _public_transport_evidence(value: Any) -> JsonDict:
    if not isinstance(value, dict):
        return {}
    public: JsonDict = {}
    for key in (
        "dispatched",
        "may_have_applied",
        "post_dispatch_replay_suppressed",
    ):
        if isinstance(value.get(key), bool):
            public[key] = value[key]
    outcome = value.get("mutation_outcome")
    if outcome in {"known", "unknown"}:
        public["mutation_outcome"] = outcome
    semantics = value.get("semantics")
    if semantics in {"read_only", "mutating"}:
        public["semantics"] = semantics
    for key in ("operation_id",):
        identifier = value.get(key)
        if (
            isinstance(identifier, str)
            and 1 <= len(identifier) <= 128
            and all(
                character.isalnum() or character in "._:-" for character in identifier
            )
        ):
            public[key] = identifier
    operation_ids = value.get("operation_ids")
    if isinstance(operation_ids, list) and all(
        isinstance(item, str)
        and 1 <= len(item) <= 128
        and all(character.isalnum() or character in "._:-" for character in item)
        for item in operation_ids
    ):
        public["operation_ids"] = list(operation_ids)
    return public


def _coerce_public_error(value: Any, *, code: str) -> JsonDict:
    try:
        parsed = PublicError.model_validate(value)
    except Exception:  # noqa: BLE001 - malformed diagnostics are discarded
        parsed = PublicError.create(
            code=code,
            generic_message=_generic_public_error_message(code),
            retryable=code in _RETRYABLE_PUBLIC_ERROR_CODES,
        )
    if parsed.code != code:
        parsed = PublicError.create(
            code=code,
            generic_message=_generic_public_error_message(code),
            retryable=code in _RETRYABLE_PUBLIC_ERROR_CODES,
        )
    return parsed.model_dump(mode="json")


def _public_readback_error(value: JsonDict | str | None) -> JsonDict | None:
    if value is None:
        return None
    return _coerce_public_error(value, code="INCOMPLETE_INSPECTION")


def _record_v2_session(
    spec: CadSpecV2,
    *,
    execution: Any,
    project: str,
    mode: str,
    dry_run: bool,
    warnings: list[str],
    readback: JsonDict | None = None,
    readback_error: JsonDict | str | None = None,
) -> JsonDict:
    """Persist a conservative journal for a typed v2 capability execution."""

    session_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    journal = SessionJournal(WORKSPACE_ROOT, project, session_id)
    execution_payload = _public_execution_payload(asdict(execution))
    readback_error = _public_readback_error(readback_error)
    has_mutation = any(
        not operation.kind.startswith("analysis.") for operation in spec.operations
    )
    simulated = dry_run or mode == "mock"
    verification = _evaluate_v2_verification(
        spec,
        execution_payload=execution_payload,
        has_mutation=has_mutation,
        simulated=simulated,
        readback=readback,
        readback_error=readback_error,
    )
    final_status = _v2_final_status(
        execution_payload=execution_payload,
        verification=verification,
        has_mutation=has_mutation,
        simulated=simulated,
    )
    summary = _v2_summary(
        provider=str(execution_payload.get("provider") or "unknown"),
        final_status=final_status,
        verification=verification,
    )
    cad_spec_path = journal.write_text("cad_spec.json", spec.to_json_text())
    journal.write_text("prompt.md", "Caller supplied CadSpec v2 JSON.\n")
    journal.write_json("execution.json", execution_payload)
    if readback is not None:
        readback_path = journal.write_json("readback.json", readback)
        verification["readback_path"] = str(readback_path)
    journal.write_json("verification.json", verification)
    journal.trace_path.touch(exist_ok=True)
    journal_path = journal.finalize(
        mode=mode,
        user_prompt=spec.intent,
        cad_spec_path=cad_spec_path,
        verification=verification,
        final_status=final_status,
        summary=summary,
        simulated=simulated,
    )
    return {
        "session_id": session_id,
        "status": final_status,
        "final_status": final_status,
        "summary": summary,
        "cad_spec_version": spec.cad_spec_version,
        "contract_eligible": True,
        "warnings": warnings,
        "cad_spec_path": str(cad_spec_path),
        "journal_path": str(journal_path),
        "trace_path": str(journal.trace_path),
        "execution": execution_payload,
        "verification": verification,
        "dry_run": dry_run,
    }


def _evaluate_v2_verification(
    spec: CadSpecV2,
    *,
    execution_payload: JsonDict,
    has_mutation: bool,
    simulated: bool,
    readback: JsonDict | None,
    readback_error: JsonDict | None,
) -> JsonDict:
    """Evaluate only assertions backed by independent execution/readback evidence.

    A successful backend return is execution evidence, not contract proof.  In
    particular, a post-dispatch unknown outcome is never promoted by a later
    positive observation because that observation cannot prove which attempt
    caused the state.
    """

    dispatched = bool(execution_payload.get("dispatched"))
    may_have_applied = bool(execution_payload.get("may_have_applied"))
    replay_suppressed = bool(execution_payload.get("post_dispatch_replay_suppressed"))
    mutation_outcome = (
        "unknown"
        if execution_payload.get("mutation_outcome") == "unknown"
        or (has_mutation and may_have_applied and not dispatched)
        else "known"
    )
    snapshot = _v2_snapshot(readback)
    inspection_complete = _v2_snapshot_complete(snapshot)

    if simulated:
        assertion_results = [
            {
                "id": assertion.id,
                "kind": assertion.kind,
                "required": assertion.required,
                "status": "not_run",
                "passed": False,
                "evidence": "simulation_only",
            }
            for assertion in spec.assertions
        ]
    else:
        assertion_results = [
            _evaluate_v2_assertion(
                assertion,
                spec=spec,
                snapshot=snapshot,
                evidence=execution_payload.get("evidence") or {},
                inspection_complete=inspection_complete,
            )
            for assertion in spec.assertions
        ]

    assertion_by_id = {item["id"]: item for item in assertion_results}
    requirement_results: list[JsonDict] = []
    for requirement in spec.requirements:
        linked = [assertion_by_id[item] for item in requirement.assertion_ids]
        independent = requirement.oracle == "independent"
        covered = bool(linked) and all(
            item["status"] in {"passed", "failed"} for item in linked
        )
        if independent:
            # custom_oracle declarations describe what an external oracle must
            # prove; their expected payload is never self-authenticating.
            covered = covered and all(
                item.get("evidence_source") == "independent_oracle" for item in linked
            )
        passed = covered and all(bool(item["passed"]) for item in linked)
        requirement_results.append(
            {
                "id": requirement.id,
                "description": requirement.description,
                "required": requirement.required,
                "assertion_ids": list(requirement.assertion_ids),
                "oracle": "independent_oracle" if independent else "contract",
                "covered": covered,
                "passed": passed,
                **(
                    {"oracle_evidence": "not_available"}
                    if independent and not covered
                    else {}
                ),
            }
        )

    required_assertions = [item for item in assertion_results if item["required"]]
    if simulated:
        assertion_status = "not_run"
    elif any(item["status"] == "failed" for item in required_assertions):
        assertion_status = "failed"
    elif any(
        item["status"] in {"incomplete", "not_run"} for item in required_assertions
    ):
        assertion_status = "incomplete"
    elif required_assertions:
        assertion_status = "passed"
    else:
        assertion_status = "not_run"

    required_requirements = [
        item for item in requirement_results if item.get("required", True)
    ]
    if not required_requirements:
        intent_coverage = "none"
    elif all(item["covered"] for item in required_requirements):
        intent_coverage = "complete"
    elif any(item["covered"] for item in required_requirements):
        intent_coverage = "partial"
    else:
        intent_coverage = "none"

    if any(
        item.oracle == "independent" and item.required for item in spec.requirements
    ):
        verification_level = "independent_oracle"
    elif required_requirements:
        verification_level = "contract"
    else:
        verification_level = "assertions_only"

    readback_observes_contract = bool(
        inspection_complete
        and assertion_status == "passed"
        and intent_coverage == "complete"
        and required_requirements
        and all(item["passed"] for item in required_requirements)
    )
    mutation_observed = bool(
        has_mutation
        and dispatched
        and mutation_outcome == "known"
        and readback_observes_contract
    )
    if simulated or not has_mutation:
        mutation_status = "not_dispatched"
    elif mutation_outcome == "unknown":
        mutation_status = "outcome_unknown"
    elif not dispatched:
        mutation_status = "not_dispatched"
    elif mutation_observed:
        mutation_status = "observed_in_readback"
    else:
        mutation_status = "unknown"

    contract_verified = bool(
        execution_payload.get("success")
        and mutation_outcome == "known"
        and readback_observes_contract
        and (not has_mutation or mutation_observed)
    )
    return {
        "mutation_status": mutation_status,
        "mutation_outcome": mutation_outcome,
        "dispatched": dispatched,
        "may_have_applied": may_have_applied,
        "post_dispatch_replay_suppressed": replay_suppressed,
        "assertion_status": assertion_status,
        "intent_coverage": intent_coverage,
        "verification_level": verification_level,
        "contract_verified": contract_verified,
        "inspection_complete": inspection_complete,
        "readback_complete": inspection_complete,
        "assertions": assertion_results,
        "requirements": requirement_results,
        **({"readback_error": readback_error} if readback_error else {}),
    }


def _v2_snapshot(readback: JsonDict | None) -> JsonDict:
    if not isinstance(readback, dict):
        return {}
    candidate = readback.get("snapshot", readback)
    return candidate if isinstance(candidate, dict) else {}


def _v2_snapshot_complete(snapshot: JsonDict) -> bool:
    return bool(
        snapshot
        and snapshot.get("complete") is True
        and snapshot.get("counts_exact") is True
        and not snapshot.get("truncated", False)
        and not snapshot.get("payload_capped", False)
        and not snapshot.get("stop_reason")
    )


def _evaluate_v2_assertion(
    assertion: Any,
    *,
    spec: CadSpecV2,
    snapshot: JsonDict,
    evidence: JsonDict,
    inspection_complete: bool,
) -> JsonDict:
    base: JsonDict = {
        "id": assertion.id,
        "kind": assertion.kind,
        "required": assertion.required,
        "target_ref": assertion.target_ref,
        "expected": assertion.expected,
    }
    status = "incomplete"
    actual: Any = None
    evidence_source = "not_available"

    if assertion.kind == "entity_exists":
        matches = _v2_entity_matches(snapshot, assertion.target_ref)
        actual = bool(matches)
        expected = True if assertion.expected is None else bool(assertion.expected)
        if matches or inspection_complete:
            status = "passed" if actual is expected else "failed"
            evidence_source = "compact_snapshot"
    elif assertion.kind == "entity_count":
        actual = _v2_entity_count(snapshot, assertion.target_ref, assertion.expected)
        expected_count = _v2_expected_count(assertion.expected)
        if inspection_complete and actual is not None and expected_count is not None:
            status = "passed" if actual == expected_count else "failed"
            evidence_source = "compact_snapshot"
    elif assertion.kind == "export_exists":
        export_path = _v2_export_path(spec, assertion.target_ref)
        if export_path is not None:
            actual = export_path.is_file()
            expected = True if assertion.expected is None else bool(assertion.expected)
            status = "passed" if actual is expected else "failed"
            evidence_source = "filesystem_readback"
    elif assertion.kind == "interference_count":
        actual = _v2_find_number(
            evidence.get(assertion.target_ref or ""),
            ("count", "interference_count"),
        )
        expected_count = _v2_expected_count(assertion.expected)
        if actual is not None and expected_count is not None:
            status = "passed" if actual == expected_count else "failed"
            evidence_source = "typed_analysis_readback"
    elif assertion.kind == "physical_property_range":
        actual, lower, upper = _v2_physical_range(
            evidence.get(assertion.target_ref or ""), assertion.expected
        )
        if actual is not None and (lower is not None or upper is not None):
            passed = (lower is None or actual >= lower) and (
                upper is None or actual <= upper
            )
            status = "passed" if passed else "failed"
            evidence_source = "typed_analysis_readback"
    # parameter/dimension assertions need a dedicated typed readback and
    # custom_oracle needs evidence produced outside this execution path.  They
    # intentionally remain incomplete here.

    return {
        **base,
        "status": status,
        "passed": status == "passed",
        "actual": actual,
        "evidence_source": evidence_source,
    }


def _v2_entity_records(snapshot: JsonDict) -> list[JsonDict]:
    records: list[JsonDict] = []
    for key in ("bodies", "occurrences", "components", "sketches", "features"):
        values = snapshot.get(key)
        if isinstance(values, list):
            records.extend(item for item in values if isinstance(item, dict))
    return records


def _v2_entity_matches(snapshot: JsonDict, target_ref: str | None) -> list[JsonDict]:
    if not target_ref:
        return []
    expected = target_ref.casefold()
    keys = (
        "name",
        "key",
        "path",
        "component",
        "component_key",
        "entity_token",
        "full_path",
        "fullPathName",
    )
    return [
        item
        for item in _v2_entity_records(snapshot)
        if any(
            isinstance(item.get(key), str) and str(item[key]).casefold() == expected
            for key in keys
        )
    ]


def _v2_entity_count(
    snapshot: JsonDict,
    target_ref: str | None,
    expected: Any,
) -> int | None:
    counts = snapshot.get("counts")
    counts = counts if isinstance(counts, dict) else {}
    category = target_ref
    if isinstance(expected, dict) and isinstance(expected.get("category"), str):
        category = expected["category"]
    aliases = {
        "body": "bodies_total",
        "bodies": "bodies_total",
        "occurrence": "occurrences_total",
        "occurrences": "occurrences_total",
        "component": "components_total",
        "components": "components_total",
    }
    count_key = aliases.get(str(category or "").casefold(), str(category or ""))
    value = counts.get(count_key)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if target_ref:
        return len(_v2_entity_matches(snapshot, target_ref))
    return None


def _v2_expected_count(expected: Any) -> int | None:
    if isinstance(expected, int) and not isinstance(expected, bool):
        return expected
    if isinstance(expected, dict):
        value = expected.get("count")
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _v2_export_path(spec: CadSpecV2, target_ref: str | None) -> Path | None:
    for operation in spec.operations:
        if operation.kind != "io.export":
            continue
        if target_ref in {None, operation.id, operation.target_ref, operation.path}:
            return Path(operation.path)
    return Path(target_ref) if target_ref else None


def _v2_find_number(value: Any, keys: tuple[str, ...]) -> float | None:
    if isinstance(value, dict):
        for key in keys:
            candidate = value.get(key)
            if isinstance(candidate, int | float) and not isinstance(candidate, bool):
                return float(candidate)
        for candidate in value.values():
            found = _v2_find_number(candidate, keys)
            if found is not None:
                return found
    elif isinstance(value, list):
        for candidate in value:
            found = _v2_find_number(candidate, keys)
            if found is not None:
                return found
    return None


def _v2_physical_range(
    value: Any,
    expected: Any,
) -> tuple[float | None, float | None, float | None]:
    if not isinstance(expected, dict):
        return None, None, None
    property_name = expected.get("property")
    lower = expected.get("min")
    upper = expected.get("max")
    if isinstance(property_name, str):
        actual = _v2_find_number(value, (property_name,))
    else:
        bound_keys = [
            key[4:]
            for key in expected
            if isinstance(key, str) and key.startswith("min_")
        ]
        suffix = bound_keys[0] if bound_keys else "kg"
        property_candidates = {
            "kg": ("mass_kg",),
            "mm3": ("volume_mm3",),
            "cm3": ("volume_cm3",),
        }.get(suffix, (suffix,))
        actual = _v2_find_number(value, property_candidates)
        lower = expected.get(f"min_{suffix}")
        upper = expected.get(f"max_{suffix}")
    normalized_lower = (
        float(lower)
        if isinstance(lower, int | float) and not isinstance(lower, bool)
        else None
    )
    normalized_upper = (
        float(upper)
        if isinstance(upper, int | float) and not isinstance(upper, bool)
        else None
    )
    return actual, normalized_lower, normalized_upper


def _v2_final_status(
    *,
    execution_payload: JsonDict,
    verification: JsonDict,
    has_mutation: bool,
    simulated: bool,
) -> str:
    if simulated:
        return "simulated"
    if verification["mutation_outcome"] == "unknown":
        return "mutation_outcome_unknown"
    if not execution_payload.get("success"):
        return (
            "applied_unverified"
            if verification.get("dispatched") or verification.get("may_have_applied")
            else "failed_before_apply"
        )
    if verification["contract_verified"]:
        return "applied_verified" if has_mutation else "observed_verified"
    if has_mutation:
        return (
            "applied_partially_verified"
            if verification["intent_coverage"] == "partial"
            or verification["assertion_status"] == "passed"
            else "applied_unverified"
        )
    return "observed_unverified"


def _v2_summary(
    *,
    provider: str,
    final_status: str,
    verification: JsonDict,
) -> str:
    if final_status == "mutation_outcome_unknown":
        detail = "Dispatch may have applied; automatic replay is suppressed and recovery requires readback."
    elif verification.get("contract_verified"):
        detail = "The declared contract is covered by complete readback evidence."
    elif final_status == "simulated":
        detail = "No mutation was dispatched and assertions were not executed."
    else:
        detail = (
            "The declared contract is not fully covered by complete readback evidence."
        )
    return f"CadSpec v2 session {final_status} via {provider}. {detail}"


async def _list_sessions_tool(args: JsonDict) -> JsonDict:
    project = _optional_str(args, "project")
    if project:
        _safe_name(project, "project")
    limit = int(args.get("limit", 20))
    sessions: list[JsonDict] = []
    projects = (
        [project]
        if project
        else [
            path.name
            for path in sorted((WORKSPACE_ROOT / "projects").glob("*"))
            if path.is_dir()
        ]
    )
    for project_name in projects:
        root = WORKSPACE_ROOT / "projects" / str(project_name) / "sessions"
        if not root.exists():
            continue
        for session_dir in sorted(
            root.iterdir(), key=lambda item: item.name, reverse=True
        ):
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
                    "artifacts": sorted(
                        path.name for path in session_dir.iterdir() if path.is_file()
                    ),
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
    content = _public_session_artifact_text(artifact, path.read_text(encoding="utf-8"))
    return {
        "path": str(path),
        "artifact": artifact,
        "content": content,
        "json": _try_json(content),
    }


def _public_session_artifact_text(artifact: str, content: str) -> str:
    if artifact == "tool_trace.jsonl":
        projected_lines = [
            json.dumps(
                _public_trace_event(_try_json(line)),
                ensure_ascii=False,
                sort_keys=True,
            )
            for line in content.splitlines()
            if line.strip()
        ]
        return "\n".join(projected_lines) + ("\n" if projected_lines else "")
    if artifact not in {
        "execution.json",
        "verification.json",
        "session_journal.json",
    }:
        return content
    parsed = _try_json(content)
    if artifact == "execution.json" and isinstance(parsed, dict):
        projected = _public_execution_payload(parsed)
    else:
        projected = _public_artifact_value(parsed)
    return json.dumps(projected, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _public_trace_event(value: Any) -> JsonDict:
    """Project one trace record and reject malformed free-form records."""

    if not isinstance(value, dict):
        public_error = PublicError.create(
            code="INVALID_REQUEST",
            generic_message="A malformed diagnostic record was discarded.",
            retryable=False,
        )
        return {
            "event": "diagnostic_record_discarded",
            "error_code": public_error.code,
            "error": public_error.model_dump(mode="json"),
        }
    projected = _public_artifact_value(value)
    return projected if isinstance(projected, dict) else {}


def _public_artifact_value(value: Any) -> Any:
    if isinstance(value, list):
        return [_public_artifact_value(item) for item in value]
    if not isinstance(value, dict):
        return value
    code = _normalized_public_error_code(value.get("error_code"))
    status = value.get("status")
    failed = bool(
        value.get("error")
        or value.get("error_message")
        or value.get("error_code")
        or value.get("exception")
        or value.get("traceback")
        or status in _SEMANTIC_FAILURE_STATUSES
        or status == "failed"
        or value.get("ok") is False
        or value.get("success") is False
    )
    public: JsonDict = {}
    for key, child in value.items():
        normalized = str(key).lower()
        if normalized in {
            "argv",
            "command",
            "exception",
            "traceback",
            "last_error",
            "_meta",
        }:
            continue
        if failed and (
            normalized
            in {
                "path",
                "root",
                "endpoint",
                "health_uri",
                "uri",
                "url",
                "content",
                "meta",
            }
            or normalized.endswith("_path")
            or normalized.endswith("_root")
            or normalized.endswith("_uri")
            or normalized.endswith("_url")
        ):
            continue
        if normalized == "transport":
            public[key] = _public_transport_evidence(child)
        elif normalized in {"error", "readback_error", "public_error"} and child:
            public[key] = _coerce_public_error(child, code=code)
        elif normalized == "error_message" and child:
            public[key] = _generic_public_error_message(code)
        else:
            public[key] = _public_artifact_value(child)
    projected = redact_sensitive(public)
    return projected if isinstance(projected, dict) else {}


async def _read_trace_tool(args: JsonDict) -> JsonDict:
    project = _required_str(args, "project")
    session_id = _required_str(args, "session_id")
    limit = int(args.get("limit", 100))
    path = _session_dir(project, session_id) / "tool_trace.jsonl"
    if not path.exists():
        raise FileNotFoundError(path)
    events = [
        _public_trace_event(_try_json(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
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
    return {
        "cad_spec": spec.model_dump(mode="json"),
        "cad_spec_json": spec.to_json_text(),
        **metadata,
    }


async def _validate_spec_tool(args: JsonDict) -> JsonDict:
    try:
        normalized = parse_cad_spec(_required_str(args, "spec_json"))
    except Exception as exc:  # noqa: BLE001 - validator diagnostics
        return {"valid": False, "error": f"{type(exc).__name__}: {exc}"}
    spec = normalized.spec or normalized.legacy_spec
    assert spec is not None
    return {
        "valid": True,
        "cad_spec": spec.model_dump(mode="json"),
        "cad_spec_version": normalized.source_version,
        "contract_eligible": normalized.contract_eligible,
        "warnings": normalized.warnings,
    }


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
    payload: JsonDict = {
        "cad_spec": spec.model_dump(mode="json"),
        "cad_spec_json": spec.to_json_text(),
        **metadata,
    }
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
            suites.append(
                {
                    "name": path.name,
                    "path": str(path),
                    "valid": False,
                    "error": str(exc),
                }
            )
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
    configuration = runtime.configuration
    diagnostics = runtime.diagnostics()
    bridge = FusionRuntimeBenchmarkBridge(runtime) if config.mode == "real" else None
    process_environment = {
        key: value
        for key, value in {
            "FUSION_AGENT_PLUGIN_VERSION": configuration.plugin_version,
            "FUSION_AGENT_WHEEL_VERSION": configuration.wheel_version,
            "FUSION_VERSION": configuration.fusion_version,
            "FUSION_MCP_MANIFEST_FINGERPRINT": (
                configuration.mcp_manifest_fingerprint
                or str(diagnostics.get("fingerprint") or "")
            ),
            "GIT_COMMIT": configuration.git_commit,
        }.items()
        if value
    }
    runner = BenchmarkRunner(
        controller=runtime.controller,
        workspace_root=WORKSPACE_ROOT,
        output_dir=OUTPUTS_ROOT,
        manifest_dir=MANIFEST_ROOT,
        route_executors=bridge.route_executors if bridge is not None else None,
        oracle_observer=bridge if bridge is not None else None,
        real_lifecycle=bridge,
        environment_metadata={
            "plugin_version": configuration.plugin_version,
            "mcp_fingerprint": diagnostics.get("fingerprint"),
            "connection_generation": diagnostics.get("connection_generation"),
        },
        process_environment=process_environment,
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
    manifest = await get_runtime().controller.discover_tools(
        mode=mode, options=_session_options(mode=mode)
    )
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
        return {
            "loaded": False,
            "path": str(path),
            "manifest": None,
            "manifest_source": None,
        }
    manifest = _read_json(path)
    return {
        "loaded": True,
        "path": str(path),
        "manifest": manifest,
        "manifest_source": manifest.get("source"),
    }


async def _memory_search_tool(args: JsonDict) -> JsonDict:
    query = _required_str(args, "query")
    project = _optional_str(args, "project") or "opencode"
    _safe_name(project, "project")
    store = MemoryStore(workspace_root=WORKSPACE_ROOT)
    store.seed_global()
    records = MemoryGate().filter(
        MemoryRetriever(store).retrieve(query, project=project), query
    )
    return {"records": [record.model_dump(mode="json") for record in records]}


async def _memory_write_tool(args: JsonDict) -> JsonDict:
    project = _required_str(args, "project")
    _safe_name(project, "project")
    relative_path = _required_str(args, "path")
    content = _required_str(args, "content")
    relative = Path(relative_path)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or relative.suffix.lower() != ".md"
    ):
        raise ValueError(
            "memory path must be a relative .md path under the project memory root"
        )
    kind = str(args.get("memory_kind") or "fact")
    kind_map = {
        "fact": MemoryType.FACT,
        "preference": MemoryType.USER_PREFERENCE,
        "result": MemoryType.RESULT,
    }
    if kind not in kind_map:
        raise ValueError("memory_kind must be fact, preference, or result")
    source = MemorySource(str(args.get("source") or "user"))
    if source == MemorySource.LEGACY:
        raise ValueError("legacy source is reserved for imported records")
    citations = args.get("citations") or []
    if not isinstance(citations, list) or any(
        not isinstance(item, str) for item in citations
    ):
        raise ValueError("citations must be an array of strings")
    expires_at_raw = _optional_str(args, "expires_at")
    expires_at = (
        datetime.fromisoformat(expires_at_raw.replace("Z", "+00:00"))
        if expires_at_raw
        else None
    )
    title = next(
        (
            line.lstrip("# ").strip()
            for line in content.splitlines()
            if line.startswith("#")
        ),
        relative.stem,
    )
    store = MemoryStore(workspace_root=WORKSPACE_ROOT)
    record = MemoryRecord(
        id=f"project:{project}:{relative.as_posix()}",
        scope=MemoryScope.PROJECT,
        project=project,
        type=kind_map[kind],
        summary=title,
        content=content,
        content_path=store.project_root(project) / relative,
        tags=[
            part.lower() for part in relative.stem.replace("-", "_").split("_") if part
        ],
        source=source,
        provenance=["mcp:fusion_agent_memory_write"],
        trust_level=TrustLevel.UNTRUSTED,
        expires_at=expires_at,
        citations=citations,
    )
    path = store.write_record(record)
    return {
        "path": str(path),
        "metadata_path": str(path.with_suffix(path.suffix + ".memory.json")),
        "content_sha256": record.content_sha256,
        "source": record.source,
        "trust_level": record.trust_level,
        "memory_kind": kind,
    }


async def _memory_list_project_tool(args: JsonDict) -> JsonDict:
    project = _optional_str(args, "project") or "opencode"
    _safe_name(project, "project")
    records, _gate_summary = _gated_project_memory_records(project)
    return {"records": [record.model_dump(mode="json") for record in records]}


def _gated_project_memory_records(
    project: str,
) -> tuple[list[MemoryRecord], JsonDict]:
    """Load project memory through the same taint/expiry gate as search.

    A resource listing has no relevance query, so a zero relevance floor keeps
    otherwise safe records visible while retaining every content, expiry,
    integrity, and trust check performed by :class:`MemoryGate`.
    """

    store = MemoryStore(workspace_root=WORKSPACE_ROOT)
    store.seed_global()
    records = store.iter_records(project=project)
    allowed = MemoryGate(min_relevance=0).filter(records, "")
    blocked_by_status: dict[str, int] = {}
    for record in records:
        if not record.safety_status.startswith("blocked_"):
            continue
        blocked_by_status[record.safety_status] = (
            blocked_by_status.get(record.safety_status, 0) + 1
        )
    return allowed, {
        "examined_record_count": len(records),
        "blocked_record_count": len(records) - len(allowed),
        "blocked_by_safety_status": blocked_by_status,
    }


def _memory_resource_item(record: MemoryRecord) -> JsonDict:
    """Wrap one allowed memory record as cited, non-authoritative data."""

    return {
        "data_classification": "untrusted_memory_data",
        "treat_as_data": True,
        "embedded_instructions_are_authoritative": False,
        "provenance": list(record.provenance),
        "citations": list(record.citations),
        "record": record.model_dump(mode="json"),
    }


async def _skills_list_tool(_: JsonDict) -> JsonDict:
    skills = SkillLoader().load().all()
    return {
        "skills": [_skill_payload(skill, include_content=False) for skill in skills]
    }


async def _skills_get_tool(args: JsonDict) -> JsonDict:
    skill = SkillLoader().load().get(_required_str(args, "name"))
    if skill is None:
        raise KeyError(args["name"])
    return {"skill": _skill_payload(skill, include_content=True)}


async def _skills_rank_tool(args: JsonDict) -> JsonDict:
    registry = SkillLoader().load()
    ranked = SkillRouter(registry).rank(
        _required_str(args, "query"), limit=int(args.get("limit", 3))
    )
    return {
        "skills": [_skill_payload(skill, include_content=False) for skill in ranked]
    }


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
            "memory_records": [
                record.model_dump(mode="json") for record in gated_memory
            ],
            "skills": [
                _skill_payload(skill, include_content=False) for skill in ranked_skills
            ],
        },
    )


def _tool_surface_specs() -> list[mcp_surface.SurfaceSpec]:
    return [spec for spec in surface_specs() if spec.kind == "tool"]


def _tool_spec_map() -> dict[str, mcp_surface.SurfaceSpec]:
    """Return the same authoritative tool entries used for advertisement."""

    return {spec.name: spec for spec in _tool_surface_specs()}


async def _read_mcp_resource(
    uri: str,
    *,
    runtime: FusionAgentRuntime,
    profile: str,
) -> JsonDict:
    """Resolve one bounded ``fusion-agent://`` resource."""

    parsed = urlsplit(uri)
    if parsed.scheme != "fusion-agent":
        raise ValueError("resource URI must use the fusion-agent scheme")
    family = parsed.netloc
    # Authorization precedes identifier parsing, lookup, and handler dispatch
    # so a restricted profile cannot use errors or timing as an existence
    # oracle for memory, benchmark, or other resource families.
    mcp_surface.authorize_resource(uri, profile)
    segments = [unquote(segment) for segment in parsed.path.split("/") if segment]
    query = parse_qs(parsed.query, keep_blank_values=False)
    offset = _resource_integer(
        query, "offset", default=0, minimum=0, maximum=10_000_000
    )
    limit = _resource_integer(query, "limit", default=100, minimum=1, maximum=1000)

    runtime_token = _RUNTIME_OVERRIDE.set(runtime)
    profile_token = _PROFILE_OVERRIDE.set(profile)
    try:
        if family == "capabilities" and not segments:
            all_specs = _tool_surface_specs()
            selected = {spec.name for spec in all_specs if profile in spec.profiles}
            return {
                "schema_version": "fusion_agent.capabilities.v1",
                "profile": profile,
                "available_profiles": list(TOOL_PROFILES),
                "frontend_transport": "stdio",
                "active_backend": _runtime_configuration().backend,
                "backend_capability_matrix": {
                    "autodesk_http": {
                        "implemented": sorted(AUTODESK_IMPLEMENTED_CAPABILITIES),
                        "availability": "filtered_by_live_manifest_before_execution",
                        "arbitrary_code": False,
                        "fallback": False,
                    },
                    "faust_stdio": {
                        "implemented": sorted(FAUST_IMPLEMENTED_CAPABILITIES),
                        "availability": "filtered_by_live_manifest_before_execution",
                        "arbitrary_code": False,
                        "blocked_native_tools": ["delete_all", "execute_code"],
                        "mutable_fast_path": False,
                        "fallback": False,
                    },
                    "mock": {
                        "implemented": sorted(MOCK_IMPLEMENTED_CAPABILITIES),
                        "evidence_mode": "mock",
                    },
                },
                "experimental_manufacturing": {
                    "profile_required": ["advanced", "all"],
                    "environment_gate": "FUSION_AGENT_EXPERIMENTAL_MANUFACTURING=1",
                },
                "tools": [
                    {
                        "name": spec.name,
                        "capability_group": spec.capability_group,
                        "risk": spec.risk,
                        "evidence_role": spec.evidence_role,
                        "profiles": list(spec.profiles),
                    }
                    for spec in all_specs
                    if spec.name in selected
                ],
            }
        if family == "readiness" and not segments:
            return await _readiness_report_tool({})
        if family == "sessions" and len(segments) == 1:
            project = segments[0]
            _safe_name(project, "project")
            result = await _list_sessions_tool(
                {"project": project, "limit": 10_000_000}
            )
            return _page(result["sessions"], offset=offset, limit=limit)
        if family == "sessions" and len(segments) == 4 and segments[2] == "artifact":
            project, session_id, _, artifact = segments
            result = await _read_session_artifact_tool(
                {"project": project, "session_id": session_id, "artifact": artifact}
            )
            content = str(result["content"])
            character_limit = min(limit, 65536)
            page = content[offset : offset + character_limit]
            next_offset = (
                offset + len(page) if offset + len(page) < len(content) else None
            )
            return {
                "artifact": artifact,
                "path": result["path"],
                "offset": offset,
                "limit": character_limit,
                "total_characters": len(content),
                "next_offset": next_offset,
                "content": page,
                "complete": next_offset is None,
            }
        if family == "traces" and len(segments) == 2:
            project, session_id = segments
            path = _session_dir(project, session_id) / "tool_trace.jsonl"
            if not path.exists():
                raise FileNotFoundError(path)
            events = [
                _public_trace_event(_try_json(line))
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            return {"path": str(path), **_page(events, offset=offset, limit=limit)}
        if family == "manifests" and len(segments) == 1:
            result = await _read_manifest_tool({"source": segments[0]})
            if not result.get("loaded"):
                raise FileNotFoundError(
                    f"manifest artifact is absent: {result.get('path') or segments[0]}"
                )
            manifest = result.get("manifest")
            if not isinstance(manifest, dict) or not manifest:
                raise ValueError("manifest artifact is incomplete")
            page = _character_page(
                json.dumps(manifest, ensure_ascii=False, sort_keys=True),
                offset=offset,
                limit=limit,
            )
            return {
                "source": segments[0],
                "path": result["path"],
                "manifest_source": result.get("manifest_source"),
                **page,
            }
        if family == "skills" and len(segments) == 1:
            result = await _skills_get_tool({"name": segments[0]})
            skill = result.get("skill")
            if not isinstance(skill, dict):
                raise ValueError("skill artifact is incomplete")
            content = skill.get("content")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("skill artifact content is absent or incomplete")
            metadata = {key: value for key, value in skill.items() if key != "content"}
            return {
                "skill": metadata,
                **_character_page(content, offset=offset, limit=limit),
            }
        if family == "memory" and len(segments) == 1:
            project = segments[0]
            _safe_name(project, "project")
            records, gate_summary = _gated_project_memory_records(project)
            page = _page(
                [_memory_resource_item(record) for record in records],
                offset=offset,
                limit=limit,
            )
            return {
                "resource_type": "memory_records",
                "policy": {
                    "data_classification": "untrusted_memory_data",
                    "treat_as_data": True,
                    "embedded_instructions_are_authoritative": False,
                    "content_gate": "MemoryGate",
                },
                **gate_summary,
                **page,
            }
        if family == "benchmarks" and len(segments) == 2:
            run_id, view = segments
            return await _read_benchmark_report_tool(
                {"run_id": run_id, "view": view, "offset": offset, "limit": limit}
            )
    finally:
        _PROFILE_OVERRIDE.reset(profile_token)
        _RUNTIME_OVERRIDE.reset(runtime_token)
    raise FileNotFoundError(f"unknown Fusion Agent resource: {uri}")


def _page(items: list[Any], *, offset: int, limit: int) -> JsonDict:
    page = items[offset : offset + limit]
    next_offset = offset + len(page) if offset + len(page) < len(items) else None
    return {
        "items": page,
        "offset": offset,
        "limit": limit,
        "total": len(items),
        "next_offset": next_offset,
        "complete": next_offset is None,
    }


def _character_page(content: str, *, offset: int, limit: int) -> JsonDict:
    page = content[offset : offset + limit]
    next_offset = offset + len(page) if offset + len(page) < len(content) else None
    return {
        "content": page,
        "offset": offset,
        "limit": limit,
        "total_characters": len(content),
        "next_offset": next_offset,
        "complete": next_offset is None,
    }


def _resource_integer(
    query: dict[str, list[str]],
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    values = query.get(name)
    if not values:
        return default
    if len(values) != 1:
        raise ValueError(f"resource query parameter {name} must occur once")
    try:
        value = int(values[0])
    except ValueError as exc:
        raise ValueError(f"resource query parameter {name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(
            f"resource query parameter {name} must be between {minimum} and {maximum}"
        )
    return value


def _bounded_json_text(payload: JsonDict) -> str:
    text = json.dumps(_jsonable(payload), ensure_ascii=False, sort_keys=True)
    maximum = _runtime_configuration().resource_max_bytes
    size = len(text.encode("utf-8"))
    if size <= maximum:
        return text
    return json.dumps(
        {
            "ok": False,
            "error_code": "RESOURCE_PAYLOAD_LIMIT_EXCEEDED",
            "actual_bytes": size,
            "max_bytes": maximum,
            "message": "request a smaller page with offset and limit",
        },
        sort_keys=True,
    )


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
        "recommended_path": "safe_harness"
        if destructive
        else "api_documentation_then_native_fast",
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
    configuration = _runtime_configuration()
    value = str(args.get("mode", _default_mode(default)))
    if value not in {"mock", "real"}:
        raise ValueError("mode must be 'mock' or 'real'")
    if configuration.require_real and value != "real":
        raise ValueError(
            "Fusion Agent is configured for real-only mode; mode must be 'real'"
        )
    return value


def _default_mode(default: str = "mock") -> str:
    configuration = _runtime_configuration()
    if configuration.require_real:
        return "real"
    return (
        configuration.default_mode if configuration.default_mode_explicit else default
    )


def _ensure_dry_run_allowed(dry_run: bool) -> None:
    if dry_run and not _runtime_configuration().allow_dry_run:
        raise ValueError(
            "Fusion Agent dry-run is disabled by FUSION_AGENT_ALLOW_DRY_RUN=0"
        )


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
    if not ok:
        error = payload.get("error")
        if isinstance(payload.get("error_code"), str):
            summary["error_code"] = payload["error_code"]
        if isinstance(error, dict) and isinstance(error.get("correlation_id"), str):
            summary["correlation_id"] = error["correlation_id"]
        return json.dumps(_jsonable(summary), ensure_ascii=False, separators=(",", ":"))
    for key in (
        "status",
        "query_type",
        "operation_id",
        "execution_path",
        "duration_ms",
        "reason",
        "recommended_path",
    ):
        if key in payload:
            summary[key] = payload[key]
    if len(summary) == 2:
        summary["result_keys"] = sorted(payload)[:24]
    return json.dumps(_jsonable(summary), ensure_ascii=False, separators=(",", ":"))


def _as_call_tool_result(name: str, response: FastPathResponse) -> types.CallToolResult:
    semantic_code = _semantic_failure_code(response.payload)
    if response.is_error or semantic_code is not None:
        code = _normalized_public_error_code(
            response.payload.get("error_code") or semantic_code
        )
        return _public_call_tool_error(
            name,
            code=code,
            generic_message=_generic_public_error_message(code),
            retryable=code in _RETRYABLE_PUBLIC_ERROR_CODES,
            safe_result=_public_failure_state(response.payload),
        )

    structured = {"ok": True, "result": response.payload}
    if not _output_envelope_valid(name, structured):
        return _public_call_tool_error(
            name,
            code="OUTPUT_SCHEMA_VIOLATION",
            generic_message="The tool returned an invalid structured result.",
        )
    content: list[types.ContentBlock] = [
        types.TextContent(
            type="text",
            text=_compact_summary(name, response.payload, True),
        )
    ]
    content.extend(_mcp_content_blocks(response.content))
    return types.CallToolResult(
        meta=response.meta or None,
        content=content,
        structuredContent=structured,
        isError=False,
    )


_PUBLIC_ERROR_CODES = frozenset(
    {item.value for item in ErrorCode}
    | {
        "ENDPOINT_SOURCE_NOT_ALLOWED",
        "FAST_PATH_UNAVAILABLE_FOR_BACKEND",
        "INTERNAL_ERROR",
        "INCOMPLETE_INSPECTION",
        "INVALID_REQUEST",
        "OUTPUT_SCHEMA_VIOLATION",
        "REQUEST_REJECTED",
        "RESOURCE_NOT_FOUND",
        "SURFACE_NOT_AVAILABLE_IN_PROFILE",
        "TOOL_NOT_AVAILABLE_IN_PROFILE",
    }
)
_RETRYABLE_PUBLIC_ERROR_CODES = frozenset(
    {
        ErrorCode.TIMEOUT.value,
        ErrorCode.CONNECTION_UNAVAILABLE.value,
        ErrorCode.CONNECTION_LOST.value,
        ErrorCode.READ_TIMEOUT_MAY_STILL_BE_RUNNING.value,
    }
)

_SEMANTIC_FAILURE_STATUSES = frozenset(
    {
        "execution_failed",
        "inspection_failed",
        "mutation_outcome_unknown",
        "read_failed",
        "recovery_failed",
    }
)


def _semantic_failure_code(payload: JsonDict) -> str | None:
    status = payload.get("status")
    if status == "mutation_outcome_unknown":
        return ErrorCode.MUTATION_OUTCOME_UNKNOWN.value
    if status in _SEMANTIC_FAILURE_STATUSES:
        return str(payload.get("error_code") or ErrorCode.FUSION_OPERATION_FAILED.value)
    if payload.get("ok") is False:
        return str(payload.get("error_code") or "REQUEST_REJECTED")
    if payload.get("success") is False and (
        payload.get("error") is not None or payload.get("error_code") is not None
    ):
        return str(payload.get("error_code") or ErrorCode.FUSION_OPERATION_FAILED.value)
    return None


def _public_failure_state(payload: JsonDict) -> JsonDict | None:
    """Retain only typed recovery state; never retain diagnostics or paths."""

    public: JsonDict = {}
    status = payload.get("status")
    if status in _SEMANTIC_FAILURE_STATUSES:
        public["status"] = status
    for key in (
        "dispatched",
        "may_have_applied",
        "post_dispatch_replay_suppressed",
    ):
        if isinstance(payload.get(key), bool):
            public[key] = payload[key]
    outcome = payload.get("mutation_outcome")
    if outcome in {"known", "unknown"}:
        public["mutation_outcome"] = outcome
    mutation_status = payload.get("mutation_status")
    if mutation_status in {
        "not_dispatched",
        "observed_in_readback",
        "outcome_unknown",
        "unknown",
    }:
        public["mutation_status"] = mutation_status
    operation_id = payload.get("operation_id")
    if (
        isinstance(operation_id, str)
        and 1 <= len(operation_id) <= 128
        and all(
            character.isalnum() or character in "._:-" for character in operation_id
        )
    ):
        public["operation_id"] = operation_id
    return public or None


def _normalized_public_error_code(value: Any) -> str:
    code = str(value) if value is not None else "REQUEST_REJECTED"
    return (
        code if code in _PUBLIC_ERROR_CODES else ErrorCode.FUSION_OPERATION_FAILED.value
    )


def _generic_public_error_message(code: str) -> str:
    return {
        ErrorCode.FUSION_OPERATION_FAILED.value: "The downstream Fusion operation failed.",
        ErrorCode.CONNECTION_UNAVAILABLE.value: "The Fusion connection is unavailable.",
        ErrorCode.CONNECTION_LOST.value: "The Fusion connection was lost.",
        ErrorCode.TIMEOUT.value: "The Fusion operation timed out.",
        ErrorCode.READ_TIMEOUT_MAY_STILL_BE_RUNNING.value: "The Fusion read timed out.",
        ErrorCode.MUTATION_OUTCOME_UNKNOWN.value: "The mutation outcome is unknown; do not replay it.",
        "INCOMPLETE_INSPECTION": "The readback inspection was incomplete.",
        "REQUEST_REJECTED": "The request was rejected before completion.",
    }.get(code, "The operation could not be completed.")


def _public_call_tool_error(
    name: str,
    *,
    code: str,
    generic_message: str,
    retryable: bool = False,
    extras: JsonDict | None = None,
    safe_result: JsonDict | None = None,
) -> types.CallToolResult:
    public_error = PublicError.create(
        code=code,
        generic_message=generic_message,
        retryable=retryable,
    )
    payload: JsonDict = {
        "ok": False,
        "error_code": code,
        "error": public_error.model_dump(mode="json"),
    }
    payload.update(extras or {})
    if safe_result:
        payload["result"] = safe_result
        if not _output_envelope_valid(name, payload):
            payload.pop("result", None)
    # This envelope is code-owned and contains no handler result.  Validate it
    # as an invariant check, but never echo validator details to the caller.
    if not _output_envelope_valid(name, payload):
        fallback = PublicError.create(
            code="INTERNAL_ERROR",
            generic_message="The operation could not be completed.",
        )
        payload = {
            "ok": False,
            "error_code": "INTERNAL_ERROR",
            "error": fallback.model_dump(mode="json"),
        }
    return types.CallToolResult(
        content=[
            types.TextContent(type="text", text=_compact_summary(name, payload, False))
        ],
        structuredContent=payload,
        isError=True,
    )


def _public_resource_error(
    *, code: str, generic_message: str, protocol_code: int
) -> McpError:
    public_error = PublicError.create(
        code=code,
        generic_message=generic_message,
        retryable=False,
    )
    return McpError(
        types.ErrorData(
            code=protocol_code,
            message=public_error.generic_message,
            data=public_error.model_dump(mode="json"),
        )
    )


def _output_envelope_valid(name: str, payload: JsonDict) -> bool:
    try:
        return not any(_output_validator(name).iter_errors(payload))
    except Exception:  # noqa: BLE001 - validator details are never public
        return False


@lru_cache(maxsize=None)
def _output_validator(name: str) -> Draft202012Validator:
    try:
        schema = _tool_output_schema(name)
    except ValueError:
        schema = _open_output_schema()
    return Draft202012Validator(schema)


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
                    mimeType=str(
                        raw.get("mimeType") or raw.get("mime_type") or "image/png"
                    ),
                )
            )
        elif raw.get("type") == "text" and isinstance(raw.get("text"), str):
            blocks.append(types.TextContent(type="text", text=raw["text"]))
    return blocks


def _open_output_schema() -> JsonDict:
    """Return the typed compatibility envelope used only by unknown extensions."""

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "result": {"$ref": "#/$defs/jsonObject"},
            "error": {"$ref": "#/$defs/publicError"},
            "error_code": {"type": "string"},
        },
        "required": ["ok"],
        "additionalProperties": False,
        "$defs": _tool_output_defs(),
    }


def _fast_path_output_schema() -> JsonDict:
    """Compatibility alias for downstream imports; new specs are per-tool."""

    return _tool_output_schema("fusion_agent_fast_execute")


def _ref(name: str) -> JsonDict:
    return {"$ref": f"#/$defs/{name}"}


def _nullable(schema: JsonDict) -> JsonDict:
    return {"anyOf": [schema, {"type": "null"}]}


def _result_object(
    properties: JsonDict,
    required: tuple[str, ...] = (),
    *,
    additive: bool = False,
) -> JsonDict:
    """Create a concrete result object while retaining deliberate 0.x extension points."""

    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": _ref("jsonValue") if additive else False,
    }


def _tool_output_defs() -> JsonDict:
    """Reusable protocol types embedded into every public tool output schema."""

    return {
        "jsonValue": {
            "oneOf": [
                {"type": "null"},
                {"type": "boolean"},
                {"type": "number"},
                {"type": "string"},
                {"type": "array", "items": _ref("jsonValue")},
                {"type": "object", "additionalProperties": _ref("jsonValue")},
            ]
        },
        "jsonObject": {"type": "object", "additionalProperties": _ref("jsonValue")},
        "publicError": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "minLength": 1},
                "generic_message": {"type": "string", "minLength": 1},
                "correlation_id": {
                    "type": "string",
                    "pattern": "^diag-[0-9a-f]{32}$",
                },
                "retryable": {"type": "boolean"},
            },
            "required": [
                "code",
                "generic_message",
                "correlation_id",
                "retryable",
            ],
            "additionalProperties": False,
        },
        "stringList": {
            "type": "array",
            "items": {"type": "string"},
        },
        "verificationRequirement": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "description": {"type": "string"},
                "required": {"type": "boolean"},
                "assertion_ids": _ref("stringList"),
                "oracle": {
                    "type": "string",
                    "enum": ["contract", "independent_oracle"],
                },
                "covered": {"type": "boolean"},
                "passed": {"type": "boolean"},
                "oracle_evidence": {"type": "string"},
            },
            "required": ["id"],
            "additionalProperties": False,
        },
        "verificationContract": {
            "type": "object",
            "properties": {
                "mutation_status": {
                    "type": "string",
                    "enum": [
                        "not_dispatched",
                        "observed_in_readback",
                        "outcome_unknown",
                        "unknown",
                    ],
                },
                "mutation_outcome": {"type": "string", "enum": ["known", "unknown"]},
                "assertion_status": {
                    "type": "string",
                    "enum": ["not_run", "passed", "failed", "incomplete"],
                },
                "intent_coverage": {
                    "type": "string",
                    "enum": ["none", "partial", "complete"],
                },
                "verification_level": {
                    "type": "string",
                    "enum": ["assertions_only", "contract", "independent_oracle"],
                },
                "contract_verified": {"type": "boolean"},
                "inspection_complete": {"type": "boolean"},
                "passed": {"type": "boolean"},
                "assertions_passed": {"type": "boolean"},
                "readback_complete": {"type": "boolean"},
                "source": {"type": "string"},
                "assertions": {"type": "array", "items": _ref("jsonObject")},
                "invariants": {"type": "array", "items": _ref("jsonObject")},
                "requirements": {
                    "type": "array",
                    "items": _ref("verificationRequirement"),
                },
                "issues": {"type": "array", "items": _ref("jsonObject")},
                "metrics": _ref("jsonObject"),
            },
            "additionalProperties": _ref("jsonValue"),
        },
        "toolDefinition": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "description": {"type": "string"},
                "input_schema": _nullable(_ref("jsonObject")),
                "output_schema": _nullable(_ref("jsonObject")),
            },
            "required": ["name", "description"],
            "additionalProperties": False,
        },
        "toolManifest": {
            "type": "object",
            "properties": {
                "schema_version": {"type": "integer", "minimum": 1},
                "source": {"type": "string"},
                "tools": {"type": "array", "items": _ref("toolDefinition")},
                "fingerprint": {"type": "string"},
                "captured_at": {"type": "string"},
                "server": _ref("jsonObject"),
                "server_name": _nullable({"type": "string"}),
                "server_version": _nullable({"type": "string"}),
                "protocol_version": _nullable({"type": "string"}),
                "previous_fingerprint": _nullable({"type": "string"}),
            },
            "required": [
                "schema_version",
                "source",
                "tools",
                "fingerprint",
                "captured_at",
            ],
            "additionalProperties": False,
        },
        "memoryRecord": {
            "type": "object",
            "properties": {
                "schema_version": {"const": "memory_record.v2"},
                "id": {"type": "string"},
                "scope": {"type": "string", "enum": ["global", "project"]},
                "type": {
                    "type": "string",
                    "enum": [
                        "fact",
                        "result",
                        "user_preference",
                        "failure_pattern",
                        "repair_recipe",
                        "design_decision",
                        "skill_note",
                        "benchmark_result",
                        "session_summary",
                    ],
                },
                "summary": {"type": "string"},
                "content": {"type": "string"},
                "content_path": {"type": "string"},
                "project": _nullable({"type": "string"}),
                "tags": _ref("stringList"),
                "confidence": {"type": "string"},
                "created_at": {"type": "string"},
                "updated_at": {"type": "string"},
                "relevance_score": {"type": "number"},
                "safety_status": {"type": "string"},
                "contradiction_status": {"type": "string"},
                "source": {
                    "type": "string",
                    "enum": ["user", "workspace", "tool", "web", "legacy"],
                },
                "provenance": _ref("stringList"),
                "trust_level": {
                    "type": "string",
                    "enum": ["verified", "trusted", "untrusted", "legacy_unverified"],
                },
                "expires_at": _nullable({"type": "string"}),
                "content_sha256": _nullable(
                    {"type": "string", "pattern": "^[a-f0-9]{64}$"}
                ),
                "citations": _ref("stringList"),
                "source_url": _nullable({"type": "string"}),
                "source_retrieved_at": _nullable({"type": "string"}),
                "source_content_sha256": _nullable(
                    {"type": "string", "pattern": "^[a-f0-9]{64}$"}
                ),
                "taint_flags": _ref("stringList"),
            },
            "required": [
                "schema_version",
                "id",
                "scope",
                "type",
                "summary",
                "content",
                "content_path",
            ],
            "additionalProperties": False,
        },
        "skill": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "path": {"type": "string"},
                "content": {"type": "string"},
                "purpose": {"type": "string"},
                "status": {"type": "string"},
                "failure_modes": _ref("stringList"),
                "facade_operations": _ref("stringList"),
            },
            "required": [
                "name",
                "path",
                "purpose",
                "status",
                "failure_modes",
                "facade_operations",
            ],
            "additionalProperties": False,
        },
    }


def _session_result_contract() -> JsonDict:
    return _result_object(
        {
            "session_id": {"type": "string"},
            "status": {"type": "string"},
            "final_status": {"type": "string"},
            "summary": {"type": "string"},
            "cad_spec_version": {"type": "string"},
            "contract_eligible": {"type": "boolean"},
            "warnings": _ref("stringList"),
            "cad_spec_path": {"type": "string"},
            "journal_path": {"type": "string"},
            "trace_path": {"type": "string"},
            "execution": _ref("jsonObject"),
            "verification": _ref("verificationContract"),
            "repair_attempts": {"type": "array", "items": _ref("jsonValue")},
            "memory_updates": _ref("stringList"),
            "planning": _ref("jsonObject"),
            "dry_run": {"type": "boolean"},
        },
        (
            "session_id",
            "status",
            "cad_spec_path",
            "journal_path",
            "trace_path",
            "execution",
            "verification",
            "dry_run",
        ),
    )


def _planning_result_contract(*, include_path: bool) -> JsonDict:
    properties: JsonDict = {
        "cad_spec": _ref("jsonObject"),
        "cad_spec_json": {"type": "string"},
        "project": {"type": "string"},
        "memory_records": {"type": "array", "items": _ref("memoryRecord")},
        "skills": {"type": "array", "items": _ref("skill")},
        "supported": {"type": "boolean"},
        "code": {"type": "string"},
        "reason": {"type": "string"},
        "recommended_path": {
            "type": "string",
            "enum": ["safe_harness", "api_documentation_then_native_fast"],
        },
        "recommended_tools": _ref("stringList"),
    }
    if include_path:
        properties["path"] = {"type": "string"}
    return _result_object(properties)


def _fast_control_properties() -> JsonDict:
    return {
        "status": {
            "type": "string",
            "enum": [
                "blocked_before_apply",
                "aborted_before_apply",
                "applied_verified",
                "applied_partially_verified",
                "applied_unverified",
                "mutation_outcome_unknown",
                "partial_change_detected",
                "execution_failed",
                "aborted_after_verification",
                "recovered_verified",
                "recovery_failed",
                "read_succeeded",
                "read_failed",
                "inspection_failed",
            ],
        },
        "error": _ref("publicError"),
        "error_code": _nullable({"type": "string"}),
        "reason": {"type": "string"},
        "message": {"type": "string"},
        "recommended_path": {"type": "string"},
        "tool": {"type": "string"},
        "backend": {"type": "string"},
        "operation_id": {"type": "string"},
        "dispatched": {"type": "boolean"},
        "may_have_applied": {"type": "boolean"},
        "post_dispatch_replay_suppressed": {"type": "boolean"},
        "mutation_outcome": {"type": "string", "enum": ["known", "unknown"]},
        "mutation_status": {
            "type": "string",
            "enum": [
                "not_dispatched",
                "observed_in_readback",
                "outcome_unknown",
                "unknown",
            ],
        },
        "assertion_status": {
            "type": "string",
            "enum": ["not_run", "passed", "failed", "incomplete"],
        },
        "intent_coverage": {"type": "string", "enum": ["none", "partial", "complete"]},
        "verification_level": {
            "type": "string",
            "enum": ["assertions_only", "contract", "independent_oracle"],
        },
    }


def _tool_result_contracts() -> dict[str, JsonDict]:
    """Return the dedicated stable result contract for every public tool."""

    path = {"type": "string"}
    string = {"type": "string"}
    boolean = {"type": "boolean"}
    json_object = _ref("jsonObject")
    contracts: dict[str, JsonDict] = {
        "fusion_agent_doctor": _result_object(
            {
                "project_root": path,
                "workspace": path,
                "outputs": path,
                "manifests": path,
                "python_executable": path,
                "launcher_path": path,
                "source_plugin_root": path,
                "cache_plugin_version": string,
                "fusion_mcp_endpoint": string,
                "fusion_mcp_endpoint_configured": boolean,
                "fusion_mcp_command_configured": boolean,
                "fusion_agent_default_mode": {
                    "type": "string",
                    "enum": ["mock", "real"],
                },
                "fusion_agent_require_real": boolean,
                "fusion_agent_allow_dry_run": boolean,
                "dry_run_policy": {"type": "string", "enum": ["allowed", "disabled"]},
                "manifest_status": json_object,
            },
            (
                "project_root",
                "workspace",
                "outputs",
                "manifests",
                "python_executable",
                "launcher_path",
                "source_plugin_root",
                "cache_plugin_version",
                "fusion_mcp_endpoint",
                "fusion_mcp_endpoint_configured",
                "fusion_mcp_command_configured",
                "fusion_agent_default_mode",
                "fusion_agent_require_real",
                "fusion_agent_allow_dry_run",
                "dry_run_policy",
                "manifest_status",
            ),
        ),
        "fusion_agent_readiness_report": _result_object(
            {
                "schema_version": {
                    "type": "string",
                    "const": "fusion_agent.readiness.v2",
                },
                "mcp_version": string,
                "tool_profile": {"type": "string", "enum": list(TOOL_PROFILES)},
                "available_tool_profiles": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(TOOL_PROFILES)},
                    "uniqueItems": True,
                },
                "doctor": json_object,
                "safe_facade_tool_count": {"type": "integer", "minimum": 0},
                "safe_facade_tools": _ref("stringList"),
                "manifest_status": json_object,
                "persistent_runtime": json_object,
                "recommended_startup_sequence": _ref("stringList"),
            },
            (
                "schema_version",
                "mcp_version",
                "tool_profile",
                "available_tool_profiles",
                "doctor",
                "safe_facade_tool_count",
                "safe_facade_tools",
                "manifest_status",
                "persistent_runtime",
                "recommended_startup_sequence",
            ),
        ),
        "fusion_agent_probe": _result_object(
            {
                "ok": boolean,
                "error_code": string,
                "error": string,
                "probes": {
                    "type": "array",
                    "items": _result_object(
                        {
                            "endpoint": string,
                            "health_uri": string,
                            "health": json_object,
                            "tools_list": json_object,
                        },
                        ("endpoint", "health_uri", "health", "tools_list"),
                    ),
                },
            },
            ("probes",),
        ),
        "fusion_agent_session_health": _result_object(
            {
                "mode": {"type": "string", "enum": ["mock", "real"]},
                "launcher_ok": boolean,
                "launcher_python": string,
                "python_executable": path,
                "fusion_mcp_endpoint": string,
                "default_mode": string,
                "require_real": string,
                "allow_dry_run": string,
                "manifest_ok": boolean,
                "manifest_error": _nullable(string),
                "manifest_source": _nullable(string),
                "manifest_tool_count": {"type": "integer", "minimum": 0},
                "manifest_status": json_object,
                "mcp_server_ok": boolean,
                "real_endpoint_ok": _nullable(boolean),
                "native_tools_attached": boolean,
                "native_tool_count": {"type": "integer", "minimum": 0},
                "native_tool_sample": _ref("stringList"),
                "live_manifest_fingerprint": string,
                "cached_manifest_fingerprint": _nullable(string),
                "manifest_drift": boolean,
                "connection": json_object,
                "native_error": string,
                "healthy": boolean,
                "persistent_runtime": json_object,
            },
            (
                "mode",
                "launcher_ok",
                "manifest_ok",
                "mcp_server_ok",
                "native_tools_attached",
                "healthy",
            ),
        ),
        "fusion_agent_inspect": _result_object(
            {
                "status": string,
                "schema_version": {"type": ["string", "integer"]},
                "document": json_object,
                "counts": json_object,
                "geometry": json_object,
                "parameters": {"type": "array", "items": _ref("jsonObject")},
                "assembly": json_object,
                "physical_properties": json_object,
                "snapshot": json_object,
                "summary": json_object,
                "completeness": json_object,
                "budget": json_object,
                "complete": boolean,
                "counts_exact": boolean,
                "truncated": boolean,
                "stop_reason": _nullable(string),
            },
            additive=True,
        ),
        "fusion_agent_native_read": _result_object(
            {
                **_fast_control_properties(),
                "query_type": {
                    "type": "string",
                    "enum": [
                        "api_documentation",
                        "projects",
                        "document",
                        "active_command",
                        "screenshot",
                    ],
                },
                "data": json_object,
                "manifest_fingerprint": string,
                "duration_ms": {"type": "integer", "minimum": 0},
                "evidence_role": {"const": "supplemental_visual"},
            }
        ),
        "fusion_agent_targeted_inspect": _result_object(
            {
                **_fast_control_properties(),
                "schema_version": {"type": ["string", "integer"]},
                "document": json_object,
                "summary": json_object,
                "results": {"type": "array", "items": _ref("jsonObject")},
                "complete": boolean,
                "counts_exact": boolean,
                "truncated": boolean,
                "stop_reason": _nullable(string),
                "manifest_fingerprint": string,
                "duration_ms": {"type": "integer", "minimum": 0},
            },
            additive=True,
        ),
        "fusion_agent_fast_execute": _result_object(
            {
                **_fast_control_properties(),
                "execution_path": {"const": "native_fast"},
                "intent": string,
                "change_class": {
                    "type": "string",
                    "enum": ["read_only", "additive", "scoped_update"],
                },
                "script_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
                "api_references": _ref("stringList"),
                "policy": json_object,
                "executor_guard": json_object,
                "baseline": json_object,
                "execution": json_object,
                "after": json_object,
                "verification": _ref("verificationContract"),
                "manifest_fingerprint": string,
                "duration_ms": {"type": "integer", "minimum": 0},
                "native_call_count": {"type": "integer", "minimum": 0},
                "declared_mutation_count": {"type": "integer", "minimum": 0},
                "transport_mutating_dispatch_count": {"type": "integer", "minimum": 0},
                "mutating_call_count": {"type": "integer", "minimum": 0},
                "bindings": json_object,
                "recovery_instruction": string,
                "screenshot": json_object,
                "artifacts": json_object,
            },
            additive=True,
        ),
        "fusion_agent_recover_change": _result_object(
            {
                **_fast_control_properties(),
                "source_operation_id": string,
                "action": {"type": "string", "enum": ["undo", "redo"]},
                "execution_path": {"const": "native_fast"},
                "verification": _ref("verificationContract"),
                "before": json_object,
                "after": json_object,
                "manifest_fingerprint": string,
                "duration_ms": {"type": "integer", "minimum": 0},
            },
            additive=True,
        ),
        "fusion_agent_compact_snapshot": _result_object(
            {
                "snapshot_id": string,
                "project": string,
                "mode": {"type": "string", "enum": ["mock", "real"]},
                "max_occurrences": {"type": "integer", "minimum": 1},
                "max_bodies": {"type": "integer", "minimum": 1},
                "include_transforms": boolean,
                "max_entities_visited": {"type": "integer", "minimum": 1},
                "deadline_ms": {"type": "integer", "minimum": 0},
                "max_response_bytes": {"type": "integer", "minimum": 1},
                "snapshot": json_object,
                "snapshot_path": path,
            },
            ("snapshot_id", "project", "mode", "snapshot", "snapshot_path"),
        ),
        "fusion_agent_hub_inventory": _result_object(
            {
                "mode": {"type": "string", "enum": ["mock", "real"]},
                "strategy": json_object,
                "results": {"type": "array", "items": _ref("jsonObject")},
                "query": string,
                "max_results": {"type": "integer", "minimum": 0},
                "enriched_count": {"type": "integer", "minimum": 0},
                "truncated": boolean,
            },
            ("mode", "results"),
            additive=True,
        ),
        "fusion_agent_safe_change_preview": _result_object(
            {
                "schema_version": {"const": "safe_change_preview.v2"},
                "preview_id": string,
                "preview_status": {
                    "type": "string",
                    "enum": ["ready", "applying", "consumed", "stale"],
                },
                "created_at": string,
                "project": string,
                "mode": {"type": "string", "enum": ["mock", "real"]},
                "operation": {
                    "type": "string",
                    "enum": ["move", "delete", "visibility", "componentize"],
                },
                "targets": {"type": "array", "items": _ref("jsonObject")},
                "policy": json_object,
                "classification": json_object,
                "blocked": boolean,
                "baseline_complete": boolean,
                "baseline_stop_reason": _nullable(string),
                "baseline_id": string,
                "before_snapshot_path": path,
                "document_identity": json_object,
                "state_fingerprint": string,
                "bound_targets": {"type": "array", "items": _ref("jsonObject")},
                "binding_errors": {"type": "array", "items": _ref("jsonValue")},
                "inspection_budget": json_object,
                "requirements": {
                    "type": "array",
                    "items": _ref("verificationRequirement"),
                },
                "negative_impact": boolean,
                "preview_path": path,
            },
            (
                "schema_version",
                "preview_id",
                "preview_status",
                "project",
                "mode",
                "operation",
                "preview_path",
            ),
        ),
        "fusion_agent_safe_change_apply": _result_object(
            {
                **_fast_control_properties(),
                "schema_version": string,
                "preview_id": string,
                "preview_status": {
                    "type": "string",
                    "enum": ["ready", "applying", "consumed", "stale"],
                },
                "project": string,
                "mode": {"type": "string", "enum": ["mock", "real"]},
                "operation": {
                    "type": "string",
                    "enum": ["move", "delete", "visibility", "componentize"],
                },
                "targets": {"type": "array", "items": _ref("jsonObject")},
                "policy": json_object,
                "classification": json_object,
                "blocked": boolean,
                "baseline_complete": boolean,
                "baseline_stop_reason": _nullable(string),
                "baseline_id": string,
                "before_snapshot_path": path,
                "document_identity": json_object,
                "state_fingerprint": string,
                "bound_targets": {"type": "array", "items": _ref("jsonObject")},
                "binding_errors": {"type": "array", "items": _ref("jsonValue")},
                "inspection_budget": json_object,
                "requirements": {
                    "type": "array",
                    "items": _ref("verificationRequirement"),
                },
                "verification": _ref("verificationContract"),
                "contract_verified": boolean,
                "negative_impact": boolean,
                "applied": json_object,
                "after_snapshot_path": path,
                "abort_reason": string,
                "recovery_instructions": string,
            },
            additive=True,
        ),
        "fusion_agent_verify_active_design": _result_object(
            {
                "session_id": string,
                "status": {"type": "string", "enum": ["success", "failed"]},
                "cad_spec_path": path,
                "journal_path": path,
                "trace_path": path,
                "verification": _ref("verificationContract"),
            },
            (
                "session_id",
                "status",
                "cad_spec_path",
                "journal_path",
                "trace_path",
                "verification",
            ),
        ),
        "fusion_agent_capture_viewport": _result_object(
            {
                "session_id": string,
                "status": string,
                "path": path,
                "journal_path": path,
                "trace_path": path,
                "capture": json_object,
                "evidence_role": {"const": "supplemental_visual"},
                "can_promote_geometry_verification": {"const": False},
            },
            (
                "session_id",
                "status",
                "path",
                "journal_path",
                "trace_path",
                "capture",
                "evidence_role",
                "can_promote_geometry_verification",
            ),
        ),
        "fusion_agent_run_session": _session_result_contract(),
        "fusion_agent_dry_run_session": _session_result_contract(),
        "fusion_agent_list_sessions": _result_object(
            {
                "sessions": {
                    "type": "array",
                    "items": _result_object(
                        {
                            "project": string,
                            "session_id": string,
                            "path": path,
                            "final_status": _nullable(string),
                            "summary": _nullable(string),
                            "artifacts": _ref("stringList"),
                        },
                        (
                            "project",
                            "session_id",
                            "path",
                            "final_status",
                            "summary",
                            "artifacts",
                        ),
                    ),
                }
            },
            ("sessions",),
        ),
        "fusion_agent_read_session_artifact": _result_object(
            {
                "path": path,
                "artifact": {"type": "string", "enum": sorted(SESSION_ARTIFACTS)},
                "content": string,
                "json": _ref("jsonValue"),
            },
            ("path", "artifact", "content", "json"),
        ),
        "fusion_agent_read_trace": _result_object(
            {
                "path": path,
                "events": {"type": "array", "items": _ref("jsonValue")},
                "event_count": {"type": "integer", "minimum": 0},
            },
            ("path", "events", "event_count"),
        ),
        "fusion_agent_plan_spec": _planning_result_contract(include_path=False),
        "fusion_agent_validate_spec": _result_object(
            {
                "valid": boolean,
                "error": string,
                "cad_spec": json_object,
                "cad_spec_version": string,
                "contract_eligible": boolean,
                "warnings": _ref("stringList"),
            },
            ("valid",),
        ),
        "fusion_agent_export_spec_json": _planning_result_contract(include_path=True),
        "fusion_agent_list_benchmarks": _result_object(
            {
                "suites": {
                    "type": "array",
                    "items": _result_object(
                        {
                            "name": string,
                            "path": path,
                            "schema_version": string,
                            "suite_id": string,
                            "case_count": {"type": "integer", "minimum": 0},
                            "valid": boolean,
                            "error": string,
                        },
                        ("name", "path", "valid"),
                    ),
                }
            },
            ("suites",),
        ),
        "fusion_agent_run_benchmark": _result_object(
            {
                "schema_version": string,
                "run_id": string,
                "suite_id": string,
                "trial_count": {"type": "integer", "minimum": 0},
                "summary": json_object,
                "report_path": path,
                "summary_path": path,
                "trials_path": path,
                "environment_path": path,
            },
            (
                "schema_version",
                "run_id",
                "suite_id",
                "trial_count",
                "summary",
                "report_path",
                "summary_path",
                "trials_path",
                "environment_path",
            ),
        ),
        "fusion_agent_read_benchmark_report": _result_object(
            {
                "legacy": boolean,
                "path": path,
                "run_id": string,
                "view": {
                    "type": "string",
                    "enum": [
                        "report",
                        "summary",
                        "trials",
                        "environment",
                        "traces",
                        "oracles",
                        "legacy",
                    ],
                },
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1},
                "total": {"type": "integer", "minimum": 0},
                "items": {"type": "array", "items": _ref("jsonValue")},
                "trials": {"type": "array", "items": _ref("jsonObject")},
                "report": json_object,
                "text": string,
                "environment": json_object,
            },
            ("view",),
        ),
        "fusion_agent_discover_tools": _ref("toolManifest"),
        "fusion_agent_propose_mapping": _result_object(
            {
                "manifest_loaded": boolean,
                "profile": string,
                "proposals": {
                    "type": "array",
                    "items": _result_object(
                        {
                            "facade_operation": string,
                            "candidate_native_tool": string,
                            "available": boolean,
                            "status": {
                                "type": "string",
                                "enum": [
                                    "allowlisted_via_vendor_facade",
                                    "blocked_until_allowlisted",
                                ],
                            },
                        },
                        (
                            "facade_operation",
                            "candidate_native_tool",
                            "available",
                            "status",
                        ),
                    ),
                },
            },
            ("manifest_loaded", "proposals"),
        ),
        "fusion_agent_read_manifest": _result_object(
            {
                "loaded": boolean,
                "path": path,
                "manifest": _nullable(_ref("toolManifest")),
                "manifest_source": _nullable(string),
            },
            ("loaded", "path", "manifest", "manifest_source"),
        ),
        "fusion_agent_memory_search": _result_object(
            {"records": {"type": "array", "items": _ref("memoryRecord")}},
            ("records",),
        ),
        "fusion_agent_memory_write": _result_object(
            {
                "path": path,
                "metadata_path": path,
                "content_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
                "source": {
                    "type": "string",
                    "enum": ["user", "workspace", "tool", "web"],
                },
                "trust_level": {
                    "type": "string",
                    "enum": ["verified", "trusted", "untrusted"],
                },
                "memory_kind": {
                    "type": "string",
                    "enum": ["fact", "preference", "result"],
                },
            },
            (
                "path",
                "metadata_path",
                "content_sha256",
                "source",
                "trust_level",
                "memory_kind",
            ),
        ),
        "fusion_agent_memory_list_project": _result_object(
            {"records": {"type": "array", "items": _ref("memoryRecord")}},
            ("records",),
        ),
        "fusion_agent_skills_list": _result_object(
            {"skills": {"type": "array", "items": _ref("skill")}},
            ("skills",),
        ),
        "fusion_agent_skills_get": _result_object(
            {"skill": _ref("skill")},
            ("skill",),
        ),
        "fusion_agent_skills_rank": _result_object(
            {"skills": {"type": "array", "items": _ref("skill")}},
            ("skills",),
        ),
    }
    return contracts


def _tool_output_schema(name: str) -> JsonDict:
    """Build a named, typed envelope for one public tool result."""

    result_contracts = _tool_result_contracts()
    try:
        result_schema = result_contracts[name]
    except KeyError as exc:  # New tools must declare an output contract deliberately.
        raise ValueError(f"missing dedicated output schema for {name}") from exc
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": f"{name}.output",
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "result": result_schema,
            "error": _ref("publicError"),
            "error_code": {"type": "string"},
            "tool": {"type": "string", "const": name},
            "profile": {"type": "string", "enum": list(TOOL_PROFILES)},
            "available_profiles": {
                "type": "array",
                "items": {"type": "string", "enum": list(TOOL_PROFILES)},
                "uniqueItems": True,
            },
        },
        "required": ["ok"],
        "allOf": [
            {
                "if": {"properties": {"ok": {"const": True}}, "required": ["ok"]},
                "then": {"required": ["result"]},
            },
            {
                "if": {
                    "properties": {"ok": {"const": False}},
                    "required": ["ok"],
                },
                "then": {
                    "anyOf": [
                        {"required": ["result"]},
                        {"required": ["error"]},
                        {"required": ["error_code"]},
                    ]
                },
            },
        ],
        "additionalProperties": False,
        "$defs": _tool_output_defs(),
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


_LOCAL_OR_REMOTE_WRITE_TOOLS = {
    "fusion_agent_compact_snapshot",
    "fusion_agent_fast_execute",
    "fusion_agent_recover_change",
    "fusion_agent_safe_change_preview",
    "fusion_agent_safe_change_apply",
    "fusion_agent_capture_viewport",
    "fusion_agent_run_session",
    "fusion_agent_dry_run_session",
    "fusion_agent_plan_spec",
    "fusion_agent_export_spec_json",
    "fusion_agent_run_benchmark",
    "fusion_agent_discover_tools",
    "fusion_agent_memory_write",
    "fusion_agent_memory_search",
    "fusion_agent_memory_list_project",
}
_DESTRUCTIVE_TOOLS = {
    "fusion_agent_fast_execute",
    "fusion_agent_recover_change",
    "fusion_agent_safe_change_apply",
    "fusion_agent_run_session",
    "fusion_agent_run_benchmark",
}
_NON_IDEMPOTENT_TOOLS = _LOCAL_OR_REMOTE_WRITE_TOOLS


def _annotations_for_tool(name: str) -> types.ToolAnnotations:
    return _tool_annotations(
        read_only=name not in _LOCAL_OR_REMOTE_WRITE_TOOLS,
        idempotent=name not in _NON_IDEMPOTENT_TOOLS,
        destructive=name in _DESTRUCTIVE_TOOLS,
    )


def _tool_metadata(name: str) -> tuple[str, str, str]:
    if name.startswith("fusion_agent_memory_"):
        group = "memory"
    elif name.startswith("fusion_agent_skills_"):
        group = "skills"
    elif "benchmark" in name:
        group = "benchmark"
    elif name in {
        "fusion_agent_plan_spec",
        "fusion_agent_validate_spec",
        "fusion_agent_export_spec_json",
    }:
        group = "planning"
    elif name in {
        "fusion_agent_doctor",
        "fusion_agent_readiness_report",
        "fusion_agent_probe",
        "fusion_agent_session_health",
        "fusion_agent_discover_tools",
        "fusion_agent_propose_mapping",
        "fusion_agent_read_manifest",
    }:
        group = "diagnostics"
    elif name in {
        "fusion_agent_native_read",
        "fusion_agent_targeted_inspect",
        "fusion_agent_inspect",
        "fusion_agent_compact_snapshot",
        "fusion_agent_hub_inventory",
        "fusion_agent_capture_viewport",
    }:
        group = "inspection"
    elif name in {
        "fusion_agent_safe_change_preview",
        "fusion_agent_safe_change_apply",
        "fusion_agent_recover_change",
        "fusion_agent_fast_execute",
    }:
        group = "change_control"
    else:
        group = "session"
    risk = (
        "destructive"
        if name in _DESTRUCTIVE_TOOLS
        else ("write" if name in _LOCAL_OR_REMOTE_WRITE_TOOLS else "read")
    )
    evidence = (
        "supplemental_visual"
        if name == "fusion_agent_capture_viewport"
        else (
            "independent_oracle"
            if name == "fusion_agent_verify_active_design"
            else "structured"
        )
    )
    return group, risk, evidence


def _fast_path_mode() -> str:
    return _context_capability_value(
        "fast_path",
        allowed={"off", "read_only", "enabled"},
        default=_runtime_configuration().fast_path_mode,
    )


def _execution_path() -> str:
    return _context_capability_value(
        "execution_path",
        allowed={"auto", "native_fast", "safe_harness"},
        default=_runtime_configuration().execution_path,
    )


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
        "response": _public_audit_response(response),
        "is_error": response.is_error,
    }
    audit_path.write_text(_json_text(audit) + "\n", encoding="utf-8", newline="\n")
    return {"audit_path": str(audit_path)}


def _public_audit_response(response: FastPathResponse) -> JsonDict:
    semantic_code = _semantic_failure_code(response.payload)
    if not response.is_error and semantic_code is None:
        projected = redact_sensitive(response.payload)
        return projected if isinstance(projected, dict) else {}
    code = _normalized_public_error_code(
        response.payload.get("error_code") or semantic_code
    )
    state = _public_failure_state(response.payload) or {}
    state["error_code"] = code
    state["error"] = PublicError.create(
        code=code,
        generic_message=_generic_public_error_message(code),
        retryable=code in _RETRYABLE_PUBLIC_ERROR_CODES,
    ).model_dump(mode="json")
    return state


def _schema(
    properties: JsonDict | None = None, required: list[str] | None = None
) -> JsonDict:
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


def _integer(
    minimum: int | None = None, maximum: int | None = None, default: int | None = None
) -> JsonDict:
    schema: JsonDict = {"type": "integer"}
    if minimum is not None:
        schema["minimum"] = minimum
    if maximum is not None:
        schema["maximum"] = maximum
    if default is not None:
        schema["default"] = default
    return schema


def _mode_property(default: str = "mock") -> JsonDict:
    return {
        "type": "string",
        "enum": ["mock", "real"],
        "default": _default_mode(default),
    }


def _mode_schema(default: str = "mock") -> JsonDict:
    return _schema({"mode": _mode_property(default)})


def _inspection_budget_properties() -> JsonDict:
    configuration = _runtime_configuration()
    return {
        "max_entities_visited": _integer(
            1, 5000, configuration.inspection_max_entities
        ),
        "deadline_ms": _integer(50, 5000, configuration.inspection_deadline_ms),
        "max_response_bytes": _integer(
            4096, 1_048_576, configuration.inspection_max_response_bytes
        ),
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
                "enum": [
                    "document",
                    "component",
                    "occurrence",
                    "body",
                    "sketch",
                    "feature",
                    "parameter",
                ],
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
                        "requirement_ids": {
                            "type": "array",
                            "items": _string(),
                            "maxItems": 100,
                            "uniqueItems": True,
                        },
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
            "requirements": {
                "type": "array",
                "maxItems": 100,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": _string(),
                        "description": _string(),
                        "required": _boolean(True),
                        "assertion_ids": {
                            "type": "array",
                            "items": _string(),
                            "maxItems": 100,
                            "uniqueItems": True,
                        },
                        "oracle": {
                            "type": "string",
                            "enum": ["contract", "independent_oracle"],
                            "default": "contract",
                        },
                    },
                    "required": ["id"],
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
                "enum": [
                    "api_documentation",
                    "projects",
                    "document",
                    "active_command",
                    "screenshot",
                ],
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
            "change_class": {
                "type": "string",
                "enum": ["read_only", "additive", "scoped_update"],
            },
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
    schema = _schema(
        {
            "prompt": _string(),
            "spec_json": _string(),
            "mode": _mode_property("mock"),
            "project": _string(),
            "max_repairs": _integer(0, 20, 5),
            "dry_run": _boolean(False),
        }
    )
    schema["oneOf"] = [
        {"required": ["prompt"], "not": {"required": ["spec_json"]}},
        {"required": ["spec_json"], "not": {"required": ["prompt"]}},
    ]
    return schema


def _dry_run_schema() -> JsonDict:
    schema = _schema(
        {
            "prompt": _string(),
            "spec_json": _string(),
            "mode": _mode_property("mock"),
            "project": _string(),
            "max_repairs": _integer(0, 20, 5),
        }
    )
    schema["oneOf"] = [
        {"required": ["prompt"], "not": {"required": ["spec_json"]}},
        {"required": ["spec_json"], "not": {"required": ["prompt"]}},
    ]
    return schema


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
            "view": {
                "type": "string",
                "enum": ["isometric", "front", "top", "right"],
                "default": "isometric",
            },
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
            "operation": {
                "type": "string",
                "enum": ["move", "delete", "visibility", "componentize"],
            },
            "targets": {
                "type": "array",
                "items": {"type": "object", "additionalProperties": True},
            },
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
        {
            "project": _string(),
            "session_id": _string(),
            "limit": _integer(1, 1000, 100),
        },
        ["project", "session_id"],
    )


def _plan_schema() -> JsonDict:
    return _schema({"prompt": _string(), "project": _string()}, ["prompt"])


def _export_spec_schema() -> JsonDict:
    return _schema(
        {"prompt": _string(), "project": _string(), "output_path": _string()},
        ["prompt"],
    )


def _benchmark_schema() -> JsonDict:
    return _schema(
        {
            "suite": _string(),
            "driver": {
                "type": "string",
                "enum": ["internal", "codex_e2e"],
                "default": "internal",
            },
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
                "enum": [
                    "report",
                    "summary",
                    "trials",
                    "environment",
                    "traces",
                    "oracles",
                ],
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
    return _schema(
        {
            "project": _string(),
            "path": _string(),
            "content": _string(),
            "memory_kind": {
                "type": "string",
                "enum": ["fact", "preference", "result"],
                "default": "fact",
            },
            "source": {
                "type": "string",
                "enum": ["user", "workspace", "tool", "web"],
                "default": "user",
            },
            "citations": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "maxItems": 50,
            },
            "expires_at": {"type": "string", "format": "date-time"},
        },
        ["project", "path", "content"],
    )


if __name__ == "__main__":
    raise SystemExit(main())
