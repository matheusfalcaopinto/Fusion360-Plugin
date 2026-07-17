from __future__ import annotations

import csv
import io
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Callable
from importlib import metadata
from pathlib import Path

import pytest
import tomllib

from scripts.bundle_integrity import (
    BundleIntegrityError,
    _TrustedDependencyWheel,
    _applicable_lock_versions,
    _entrypoint_script_bytes,
    _locked_requirements,
    _reject_locked_startup_member,
    _verify_site_package_ownership,
    _verify_entrypoint_wrapper,
    record_digest,
    verify_installed_dependency_set,
    verify_installed_distribution,
    verify_wheel,
)
from scripts.preinstall_verify import _installed_site_packages
from scripts import preinstall_verify
from scripts.validate_plugin import _check_fusion_data
from scripts.verify_installation_parity import (
    InstallationParityError,
    _read_object,
)


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
REQUIREMENTS = ROOT / "harness" / "requirements"


def _wheel() -> Path:
    wheels = sorted((ROOT / "wheels").glob("fusion_agent_harness-*.whl"))
    assert len(wheels) == 1
    return wheels[0]


def _refresh_record(files: dict[str, bytes]) -> None:
    record_name = next(name for name in files if name.endswith(".dist-info/RECORD"))
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    for name in sorted(set(files) - {record_name}):
        data = files[name]
        writer.writerow([name, record_digest(data), len(data)])
    writer.writerow([record_name, "", ""])
    files[record_name] = output.getvalue().encode("utf-8")


def _rewrite_wheel(
    source: Path,
    target: Path,
    mutate: Callable[[dict[str, bytes]], None],
) -> None:
    with zipfile.ZipFile(source) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    mutate(files)
    with zipfile.ZipFile(target, "w") as archive:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data, compress_type=zipfile.ZIP_STORED)


class _FakeInstalledDistribution:
    def __init__(
        self,
        site_packages: Path,
        name: str,
        version: str,
        files: list[str],
    ) -> None:
        self._site_packages = site_packages
        self.metadata = {"Name": name}
        self.version = version
        self.files = files

    def locate_file(self, name: object) -> Path:
        return self._site_packages.joinpath(*Path(str(name)).parts)


