# Skill: create_box_enclosure

## Status

v0 stable target

## Purpose

Create a simple open or lidded box/enclosure with parametric wall thickness.

## Inputs

- length
- width
- height
- wall_thickness
- lid optional

## Preconditions

- Fusion document is active or mock design state is available.
- Units have been inspected.
- CAD Spec has passed schema validation.
- All dimensions are explicit unit strings or named parameter expressions.

## Tool facade operations

- create_named_parameter
- create_component
- create_sketch_on_plane
- extrude_profile
- shell or cut_profile
- apply_fillet
- measure_bounding_box

## Procedure

1. Create parameters.
2. Create outer rectangular body.
3. Use shell/cut workflow for wall thickness.
4. Optional: create lid as separate component/body.
5. Apply optional fillets.
6. Validate dimensions and body count.

## Acceptance tests

- outer bounding box matches
- wall thickness parameter exists
- open top or lid state matches request
- named objects

## Common failure modes

- SHELL_FAILED
- UNIT_MISMATCH
- BODY_COUNT_MISMATCH
- THIN_WALL_INVALID

## Memory hooks

- On success: record stable recipe if this skill passed verification.
- On failure: classify failure and update project/global memory if reusable.

## Notes for executor

- Do not rely on active selection unless the CAD Spec explicitly requires it.
- Name all created objects.
- Verify after execution before moving to the next transaction.
