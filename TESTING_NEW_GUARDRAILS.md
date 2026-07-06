# Testing New Fusion Guardrails

Use a new Codex thread after installing this plugin version so the updated
skill and MCP tool surface are loaded.

## Installed Version

Expected plugin cachebuster:

```text
0.1.0+codex.20260706164939
```

Expected public MCP tool count:

```text
31
```

## Readiness Smoke

1. Run `fusion_agent_readiness_report`.
2. Run `fusion_agent_session_health` with `mode=real`.
3. Run `fusion_agent_probe`.
4. Run `fusion_agent_inspect` only after health/probe results are clear.

Acceptance:

- `doctor` reports Python path, launcher path, endpoint, dry-run policy and
  real/mock manifest status.
- `session_health` distinguishes launcher, MCP server, endpoint, manifest and
  native tool attachment states.

## Planner Guard

Prompt examples that must return `unsupported_for_planner`:

```text
Audite o hub sem modificar nada.
Reorganize a Personal Library em levas.
Delete hidden imported roots.
Inspect read-only before cleanup.
```

Acceptance:

- No generic box, plate, cube or parameter-edit CAD spec is returned.
- The response recommends health/snapshot/hub/safe-change tools.

## Programmatic Evidence

Run `fusion_agent_compact_snapshot` on a known active design:

```json
{
  "mode": "real",
  "project": "guardrails_real_smoke",
  "max_occurrences": 500,
  "max_bodies": 500,
  "include_transforms": false
}
```

Acceptance:

- Snapshot includes visible occurrence paths, visible body keys, visible
  component keys, visible-body bbox and duplicate-name warnings.
- Payload caps are reported when limits are reached.

## Hub Inventory

Run `fusion_agent_hub_inventory`:

```json
{
  "mode": "real",
  "query": "",
  "max_results": 50,
  "enrich": true
}
```

Acceptance:

- Result reports metadata-search/findFileById strategy.
- No direct recursive `DataFolder` traversal is used.

## Safe Change Preview

Run a delete preview against a known hidden/imported/shared-definition target:

```json
{
  "mode": "real",
  "project": "guardrails_real_smoke",
  "operation": "delete",
  "targets": [
    {"kind": "occurrence", "path": "example/hidden/root"}
  ],
  "policy": {"allow_delete": false}
}
```

Acceptance:

- Preview is `blocked_by_default`.
- Baseline fields include `baseline_id` and `before_snapshot_path`.

## Safe Change Apply

Use only a disposable scratch model.

1. Create a preview for a scoped reversible visibility change.
2. Apply with `batch_size<=5`, `save_after=false`.
3. Confirm `negative_impact=false`.

For destructive delete:

- Use only scoped body/occurrence targets.
- Require `confirm_destructive=true`.
- Keep the first batch `batch_size<=5`.
- If visible paths/body keys/component keys/counts/bbox regress, do not save;
  use Fusion Undo and rerun `fusion_agent_compact_snapshot`.
