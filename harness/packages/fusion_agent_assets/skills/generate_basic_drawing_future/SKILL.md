# Skill: generate_basic_drawing_future

## Status

future v2.5; not executable in 0.4.1

## Purpose

Generate a simple drawing/documentation package from a validated part or assembly.
This design note is not an advertised 0.4.1 capability. Real PDF export remains
`deny_io` and must not be dispatched by the 0.4.1 runtime.

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

- none in 0.4.1

## Procedure

1. Validate the request as a future capability proposal.
2. Return an unsupported/fail-closed result before provider dispatch.
3. Do not create a drawing or export a PDF in 0.4.1.

## Acceptance tests

- zero provider calls
- no output file created
- future capability reported explicitly

## Common failure modes

- CAPABILITY_NOT_AVAILABLE
- HOST_OUTPUT_DISABLED

## Memory hooks

- On success: record stable recipe if this skill passed verification.
- On failure: classify failure and update project/global memory if reusable.

## Notes for executor

- Do not rely on active selection unless the CAD Spec explicitly requires it.
- Name all created objects.
- Verify after execution before moving to the next transaction.
