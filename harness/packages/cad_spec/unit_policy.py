"""Explicit unit validation and simple expression evaluation."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


LENGTH_UNITS = {"mm": 1.0, "cm": 10.0, "in": 25.4}
ANGLE_UNITS = {"deg", "rad"}
UNIT_EXPRESSION_RE = re.compile(r"^\s*-?\d+(?:\.\d+)?\s*(mm|cm|in|deg|rad)\s*$", re.IGNORECASE)
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
    """Return true for strings with explicit length or angle units."""

    return bool(UNIT_EXPRESSION_RE.fullmatch(value))


def is_parameter_expression(value: str) -> bool:
    """Return true for a named parameter expression."""

    return bool(PARAMETER_RE.fullmatch(value))


def validate_dimension_expression(value: Any, path: str = "value") -> str:
    """Validate one explicit unit string or named parameter expression."""

    if not isinstance(value, str):
        raise ValueError(f"{path} must be an explicit unit string or parameter expression")
    if is_unit_expression(value) or is_parameter_expression(value):
        return value
    raise ValueError(f"{path} must include units or name a parameter: {value!r}")


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
        return
    if isinstance(value, int | float):
        raise ValueError(f"{path} is an ambiguous numeric dimension; use an explicit unit string")
    if isinstance(value, Mapping):
        for key, child in value.items():
            _reject_numeric_tree(child, f"{path}.{key}")
    elif isinstance(value, list | tuple):
        for index, child in enumerate(value):
            _reject_numeric_tree(child, f"{path}[{index}]")
    elif isinstance(value, str):
        validate_dimension_expression(value, path)


def expression_to_mm(expression: str, parameters: Mapping[str, str] | None = None) -> float:
    """Resolve a simple length expression to millimeters."""

    parameters = parameters or {}
    visited: set[str] = set()
    current = expression
    while is_parameter_expression(current) and current in parameters:
        if current in visited:
            raise ValueError(f"cyclic parameter reference: {current}")
        visited.add(current)
        current = parameters[current]
    match = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*(mm|cm|in)\s*", current, re.IGNORECASE)
    if not match:
        raise ValueError(f"cannot resolve length expression to mm: {expression!r}")
    value = float(match.group(1))
    unit = match.group(2).lower()
    return value * LENGTH_UNITS[unit]
