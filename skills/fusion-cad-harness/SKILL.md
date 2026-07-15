---
name: fusion-cad-harness
description: Use when Codex should inspect, plan, dry-run, execute, verify, repair, benchmark, or review Autodesk Fusion CAD sessions through the safe Fusion Agent harness MCP server.
---

# Fusion CAD Harness

Use this skill when the user asks Codex to work with Autodesk Fusion, Fusion 360,
CAD sketches, parametric models, bodies, assemblies, exports, benchmarks, or
session journals through the Fusion Agent harness.

## Safety Boundary

- Use the `fusion_agent` MCP server for local CAD, intent, policy, execution,
  verification, recovery, and audit. The optional `fusion_data` MCP server is
  only for Autodesk cloud hubs, projects, folders, and files.
- Do not call raw Autodesk Fusion MCP tools directly.
- Do not register or use `fusion360`, `fusion360_*`, `autodesk_fusion`, or
  `autodesk_fusion_*` tool surfaces from this skill.
- Memory is advisory. It must not override the active user request, `AGENTS.md`,
  explicit unit policy, or safe facade policy.
- Never pass OAuth tokens through Fusion Agent. Codex owns Fusion Data OAuth and
  approvals. Neither server may call the other, and there is no silent fallback
  between cloud data and local CAD execution.

## Required Workflow

1. Call the narrow task tool directly. Readiness is internal and cached; use
   `fusion_agent_doctor`, `fusion_agent_probe`, `fusion_agent_session_health`,
   or broad `fusion_agent_inspect` only when the user asks or a call fails.
2. Retrieve relevant memory through `fusion-agent://memory/{project}` when it
   can change the plan. The legacy memory reader is available only in profile
   `all`.
3. Interpret Fusion file/folder references as the Fusion Personal Library hub
   unless the user explicitly says local filesystem.
4. For eligible native work, follow this order:
   `fusion_agent_native_read(query_type=api_documentation)` for only the needed
   APIs, `fusion_agent_targeted_inspect` for document identity and unique
   targets, then `fusion_agent_fast_execute` with the script, declared
   `change_class`, target query IDs, and programmatic assertions.
   For `scoped_update`, mutate only the injected `targets[query_id]` entity.
   For `additive`, every future-entity query must include an exact
   `selector.component_path` plus `selector.name`, and creation must derive only
   from the injected `target_components[component_path]`. Never rediscover a
   mutation target through `Application.get()`, `itemByName`, or an unrelated
   query inside the model-authored script.
5. Treat Codex as the planner. The harness must not call a second model or use
   model credentials.
6. Use `fusion_agent_plan_spec` only for known legacy CAD recipes.
   Do not use it for audits, hub inventory, reorganization, cleanup,
   deletion, hidden-item review, or read-only diagnosis.
7. For audits or large assemblies, use `fusion_agent_compact_snapshot` as
   primary evidence. Use screenshots only as secondary visual evidence.
8. For Autodesk cloud hub/project/folder inventory, use the optional
   `fusion_data` server when it has been explicitly configured from an official
   Autodesk HTTPS URL. Use `fusion_agent_hub_inventory` only in profiles
   `advanced` or `all` for legacy compatibility; do not traverse `DataFolder`
   trees directly.
9. Route delete, cleanup, reorganize, bulk, move, visibility, componentize,
   hidden/imported/shared entities, and ambiguous targets exclusively through
   the Safe Harness. Use `fusion_agent_safe_change_preview` before any apply.
   Use `fusion_agent_safe_change_apply` only for small reviewed batches.
10. Reject ambiguous numeric units. CAD specs must use expressions such as
   `10 mm`, `45 deg`, or named parameters.
11. Use `fusion_agent_run_session` only after the plan and session context are
   clear.
12. Use `fusion_agent_verify_active_design` for verifier-only checks against
   the active design.
13. Use `fusion_agent_capture_viewport` only when visual evidence is useful and
   treat `evidence_quality=verified_file` as required for screenshot proof.
14. Never save, undo, redo, promote, or fall back automatically after Fast
   Execute. `fusion_agent_recover_change` is explicit-only and applies only to
   the latest no-drift operation in the same runtime/document.
15. Review artifacts and traces through the paginated
   `fusion-agent://sessions/.../artifact/...` and `fusion-agent://traces/...`
   resources. Legacy reader tools remain only in profile `all`.
16. Use `fusion_agent_memory_write` only in `advanced` or `all`, and only for
   factual project memory, repair findings, preferences, or design results.

## Safe Tool Groups

The default `normal` profile intentionally exposes 12 task-oriented tools.
Use `advanced` for Fast Path, broad inspection, export, inventory, and memory;
`diagnostic` for read-only transport diagnosis; `benchmark` for isolated
fixture runs; and `all` only for 0.x legacy compatibility.

