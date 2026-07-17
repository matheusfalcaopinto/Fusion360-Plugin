# Skill: fusion_mechanical_pro

## Status

v1.5 candidate

## Purpose

Plan and verify professional mechanical assemblies with component-owned geometry, metadata, occurrences, joints, viewport evidence, physical properties, and interference checks.

## Inputs

- assembly intent
- component metadata contract
- joint contract
- occurrence contract
- viewport output requests
- physical and interference acceptance gates

## Preconditions

- Fusion document is active or mock design state is available.
- Units have been inspected.
- CAD Spec has passed schema validation.
- Every numeric dimension is an explicit unit string or a named parameter expression.
- Existing documents follow checkpoint and modification policy.

## Tool facade operations

- create_spacer_plate_assembly
- create_hinge_assembly
- set_component_metadata
- create_assembly_joints
- analyze_interference
- measure_physical_properties
- inspect_design
- measure_bounding_box
- validate_named_objects

## Procedure

1. Plan a component-first CadSpec with metadata, joints, and programmatic acceptance tests.
2. Inspect the active design before any write.
3. Execute geometry through facade operations only.
4. Write metadata and joint contracts after geometry exists.
5. Run programmatic verification for metadata, joints, occurrences, interference, physical properties, names, feature health, and critical dimensions.
6. Treat real capture/export as `HOST_OUTPUT_DISABLED`; mock/dry-run receipts are not acceptance evidence.
7. Fail closed with classified V1.5 failure codes when typed evidence is missing.

## Acceptance tests

- component_metadata
- joint_contract
- occurrence_contract
- interference_free
- physical_properties
- named_objects
- feature_health

## Common failure modes

- METADATA_MISSING
- JOINT_MISMATCH
- INTERFERENCE_DETECTED
- PHYSICAL_PROPERTY_MISMATCH
- HOST_OUTPUT_DISABLED (when a caller requests real capture/export)
- UNIT_MISMATCH
- INVALID_REFERENCE

## Memory hooks

- On success: record assembly recipe, verified component names, joint names, and material assumptions.
- On failure: record the failed gate, failure code, missing evidence, and whether the issue was mock-only or real-Fusion-specific.

## Notes for executor

- Do not expose raw Fusion MCP tools outside facade methods.
- Do not treat bodies in the root component as a valid assembly unless the CadSpec explicitly contracts that structure.
- Real capture/export is unavailable in 0.4.1; mock/dry-run screenshots are not
  acceptance evidence for a real assembly.
