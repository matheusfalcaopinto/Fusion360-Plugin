"""Declarative MCP surface metadata and profile authorization.

Advertisement and use-time authorization intentionally consume the same
``SurfaceSpec`` objects.  A profile is therefore unable to construct a URI or
prompt name that bypasses the policy applied while listing the surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal, Mapping
from urllib.parse import parse_qs, unquote, urlsplit

import mcp.types as types
from pydantic import AnyUrl

from fusion_agent_mcp.profiles import TOOL_PROFILES, resolve_tool_profile


RESOURCE_MIME_TYPE = "application/json"
SurfaceKind = Literal["tool", "resource", "resource_template", "prompt"]
ContentPolicy = Literal["structured_only", "validated_png"]
SurfaceCallable = Callable[..., Any]

_ALL_PROFILES = tuple(TOOL_PROFILES)
_STANDARD_RESOURCE_PROFILES = ("normal", "advanced", "diagnostic", "all")
_NORMAL_PROMPT_PROFILES = ("normal", "advanced", "all")


@dataclass(frozen=True, slots=True)
class SurfaceSpec:
    """One public MCP entry with explicit authorization and data metadata."""

    kind: SurfaceKind
    name: str
    profiles: tuple[str, ...]
    risk: Literal["read", "write", "destructive"]
    data_class: str
    capability_group: str = "orchestration"
    evidence_role: str = "structured"
    content_policy: ContentPolicy = "structured_only"
    description: str = ""
    title: str | None = None
    uri: str | None = None
    uri_template: str | None = None
    resource_family: str | None = None
    resource_path: tuple[str, ...] | None = None
    resource_query_fields: tuple[str, ...] = ()
    prompt_arguments: tuple[types.PromptArgument, ...] = ()
    prompt_workflow: str | None = None
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    annotations: types.ToolAnnotations | None = None
    handler: SurfaceCallable | None = None
    projector: SurfaceCallable | None = None

    def __post_init__(self) -> None:
        if self.content_policy not in {"structured_only", "validated_png"}:
            raise ValueError(
                f"surface {self.name!r} declares an unknown content policy"
            )
        if not self.profiles:
            raise ValueError(f"surface {self.name!r} must declare at least one profile")
        unknown = set(self.profiles) - set(TOOL_PROFILES)
        if unknown:
            raise ValueError(
                f"surface {self.name!r} declares unknown profiles: {sorted(unknown)}"
            )
        if self.kind in {"resource", "resource_template"}:
            if not self.resource_family:
                raise ValueError(
                    f"resource surface {self.name!r} must declare a family"
                )
            if self.resource_path is None:
                raise ValueError(
                    f"resource surface {self.name!r} must declare an exact path"
                )
        if (
            self.input_schema is None
            or self.output_schema is None
            or self.handler is None
            or self.projector is None
        ):
            raise ValueError(
                f"surface {self.name!r} must declare schemas, handler, and projector"
            )
        if self.kind == "prompt" and not self.prompt_workflow:
            raise ValueError(
                f"prompt surface {self.name!r} must declare its projector text"
            )


@dataclass(slots=True)
class SurfaceProfileError(PermissionError):
    """A resource or prompt is unavailable in the fixed server profile."""

    kind: SurfaceKind
    name: str
    profile: str
    available_profiles: tuple[str, ...]
    code: str = "SURFACE_NOT_AVAILABLE_IN_PROFILE"

    def __str__(self) -> str:
        # Deliberately generic: identifiers and configured values never enter
        # the public protocol error emitted by the MCP framework.
        return f"{self.code}: requested {self.kind} is unavailable in this profile"


_RESOURCE_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "uri": {"type": "string", "pattern": r"^fusion-agent://"},
    },
    "required": ["uri"],
    "additionalProperties": False,
}
_RESOURCE_OUTPUT_SCHEMA: dict[str, Any] = {"type": "object"}
_PROMPT_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "description": {"type": ["string", "null"]},
        "messages": {"type": "array"},
    },
    "required": ["messages"],
}


async def _dispatch_declared_resource(
    spec: SurfaceSpec,
    uri: str,
    *,
    runtime: Any,
    profile: str,
    dispatcher: SurfaceCallable,
) -> Any:
    """Invoke only the dispatcher bound to an already-authorized entry."""

    return await dispatcher(spec, uri, runtime=runtime, profile=profile)


def _project_resource_result(spec: SurfaceSpec, payload: Any) -> dict[str, Any]:
    """Keep resource projectors structural and independent of provider text."""

    del spec
    if not isinstance(payload, Mapping):
        raise TypeError("resource handler must return an object")
    return dict(payload)


def _resource_spec(**values: Any) -> SurfaceSpec:
    return SurfaceSpec(
        input_schema=dict(_RESOURCE_INPUT_SCHEMA),
        output_schema=dict(_RESOURCE_OUTPUT_SCHEMA),
        handler=_dispatch_declared_resource,
        projector=_project_resource_result,
        **values,
    )


_RESOURCE_SPECS: tuple[SurfaceSpec, ...] = (
    _resource_spec(
        kind="resource",
        name="fusion-agent-capabilities",
        profiles=_ALL_PROFILES,
        risk="read",
        data_class="public_capability_metadata",
        title="Fusion Agent capabilities",
        uri="fusion-agent://capabilities",
        resource_family="capabilities",
        resource_path=(),
        description="Active tool profile, capability groups and risk metadata.",
    ),
    _resource_spec(
        kind="resource",
        name="fusion-agent-readiness",
        profiles=_ALL_PROFILES,
        risk="read",
        data_class="public_readiness_metadata",
        title="Fusion Agent readiness",
        uri="fusion-agent://readiness",
        resource_family="readiness",
        resource_path=(),
        description="Current local harness and backend readiness snapshot.",
    ),
)


_RESOURCE_TEMPLATE_SPECS: tuple[SurfaceSpec, ...] = (
    _resource_spec(
        kind="resource_template",
        name="fusion-agent-sessions",
        profiles=_STANDARD_RESOURCE_PROFILES,
        risk="read",
        data_class="session_summary",
        uri_template="fusion-agent://sessions/{project}{?offset,limit}",
        resource_family="sessions",
        resource_path=("{project}",),
        resource_query_fields=("offset", "limit"),
        description="Saved session summaries.",
    ),
    _resource_spec(
        kind="resource_template",
        name="fusion-agent-session-artifact",
        profiles=_STANDARD_RESOURCE_PROFILES,
        risk="read",
        data_class="session_artifact",
        uri_template="fusion-agent://sessions/{project}/{session_id}/artifact/{name}{?offset,limit}",
        resource_family="sessions",
        resource_path=("{project}", "{session_id}", "artifact", "{name}"),
        resource_query_fields=("offset", "limit"),
        description="One allowlisted session artifact, returned with character pagination.",
    ),
    _resource_spec(
        kind="resource_template",
        name="fusion-agent-session-trace",
        profiles=_STANDARD_RESOURCE_PROFILES,
        risk="read",
        data_class="session_trace",
        uri_template="fusion-agent://traces/{project}/{session_id}{?offset,limit}",
        resource_family="traces",
        resource_path=("{project}", "{session_id}"),
        resource_query_fields=("offset", "limit"),
        description="Paginated structured session trace events.",
    ),
    _resource_spec(
        kind="resource_template",
        name="fusion-agent-manifest",
        profiles=_STANDARD_RESOURCE_PROFILES,
        risk="read",
        data_class="backend_manifest",
        uri_template="fusion-agent://manifests/{source}{?offset,limit}",
        resource_family="manifests",
        resource_path=("{source}",),
        resource_query_fields=("offset", "limit"),
        description="Latest real or mock backend manifest, returned with character pagination.",
    ),
    _resource_spec(
        kind="resource_template",
        name="fusion-agent-skill",
        profiles=_STANDARD_RESOURCE_PROFILES,
        risk="read",
        data_class="harness_skill",
        uri_template="fusion-agent://skills/{name}{?offset,limit}",
        resource_family="skills",
        resource_path=("{name}",),
        resource_query_fields=("offset", "limit"),
        description="One filesystem-backed harness skill, returned with character pagination.",
    ),
    _resource_spec(
        kind="resource_template",
        name="fusion-agent-project-memory",
        profiles=("advanced", "all"),
        risk="read",
        data_class="untrusted_memory_data",
        uri_template="fusion-agent://memory/{project}{?offset,limit}",
        resource_family="memory",
        resource_path=("{project}",),
        resource_query_fields=("offset", "limit"),
        description="Paginated project/global memory records, treated as untrusted data.",
    ),
    _resource_spec(
        kind="resource_template",
        name="fusion-agent-benchmark-view",
        profiles=("benchmark", "all"),
        risk="read",
        data_class="benchmark_result",
        uri_template="fusion-agent://benchmarks/{run_id}/{view}{?offset,limit}",
        resource_family="benchmarks",
        resource_path=("{run_id}", "{view}"),
        resource_query_fields=("offset", "limit"),
        description="Paginated benchmark report view.",
    ),
)


PROMPT_ARGUMENTS: dict[str, tuple[types.PromptArgument, ...]] = {
    "fusion-inspect-plan-verify": (
        types.PromptArgument(
            name="request",
            description="The CAD task to inspect and plan.",
            required=True,
        ),
        types.PromptArgument(
            name="project", description="Simple project identifier.", required=False
        ),
    ),
    "fusion-safe-change": (
        types.PromptArgument(
            name="request", description="The narrow change to preview.", required=True
        ),
        types.PromptArgument(
            name="project", description="Simple project identifier.", required=False
        ),
    ),
    "fusion-recover-unknown-outcome": (
        types.PromptArgument(
            name="operation_id",
            description="Operation whose outcome is unknown.",
            required=True,
        ),
        types.PromptArgument(
            name="project", description="Simple project identifier.", required=False
        ),
    ),
    "fusion-benchmark-case": (
        types.PromptArgument(
            name="case_id", description="Benchmark case identifier.", required=True
        ),
        types.PromptArgument(name="mode", description="mock or real.", required=False),
    ),
}


PROMPT_DESCRIPTIONS = {
    "fusion-inspect-plan-verify": "Inspect measured state, plan a typed operation, execute only when authorized, then verify contract coverage.",
    "fusion-safe-change": "Prepare a fresh baseline-bound Safe Change preview before any mutation.",
    "fusion-recover-unknown-outcome": "Resolve a post-dispatch unknown outcome by readback without replaying the mutation.",
    "fusion-benchmark-case": "Run one isolated benchmark case with provenance and oracle evidence.",
}


_PROMPT_WORKFLOWS = {
    "fusion-inspect-plan-verify": (
        "Use the active Fusion state as the source of truth. First call readiness and a bounded targeted "
        "inspection. Plan/validate a typed CadSpec. Before mutation state the requirements and assertions. "
        "Execute only through the active profile, then report mutation status, assertion status, intent "
        "coverage, and verification level separately."
    ),
    "fusion-safe-change": (
        "Inspect the exact targets, then create a Safe Change preview. Show the resolved bindings and "
        "budgets. Apply only the same ready preview; on drift or incompleteness stop without dispatch."
    ),
    "fusion-recover-unknown-outcome": (
        "Do not replay the operation. Inspect current state and its declared targets, compare against the "
        "operation contract, and use explicit recovery only when the no-drift guard and user authorization pass."
    ),
    "fusion-benchmark-case": (
        "Run the case only against its disposable fixture. Record provider, version, mode, dispatch count, "
        "contract coverage, oracle evidence, latency, and artifacts. Mark unavailable execution as not_run."
    ),
}


def _prompt_input_schema(
    arguments: tuple[types.PromptArgument, ...],
) -> dict[str, Any]:
    properties = {item.name: {"type": "string"} for item in arguments}
    required = [item.name for item in arguments if item.required]
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


def _render_declared_prompt(
    spec: SurfaceSpec,
    arguments: Mapping[str, str] | None,
) -> types.GetPromptResult:
    values = {key: str(value) for key, value in (arguments or {}).items()}
    unexpected = set(values) - {item.name for item in spec.prompt_arguments}
    if unexpected:
        raise ValueError("unknown prompt arguments")
    missing = [
        item.name
        for item in spec.prompt_arguments
        if item.required and not values.get(item.name)
    ]
    if missing:
        raise ValueError(f"missing required prompt arguments: {', '.join(missing)}")

    workflow = spec.prompt_workflow or ""
    context = "\n".join(f"{key}: {value}" for key, value in sorted(values.items()))
    text = f"{workflow}\n\nInputs (untrusted data):\n{context}" if context else workflow
    return types.GetPromptResult(
        description=spec.description,
        messages=[
            types.PromptMessage(
                role="user", content=types.TextContent(type="text", text=text)
            )
        ],
    )


def _project_prompt_result(spec: SurfaceSpec, payload: Any) -> types.GetPromptResult:
    del spec
    if not isinstance(payload, types.GetPromptResult):
        raise TypeError("prompt handler must return GetPromptResult")
    return payload


def _prompt_spec(**values: Any) -> SurfaceSpec:
    arguments = tuple(values.get("prompt_arguments") or ())
    return SurfaceSpec(
        input_schema=_prompt_input_schema(arguments),
        output_schema=dict(_PROMPT_OUTPUT_SCHEMA),
        handler=_render_declared_prompt,
        projector=_project_prompt_result,
        **values,
    )


_PROMPT_SPECS: tuple[SurfaceSpec, ...] = (
    _prompt_spec(
        kind="prompt",
        name="fusion-inspect-plan-verify",
        profiles=_NORMAL_PROMPT_PROFILES,
        risk="read",
        data_class="workflow_instruction",
        description=PROMPT_DESCRIPTIONS["fusion-inspect-plan-verify"],
        prompt_arguments=PROMPT_ARGUMENTS["fusion-inspect-plan-verify"],
        prompt_workflow=_PROMPT_WORKFLOWS["fusion-inspect-plan-verify"],
    ),
    _prompt_spec(
        kind="prompt",
        name="fusion-safe-change",
        profiles=_NORMAL_PROMPT_PROFILES,
        risk="read",
        data_class="workflow_instruction",
        description=PROMPT_DESCRIPTIONS["fusion-safe-change"],
        prompt_arguments=PROMPT_ARGUMENTS["fusion-safe-change"],
        prompt_workflow=_PROMPT_WORKFLOWS["fusion-safe-change"],
    ),
    _prompt_spec(
        kind="prompt",
        name="fusion-recover-unknown-outcome",
        profiles=("normal", "advanced", "diagnostic", "all"),
        risk="read",
        data_class="workflow_instruction",
        description=PROMPT_DESCRIPTIONS["fusion-recover-unknown-outcome"],
        prompt_arguments=PROMPT_ARGUMENTS["fusion-recover-unknown-outcome"],
        prompt_workflow=_PROMPT_WORKFLOWS["fusion-recover-unknown-outcome"],
    ),
    _prompt_spec(
        kind="prompt",
        name="fusion-benchmark-case",
        profiles=("benchmark", "all"),
        risk="read",
        data_class="benchmark_instruction",
        description=PROMPT_DESCRIPTIONS["fusion-benchmark-case"],
        prompt_arguments=PROMPT_ARGUMENTS["fusion-benchmark-case"],
        prompt_workflow=_PROMPT_WORKFLOWS["fusion-benchmark-case"],
    ),
)


def surface_specs() -> tuple[SurfaceSpec, ...]:
    """Return every non-tool declarative MCP surface entry."""

    return _RESOURCE_SPECS + _RESOURCE_TEMPLATE_SPECS + _PROMPT_SPECS


def resources(profile: str | None = None) -> list[types.Resource]:
    """Return stable top-level resources authorized for ``profile``."""

    resolved = _resolve_surface_profile(profile)
    return [
        types.Resource(
            name=spec.name,
            title=spec.title,
            uri=AnyUrl(spec.uri or ""),
            description=spec.description,
            mimeType=RESOURCE_MIME_TYPE,
        )
        for spec in _RESOURCE_SPECS
        if resolved in spec.profiles
    ]


def resource_templates(profile: str | None = None) -> list[types.ResourceTemplate]:
    """Return only templates authorized for the fixed server profile."""

    resolved = _resolve_surface_profile(profile)
    return [
        types.ResourceTemplate(
            name=spec.name,
            title=(
                spec.title
                or spec.name.replace("fusion-agent-", "").replace("-", " ").title()
            ),
            uriTemplate=spec.uri_template or "",
            description=spec.description,
            mimeType=RESOURCE_MIME_TYPE,
        )
        for spec in _RESOURCE_TEMPLATE_SPECS
        if resolved in spec.profiles
    ]


def authorize_resource(uri: str, profile: str) -> SurfaceSpec:
    """Match and authorize one exact declared URI before handler dispatch."""

    parsed = urlsplit(uri)
    if parsed.scheme != "fusion-agent":
        raise ValueError("resource URI must use the fusion-agent scheme")
    if parsed.fragment:
        raise FileNotFoundError("unknown Fusion Agent resource route")
    segments = tuple(unquote(item) for item in parsed.path.split("/") if item)
    query_fields = frozenset(parse_qs(parsed.query, keep_blank_values=True))
    candidates = tuple(
        spec
        for spec in _RESOURCE_SPECS + _RESOURCE_TEMPLATE_SPECS
        if _resource_route_matches(spec, parsed.netloc, segments, query_fields)
    )
    if not candidates:
        raise FileNotFoundError("unknown Fusion Agent resource route")
    if len(candidates) != 1:
        raise RuntimeError("ambiguous Fusion Agent resource registry")
    candidate = candidates[0]
    resolved = resolve_tool_profile(profile)
    if resolved in candidate.profiles:
        return candidate
    raise SurfaceProfileError(
        kind=candidate.kind,
        name=candidate.name,
        profile=resolved,
        available_profiles=candidate.profiles,
    )


def _resource_route_matches(
    spec: SurfaceSpec,
    family: str,
    segments: tuple[str, ...],
    query_fields: frozenset[str],
) -> bool:
    shape = spec.resource_path
    if spec.resource_family != family or shape is None or len(shape) != len(segments):
        return False
    if not query_fields.issubset(spec.resource_query_fields):
        return False
    for expected, actual in zip(shape, segments, strict=True):
        placeholder = expected.startswith("{") and expected.endswith("}")
        if placeholder:
            if (
                not actual
                or actual in {".", ".."}
                or "\x00" in actual
                or "/" in actual
                or "\\" in actual
            ):
                return False
        elif expected != actual:
            return False
    return True


def prompts(profile: str | None = None) -> list[types.Prompt]:
    """Return prompts authorized for ``profile``."""

    resolved = _resolve_surface_profile(profile)
    return [
        types.Prompt(
            name=spec.name,
            title=spec.name.replace("fusion-", "Fusion ").replace("-", " ").title(),
            description=spec.description,
            arguments=list(spec.prompt_arguments),
        )
        for spec in _PROMPT_SPECS
        if resolved in spec.profiles
    ]


def render_prompt(
    name: str,
    arguments: Mapping[str, str] | None,
    *,
    profile: str | None = None,
) -> types.GetPromptResult:
    """Authorize and render a safe workflow prompt without resource injection."""

    resolved = _resolve_surface_profile(profile)
    spec = next((item for item in _PROMPT_SPECS if item.name == name), None)
    if spec is None:
        raise KeyError("unknown Fusion Agent prompt")
    if resolved not in spec.profiles:
        raise SurfaceProfileError(
            kind="prompt",
            name=name,
            profile=resolved,
            available_profiles=spec.profiles,
        )
    handler = spec.handler
    projector = spec.projector
    if not callable(handler) or not callable(projector):
        raise RuntimeError("prompt registry entry is incomplete")
    payload = handler(spec, arguments)
    projected = projector(spec, payload)
    if not isinstance(projected, types.GetPromptResult):
        raise TypeError("prompt projector must return GetPromptResult")
    return projected


def _resolve_surface_profile(profile: str | None) -> str:
    # Omitted policy is fail-safe: use the configured/default normal profile.
    # Catalog and diagnostics callers that genuinely need everything must ask
    # for ``all`` explicitly.
    return resolve_tool_profile(profile if profile is not None else "normal")
