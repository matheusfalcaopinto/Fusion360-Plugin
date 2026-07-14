# Skill: create_hole_pattern

## Status

v0 stable target

## Purpose

Create a parametric hole pattern, usually four holes near corners of a plate.

## Inputs

- target_component
- target_body
- hole_diameter
- hole_positions or offset rules
- cut_depth

## Preconditions

- Fusion document is active or mock design state is available.
- Units have been inspected.
- CAD Spec has passed schema validation.
- All dimensions are explicit unit strings or named parameter expressions.

## Tool facade operations

- create_sketch_on_plane
- draw_constrained_circle
- apply_dimension
- cut_profile
- create_linear_pattern or circular pattern
- measure/validate hole count

## Procedure

1. Confirm target body and face/plane.
2. Create named hole sketch.
3. Add constrained circles using explicit offsets.
4. Validate circle profiles.
5. Cut through all or specified depth.
6. Name cut feature.
7. Validate expected hole count.

## Acceptance tests

- hole_count expected
- body_count unchanged unless specified
- bounding_box unchanged except through-cuts
- feature health valid

## Common failure modes

- MISSING_PROFILE
- CUT_FAILED
- WRONG_TARGET_BODY
- HOLE_COUNT_MISMATCH

## Memory hooks

- On success: record stable recipe if this skill passed verification.
- On failure: classify failure and update project/global memory if reusable.

## Notes for executor

- Do not rely on active selection unless the CAD Spec explicitly requires it.
- Name all created objects.
- Verify after execution before moving to the next transaction.
