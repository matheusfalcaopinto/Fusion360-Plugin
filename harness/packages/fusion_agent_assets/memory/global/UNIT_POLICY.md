# Unit Policy

## Rule

All CAD Spec values must use explicit units or named parameter expressions.

Accepted:

```text
"10 mm"
"45 deg"
"plate_thickness"
"hole_diameter / 2"
```

Rejected:

```text
10
5.0
```

## Rationale

Fusion API real values use internal database units. Explicit unit strings avoid mm/cm/in mistakes.
