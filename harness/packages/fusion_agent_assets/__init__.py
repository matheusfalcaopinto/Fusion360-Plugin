"""Packaged runtime assets for installed Fusion Agent wheels."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path


def asset_root(name: str) -> Path:
    """Return the filesystem path for a bundled runtime asset directory."""

    return Path(str(files(__name__).joinpath(name)))


def has_asset_root(name: str) -> bool:
    """Return whether a bundled runtime asset directory is available."""

    try:
        return asset_root(name).exists()
    except Exception:
        return False
