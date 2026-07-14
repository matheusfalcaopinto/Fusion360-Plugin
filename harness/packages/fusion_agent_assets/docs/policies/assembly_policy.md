# Assembly Policy

Generic assembly workflows must create component-owned geometry and inspectable assembly structure. A passing model is not a loose set of bodies.

Required behavior:

- Create a named top-level assembly component.
- Create named child components for each physical part class.
- Keep bodies inside their owning components.
- Represent repeated parts as repeated occurrences when the workflow calls for reuse.
- Create or persist inspectable joint contracts for every expected relationship.
- Verify components, bodies, occurrences, joints, metadata, screenshots, physical properties, and interference before reporting success.
- Fail closed when the active design cannot prove the requested assembly contract.

V1.5 first-class workflows:

- `spacer_plate_assembly`: two plates, repeated standoff occurrences, rigid joint contracts, metadata, screenshots, physical checks, and interference checks.
- `hinge_assembly`: two hinge leaves, pin, alternating knuckles, revolute joint contract, metadata, screenshots, physical checks, and interference checks.

Out of scope for V1.5:

- CNC, NEMA, and MGN workflow retrofits except shared schema, facade, and verifier compatibility.
- PMI, drawings, CAM, supplier CAD import, PDM/PLM, and full BOM generation.
