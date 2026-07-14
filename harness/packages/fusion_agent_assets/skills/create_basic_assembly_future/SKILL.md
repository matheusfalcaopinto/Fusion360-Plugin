# Skill: create_basic_assembly_future

## Status

future v2

## Purpose

Create a simple component assembly using occurrences and basic joints.

## Inputs

- components
- occurrences
- transforms
- joint definitions

## Preconditions

- Fusion document is active or mock design state is available.
- Units have been inspected.
- CAD Spec has passed schema validation.
- All dimensions are explicit unit strings or named parameter expressions.

## Tool facade operations

- create_component
- create_occurrence
- transform_occurrence
- create_joint_origin
- create_rigid_joint
- create_revolute_joint

## Procedure

1. Validate all child components.
2. Create/position occurrences.
3. Create joint origins.
4. Apply joints.
5. Validate assembly tree and transforms.

## Acceptance tests

- component_count
- occurrence_count
- joint_count
- transforms valid
- no obvious collisions

## Common failure modes

- OCCURRENCE_TRANSFORM_ERROR
- JOINT_ORIGIN_MISSING
- JOINT_CREATION_FAILED

## Memory hooks

- On success: record stable recipe if this skill passed verification.
- On failure: classify failure and update project/global memory if reusable.

## Notes for executor

- Do not rely on active selection unless the CAD Spec explicitly requires it.
- Name all created objects.
- Verify after execution before moving to the next transaction.
