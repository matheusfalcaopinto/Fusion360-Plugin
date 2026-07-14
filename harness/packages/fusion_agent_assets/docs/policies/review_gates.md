# Review Gates

Professional assembly sessions use gate-based acceptance. A gate either passes with evidence or fails with a classified reason.

Required gates for V1.5 assemblies:

- Inspect active design before modification.
- Validate CAD Spec with explicit units.
- Execute geometry through facade operations only.
- Set component metadata.
- Create or persist joint contracts.
- Capture required viewport screenshots.
- Analyze interference.
- Measure physical properties.
- Verify required components, bodies, occurrences, joints, metadata, screenshots, and positive mass/volume.

Fail-closed conditions:

- Metadata cannot be written or inspected.
- Joint creation or persisted joint contract cannot be inspected.
- Interference analysis fails or reports unapproved interference.
- Physical properties are missing or non-positive.
- Screenshot capture fails or writes an empty file.
- Raw Fusion MCP tools are exposed outside an allowlisted facade operation.
