# Acceptance Metrics

V1.5 assembly acceptance relies on programmatic evidence before screenshots.

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
- screenshot paths and byte counts

Required acceptance test types:

- component_metadata
- joint_contract
- occurrence_contract
- interference_free
- physical_properties
- screenshots_exist

Failure codes:

- METADATA_MISSING
- JOINT_MISMATCH
- INTERFERENCE_DETECTED
- PHYSICAL_PROPERTY_MISMATCH
- SCREENSHOT_FAILED

Screenshots are evidence artifacts, not the primary source of truth.
