# Review Gates

Professional assembly sessions use gate-based acceptance. A gate either passes with evidence or fails with a classified reason.

Required gates for V1.5 assemblies:

- Inspect active design before modification.
- Validate CAD Spec with explicit units.
- Execute geometry through facade operations only.
- Set component metadata.
- Create or persist joint contracts.
- Analyze interference.
- Measure physical properties.
- Verify required components, bodies, occurrences, joints, metadata, typed
  inspection evidence, and positive mass/volume.

Fail-closed conditions:

- Metadata cannot be written or inspected.
- Joint creation or persisted joint contract cannot be inspected.
- Interference analysis fails or reports unapproved interference.
- Physical properties are missing or non-positive.
- A plan attempts real capture/export instead of returning
  `HOST_OUTPUT_DISABLED` before provider dispatch.
- Raw Fusion MCP tools are exposed outside an allowlisted facade operation.
