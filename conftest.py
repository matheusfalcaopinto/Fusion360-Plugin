"""Test configuration for the canonical harness source tree."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT / "harness" / "packages"), str(ROOT / "harness" / "apps")]