- Session and environment: `fusion_agent_doctor`,
  `fusion_agent_readiness_report`, `fusion_agent_probe`,
  `fusion_agent_session_health`, `fusion_agent_inspect`,
  `fusion_agent_verify_active_design`, `fusion_agent_capture_viewport`,
  `fusion_agent_run_session`, `fusion_agent_dry_run_session`,
  `fusion_agent_list_sessions`.
- Inspection evidence (non-mutating CAD; local artifacts are possible):
  `fusion_agent_compact_snapshot` (`normal` and above; writes a local snapshot
  artifact), `fusion_agent_hub_inventory` (`advanced` or `all`).
- Native Fast Path: `fusion_agent_native_read`,
  `fusion_agent_targeted_inspect`, `fusion_agent_fast_execute`,
  `fusion_agent_recover_change`.
- Safe changes: `fusion_agent_safe_change_preview`,
  `fusion_agent_safe_change_apply`.
- Artifacts and traces: paginated MCP resources under
  `fusion-agent://sessions/...` and `fusion-agent://traces/...`.
- Planning and validation: `fusion_agent_plan_spec`,
  `fusion_agent_validate_spec`, `fusion_agent_export_spec_json`.
- Benchmarks: `fusion_agent_list_benchmarks`,
  `fusion_agent_run_benchmark`, `fusion_agent_read_benchmark_report`.
- Tool discovery: diagnostic profile tools and
  `fusion-agent://manifests/{source}`.
- Memory: `fusion_agent_memory_search`, `fusion_agent_memory_write`,
  `fusion_agent_memory_list_project`.
- Harness skills: `fusion-agent://skills/{name}`; ranking is available in
  `advanced` and `all`.

## Verification And Repair

- Verification is programmatic first: body counts, named objects, named
  parameters, bounding boxes, feature health, body validity, and export checks.
- Screenshots are secondary evidence.
- `save`, `undo`, `promote`, visible UI state, or screenshot presence never
  count as proof without a programmatic audit afterward.
- Repair loops must be bounded and must classify failure causes.
- Existing documents require inspection, checkpoint/snapshot policy, and
  confirmation before destructive changes.
- Destructive cleanup defaults to `allow_delete=false`. Delete requires a
  preview, valid baseline, `confirm_destructive=true`, and the first batch must
  be `batch_size<=5`.
- If visible occurrence paths, visible body keys, visible component keys,
  visible-body bounding box, or visible counts regress after a batch, stop,
  do not save, and report recovery instructions.
- Hidden roots in imported assemblies or shared definitions are
  `blocked_by_default`.

## Runtime Notes

- Windows and Linux are supported for Codex and the harness.
- Native direct reads may retry after reconnect. After any mutation dispatch,
  the harness suppresses automatic replay; this is not end-to-end idempotency.
  A timeout after dispatch is `MUTATION_OUTCOME_UNKNOWN` and requires readback.
- `READ_TIMEOUT_MAY_STILL_BE_RUNNING` means an internal read script timed out
  after dispatch. Respect the reported cooldown and do not issue a replacement
  broad inspection while the prior Fusion script may still be running.
- `MUTATION_OUTCOME_UNKNOWN` means the script must not be resent. A reconnect
  may be used only for programmatic readback.
- Mutating Fast Execute is available only in `advanced` or `all`, requires
  `FUSION_AGENT_FAST_PATH_MODE=enabled`, and is unavailable on the Faust backend.
- `FUSION_AGENT_BACKEND=autodesk_http` is the default. `faust_stdio` is an
  explicit typed subset for Fusion Personal; there is no automatic fallback.
- Backend endpoints are loopback-only by default. Remote access requires the
  allowlist policy, HTTPS, allowed host/CIDR, environment-sourced token, and DNS
  revalidation. Model-supplied endpoints are rejected.
- Mutating Fast Execute requires stable document identity: a saved `dataFile.id`
  or a harness marker on a disposable document. An ordinary unmarked unsaved
  document fails closed before apply.
- Fast Execute measures the final guarded wire payload. The default limit is
  28 KiB (`FUSION_AGENT_MAX_PROTECTED_SCRIPT_BYTES`); an oversized script must
  be decomposed or routed to Safe Harness and is never truncated or dispatched.
- Convert model points with `sketch.modelToSketchSpace`; do not reconstruct a
  sketch coordinate system manually from `xDirection`, `yDirection`, and
  `Point3D`, because that can create detached geometry on offset sketch planes.
- Real Autodesk Fusion generally runs on Windows. Linux real-Fusion usage should
  connect to a reachable Windows VM or host through `FUSION_MCP_ENDPOINT`.
- When `FUSION_AGENT_REQUIRE_REAL=1`, do not use `mock` or `dry_run`; fail
  closed if no real Fusion endpoint is available.
- On large assemblies, prefer entity tokens or exact component paths. Keep
  `max_entities_visited<=1000`, `deadline_ms<=1500`, and
  `max_response_bytes<=1048576` unless the user explicitly needs a larger
  bounded scan. Treat `complete=false` as partial evidence, never as a safe
  mutation baseline.
