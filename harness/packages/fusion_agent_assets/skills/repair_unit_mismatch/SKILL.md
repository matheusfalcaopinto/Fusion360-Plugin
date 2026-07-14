# Skill: repair_unit_mismatch

## Status

v1 repair target

## Purpose

Detect and repair likely mm/cm/in unit mistakes.

## Inputs

- measured bounding box
- expected bounding box
- parameter list

## Preconditions

- Fusion document is active or mock design state is available.
- Units have been inspected.
- CAD Spec has passed schema validation.
- All dimensions are explicit unit strings or named parameter expressions.

## Tool facade operations

- inspect_active_units
- list_parameters
- update_named_parameter
- measure_bounding_box

## Procedure

1. Compare measured vs expected dimensions.
2. Detect ratio patterns such as 10x or 25.4x.
3. Inspect parameter expressions.
4. Replace ambiguous values with explicit unit strings.
5. Recompute and remeasure.

## Acceptance tests

- bounding_box within tolerance
- no raw numeric dimension remains in CAD Spec
- memory written if recurring

## Common failure modes

- PARAMETER_EXPRESSION_INVALID
- DOCUMENT_UNITS_UNEXPECTED
- REPAIR_ATTEMPTS_EXCEEDED

## Memory hooks

- On success: record stable recipe if this skill passed verification.
- On failure: classify failure and update project/global memory if reusable.

## Notes for executor

- Do not rely on active selection unless the CAD Spec explicitly requires it.
- Name all created objects.
- Verify after execution before moving to the next transaction.
