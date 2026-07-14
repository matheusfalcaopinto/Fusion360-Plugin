# Skill: create_l_bracket

## Status

v0 stable target

## Purpose

Create a simple L-shaped bracket with holes and thickness.

## Inputs

- leg_length
- width
- thickness
- hole_diameter
- hole_locations

## Preconditions

- Fusion document is active or mock design state is available.
- Units have been inspected.
- CAD Spec has passed schema validation.
- All dimensions are explicit unit strings or named parameter expressions.

## Tool facade operations

- create_named_parameter
- create_sketch_on_plane
- extrude_profile
- cut_profile
- apply_fillet
- measure_bounding_box

## Procedure

1. Define parameters.
2. Sketch L profile or create two plates combined.
3. Extrude thickness.
4. Add hole sketches/cuts.
5. Fillet/chamfer as requested.
6. Validate geometry.

## Acceptance tests

- one final body unless requested otherwise
- dimensions match
- hole count matches
- parameters exist

## Common failure modes

- PROFILE_AMBIGUOUS
- BOOLEAN_COMBINE_FAILED
- HOLE_PLANE_ERROR
- UNIT_MISMATCH

## Memory hooks

- On success: record stable recipe if this skill passed verification.
- On failure: classify failure and update project/global memory if reusable.

## Notes for executor

- Do not rely on active selection unless the CAD Spec explicitly requires it.
- Name all created objects.
- Verify after execution before moving to the next transaction.
