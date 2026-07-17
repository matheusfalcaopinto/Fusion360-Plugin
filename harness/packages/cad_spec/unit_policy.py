"""Explicit unit validation and simple expression evaluation."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any


LENGTH_UNITS = {"mm": 1.0, "cm": 10.0, "in": 25.4}
ANGLE_UNITS = {"deg", "rad"}
UNIT_EXPRESSION_RE = re.compile(
    r"^\s*(?P<value>-?\d+(?:\.\d+)?)\s*"
    r"(?P<unit>mm|cm|in|deg|rad)\s*$",
    re.IGNORECASE,
)
PARAMETER_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")

DIMENSION_KEYS = {
    "x",
    "y",
    "z",
    "width",
    "height",
    "length",
    "depth",
    "distance",
    "diameter",
    "radius",
    "thickness",
    "spacing",
    "offset",
    "angle",
    "min",
    "max",
    "lower",
    "upper",
    "target_mm",
    "tolerance_mm",
    "bbox",
    "bounding_box",
    "center",
    "p1",
    "p2",
}
DIMENSIONLESS_KEYS = {
    "count",
    "quantity",
    "body_count",
    "component_count",
    "hole_count",
    "target",
    "attempt",
    "max_attempts",
}


def is_unit_expression(value: str) -> bool:
    """Return true for finite strings with explicit length or angle units."""

    try:
        parse_finite_unit_expression(value)
    except ValueError:
        return False
    return True


def is_parameter_expression(value: str) -> bool:
    """Return true for a named parameter expression."""

    return isinstance(value, str) and bool(PARAMETER_RE.fullmatch(value))


def parse_finite_unit_expression(value: Any, path: str = "value") -> tuple[float, str]:
    """Parse one literal unit expression without permitting float overflow."""

    if not isinstance(value, str):
        raise ValueError(f"{path} must be a string with an explicit unit")
    match = UNIT_EXPRESSION_RE.fullmatch(value)
    if match is None:
        raise ValueError(f"{path} must be a literal numeric unit expression: {value!r}")
    try:
        decimal_value = Decimal(match.group("value"))
        numeric_value = float(decimal_value)
    except (InvalidOperation, OverflowError, ValueError) as exc:
        raise ValueError(
            f"{path} must be a finite literal numeric unit expression"
        ) from exc
    if not decimal_value.is_finite() or not math.isfinite(numeric_value):
        raise ValueError(f"{path} must be a finite literal numeric unit expression")

    unit = match.group("unit").lower()
    if unit in LENGTH_UNITS and not math.isfinite(numeric_value * LENGTH_UNITS[unit]):
        raise ValueError(f"{path} must remain finite after conversion to millimeters")
    return numeric_value, unit


def validate_dimension_expression(value: Any, path: str = "value") -> str:
    """Validate one explicit unit string or named parameter expression."""

    if not isinstance(value, str):
        raise ValueError(
            f"{path} must be an explicit unit string or parameter expression"
        )
    if UNIT_EXPRESSION_RE.fullmatch(value):
        parse_finite_unit_expression(value, path)
        return value
    if is_parameter_expression(value):
        return value
    raise ValueError(f"{path} must include units or name a parameter: {value!r}")


def validate_non_negative_dimension_expression(value: Any, path: str = "value") -> str:
    """Validate a dimension expression and reject negative literal tolerances."""

    validated = validate_dimension_expression(value, path)
    if UNIT_EXPRESSION_RE.fullmatch(validated):
        numeric_value, _unit = parse_finite_unit_expression(validated, path)
        if numeric_value < 0.0:
            raise ValueError(f"{path} must be non-negative")
    return validated


def reject_ambiguous_numeric_dimensions(value: Any, path: str = "value") -> None:
    """Reject raw numeric length/angle-like values inside CAD feature inputs."""

    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if key in DIMENSIONLESS_KEYS:
                continue
            if key in DIMENSION_KEYS:
                _reject_numeric_tree(child, child_path)
            else:
                reject_ambiguous_numeric_dimensions(child, child_path)
        return
    if isinstance(value, list | tuple):
        for index, child in enumerate(value):
            reject_ambiguous_numeric_dimensions(child, f"{path}[{index}]")


def _reject_numeric_tree(value: Any, path: str) -> None:
    if isinstance(value, bool):
        raise ValueError(
            f"{path} is an ambiguous boolean dimension; use an explicit unit string"
        )
    if isinstance(value, int | float):
        raise ValueError(
            f"{path} is an ambiguous numeric dimension; use an explicit unit string"
        )
    if isinstance(value, Mapping):
        for key, child in value.items():
            _reject_numeric_tree(child, f"{path}.{key}")
    elif isinstance(value, list | tuple):
        for index, child in enumerate(value):
            _reject_numeric_tree(child, f"{path}[{index}]")
    elif isinstance(value, str):
        validate_dimension_expression(value, path)


def expression_to_mm(
    expression: str, parameters: Mapping[str, str] | None = None
) -> float:
    """Resolve a simple length expression to millimeters."""

    if not isinstance(expression, str):
        raise ValueError("length expression must be a string with an explicit unit")
    parameters = parameters or {}
    visited: set[str] = set()
    current = expression
    while is_parameter_expression(current) and current in parameters:
        if current in visited:
            raise ValueError(f"cyclic parameter reference: {current}")
        visited.add(current)
        current = parameters[current]
        if not isinstance(current, str):
            raise ValueError(
                f"parameter {expression!r} must resolve to a string with explicit units"
            )
    try:
        value, unit = parse_finite_unit_expression(current, "length expression")
    except ValueError as exc:
        raise ValueError(
            f"cannot resolve finite length expression to mm: {expression!r}"
        ) from exc
    if unit not in LENGTH_UNITS:
        raise ValueError(f"cannot resolve length expression to mm: {expression!r}")
    result = value * LENGTH_UNITS[unit]
    if not math.isfinite(result):
        raise ValueError(f"length expression must remain finite in mm: {expression!r}")
    return result
