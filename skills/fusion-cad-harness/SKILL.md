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

1. Start with `fusion_agent_doctor` and `fusion_agent_inspect`.
2. Retrieve relevant memory with `fusion_agent_memory_search`.
3. Plan with `fusion_agent_plan_spec` or run `fusion_agent_dry_run_session`
   before real execution.
4. Reject ambiguous numeric units. CAD specs must use expressions such as
   `10 mm`, `45 deg`, or named parameters.
5. Use `fusion_agent_run_session` only after the dry-run or session context is
   clear.
6. Use `fusion_agent_verify_active_design` for verifier-only checks against
   the active design.
7. Use `fusion_agent_capture_viewport` for safe viewport evidence captures.
8. Review artifacts with `fusion_agent_read_session_artifact`.
9. Review traces with `fusion_agent_read_trace`.
10. Use `fusion_agent_memory_write` only for factual project memory, repair
   findings, or design decisions.

## Safe Tool Groups

- Session and environment: `fusion_agent_doctor`, `fusion_agent_probe`,
  `fusion_agent_inspect`, `fusion_agent_verify_active_design`,
  `fusion_agent_capture_viewport`, `fusion_agent_run_session`,
  `fusion_agent_dry_run_session`, `fusion_agent_list_sessions`.
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
- Repair loops must be bounded and must classify failure causes.
- Existing documents require inspection, checkpoint/snapshot policy, and
  confirmation before destructive changes.

## Runtime Notes

- Windows and Linux are supported for Codex and the harness.
- Real Autodesk Fusion generally runs on Windows. Linux real-Fusion usage should
  connect to a reachable Windows VM or host through `FUSION_MCP_ENDPOINT`.
- If no real Fusion endpoint is available, stay in `mock` or `dry_run` mode.
