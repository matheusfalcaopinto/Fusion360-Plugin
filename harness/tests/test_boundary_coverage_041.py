from __future__ import annotations

import ast
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_core.request_context import (
    RequestContext,
    bind_request_context,
    current_request_context,
    require_request_context,
)
from benchmark import filesystem
from benchmark import provenance
from benchmark.provenance import RevisionIdentity
from cad_spec.models import CadSpec
from cad_spec.transactions import normalize_transactions


def test_async_benchmark_boundaries_do_not_read_process_environment() -> None:
    harness_root = Path(__file__).resolve().parents[1]
    paths = [
        harness_root / "packages" / "benchmark" / name
        for name in (
            "codex_driver.py",
            "artifacts.py",
            "runner.py",
            "public.py",
            "provenance.py",
            "real_capability_packs.py",
        )
    ]
    violations: list[str] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for function in (
            node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef)
        ):
            for node in ast.walk(function):
                if not isinstance(node, ast.Attribute):
                    continue
                if (
                    isinstance(node.value, ast.Name)
                    and node.value.id == "os"
                    and node.attr in {"getenv", "environ"}
                ):
                    violations.append(
                        f"{path.name}:{function.name}:{node.lineno}:os.{node.attr}"
                    )
    assert violations == []


def _request_context(**overrides: object) -> RequestContext:
    values: dict[str, object] = {
        "request_id": "request-coverage",
        "profile": "normal",
        "mode": "mock",
        "backend": "mock",
    }
    values.update(overrides)
    return RequestContext(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["request_id", "profile", "mode", "backend"])
@pytest.mark.parametrize("value", ["", "   ", None, 7])
def test_request_context_rejects_missing_required_identity(
    field: str, value: object
) -> None:
    with pytest.raises(ValueError, match=f"{field} must be a non-empty string"):
        _request_context(**{field: value})


