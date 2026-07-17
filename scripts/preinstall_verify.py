"""Verify the Fusion Agent bundle before installation or import."""

from __future__ import annotations

import argparse
import json
import os
import sys
import sysconfig
from pathlib import Path

_SCRIPT_DIRECTORY = Path(__file__).resolve().parent
if str(_SCRIPT_DIRECTORY) not in sys.path:
    # Isolated invocation can remove the script directory. Restore only this
    # reviewed directory; installed metadata is discovered through an explicit
    # site-packages path without enabling ``site`` or importing package code.
    sys.path.insert(0, str(_SCRIPT_DIRECTORY))

try:
    from scripts.bundle_integrity import (
        BundleIntegrityError,
        expected_version_from_checkout,
        verify_installed_dependency_set,
        verify_installed_distribution,
        verify_wheel,
    )
except ModuleNotFoundError:  # Executed as ``python scripts/preinstall_verify.py``.
    from bundle_integrity import (  # type: ignore[no-redef]
        BundleIntegrityError,
        expected_version_from_checkout,
        verify_installed_dependency_set,
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


def _installed_site_packages() -> Path:
    executable = Path(sys.executable).absolute()
    in_environment_scripts = executable.parent.name.lower() in {"bin", "scripts"}
    environment_root = (
        executable.parent.parent if in_environment_scripts else executable.parent
    )
    if (environment_root / "pyvenv.cfg").is_file():
        if os.name == "nt":
            candidate = environment_root / "Lib" / "site-packages"
        else:
            candidate = (
                environment_root
                / "lib"
                / f"python{sys.version_info.major}.{sys.version_info.minor}"
                / "site-packages"
            )
    else:
        candidate = Path(sysconfig.get_path("purelib"))
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise BundleIntegrityError(
            "installed site-packages cannot be located without site initialization"
        ) from exc
    if not resolved.is_dir():
        raise BundleIntegrityError("installed site-packages is not a directory")
    return resolved


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plugin-root", type=Path, required=True)
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--verify-installed", action="store_true")
    parser.add_argument("--dependency-wheelhouse", type=Path)
    parser.add_argument(
        "--dependency-lock",
        choices=("runtime.lock", "test.lock", "quality.lock", "faust.lock"),
        default="runtime.lock",
    )
    args = parser.parse_args()
    root = args.plugin_root.resolve()
    try:
        wheel = (args.wheel or _single_wheel(root)).resolve()
        report = verify_wheel(
            wheel,
            plugin_root=root,
            expected_version=expected_version_from_checkout(root),
            require_source_parity=True,
        )
        if args.verify_installed:
            if args.dependency_wheelhouse is None:
                raise BundleIntegrityError(
                    "--dependency-wheelhouse is required with --verify-installed"
                )
            site_packages = _installed_site_packages()
            verify_installed_distribution(wheel, site_packages=site_packages)
            verify_installed_dependency_set(
                root,
                dependency_wheelhouse=args.dependency_wheelhouse,
                lock_name=args.dependency_lock,
                site_packages=site_packages,
            )
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
                "dependency_lock": args.dependency_lock,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
