# Purchased And Custom Classification Policy

Component source classification guides review strictness and downstream sourcing assumptions.

Allowed `source_type` values:

- `custom`: fabricated or modeled directly by the harness.
- `purchased`: real purchased part represented by native geometry or imported CAD.
- `library`: reusable standard component from a controlled internal library.
- `placeholder`: temporary geometry that must be explicitly marked as not production-ready.

Rules:

- A placeholder cannot be silently treated as a finished purchased component.
- A purchased component needs part number, description, material where known, and source notes when supplier CAD is not imported.
- Custom components need enough parameters and dimensions to be recreated and verified.
- Library components must keep stable names and revisions.