def _fake_installed_distribution(
    site_packages: Path,
    name: str,
    version: str,
    *,
    files: dict[str, bytes] | None = None,
    blank_hashes: set[str] | None = None,
) -> _FakeInstalledDistribution:
    normalized = re.sub(r"[-_.]+", "_", name)
    dist_info = f"{normalized}-{version}.dist-info"
    record_name = f"{dist_info}/RECORD"
    members = dict(files or {})
    members[f"{dist_info}/METADATA"] = (
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
    ).encode()
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    for relative, data in sorted(members.items()):
        target = site_packages.joinpath(*relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        if relative in (blank_hashes or set()):
            writer.writerow([relative, "", ""])
        else:
            writer.writerow([relative, record_digest(data), len(data)])
    writer.writerow([record_name, "", ""])
    record = site_packages.joinpath(*record_name.split("/"))
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(output.getvalue(), encoding="utf-8", newline="")
    return _FakeInstalledDistribution(
        site_packages,
        name,
        version,
        [*sorted(members), record_name],
    )


def _dependency_wheel(
    wheelhouse: Path,
    name: str = "example-runtime",
    version: str = "1.0",
    *,
    package_members: dict[str, bytes] | None = None,
) -> Path:
    normalized = re.sub(r"[-_.]+", "_", name)
    dist_info = f"{normalized}-{version}.dist-info"
    record_name = f"{dist_info}/RECORD"
    files = {
        **(package_members or {"example_runtime/__init__.py": b"SAFE = True\n"}),
        f"{dist_info}/METADATA": (
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
        ).encode(),
        f"{dist_info}/WHEEL": (
            b"Wheel-Version: 1.0\n"
            b"Generator: supply-chain-test\n"
            b"Root-Is-Purelib: true\n"
            b"Tag: py3-none-any\n"
        ),
    }
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    for relative, data in sorted(files.items()):
        writer.writerow([relative, record_digest(data), len(data)])
    writer.writerow([record_name, "", ""])
    files[record_name] = output.getvalue().encode()
    wheelhouse.mkdir(parents=True, exist_ok=True)
    wheel = wheelhouse / f"{normalized}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative, data in sorted(files.items()):
            archive.writestr(relative, data)
    return wheel


def _install_dependency_wheel(wheel: Path, site: Path) -> None:
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        wheel_record = next(
            name for name in names if name.endswith(".dist-info/RECORD")
        )
        installed: dict[str, bytes] = {}
        data_root = wheel_record.rsplit("/", 1)[0].removesuffix(".dist-info") + ".data/"
        for name in names - {wheel_record}:
            installed_name = name
            if name.startswith(data_root):
                category, installed_name = name.removeprefix(data_root).split("/", 1)
                assert category in {"purelib", "platlib"}
            installed[installed_name] = archive.read(name)
    for relative, data in installed.items():
        target = site.joinpath(*relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    for relative, data in sorted(installed.items()):
        writer.writerow([relative, record_digest(data), len(data)])
    writer.writerow([wheel_record, "", ""])
    record = site.joinpath(*wheel_record.split("/"))
    record.write_text(output.getvalue(), encoding="utf-8", newline="")


def _installed_dependency_fixture(
    tmp_path: Path,
    *,
    name: str = "example-runtime",
    version: str = "1.0",
    package_members: dict[str, bytes] | None = None,
) -> tuple[Path, Path, Path, Path]:
    plugin = tmp_path / "plugin"
    wheelhouse = tmp_path / "wheelhouse"
    site = tmp_path / "venv" / "Lib" / "site-packages"
    site.mkdir(parents=True)
    wheel = _dependency_wheel(
        wheelhouse,
        name,
        version,
        package_members=package_members,
    )
    requirements = plugin / "harness" / "requirements"
    requirements.mkdir(parents=True)
    (requirements / "runtime.lock").write_text(
        f"--only-binary=:all:\n{name}=={version} --hash=sha256:{sha256_file(wheel)}\n",
        encoding="utf-8",
    )
    (plugin / "harness" / "pyproject.toml").write_text(
        '[project]\nname = "fusion-agent-harness"\nversion = "0.4.1"\n',
        encoding="utf-8",
    )
    _install_dependency_wheel(wheel, site)
    _fake_installed_distribution(site, "fusion-agent-harness", "0.4.1")
    return plugin, wheelhouse, site, wheel


def sha256_file(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rewrite_installed_record(
    record: Path,
    mutate: Callable[[list[list[str]]], None],
) -> None:
    rows = list(csv.reader(io.StringIO(record.read_text(encoding="utf-8"))))
    mutate(rows)
    output = io.StringIO(newline="")
    csv.writer(output, lineterminator="\n").writerows(rows)
    record.write_text(output.getvalue(), encoding="utf-8", newline="")


def test_wheel_metadata_dependencies_are_bound_to_reviewed_pyproject(
    tmp_path: Path,
) -> None:
    wheel = _wheel()
    verify_wheel(wheel, plugin_root=ROOT)
    tampered = tmp_path / wheel.name

    def inject_dependency(files: dict[str, bytes]) -> None:
        metadata_name = next(
            name for name in files if name.endswith(".dist-info/METADATA")
        )
        files[metadata_name] = files[metadata_name].replace(
            b"Requires-Python:",
            b"Requires-Dist: unreviewed-build-hook @ https://attacker.invalid/hook.whl\nRequires-Python:",
            1,
        )
        _refresh_record(files)

    _rewrite_wheel(wheel, tampered, inject_dependency)

    with pytest.raises(BundleIntegrityError, match="METADATA.*reviewed source"):
        verify_wheel(tampered, plugin_root=ROOT)


def test_wheel_generated_entry_points_are_exactly_bound_to_reviewed_source(
    tmp_path: Path,
) -> None:
    wheel = _wheel()
    tampered = tmp_path / wheel.name

    def inject_entry_point(files: dict[str, bytes]) -> None:
        entry_points = next(
            name for name in files if name.endswith(".dist-info/entry_points.txt")
        )
        files[entry_points] += b"unreviewed-hook = os:system\n"
        _refresh_record(files)

    _rewrite_wheel(wheel, tampered, inject_entry_point)

    with pytest.raises(BundleIntegrityError, match="entry_points.txt"):
        verify_wheel(tampered)


def test_wheel_verifier_rejects_noncanonical_zip_metadata(tmp_path: Path) -> None:
    wheel = _wheel()
    tampered = tmp_path / wheel.name
    with zipfile.ZipFile(wheel) as source, zipfile.ZipFile(tampered, "w") as target:
        for index, name in enumerate(source.namelist()):
            timestamp = (1981, 1, 1, 0, 0, 0) if index == 0 else (1980, 1, 1, 0, 0, 0)
            info = zipfile.ZipInfo(name, timestamp)
            info.create_system = 3
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = 0o100644 << 16
            target.writestr(info, source.read(name), compress_type=zipfile.ZIP_STORED)

    with pytest.raises(BundleIntegrityError, match="metadata is not canonical"):
        verify_wheel(tampered)


def test_wheel_manifest_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    wheel = _wheel()
    tampered = tmp_path / wheel.name

    def duplicate_project_key(files: dict[str, bytes]) -> None:
        manifest = next(
            name for name in files if name.endswith(".dist-info/SOURCE-MANIFEST.json")
        )
        files[manifest] = files[manifest].replace(
            b"{",
            b'{"project":"fusion-agent-harness",',
            1,
        )
        _refresh_record(files)

    _rewrite_wheel(wheel, tampered, duplicate_project_key)

    with pytest.raises(BundleIntegrityError, match="duplicate JSON key"):
        verify_wheel(tampered)


def test_parity_json_reader_rejects_duplicate_keys(tmp_path: Path) -> None:
    payload = tmp_path / ".mcp.json"
    payload.write_text('{"mcpServers":{},"mcpServers":{}}', encoding="utf-8")

    with pytest.raises(InstallationParityError, match="invalid required JSON"):
        _read_object(payload)


def test_installed_verifier_rejects_extra_first_party_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheel = _wheel()
    site = tmp_path / "site-packages"
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(site)

    class FakeDistribution:
        def locate_file(self, name: object) -> Path:
            return site / str(name)

    monkeypatch.setattr(
        "scripts.bundle_integrity.metadata.distribution",
        lambda _name: FakeDistribution(),
    )
    verify_installed_distribution(wheel)

    extra = site / "fusion_agent_mcp" / "unreviewed.py"
    extra.write_text("UNREVIEWED = True\n", encoding="utf-8")

    with pytest.raises(BundleIntegrityError, match="unexpected first-party"):
        verify_installed_distribution(wheel)


def test_installed_verifier_rejects_generated_first_party_bytecode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheel = _wheel()
    site = tmp_path / "site-packages"
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(site)

    class FakeDistribution:
        def locate_file(self, name: object) -> Path:
            return site / str(name)

    monkeypatch.setattr(
        "scripts.bundle_integrity.metadata.distribution",
        lambda _name: FakeDistribution(),
    )

    # An exact no-compile install is the legitimate control.
    verify_installed_distribution(wheel)

    bytecode = (
        site
        / "fusion_agent_mcp"
        / "__pycache__"
        / f"server.cpython-{sys.version_info.major}{sys.version_info.minor}.pyc"
    )
    bytecode.parent.mkdir()
    bytecode.write_bytes(b"unreviewed executable bytecode")

    with pytest.raises(BundleIntegrityError, match="unexpected first-party"):
        verify_installed_distribution(wheel)


def test_installed_verifier_allows_only_reviewed_installer_generated_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheel = _wheel()
    site = tmp_path / "site-packages"
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(site)
        names = list(archive.namelist())
        dist_info = next(
            name.rsplit("/", 1)[0]
            for name in names
            if name.endswith(".dist-info/RECORD")
        )

    class FakeDistribution:
        files = [
            *names,
            "../../Scripts/fusion-agent.exe",
            "../../Scripts/fusion-agent-mcp.exe",
            f"{dist_info}/INSTALLER",
            f"{dist_info}/REQUESTED",
            f"{dist_info}/direct_url.json",
        ]

        def locate_file(self, name: object) -> Path:
            return site / str(name)

    distribution = FakeDistribution()
    monkeypatch.setattr(
        "scripts.bundle_integrity.metadata.distribution", lambda _name: distribution
    )
    verify_installed_distribution(wheel)

    distribution.files.append("unreviewed-runtime.pth")
    with pytest.raises(BundleIntegrityError, match="unexpected first-party"):
        verify_installed_distribution(wheel)


def test_installed_verifier_rejects_unrecorded_extra_first_party_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheel = _wheel()
    site = tmp_path / "site-packages"
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(site)
        names = list(archive.namelist())

    class FakeDistribution:
        # Simulate importlib.metadata reading an attacker-edited installed RECORD
        # which simply omits the extra importable file.
        files = names

        def locate_file(self, name: object) -> Path:
            return site / str(name)

    monkeypatch.setattr(
        "scripts.bundle_integrity.metadata.distribution",
        lambda _name: FakeDistribution(),
    )
    (site / "fusion_agent_mcp" / "unrecorded_hook.py").write_text(
        "UNREVIEWED = True\n", encoding="utf-8"
    )

    with pytest.raises(BundleIntegrityError, match="unexpected first-party"):
        verify_installed_distribution(wheel)


def test_installed_dependency_set_is_exactly_bound_to_selected_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = _applicable_lock_versions(REQUIREMENTS / "runtime.lock")
    expected["fusion-agent-harness"] = "0.4.1"

    site = tmp_path / "site-packages"
    site.mkdir()
    distributions = [
        _fake_installed_distribution(site, name, version)
        for name, version in expected.items()
    ]
    trusted = {}
    for name, version in expected.items():
        if name == "fusion-agent-harness":
            continue
        normalized = re.sub(r"[-_.]+", "_", name)
        dist_info = f"{normalized}-{version}.dist-info"
        metadata_name = f"{dist_info}/METADATA"
        trusted[name] = _TrustedDependencyWheel(
            name=name,
            version=version,
            wheel=tmp_path / f"{normalized}.whl",
            members={
                metadata_name: site.joinpath(*metadata_name.split("/")).read_bytes()
            },
            record_name=f"{dist_info}/RECORD",
            entry_points={},
            wheel_scripts={},
        )
    monkeypatch.setattr(
        "scripts.bundle_integrity.metadata.distributions", lambda: distributions
    )
    monkeypatch.setattr(
        "scripts.bundle_integrity._verify_dependency_wheelhouse",
        lambda *_args, **_kwargs: trusted,
    )
    verify_installed_dependency_set(ROOT, dependency_wheelhouse=tmp_path)

    (site / "unowned-startup-hook.pth").write_text(
        "import unreviewed_runtime_hook\n", encoding="utf-8"
    )
    with pytest.raises(BundleIntegrityError, match="unowned"):
        verify_installed_dependency_set(ROOT, dependency_wheelhouse=tmp_path)
    (site / "unowned-startup-hook.pth").unlink()

    distributions.append(
        _fake_installed_distribution(site, "unreviewed-runtime-hook", "1.0")
    )
    with pytest.raises(BundleIntegrityError, match="installed dependency set"):
        verify_installed_dependency_set(ROOT, dependency_wheelhouse=tmp_path)


def test_installed_dependency_is_anchored_to_legitimate_locked_wheelhouse(
    tmp_path: Path,
) -> None:
    plugin, wheelhouse, site, _wheel_path = _installed_dependency_fixture(
        tmp_path,
        package_members={
            "example_runtime/__init__.py": b"SAFE = True\n",
            "example_runtime-1.0.data/purelib/pure_projection/__init__.py": (
                b"PURE = True\n"
            ),
            "example_runtime-1.0.data/platlib/platform_projection.pyd": b"NATIVE",
        },
    )

    verify_installed_dependency_set(
        plugin,
        dependency_wheelhouse=wheelhouse,
        site_packages=site,
    )


def test_installed_dependency_rejects_coordinated_payload_and_record_tamper(
    tmp_path: Path,
) -> None:
    plugin, wheelhouse, site, _wheel_path = _installed_dependency_fixture(tmp_path)
    payload = site / "example_runtime" / "__init__.py"
    payload.write_bytes(b"import attacker_payload\n")
    record = site / "example_runtime-1.0.dist-info" / "RECORD"

    def bind_record_to_tamper(rows: list[list[str]]) -> None:
        row = next(row for row in rows if row[0] == "example_runtime/__init__.py")
        row[1] = record_digest(payload.read_bytes())
        row[2] = str(payload.stat().st_size)

    _rewrite_installed_record(record, bind_record_to_tamper)

    with pytest.raises(BundleIntegrityError, match="trusted wheel"):
        verify_installed_dependency_set(
            plugin,
            dependency_wheelhouse=wheelhouse,
            site_packages=site,
        )


@pytest.mark.parametrize("hash_bound", (False, True))
def test_installed_dependency_rejects_recorded_bytecode(
    tmp_path: Path,
    hash_bound: bool,
) -> None:
    plugin, wheelhouse, site, _wheel_path = _installed_dependency_fixture(tmp_path)
    bytecode = site / "example_runtime" / "__pycache__" / "__init__.cpython-312.pyc"
    bytecode.parent.mkdir()
    bytecode.write_bytes(b"crafted-pyc")
    record = site / "example_runtime-1.0.dist-info" / "RECORD"

    def add_bytecode(rows: list[list[str]]) -> None:
        rows.insert(
            -1,
            [
                "example_runtime/__pycache__/__init__.cpython-312.pyc",
                record_digest(bytecode.read_bytes()) if hash_bound else "",
                str(bytecode.stat().st_size) if hash_bound else "",
            ],
        )

    _rewrite_installed_record(record, add_bytecode)

    with pytest.raises(BundleIntegrityError, match="bytecode is not permitted"):
        verify_installed_dependency_set(
            plugin,
            dependency_wheelhouse=wheelhouse,
            site_packages=site,
        )


def test_installed_dependency_rejects_package_form_sitecustomize(
    tmp_path: Path,
) -> None:
    plugin, wheelhouse, site, _wheel_path = _installed_dependency_fixture(tmp_path)
    customizer = site / "sitecustomize" / "__init__.py"
    customizer.parent.mkdir()
    customizer.write_bytes(b"import attacker_payload\n")
    record = site / "example_runtime-1.0.dist-info" / "RECORD"

    def add_customizer(rows: list[list[str]]) -> None:
        rows.insert(
            -1,
            [
                "sitecustomize/__init__.py",
                record_digest(customizer.read_bytes()),
                str(customizer.stat().st_size),
            ],
        )

    _rewrite_installed_record(record, add_customizer)

    with pytest.raises(BundleIntegrityError, match="startup customizer"):
        verify_installed_dependency_set(
            plugin,
            dependency_wheelhouse=wheelhouse,
            site_packages=site,
        )


@pytest.mark.parametrize(
    "relative",
    (
        "sitecustomize.py",
        "sitecustomize.pyc",
        "sitecustomize.cp312-win_amd64.pyd",
        "sitecustomize/__init__.py",
        "usercustomize.so",
        "__pycache__/usercustomize.cpython-312.pyc",
    ),
)
def test_all_startup_customizer_import_forms_are_rejected(relative: str) -> None:
    with pytest.raises(BundleIntegrityError, match="startup customizer"):
        _reject_locked_startup_member(relative)
    _reject_locked_startup_member("ordinary_package/sitecustomize.py")


@pytest.mark.parametrize(
    "alias",
    (
        "example_runtime/./__init__.py",
        "example_runtime/subdirectory/../__init__.py",
    ),
)
def test_installed_dependency_rejects_alias_in_record_path(
    tmp_path: Path,
    alias: str,
) -> None:
    plugin, wheelhouse, site, _wheel_path = _installed_dependency_fixture(tmp_path)
    record = site / "example_runtime-1.0.dist-info" / "RECORD"

    def alias_member(rows: list[list[str]]) -> None:
        row = next(row for row in rows if row[0] == "example_runtime/__init__.py")
        row[0] = alias

    _rewrite_installed_record(record, alias_member)

    with pytest.raises(BundleIntegrityError, match="RECORD|noncanonical"):
        verify_installed_dependency_set(
            plugin,
            dependency_wheelhouse=wheelhouse,
            site_packages=site,
        )


def test_installed_dependency_rejects_symlinked_package_directory(
    tmp_path: Path,
) -> None:
    plugin, wheelhouse, site, _wheel_path = _installed_dependency_fixture(tmp_path)
    package = site / "example_runtime"
    external = tmp_path / "external-package"
    shutil.copytree(package, external)
    shutil.rmtree(package)
    try:
        os.symlink(external, package, target_is_directory=True)
    except OSError as exc:
        if os.name != "nt":
            raise
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(package), str(external)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, (exc, completed.stderr, completed.stdout)

    with pytest.raises(BundleIntegrityError, match="symlink or reparse point"):
        verify_installed_dependency_set(
            plugin,
            dependency_wheelhouse=wheelhouse,
            site_packages=site,
        )


def test_installed_dependency_preserves_exact_reviewed_pth_from_wheelhouse(
    tmp_path: Path,
) -> None:
    pywin32_hook = (
        b"# .pth file for the PyWin32 extensions\r\n"
        b"win32\r\n"
        b"win32\\lib\r\n"
        b"pythonwin\r\n"
        b"# And some hackery to deal with environments where the post_install "
        b"script\r\n"
        b"# isn't run.\r\n"
        b"import pywin32_bootstrap\r\n"
    )
    plugin, wheelhouse, site, _wheel_path = _installed_dependency_fixture(
        tmp_path,
        name="pywin32",
        version="312",
        package_members={"pywin32.pth": pywin32_hook},
    )

    verify_installed_dependency_set(
        plugin,
        dependency_wheelhouse=wheelhouse,
        site_packages=site,
    )


def test_dependency_wheelhouse_rejects_whole_file_hash_mismatch(
    tmp_path: Path,
) -> None:
    plugin, wheelhouse, site, wheel = _installed_dependency_fixture(tmp_path)
    wheel.write_bytes(wheel.read_bytes() + b"tamper")

    with pytest.raises(BundleIntegrityError, match="not selected by the hash lock"):
        verify_installed_dependency_set(
            plugin,
            dependency_wheelhouse=wheelhouse,
            site_packages=site,
        )


def test_dependency_wrapper_is_strictly_projected_from_trusted_entry_point(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = "example_runtime.cli:main"
    script = _entrypoint_script_bytes(target)
    shebang = f"#!{Path(sys.executable).resolve(strict=True)}\n".encode()
    if os.name == "nt":
        launcher = b"MZ-reviewed-distlib-launcher"
        monkeypatch.setattr(
            "scripts.bundle_integrity._trusted_distlib_launcher",
            lambda **_kwargs: launcher,
        )
        zipped = io.BytesIO()
        with zipfile.ZipFile(zipped, "w") as archive:
            archive.writestr("__main__.py", script)
        legitimate = launcher + shebang + zipped.getvalue()
        arbitrary = b"MZ-unreviewed-launcher" + shebang + zipped.getvalue()
        error = "untrusted Windows launcher"
    else:
        legitimate = shebang + script
        arbitrary = shebang + script + b"import attacker_payload\n"
        error = "strict POSIX projection"

    _verify_entrypoint_wrapper(
        legitimate,
        target=target,
        gui=False,
        root=tmp_path,
        distributions=[],
    )
    with pytest.raises(BundleIntegrityError, match=error):
        _verify_entrypoint_wrapper(
            arbitrary,
            target=target,
            gui=False,
            root=tmp_path,
            distributions=[],
        )


@pytest.mark.parametrize("tamper", ("hash", "size"))
def test_installed_dependency_record_rejects_hash_or_size_tampering(
    tmp_path: Path,
    tamper: str,
) -> None:
    site = tmp_path / "venv" / "Lib" / "site-packages"
    site.mkdir(parents=True)
    distribution = _fake_installed_distribution(
        site,
        "example-runtime",
        "1.0",
        files={"example_runtime/__init__.py": b"SAFE = True\n"},
    )
    record = site / "example_runtime-1.0.dist-info" / "RECORD"
    rows = list(csv.reader(io.StringIO(record.read_text(encoding="utf-8"))))
    for row in rows:
        if row[0] != "example_runtime/__init__.py":
            continue
        if tamper == "hash":
            row[1] = "sha256=" + ("A" * 43)
        else:
            row[2] = str(int(row[2]) + 1)
    output = io.StringIO(newline="")
    csv.writer(output, lineterminator="\n").writerows(rows)
    record.write_text(output.getvalue(), encoding="utf-8", newline="")

    with pytest.raises(BundleIntegrityError, match="RECORD hash/size mismatch"):
        _verify_site_package_ownership([distribution])


def test_installed_dependency_record_rejects_recorded_startup_hook(
    tmp_path: Path,
) -> None:
    site = tmp_path / "venv" / "Lib" / "site-packages"
    site.mkdir(parents=True)
    distribution = _fake_installed_distribution(
        site,
        "example-runtime",
        "1.0",
        files={"example-startup.pth": b"import unreviewed_runtime_hook\n"},
    )

    with pytest.raises(BundleIntegrityError, match="startup hook.*not explicitly"):
        _verify_site_package_ownership([distribution])


def test_installed_dependency_record_preserves_reviewed_hook_and_wrapper(
    tmp_path: Path,
) -> None:
    site = tmp_path / "venv" / "Lib" / "site-packages"
    site.mkdir(parents=True)
    pywin32_hook = (
        b"# .pth file for the PyWin32 extensions\r\n"
        b"win32\r\n"
        b"win32\\lib\r\n"
        b"pythonwin\r\n"
        b"# And some hackery to deal with environments where the post_install "
        b"script\r\n"
        b"# isn't run.\r\n"
        b"import pywin32_bootstrap\r\n"
    )
    distribution = _fake_installed_distribution(
        site,
        "pywin32",
        "312",
        files={
            "pywin32.pth": pywin32_hook,
            "pywin32-312.dist-info/entry_points.txt": (
                b"[console_scripts]\n"
                b"pywin32_postinstall = win32.scripts.pywin32_postinstall:main\n"
            ),
            "../../Scripts/pywin32_postinstall.exe": b"reviewed-wrapper",
        },
    )

    _verify_site_package_ownership([distribution])


def _assert_hash_locked(path: Path, required_names: set[str]) -> None:
    text = path.read_text(encoding="utf-8")
    assert "--hash=sha256:" in text
    assert "git+" not in text and " @ http" not in text
    locked = {
        match.group(1).lower().replace("_", "-")
        for match in re.finditer(r"(?m)^([A-Za-z0-9_.-]+)==[^\\\s]+", text)
    }
    assert required_names <= locked


def test_runtime_test_quality_faust_and_build_locks_are_complete() -> None:
    project = tomllib.loads(
        (ROOT / "harness" / "pyproject.toml").read_text(encoding="utf-8")
    )
    build_requires = project["build-system"]["requires"]
    assert build_requires == ["setuptools==80.9.0", "wheel==0.45.1"]

    runtime_names = {
        re.match(r"[A-Za-z0-9_.-]+", dependency).group(0).lower().replace("_", "-")
        for dependency in project["project"]["dependencies"]
    }
    _assert_hash_locked(REQUIREMENTS / "runtime.lock", runtime_names)
    _assert_hash_locked(REQUIREMENTS / "test.lock", runtime_names | {"pytest"})
    _assert_hash_locked(
        REQUIREMENTS / "quality.lock",
        runtime_names | {"mypy", "pip-audit", "pytest", "pytest-cov", "ruff", "uv"},
    )
    _assert_hash_locked(
        REQUIREMENTS / "faust.lock", runtime_names | {"fusion360-mcp-server"}
    )
    _assert_hash_locked(REQUIREMENTS / "build.lock", {"setuptools", "wheel"})


def test_dependency_lock_rejects_recursive_or_index_directives(tmp_path: Path) -> None:
    digest = "a" * 64
    for directive in (
        "--requirement injected.lock",
        "--index-url https://packages.invalid/simple",
        "--find-links file:///unreviewed",
        f"example==1.0 --hash=sha256:{digest} --index-url https://packages.invalid/simple",
    ):
        lock = tmp_path / "runtime.lock"
        body = (
            f"{directive}\n"
            if directive.startswith("example==")
            else f"{directive}\nexample==1.0 --hash=sha256:{digest}\n"
        )
        lock.write_text(body, encoding="utf-8")
        with pytest.raises(BundleIntegrityError, match="unsupported option"):
            _locked_requirements(lock)


def test_preinstall_missing_wheel_fails_as_typed_json(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-B",
            str(ROOT / "scripts" / "preinstall_verify.py"),
            "--plugin-root",
            str(tmp_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert completed.stderr == ""
    assert json.loads(completed.stdout) == {
        "ok": False,
        "error_code": "BUNDLE_INTEGRITY_FAILED",
        "message": "expected exactly one bundled wheel, found 0",
    }


def test_preinstall_requires_dependency_wheelhouse_for_installed_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        preinstall_verify,
        "verify_wheel",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        preinstall_verify,
        "expected_version_from_checkout",
        lambda _root: "0.4.1",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "preinstall_verify.py",
            "--plugin-root",
            str(tmp_path),
            "--wheel",
            str(tmp_path / "project.whl"),
            "--verify-installed",
        ],
    )

    assert preinstall_verify.main() == 1
    assert json.loads(capsys.readouterr().out) == {
        "ok": False,
        "error_code": "BUNDLE_INTEGRITY_FAILED",
        "message": "--dependency-wheelhouse is required with --verify-installed",
    }


def test_installed_metadata_is_discovered_without_site_initialization() -> None:
    site_packages = _installed_site_packages()
    names = {
        str(distribution.metadata["Name"] or "").lower()
        for distribution in metadata.distributions(path=[str(site_packages)])
    }

    assert "fusion-agent-harness" in names


def test_setup_installs_only_hash_locked_dependencies_and_verified_wheel() -> None:
    powershell = (ROOT / "scripts" / "setup.ps1").read_text(encoding="utf-8")
    shell = (ROOT / "scripts" / "setup.sh").read_text(encoding="utf-8")

    for source in (powershell, shell):
        assert "runtime.lock" in source
        assert "faust.lock" in source
        assert "build.lock" in source
        assert "--require-hashes" in source
        assert "--no-deps" in source
        assert "fusion360-mcp-server==0.1.0" not in source
    assert "--no-build-isolation" in powershell
    assert "--no-build-isolation" in shell
    assert (
        '$Python -I -S -B (Join-Path $PluginRoot "scripts\\preinstall_verify.py")'
        in powershell
    )
    assert '"$PYTHON" -I -S -B "$PLUGIN_ROOT/scripts/preinstall_verify.py"' in shell
    assert "'script' not in tool.inputSchema.get('properties', {})" in powershell
    assert "'script' not in tool.inputSchema.get('properties', {})" in shell


def test_workflows_bind_privileged_execution_and_use_locked_installs() -> None:
    ci = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    release = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    nightly = (WORKFLOWS / "fusion-real-nightly.yml").read_text(encoding="utf-8")

    assert "pip install -e" not in ci + release + nightly
    quality_job = ci.split("quality-and-policy:", 1)[1]
    assert quality_job.index(
        "Verify wheel and dependency locks before quality installation"
    ) < quality_job.index("Install frozen quality environment")
    assert ci.count("--require-hashes") >= 2
    assert "test.lock" in ci and "quality.lock" in ci
    assert "--no-deps" in ci
    assert "test.lock" in release and "--require-hashes" in release
    assert "--no-deps" in release
    assert "python -I -S -B - <<'PY'" in release
    assert (
        "python -I -S -B scripts/preinstall_verify.py --plugin-root . --verify-installed"
        in release
    )
    assert release.index(
        "Verify installed wheel and exact dependency set before import"
    ) < release.index("Run complete test suite")

    assert "environment: fusion-real-nightly" in nightly
    assert "Tee-Object" not in nightly
    assert "doctor *> nightly-private/doctor.json" in nightly
    assert "tools probe *> nightly-private/probe.json" in nightly
    assert "FUSION_AGENT_RELEASE_CANDIDATE_SHA" in nightly
    assert "github.event.repository.default_branch" in nightly
    assert "ref: ${{ needs.preflight.outputs.candidate_sha }}" in nightly
    assert "git rev-parse HEAD" in nightly
    assert "test.lock" in nightly and "--require-hashes" in nightly
    assert "--no-deps" in nightly
    assert (
        "python -I -S -B scripts/preinstall_verify.py --plugin-root . --verify-installed"
        in nightly
    )
    assert nightly.index(
        "Verify installed wheel and exact dependency set"
    ) < nightly.index("Doctor")

    assert "candidate_sha:" in release
    assert "CANDIDATE_SHA" in release
    assert "git ls-remote" in release
    assert "refs/tags/${GITHUB_REF_NAME}^{}" in release
    assert release.index("git ls-remote") < release.index("gh release create")


@pytest.mark.parametrize(
    "url",
    (
        "https://fusion-data.example.test/mcp?token",
        "https://fusion-data.example.test/mcp?access_token=",
    ),
)
def test_plugin_validator_rejects_blank_sensitive_oauth_query(url: str) -> None:
    errors: list[str] = []
    _check_fusion_data(
        {
            "mcpServers": {
                "fusion_data": {
                    "url": url,
                    "auth": "oauth",
                    "enabled": True,
                    "required": False,
                    "default_tools_approval_mode": "writes",
                }
            }
        },
        {},
        errors,
    )
    assert errors == [
        "fusion_data.url must not contain token or secret query parameters"
    ]
