# Tooling Rules

- Raw MCP tools are not exposed directly to executor.
- Tool discovery manifest must be saved with real/mock latest files separated.
- Mock discovery must not overwrite the latest real manifest.
- Unknown tools are blocked.
- Sensitive operations require confirmation policy.
- Delete defaults to `allow_delete=false`; destructive apply requires preview,
  baseline, `confirm_destructive=true`, and first batch `<=5`.
- Hidden roots in imported/shared-definition assemblies are blocked by default.
- If visible occurrence paths, visible body keys, visible component keys,
  visible-body bbox, or visible counts regress after a batch, abort and do not
  save.
- Each tool call is logged.
- Tool result schemas are validated when available.
- Timeouts are mandatory.
- Programmatic reads are the real-session evidence. Screenshot receipts are
  mock/dry-run compatibility only; real capture/export is `deny_io` in 0.4.1.
- `plan_spec` is only for known CAD creation/modeling, not hub inventory,
  audit, reorg, cleanup, delete, or read-only diagnosis.
