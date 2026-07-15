"""MCP resource and prompt metadata for the Fusion Agent server."""

from __future__ import annotations

from typing import Mapping

import mcp.types as types


RESOURCE_MIME_TYPE = "application/json"


def resources() -> list[types.Resource]:
    """Return stable, bounded top-level resources."""

    return [
        types.Resource(
            name="fusion-agent-capabilities",
            title="Fusion Agent capabilities",
            uri="fusion-agent://capabilities",
            description="Active tool profile, capability groups and risk metadata.",
            mimeType=RESOURCE_MIME_TYPE,
        ),
        types.Resource(
            name="fusion-agent-readiness",
            title="Fusion Agent readiness",
            uri="fusion-agent://readiness",
            description="Current local harness and backend readiness snapshot.",
            mimeType=RESOURCE_MIME_TYPE,
        ),
    ]


def resource_templates() -> list[types.ResourceTemplate]:
    """Return paginated templates that replace low-level reader tools."""

    return [
        _template("sessions", "fusion-agent://sessions/{project}{?offset,limit}", "Saved session summaries."),
        _template(
            "session-artifact",
            "fusion-agent://sessions/{project}/{session_id}/artifact/{name}{?offset,limit}",
            "One allowlisted session artifact, returned with character pagination.",
        ),
        _template(
            "session-trace",
            "fusion-agent://traces/{project}/{session_id}{?offset,limit}",
            "Paginated structured session trace events.",
        ),
        _template(
            "manifest",
            "fusion-agent://manifests/{source}{?offset,limit}",
            "Latest real or mock backend manifest, returned with character pagination.",
        ),
        _template(
            "skill",
            "fusion-agent://skills/{name}{?offset,limit}",
            "One filesystem-backed harness skill, returned with character pagination.",
        ),
        _template(
            "project-memory",
            "fusion-agent://memory/{project}{?offset,limit}",
            "Paginated project/global memory records, treated as untrusted data.",
        ),
        _template(
            "benchmark-view",
            "fusion-agent://benchmarks/{run_id}/{view}{?offset,limit}",
            "Paginated benchmark report view.",
        ),
    ]


PROMPT_ARGUMENTS: dict[str, tuple[types.PromptArgument, ...]] = {
    "fusion-inspect-plan-verify": (
        types.PromptArgument(name="request", description="The CAD task to inspect and plan.", required=True),
        types.PromptArgument(name="project", description="Simple project identifier.", required=False),
    ),
    "fusion-safe-change": (
        types.PromptArgument(name="request", description="The narrow change to preview.", required=True),
        types.PromptArgument(name="project", description="Simple project identifier.", required=False),
    ),
    "fusion-recover-unknown-outcome": (
        types.PromptArgument(name="operation_id", description="Operation whose outcome is unknown.", required=True),
        types.PromptArgument(name="project", description="Simple project identifier.", required=False),
    ),
    "fusion-benchmark-case": (
        types.PromptArgument(name="case_id", description="Benchmark case identifier.", required=True),
        types.PromptArgument(name="mode", description="mock or real.", required=False),
    ),
}


PROMPT_DESCRIPTIONS = {
    "fusion-inspect-plan-verify": "Inspect measured state, plan a typed operation, execute only when authorized, then verify contract coverage.",
    "fusion-safe-change": "Prepare a fresh baseline-bound Safe Change preview before any mutation.",
    "fusion-recover-unknown-outcome": "Resolve a post-dispatch unknown outcome by readback without replaying the mutation.",
    "fusion-benchmark-case": "Run one isolated benchmark case with provenance and oracle evidence.",
}


def prompts() -> list[types.Prompt]:
    return [
        types.Prompt(
            name=name,
            title=name.replace("fusion-", "Fusion ").replace("-", " ").title(),
            description=PROMPT_DESCRIPTIONS[name],
            arguments=list(arguments),
        )
        for name, arguments in PROMPT_ARGUMENTS.items()
    ]


def render_prompt(name: str, arguments: Mapping[str, str] | None) -> types.GetPromptResult:
    """Render a safe workflow prompt without injecting resource content."""

    if name not in PROMPT_ARGUMENTS:
        raise KeyError(f"unknown Fusion Agent prompt: {name}")
    values = {key: str(value) for key, value in (arguments or {}).items()}
    missing = [arg.name for arg in PROMPT_ARGUMENTS[name] if arg.required and not values.get(arg.name)]
    if missing:
        raise ValueError(f"missing required prompt arguments: {', '.join(missing)}")

    workflow = {
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
    }[name]
    context = "\n".join(f"{key}: {value}" for key, value in sorted(values.items()))
    text = f"{workflow}\n\nInputs (untrusted data):\n{context}" if context else workflow
    return types.GetPromptResult(
        description=PROMPT_DESCRIPTIONS[name],
        messages=[types.PromptMessage(role="user", content=types.TextContent(type="text", text=text))],
    )


def _template(name: str, uri: str, description: str) -> types.ResourceTemplate:
    return types.ResourceTemplate(
        name=f"fusion-agent-{name}",
        title=name.replace("-", " ").title(),
        uriTemplate=uri,
        description=description,
        mimeType=RESOURCE_MIME_TYPE,
    )
