# Skill: validate_export

## Status

v0 stable target

## Purpose

Export validated model after acceptance tests pass.

## Inputs

- target component/body/design
- export_format
- output_path

## Preconditions

- Fusion document is active or mock design state is available.
- Units have been inspected.
- CAD Spec has passed schema validation.
- All dimensions are explicit unit strings or named parameter expressions.

## Tool facade operations

- validate_feature_health
- export_step
- export_stl
- validate_export

## Procedure

1. Run final acceptance tests.
2. Reject export if validation fails unless user overrides.
3. Export to output path.
4. Validate file exists and non-empty.
5. Record export in session journal.

## Acceptance tests

- validation passed before export
- file exists
- file size > 0
- export path recorded

## Common failure modes

- EXPORT_FAILED
- PERMISSION_ERROR
- VALIDATION_NOT_PASSED

## Memory hooks

- On success: record stable recipe if this skill passed verification.
- On failure: classify failure and update project/global memory if reusable.

## Notes for executor

- Do not rely on active selection unless the CAD Spec explicitly requires it.
- Name all created objects.
- Verify after execution before moving to the next transaction.
