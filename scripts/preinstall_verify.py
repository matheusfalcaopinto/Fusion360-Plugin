"""Verify the Fusion Agent bundle before installation or import."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_SCRIPT_DIRECTORY = Path(__file__).resolve().parent
if str(_SCRIPT_DIRECTORY) not in sys.path:
    # ``-I`` deliberately removes the script directory. Restore only this
    # reviewed directory so the exact runtime can execute the stdlib verifier
    # while retaining access to its own installed distribution metadata.
    sys.path.insert(0, str(_SCRIPT_DIRECTORY))

try:
    from scripts.bundle_integrity import (
        BundleIntegrityError,
        expected_version_from_checkout,
        verify_installed_distribution,
        verify_wheel,
    )
except ModuleNotFoundError:  # Executed as ``python scripts/preinstall_verify.py``.
    from bundle_integrity import (  # type: ignore[no-redef]
        BundleIntegrityError,
        expected_version_from_checkout,
        verify_installed_distribution,
        verify_wheel,
    )


def _single_wheel(plugin_root: Path) -> Path:
    wheels = sorted((plugin_root / "wheels").glob("fusion_agent_harness-*.whl"))
    if len(wheels) != 1:
        raise BundleIntegrityError(
            f"expected exactly one bundled wheel, found {len(wheels)}"
        )
    return wheels[0]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plugin-root", type=Path, required=True)
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--verify-installed", action="store_true")
    args = parser.parse_args()
    root = args.plugin_root.resolve()
    wheel = (args.wheel or _single_wheel(root)).resolve()
    try:
        report = verify_wheel(
            wheel,
            plugin_root=root,
            expected_version=expected_version_from_checkout(root),
            require_source_parity=True,
        )
        if args.verify_installed:
            verify_installed_distribution(wheel)
    except (BundleIntegrityError, OSError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error_code": "BUNDLE_INTEGRITY_FAILED",
                    "message": str(exc),
                }
            )
        )
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "wheel": str(report.wheel),
                "version": report.version,
                "sha256": report.sha256,
                "member_count": report.member_count,
                "source_file_count": report.source_file_count,
                "installed_verified": args.verify_installed,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
