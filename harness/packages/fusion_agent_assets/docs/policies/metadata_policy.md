# Component Metadata Policy

Every contracted component in a professional assembly must carry non-empty engineering metadata.

Required fields:

- component
- part_number
- description
- role
- source_type
- physical_material

Optional fields:

- appearance
- placeholder
- revision

Rules:

- Metadata writes must target Fusion components, not bodies.
- Real Fusion writes set `partNumber` and `description` where the API supports them.
- Materials and appearances are applied when a matching library item is available.
- Unsupported metadata fields must remain inspectable through component attributes.
- Missing, empty, or mismatched required metadata fails verification with `METADATA_MISSING`.
