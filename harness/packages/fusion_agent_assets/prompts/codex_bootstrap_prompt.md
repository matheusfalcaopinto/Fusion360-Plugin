# Codex Maintenance Prompt

Use this prompt to ask a Codex/local coding agent to continue implementing or
reviewing the Fusion Agent harness.

```text
You are implementing `fusion-agent-harness`, a Python project for a standalone
Autodesk Fusion modeling agent/harness.

Read AGENTS.md, README.md, QUICKSTART.md, schemas/cad_spec_schema.md,
schemas/acceptance_test_schema.md, docs/policies/*.md, and the relevant package
entry points before editing.

Current architecture:
- User request -> memory retrieval -> planner -> CAD Spec -> executor -> safe facade -> verifier -> bounded repair -> journal/memory/benchmark.
- Mock mode must stay deterministic and fully tested.
- Real Fusion support must remain behind the allowlisted facade and existing CRUD script bridge.
- No raw Autodesk Fusion MCP tool surface may be exposed directly to the model.
- CAD Spec requires explicit units and rejects ambiguous numeric dimensions.
- Assemblies must use named components/occurrences, metadata contracts, joint contracts, screenshot outputs, physical property checks, and interference checks when requested.
- Verification must fail closed when native joints, metadata, occurrences, screenshots, physical properties, or interference analysis cannot be proven.
- Distribution uses a Python wheel plus a lightweight Codex plugin zip with the wheel bundled under `wheels/`.

Implementation expectations:
- Inspect before modify.
- Keep changes scoped and covered by tests.
- Add mock tests before relying on real Fusion.
- Add opt-in real tests only behind explicit environment flags and disposable-document requirements.
- Keep runtime skills, memory templates, policies, and prompts synchronized with packaged wheel assets.
- Run focused tests first, then the full pytest suite.
```
