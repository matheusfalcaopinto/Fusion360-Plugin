# Skill: prepare_basic_cam_setup_future

## Status

future v3

## Purpose

Prepare a basic CAM setup and generate simple toolpaths if supported locally.

## Inputs

- target component/body
- stock settings
- tool selection
- operation type

## Preconditions

- Fusion document is active or mock design state is available.
- Units have been inspected.
- CAD Spec has passed schema validation.
- All dimensions are explicit unit strings or named parameter expressions.

## Tool facade operations

- inspect_cam_product
- create_basic_setup
- select_tool_from_library
- create_facing_operation
- generate_toolpaths
- check_toolpath_validity

## Procedure

1. Validate model.
2. Inspect CAM availability.
3. Create setup.
4. Select tool.
5. Create simple operation.
6. Generate/check toolpath.
7. Generate setup sheet if supported.

## Acceptance tests

- setup created
- toolpath valid
- setup sheet optional

## Common failure modes

- CAM_NOT_AVAILABLE
- TOOL_LIBRARY_MISSING
- TOOLPATH_GENERATION_FAILED

## Memory hooks

- On success: record stable recipe if this skill passed verification.
- On failure: classify failure and update project/global memory if reusable.

## Notes for executor

- Do not rely on active selection unless the CAD Spec explicitly requires it.
- Name all created objects.
- Verify after execution before moving to the next transaction.
