# Naming Policy

## Object names

Use snake_case names.

Examples:

```text
component: mounting_plate
sketch: base_profile_sketch
body: mounting_plate_body
feature: base_plate_extrude
parameter: plate_length
```

## Avoid

```text
Sketch1
Body1
Extrude1
Component1
```

## Collision policy

If a name already exists:

1. If same semantic object, reuse/update.
2. If conflict, append numeric suffix and record in session journal.
