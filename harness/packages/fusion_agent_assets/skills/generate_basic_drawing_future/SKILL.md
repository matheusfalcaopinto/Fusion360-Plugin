# Skill: generate_basic_drawing_future

## Status

future v2.5

## Purpose

Generate a simple drawing/documentation package from a validated part or assembly.

## Inputs

- design/component
- view set
- dimensions
- output PDF path

## Preconditions

- Fusion document is active or mock design state is available.
- Units have been inspected.
- CAD Spec has passed schema validation.
- All dimensions are explicit unit strings or named parameter expressions.

## Tool facade operations

- create_drawing_from_design
- create_base_view
- create_projected_view
- add_basic_dimensions
- export_drawing_pdf

## Procedure

1. Validate source model.
2. Create drawing.
3. Add base/projected views.
4. Add dimensions/hole callouts.
5. Export PDF.
6. Validate drawing exists.

## Acceptance tests

- drawing created
- views present
- PDF export success

## Common failure modes

- VIEW_LAYOUT_FAILED
- DIMENSION_SELECTION_FAILED
- EXPORT_FAILED

## Memory hooks

- On success: record stable recipe if this skill passed verification.
- On failure: classify failure and update project/global memory if reusable.

## Notes for executor

- Do not rely on active selection unless the CAD Spec explicitly requires it.
- Name all created objects.
- Verify after execution before moving to the next transaction.
