"""Run pip from an environment without processing startup hooks.

Invoke this script only with ``python -I -S -B``.  It adds the exact
environment site-packages directory to ``sys.path`` as plain data; unlike
``site`` initialization, this does not execute ``.pth`` files or customizers.
"""

from __future__ import annotations

import os
import runpy
import sys
import sysconfig
from pathlib import Path


def _environment_layout() -> tuple[Path, Path, bool]:
    executable = Path(sys.executable).absolute()
    in_environment_scripts = executable.parent.name.lower() in {"bin", "scripts"}
    environment_root = (
        executable.parent.parent if in_environment_scripts else executable.parent
    )
    is_virtual_environment = (environment_root / "pyvenv.cfg").is_file()
    if is_virtual_environment:
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
    resolved = candidate.resolve(strict=True)
    if not resolved.is_dir():
        raise RuntimeError("isolated pip site-packages is not a directory")
    return environment_root.resolve(strict=True), resolved, is_virtual_environment


def _bind_virtual_environment(environment_root: Path) -> None:
    """Restore the venv prefix that CPython <=3.13 omits under ``-S``."""

    if environment_root == Path(sys.base_prefix).resolve():
        raise RuntimeError("isolated pip virtual environment matches the base prefix")
    sys.prefix = str(environment_root)
    sys.exec_prefix = str(environment_root)
    os.environ["VIRTUAL_ENV"] = str(environment_root)


def main() -> None:
    if not (
        sys.flags.isolated
        and sys.flags.ignore_environment
        and sys.flags.no_user_site
        and sys.flags.no_site
        and sys.flags.dont_write_bytecode
    ):
        raise RuntimeError("isolated pip requires python -I -S -B")
    if "site" in sys.modules:
        raise RuntimeError("site initialization occurred before isolated pip")
    environment_root, site_packages, is_virtual_environment = _environment_layout()
    if is_virtual_environment:
        _bind_virtual_environment(environment_root)
    sys.path.insert(0, str(site_packages))
    sys.argv = ["pip", "--isolated", *sys.argv[1:]]
    runpy.run_module("pip", run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
