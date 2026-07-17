# Skill: validate_export

## Status

0.4.1 compatibility-only; real host output is fail-closed

## Purpose

Validate an export request without dispatching real host output. In 0.4.1,
real export always returns `HOST_OUTPUT_DISABLED`; mock and dry-run may retain a
confined receipt for compatibility.

## Inputs

- target component/body/design
- export_format
- output_path (validation input only)

## Preconditions

- Mock design state is available, or the caller accepts a fail-closed real
  result without provider dispatch.
- Units have been inspected.
- CAD Spec has passed schema validation.
- All dimensions are explicit unit strings or named parameter expressions.

## Tool facade operations

- validate_feature_health
- validate_named_objects

## Procedure

1. Run final programmatic acceptance tests.
2. Validate the requested path and format without dispatch.
3. In real mode, return `HOST_OUTPUT_DISABLED` before binding, compilation, or
   provider construction.
4. In mock or dry-run only, record a confined compatibility receipt.

## Acceptance tests

- real output denied with zero provider calls
- no output file created in real mode
- mock/dry-run receipt remains confined to the local output root

## Common failure modes

- HOST_OUTPUT_DISABLED
- VALIDATION_NOT_PASSED
- INVALID_OUTPUT_PATH

## Memory hooks

- On mock/dry-run success: record a stable validation recipe if useful.
- On failure: classify failure and update project/global memory if reusable.

## Notes for executor

- Do not rely on active selection unless the CAD Spec explicitly requires it.
- Name all created objects.
- Never treat `export_roots` or `allow_overwrite` as enabling real output in
  0.4.1.
