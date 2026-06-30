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

1. Start with `fusion_agent_doctor`, `fusion_agent_capabilities`, and
   `fusion_agent_inspect`.
2. For existing projects, run `fusion_agent_analyze_project`,
   `fusion_agent_spatial_map`, and `fusion_agent_generate_bom` before proposing
   edits.
3. Retrieve relevant memory with `fusion_agent_memory_search`.
4. Plan with `fusion_agent_plan_spec` or run `fusion_agent_dry_run_session`
   before real execution.
5. Reject ambiguous numeric units. CAD specs must use expressions such as
   `10 mm`, `45 deg`, or named parameters.
6. Use `fusion_agent_run_session` in `mode=real` only with a matching
   `dry_run_session_id` and `allow_existing_document_write=true`.
7. Use `fusion_agent_preview_modification` before tools that move, import,
   delete, organize, repair, materialize, draw, or otherwise modify a design.
8. Use `fusion_agent_apply_controlled_modification` only when the preview is
   acceptable and `allow_apply=true` is explicit.
9. Use `fusion_agent_run_sandbox_session` for real write validation in a
   disposable scratch document that is closed without saving.
10. Use `fusion_agent_verify_active_design` for verifier-only checks against
   the active design.
11. Use `fusion_agent_capture_viewport`, `fusion_agent_create_exploded_view`,
   `fusion_agent_generate_design_review`, and
   `fusion_agent_generate_project_report` for evidence and deliverables.
12. Review artifacts with `fusion_agent_read_session_artifact`.
13. Review traces with `fusion_agent_read_trace`.
14. Use `fusion_agent_memory_write` only for factual project memory, repair
   findings, or design decisions.

## Safe Tool Groups

- Session and environment: `fusion_agent_doctor`,
  `fusion_agent_capabilities`, `fusion_agent_self_test`,
  `fusion_agent_probe`, `fusion_agent_inspect`,
  `fusion_agent_verify_active_design`, `fusion_agent_capture_viewport`,
  `fusion_agent_run_session`, `fusion_agent_dry_run_session`,
  `fusion_agent_run_sandbox_session`, `fusion_agent_list_sessions`.
- Read-only geometry: `fusion_agent_extract_geometry`.
- Project companion: `fusion_agent_analyze_project`,
  `fusion_agent_explain_assembly`, `fusion_agent_spatial_map`,
  `fusion_agent_find_root_bodies`, `fusion_agent_find_loose_components`,
  `fusion_agent_find_unused_parts`, `fusion_agent_find_alignment_issues`,
  `fusion_agent_find_interferences`, `fusion_agent_measure_clearances`,
  `fusion_agent_motion_envelope_check`.
- Native Fusion document/read wrappers: `fusion_agent_list_projects`,
  `fusion_agent_search_documents`, `fusion_agent_list_open_documents`,
  `fusion_agent_list_recent_documents`, `fusion_agent_open_document`,
  `fusion_agent_save_document`, `fusion_agent_close_document`,
  `fusion_agent_close_without_save`, `fusion_agent_search_fusion_api_docs`.
- Controlled modification: `fusion_agent_preview_modification`,
  `fusion_agent_apply_controlled_modification`,
  `fusion_agent_compare_before_after`, `fusion_agent_create_checkpoint`,
  `fusion_agent_undo`, `fusion_agent_redo`,
  `fusion_agent_execute_approved_script`.
- Components and assembly: `fusion_agent_find_library_components`,
  `fusion_agent_insert_existing_component`,
  `fusion_agent_generate_standard_component`, `fusion_agent_place_component`,
  `fusion_agent_move_component`, `fusion_agent_align_component`,
  `fusion_agent_pattern_component`, `fusion_agent_create_rigid_group`,
  `fusion_agent_create_joint`, `fusion_agent_set_joint_limits`,
  `fusion_agent_add_fasteners_to_holes`,
  `fusion_agent_organize_component_tree`,
  `fusion_agent_delete_unused_parts`.
- Materials, sketches, drawings, reports: `fusion_agent_list_materials`,
  `fusion_agent_list_appearances`, `fusion_agent_apply_material`,
  `fusion_agent_apply_appearance`, `fusion_agent_set_part_metadata`,
  `fusion_agent_analyze_sketches`, `fusion_agent_repair_sketch`,
  `fusion_agent_constrain_sketch`, `fusion_agent_create_parametric_part`,
  `fusion_agent_modify_parametric_feature`,
  `fusion_agent_create_adapter_part`, `fusion_agent_create_part_drawing`,
  `fusion_agent_create_assembly_drawing`,
  `fusion_agent_create_exploded_view`, `fusion_agent_export_pdf`,
  `fusion_agent_export_dxf`, `fusion_agent_generate_bom`,
  `fusion_agent_generate_design_review`,
  `fusion_agent_generate_project_report`.
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
- Every MCP response includes `schema_version`, `tool`, `ok`, and `artifacts`.
  Read those fields before assuming a session result is safe to use.