@pytest.mark.parametrize(
    ("timeouts", "message"),
    [
        ({"": 1}, "timeout names"),
        ({7: 1}, "timeout names"),
        ({"operation": True}, "must be numeric"),
        ({"operation": "1"}, "must be numeric"),
        ({"operation": -0.1}, "finite and non-negative"),
        ({"operation": float("nan")}, "finite and non-negative"),
        ({"operation": float("inf")}, "finite and non-negative"),
    ],
)
def test_request_context_rejects_ambiguous_timeouts(
    timeouts: dict[object, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _request_context(timeouts=timeouts)


@pytest.mark.parametrize("capabilities", [("",), (7,), ("safe", None)])
def test_request_context_rejects_invalid_capabilities(
    capabilities: tuple[object, ...],
) -> None:
    with pytest.raises(ValueError, match="capabilities must be non-empty strings"):
        _request_context(capabilities=capabilities)


def test_request_context_require_and_bind_fail_closed() -> None:
    assert current_request_context() is None
    with pytest.raises(RuntimeError, match="not bound"):
        require_request_context()
    with pytest.raises(TypeError, match="must be RequestContext"):
        with bind_request_context("forged"):  # type: ignore[arg-type]
            pass

    context = _request_context(timeouts={"operation": 0, "read": 1.25})
    with bind_request_context(context) as bound:
        assert bound is context
        assert require_request_context() is context
        assert dict(context.timeouts) == {"operation": 0.0, "read": 1.25}
    assert current_request_context() is None


def test_filesystem_boundary_lists_reads_replaces_and_removes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    filesystem.mkdir(source)
    filesystem.atomic_write_text(source / "b.json", "b\r\n")
    filesystem.atomic_write_text(source / "a.txt", "a")

    assert filesystem.path_exists(source / "b.json")
    assert filesystem.path_is_dir(source)
    assert filesystem.read_text(source / "b.json") == "b\n"
    assert [path.name for path in filesystem.list_files(source)] == ["a.txt", "b.json"]
    assert [path.name for path in filesystem.list_files(source, suffix=".json")] == [
        "b.json"
    ]

    destination = source / "published.json"
    filesystem.replace(source / "b.json", destination)
    assert filesystem.read_text(destination) == "b\n"
    filesystem.rmtree(source)
    assert not filesystem.path_exists(source)


def test_filesystem_io_path_handles_windows_namespaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(filesystem.os, "name", "nt")
    monkeypatch.setattr(
        filesystem.os.path, "abspath", lambda _: r"\\server\share\artifact.json"
    )
    assert filesystem.io_path("ignored") == r"\\?\UNC\server\share\artifact.json"

    monkeypatch.setattr(
        filesystem.os.path, "abspath", lambda _: r"\\?\C:\already-extended.json"
    )
    assert filesystem.io_path("ignored") == r"\\?\C:\already-extended.json"

    monkeypatch.setattr(filesystem.os, "name", "posix")
    monkeypatch.setattr(filesystem.os.path, "abspath", lambda _: "/tmp/artifact.json")
    assert filesystem.io_path("ignored") == "/tmp/artifact.json"


def test_atomic_write_rejects_parent_identity_drift_and_cleans_temp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    identities = iter([(1, 2, "before"), (1, 3, "after")])
    monkeypatch.setattr(filesystem, "_directory_identity", lambda _: next(identities))
    destination = tmp_path / "drift" / "result.json"

    with pytest.raises(OSError, match="parent changed"):
        filesystem.atomic_write_text(destination, "secret")

    assert not destination.exists()
    assert list(destination.parent.glob(".tmp-*.tmp")) == []


def test_atomic_write_rejects_cross_volume_temp_and_cleans_temp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    destination = tmp_path / "volume" / "result.json"
    monkeypatch.setattr(
        filesystem,
        "_directory_identity",
        lambda _: (-1, 2, "same-parent"),
    )

    with pytest.raises(OSError, match="changed volume"):
        filesystem.atomic_write_text(destination, "secret")

    assert not destination.exists()
    assert list(destination.parent.glob(".tmp-*.tmp")) == []


def test_atomic_write_tolerates_temp_already_removed_during_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    destination = tmp_path / "publish" / "result.json"

    def fail_after_removing(source: Path | str, _: Path | str) -> None:
        os.unlink(filesystem.io_path(source))
        raise OSError("replace failed")

    monkeypatch.setattr(filesystem, "replace", fail_after_removing)
    with pytest.raises(OSError, match="replace failed"):
        filesystem.atomic_write_text(destination, "secret")
    assert not destination.exists()


def test_directory_identity_rejects_non_directory(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    target.write_text("data", encoding="utf-8")
    with pytest.raises(NotADirectoryError):
        filesystem._directory_identity(filesystem.io_path(target))


@pytest.mark.parametrize("suffix", ["json", ".", ".waytoolong11", ".bad/path"])
def test_physical_artifact_name_rejects_unsafe_suffix(suffix: str) -> None:
    with pytest.raises(ValueError, match="simple extension"):
        filesystem.physical_artifact_name("logical", suffix=suffix)


def test_physical_artifact_name_is_short_stable_and_never_empty() -> None:
    first = filesystem.physical_artifact_name("../ unsafe logical identifier !!!")
    second = filesystem.physical_artifact_name("../ unsafe logical identifier !!!")
    empty = filesystem.physical_artifact_name("...---")
    assert first == second
    assert len(first) <= 20 + 1 + 32 + len(".json")
    assert "unsafe-logical-ident" in first
    assert empty.startswith("artifact-")


def test_revision_identity_exactness_and_mismatch_decisions() -> None:
    commit = "a" * 40
    digest = "b" * 64
    exact = RevisionIdentity(
        expected_git_commit=commit,
        observed_git_commit=commit,
        expected_source_manifest_sha256=digest,
        observed_source_manifest_sha256=digest,
        tracked_state="clean",
    )
    dirty = exact.model_copy(update={"tracked_state": "dirty"})
    mismatch = exact.model_copy(update={"observed_git_commit": "c" * 40})
    incomplete = RevisionIdentity(tracked_state="unavailable")

    assert exact.expected_complete and exact.exact and not exact.explicit_mismatch
    assert not dirty.exact
    assert mismatch.explicit_mismatch and not mismatch.exact
    assert not incomplete.expected_complete and not incomplete.exact


def test_collect_workspace_revision_fails_closed_and_normalizes_explicit_expectations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        provenance, "_git", lambda *_args: (_ for _ in ()).throw(OSError("no git"))
    )
    identity = provenance.collect_workspace_revision(
        tmp_path,
        expected_git_commit="A" * 40,
        expected_source_manifest_sha256="B" * 64,
    )

    assert identity.tracked_state == "unavailable"
    assert identity.expected_git_commit == "a" * 40
    assert identity.expected_source_manifest_sha256 == "b" * 64
    assert identity.observed_git_commit is None


@pytest.mark.parametrize("status", [b"", b" M tracked.py\n"])
def test_collect_workspace_revision_records_clean_or_dirty_tracked_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, status: bytes
) -> None:
    tracked = tmp_path / "tracked.py"
    tracked.write_text("print('tracked')\n", encoding="utf-8")

    def fake_git(_root: Path, *arguments: str) -> str:
        if arguments == ("rev-parse", "--show-toplevel"):
            return str(tmp_path)
        if arguments == ("rev-parse", "HEAD"):
            return "a" * 40
        raise AssertionError(arguments)

    def fake_git_bytes(_root: Path, *arguments: str) -> bytes:
        if arguments == ("ls-files", "-z"):
            return b"tracked.py\0"
        if arguments == ("status", "--porcelain=v1", "--untracked-files=no"):
            return status
        raise AssertionError(arguments)

    monkeypatch.setattr(provenance, "_git", fake_git)
    monkeypatch.setattr(provenance, "_git_bytes", fake_git_bytes)
    expected_manifest = provenance.source_manifest_digest(tmp_path, ["tracked.py"])
    identity = provenance.collect_workspace_revision(
        tmp_path,
        expected_git_commit="a" * 40,
        expected_source_manifest_sha256=expected_manifest,
    )

    assert identity.tracked_state == ("dirty" if status else "clean")
    assert identity.tracked_changes_sha256 is not None
    assert identity.exact is (not status)


def test_source_manifest_digest_binds_paths_missing_files_and_contents(
    tmp_path: Path,
) -> None:
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("first", encoding="utf-8")
    first = provenance.source_manifest_digest(
        tmp_path, ["missing.txt", "tracked.txt", "tracked.txt"]
    )
    tracked.write_text("second", encoding="utf-8")
    second = provenance.source_manifest_digest(tmp_path, ["tracked.txt", "missing.txt"])

    assert first != second
    with pytest.raises(ValueError, match="escapes workspace"):
        provenance.source_manifest_digest(tmp_path, ["../outside.txt"])


def test_git_bytes_uses_bounded_noninteractive_subprocess(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[dict[str, object]] = []

    def successful_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append({"command": command, **kwargs})
        return SimpleNamespace(returncode=0, stdout=b"result")

    monkeypatch.setattr(provenance.subprocess, "run", successful_run)
    assert provenance._git_bytes(tmp_path, "status") == b"result"
    assert calls[0]["stdin"] is subprocess.DEVNULL
    assert calls[0]["timeout"] == 10

    monkeypatch.setattr(
        provenance.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout=b"private"),
    )
    with pytest.raises(subprocess.SubprocessError, match="query failed"):
        provenance._git_bytes(tmp_path, "status")


@pytest.mark.parametrize(
    ("value", "normalizer"),
    [
        (None, provenance._normalized_commit),
        ("g" * 40, provenance._normalized_commit),
        ("a" * 39, provenance._normalized_commit),
        ("z" * 64, provenance._normalized_digest),
        ("b" * 63, provenance._normalized_digest),
    ],
)
def test_revision_normalizers_reject_malformed_values(
    value: str | None, normalizer
) -> None:
    assert normalizer(value) is None


def _transaction_spec(*, checkpoint: bool) -> CadSpec:
    return CadSpec.model_validate(
        {
            "intent": "exercise deterministic transaction normalization",
            "document_policy": {"create_checkpoint": checkpoint},
            "parameters": [{"name": "width", "expression": "10 mm"}],
            "components": [
                {
                    "name": "fixture_component",
                    "features": [
                        {
                            "name": "fixture_body_feature",
                            "type": "extrude",
                            "inputs": {"distance": "2 mm"},
                        }
                    ],
                }
            ],
            "acceptance_tests": [{"type": "body_count", "target": 1}],
        }
    )


@pytest.mark.parametrize("checkpoint", [True, False])
def test_transaction_normalization_is_ordered_and_policy_driven(
    checkpoint: bool,
) -> None:
    steps = normalize_transactions(_transaction_spec(checkpoint=checkpoint))
    names = [step.name for step in steps]

    assert names[0] == "inspect_design"
    assert names[-1] == "verify_final"
    assert ("checkpoint_policy" in names) is checkpoint
    assert "parameter_width" in names
    assert "component_fixture_component" in names
    feature = next(
        step for step in steps if step.name == "feature_fixture_body_feature"
    )
    assert feature.operation == "extrude"
    assert feature.payload["component"] == "fixture_component"
