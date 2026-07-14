"""Expose the canonical package roots to harness-local tests."""

from __future__ import annotations

import sys
from pathlib import Path


HARNESS_ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(HARNESS_ROOT / "packages"), str(HARNESS_ROOT / "apps")]
