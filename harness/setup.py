from __future__ import annotations

from pathlib import Path

from setuptools import find_packages, setup


ROOT = Path(__file__).parent
PACKAGE_ROOTS = ("packages", "apps")
PACKAGE_DIRS: dict[str, str] = {}
for package_root in PACKAGE_ROOTS:
    for package in find_packages(str(ROOT / package_root)):
        PACKAGE_DIRS[package] = str(Path(package_root) / Path(*package.split(".")))


setup(
    packages=sorted(PACKAGE_DIRS),
    package_dir=PACKAGE_DIRS,
    include_package_data=True,
    package_data={"fusion_agent_assets": ["**/*"]},
)
