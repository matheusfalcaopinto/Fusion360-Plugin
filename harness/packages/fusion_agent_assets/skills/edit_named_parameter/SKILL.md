# Skill: edit_named_parameter

## Status

v0 stable target

## Purpose

Safely update an existing named parameter and verify downstream geometry.

## Inputs

- parameter_name
- new_expression
- expected affected geometry

## Preconditions

- Fusion document is active or mock design state is available.
- Units have been inspected.
- CAD Spec has passed schema validation.
- All dimensions are explicit unit strings or named parameter expressions.

## Tool facade operations

- list_parameters
- update_named_parameter
- validate_feature_health
- measure_bounding_box

## Procedure

1. Inspect parameter existence.
2. Update expression with explicit unit or expression.
3. Recompute/refresh document if needed.
4. Validate affected features and dimensions.
5. Record decision in project memory.

## Acceptance tests

- parameter updated
- feature health valid
- expected geometry changed
- no unexpected body count changes

## Common failure modes

- PARAMETER_NOT_FOUND
- EXPRESSION_INVALID
- FEATURE_REGEN_FAILED

## Memory hooks

- On success: record stable recipe if this skill passed verification.
- On failure: classify failure and update project/global memory if reusable.

## Notes for executor

- Do not rely on active selection unless the CAD Spec explicitly requires it.
- Name all created objects.
- Verify after execution before moving to the next transaction.
