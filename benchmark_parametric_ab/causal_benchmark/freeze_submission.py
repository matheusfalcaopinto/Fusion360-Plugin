"""CLI for freezing a schema-shaped planner submission."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .submission import freeze_planner_submission


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate and freeze planner plan/script JSON")
    parser.add_argument("--submission", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    print(
        json.dumps(
            freeze_planner_submission(args.submission, args.output_dir),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
