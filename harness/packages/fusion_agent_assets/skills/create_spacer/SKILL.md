# Skill: create_spacer

## Status

v0 stable target

## Purpose

Create a cylindrical spacer with optional through hole.

## Inputs

- outer_diameter
- inner_diameter optional
- height

## Preconditions

- Fusion document is active or mock design state is available.
- Units have been inspected.
- CAD Spec has passed schema validation.
- All dimensions are explicit unit strings or named parameter expressions.

## Tool facade operations

- create_named_parameter
- create_component
- create_sketch_on_plane
- draw_constrained_circle
- extrude_profile
- cut_profile
- measure_bounding_box

## Procedure

1. Create parameters.
2. Sketch outer circle on XY.
3. Extrude height.
4. If inner diameter specified, sketch/cut through hole.
5. Validate bounding box and hole.

## Acceptance tests

- body_count = 1
- bounding_box = outer_diameter x outer_diameter x height
- inner hole if requested

## Common failure modes

- CIRCLE_PROFILE_FAILED
- CUT_FAILED
- UNIT_MISMATCH

## Memory hooks

- On success: record stable recipe if this skill passed verification.
- On failure: classify failure and update project/global memory if reusable.

## Notes for executor

- Do not rely on active selection unless the CAD Spec explicitly requires it.
- Name all created objects.
- Verify after execution before moving to the next transaction.
