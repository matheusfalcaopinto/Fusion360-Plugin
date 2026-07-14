# Skill: repair_open_profile

## Status

v1 repair target

## Purpose

Repair or prevent extrusion failures caused by open sketch profiles.

## Inputs

- sketch_name
- expected profile type
- source feature

## Preconditions

- Fusion document is active or mock design state is available.
- Units have been inspected.
- CAD Spec has passed schema validation.
- All dimensions are explicit unit strings or named parameter expressions.

## Tool facade operations

- validate_closed_profiles
- apply_constraint
- draw_constrained_rectangle
- extrude_profile

## Procedure

1. Inspect sketch profile count.
2. If zero, inspect curves/endpoints.
3. Prefer rebuilding with helper operation over manual patching.
4. Validate profile count before extrusion.
5. Retry feature creation.

## Acceptance tests

- profiles.count > 0
- extrude succeeds
- body/feature created

## Common failure modes

- SKETCH_CONSTRAINT_CONFLICT
- GEOMETRY_AMBIGUOUS
- REPAIR_ATTEMPTS_EXCEEDED

## Memory hooks

- On success: record stable recipe if this skill passed verification.
- On failure: classify failure and update project/global memory if reusable.

## Notes for executor

- Do not rely on active selection unless the CAD Spec explicitly requires it.
- Name all created objects.
- Verify after execution before moving to the next transaction.
