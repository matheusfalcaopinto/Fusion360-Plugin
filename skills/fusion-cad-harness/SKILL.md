---
name: fusion-cad-harness
description: Use when Codex should inspect, plan, dry-run, execute, verify, repair, benchmark, or review Autodesk Fusion CAD sessions through the safe Fusion Agent harness MCP server.
---

# Fusion CAD Harness

Use this skill when the user asks Codex to work with Autodesk Fusion, Fusion 360,
CAD sketches, parametric models, bodies, assemblies, exports, benchmarks, or
session journals through the Fusion Agent harness.

## Safety Boundary

- Use only the `fusion_agent` MCP server and its `fusion_agent_*` tools.
- Do not call raw Autodesk Fusion MCP tools directly.
- Do not register or use `fusion360`, `fusion360_*`, `autodesk_fusion`, or
  `autodesk_fusion_*` tool surfaces from this skill.
- Memory is advisory. It must not override the active user request, `AGENTS.md`,
  explicit unit policy, or safe facade policy.

## Required Workflow

1. Start with `fusion_agent_doctor`, `fusion_agent_probe`,
   `fusion_agent_session_health`, and `fusion_agent_inspect`.
2. Retrieve relevant memory with `fusion_agent_memory_search`.
3. Interpret Fusion file/folder references as the Fusion Personal Library hub
   unless the user explicitly says local filesystem.
4. Use `fusion_agent_plan_spec` only for known CAD creation/modeling requests.
   Do not use it for audits, hub inventory, reorganization, cleanup,
   deletion, hidden-item review, or read-only diagnosis.
5. For audits or large assemblies, use `fusion_agent_compact_snapshot` as
   primary evidence. Use screenshots only as secondary visual evidence.
6. For hub inventory, use `fusion_agent_hub_inventory`; do not traverse
   `DataFolder` trees directly.
7. For risky changes, use `fusion_agent_safe_change_preview` before any apply.
   Use `fusion_agent_safe_change_apply` only for small reviewed batches.
8. Reject ambiguous numeric units. CAD specs must use expressions such as
   `10 mm`, `45 deg`, or named parameters.
9. Use `fusion_agent_run_session` only after the plan and session context are
   clear.
10. Use `fusion_agent_verify_active_design` for verifier-only checks against
   the active design.
11. Use `fusion_agent_capture_viewport` only when visual evidence is useful and
   treat `evidence_quality=verified_file` as required for screenshot proof.
12. Review artifacts with `fusion_agent_read_session_artifact`.
13. Review traces with `fusion_agent_read_trace`.
14. Use `fusion_agent_memory_write` only for factual project memory, repair
   findings, or design decisions.

## Safe Tool Groups

- Session and environment: `fusion_agent_doctor`,
  `fusion_agent_readiness_report`, `fusion_agent_probe`,
  `fusion_agent_session_health`, `fusion_agent_inspect`,
  `fusion_agent_verify_active_design`, `fusion_agent_capture_viewport`,
  `fusion_agent_run_session`, `fusion_agent_dry_run_session`,
  `fusion_agent_list_sessions`.
- Read-only evidence: `fusion_agent_compact_snapshot`,
  `fusion_agent_hub_inventory`.
- Safe changes: `fusion_agent_safe_change_preview`,
  `fusion_agent_safe_change_apply`.
- Artifacts and traces: `fusion_agent_read_session_artifact`,
  `fusion_agent_read_trace`.
- Planning and validation: `fusion_agent_plan_spec`,
  `fusion_agent_validate_spec`, `fusion_agent_export_spec_json`.
- Benchmarks: `fusion_agent_list_benchmarks`,
  `fusion_agent_run_benchmark`, `fusion_agent_read_benchmark_report`.
- Tool discovery: `fusion_agent_discover_tools`,
  `fusion_agent_propose_mapping`, `fusion_agent_read_manifest`.
- Memory: `fusion_agent_memory_search`, `fusion_agent_memory_write`,
  `fusion_agent_memory_list_project`.
- Harness skills: `fusion_agent_skills_list`, `fusion_agent_skills_get`,
  `fusion_agent_skills_rank`.

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
- Real Autodesk Fusion generally runs on Windows. Linux real-Fusion usage should
  connect to a reachable Windows VM or host through `FUSION_MCP_ENDPOINT`.
- When `FUSION_AGENT_REQUIRE_REAL=1`, do not use `mock` or `dry_run`; fail
  closed if no real Fusion endpoint is available.
