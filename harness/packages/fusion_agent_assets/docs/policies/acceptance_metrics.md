# Acceptance Metrics

V1.5 assembly acceptance relies exclusively on programmatic evidence in real
0.4.1 sessions. Screenshot receipts remain mock/dry-run compatibility only.

Core metrics:

- body_count
- component_count
- parameter_names
- metadata_components
- occurrence_names
- joint_names
- bounding_box_mm for critical bodies
- interference.count
- physical_properties mass_kg and volume_mm3

Required acceptance test types:

- component_metadata
- joint_contract
- occurrence_contract
- interference_free
- physical_properties

Failure codes:

- METADATA_MISSING
- JOINT_MISMATCH
- INTERFERENCE_DETECTED
- PHYSICAL_PROPERTY_MISMATCH

Real capture/export is `deny_io` and must return `HOST_OUTPUT_DISABLED` before
provider dispatch. A screenshot requirement cannot qualify a real assembly.
