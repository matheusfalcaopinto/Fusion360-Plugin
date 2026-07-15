"""Run non-experimental CadSpec v2 packs on disposable real-Fusion fixtures."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from benchmark.real_capability_packs import run_real_capability_packs


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Execute strict CadSpec v2 capability packs against Autodesk Fusion real "
            "using only uniquely marked, disposable unsaved documents."
        )
    )
    parser.add_argument(
        "--artifact-root",
        type=Path,
        default=Path("nightly-artifacts"),
        help="Repository-local directory for the normalized JSON result and I/O artifacts.",
    )
    arguments = parser.parse_args()
    result = asyncio.run(run_real_capability_packs(arguments.artifact_root))
    print(
        json.dumps(
            {
                "status": result["status"],
                "artifact": str(arguments.artifact_root / "capability-packs.json"),
            },
            sort_keys=True,
        )
    )
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
