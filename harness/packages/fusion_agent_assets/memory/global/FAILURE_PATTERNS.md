# Global Failure Patterns

## UNIT_MISMATCH

Symptom: bounding box is off by factor like 10, 2.54 or 25.4.

Detection: compare measured dimensions to expected dimensions.

Repair: inspect parameter expressions and replace ambiguous values with explicit unit strings.

## OPEN_PROFILE

Symptom: extrude cannot find profile or sketch profile count is zero.

Detection: validate closed profiles before extrusion.

Repair: rebuild sketch using constrained helper operation or apply coincident constraints.

## WRONG_ACTIVE_COMPONENT

Symptom: body/feature created under unexpected component.

Detection: compare created object's parent component to target component.

Repair: activate target component or move object only if safe.

## PLANNER_INTENT_DRIFT

Symptom: an audit, hub inventory, reorganization, read-only, or cleanup prompt
produces a generic CAD spec such as a plate, cube, box, or parameter edit.

Detection: prompt contains audit/reorg/delete/hub/read-only terms but
`plan_spec` returns a modeling intent.

Repair: return `unsupported_for_planner` and route to health, snapshot,
hub inventory, or safe-change preview tools.

## MOCK_MANIFEST_OVERWRITE

Symptom: a mock discovery causes later real sessions to believe the real MCP
tool surface is missing or stale.

Detection: latest manifest source is `mock` while real mode is requested.

Repair: keep `fusion_mcp_tools_latest_real.json` and
`fusion_mcp_tools_latest_mock.json` separate.

## SCREENSHOT_NOT_PROOF

Symptom: a screenshot path is returned but the file is missing, empty, or only
mentioned in the Fusion journal.

Detection: a real session requested capture/export, or a mock/dry-run receipt
does not reference an existing non-empty local file.

Repair: real mode returns `HOST_OUTPUT_DISABLED` with zero dispatch. In
mock/dry-run, fail the receipt with `evidence_quality=failed` or `empty_file`,
then use programmatic snapshot evidence.

## VISIBLE_REGRESSION_AFTER_BATCH

Symptom: a cleanup/delete/reorg batch reduces visible occurrence paths, visible
body keys, visible component keys, visible-body bbox, or visible counts.

Detection: compare compact snapshots before and after the batch.

Repair: abort, do not save, use Fusion Undo for the last batch, and rerun a
compact snapshot before continuing.
