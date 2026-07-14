# Skill: create_parametric_plate

## Status

v0 stable target

## Purpose

Create a named rectangular parametric plate from length, width and thickness.

## Inputs

- component_name
- plate_length
- plate_width
- plate_thickness
- origin convention

## Preconditions

- Fusion document is active or mock design state is available.
- Units have been inspected.
- CAD Spec has passed schema validation.
- All dimensions are explicit unit strings or named parameter expressions.

## Tool facade operations

- create_named_parameter
- create_component
- create_sketch_on_plane
- draw_constrained_rectangle
- extrude_profile
- measure_bounding_box

## Procedure

1. Create or update named parameters.
2. Create target component.
3. Create XY sketch named `base_profile_sketch`.
4. Draw center rectangle controlled by length/width.
5. Validate closed profile.
6. Extrude by thickness as new body.
7. Name body and feature.
8. Measure bounding box.

## Acceptance tests

- body_count = 1
- bounding_box matches length/width/thickness
- required parameters exist
- object names are non-default

## Common failure modes

- UNIT_MISMATCH
- OPEN_PROFILE
- WRONG_ACTIVE_COMPONENT
- NAME_COLLISION

## Memory hooks

- On success: record stable recipe if this skill passed verification.
- On failure: classify failure and update project/global memory if reusable.

## Notes for executor

- Do not rely on active selection unless the CAD Spec explicitly requires it.
- Name all created objects.
- Verify after execution before moving to the next transaction.
