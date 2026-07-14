"""Naming rules for Fusion objects created by the harness."""

from __future__ import annotations

import re


SNAKE_CASE_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
DEFAULT_NAME_PREFIXES = ("body", "sketch", "component", "feature")


def is_snake_case_name(value: str) -> bool:
    """Return true when a name follows the project snake_case policy."""

    return bool(SNAKE_CASE_RE.fullmatch(value))


def validate_name(value: str, field_name: str = "name") -> str:
    """Validate a stable object name and return it unchanged."""

    if not value or not isinstance(value, str):
        raise ValueError(f"{field_name} must be a non-empty string")
    if not is_snake_case_name(value):
        raise ValueError(f"{field_name} must be snake_case: {value!r}")
    lowered = value.lower()
    if any(lowered.startswith(prefix) and lowered[len(prefix) :].isdigit() for prefix in DEFAULT_NAME_PREFIXES):
        raise ValueError(f"{field_name} must not be a default Fusion-style name: {value!r}")
    return value


def has_default_name(value: str) -> bool:
    """Return true when a name looks like a default generated Fusion name."""

    lowered = value.lower()
    return any(lowered.startswith(prefix) and lowered[len(prefix) :].isdigit() for prefix in DEFAULT_NAME_PREFIXES)
