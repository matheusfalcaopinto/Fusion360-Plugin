from __future__ import annotations

import json
import os
import string
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from agent_core import authority as authority_module
from agent_core.authority import (
    AuthorityBroker,
    AuthorityDeniedError,
    AuthorityPolicy,
    CadTargetBinding,
    CapabilityLedger,
    revalidate_host_path,
)
from cad_spec.v2 import CadSpecV2


def _prepare_graph(broker: AuthorityBroker, spec: CadSpecV2, **kwargs):
    targets = {
        operation.id: (
            CadTargetBinding(
                reference_kind="export_target",
                requested_ref=str(operation.target_ref),
                document_identity="document:bypass-fixture",
                entity_identity="entity:bypass-fixture",
                fingerprint="b" * 64,
            ),
        )
        for operation in spec.operations
        if operation.kind == "io.export"
    }
    return broker.prepare_graph(spec, target_bindings_by_operation=targets, **kwargs)


def _policy(
    tmp_path: Path,
    *,
    ttl: int = 1800,
    allow_overwrite: bool = False,
) -> tuple[AuthorityPolicy, Path, Path]:
    import_root = tmp_path / "imports"
    export_root = tmp_path / "exports"
    import_root.mkdir()
    export_root.mkdir()
    policy_path = tmp_path / "authority.json"
    policy_path.write_text(
        json.dumps(
            {
                "schema_version": "fusion_agent.authority_policy.v1",
                "import_roots": [
                    {
                        "id": "approved-imports",
                        "path": str(import_root),
                        "formats": ["step"],
                        "default": True,
                    }
                ],
                "export_roots": [
                    {
                        "id": "approved-exports",
                        "path": str(export_root),
                        "formats": ["step"],
                        "default": True,
                    }
                ],
                "allow_overwrite": allow_overwrite,
                "capability_ttl_seconds": ttl,
            }
        ),
        encoding="utf-8",
    )
    return AuthorityPolicy.load(policy_path), import_root, export_root


def _export_spec(
    *,
    path: str | None = None,
    relative_path: str | None = None,
    overwrite: bool = False,
) -> CadSpecV2:
    operation: dict[str, object] = {
        "id": "export_part",
        "kind": "io.export",
        "target_ref": "part_body",
        "format": "step",
        "overwrite": overwrite,
        "requirement_ids": ["exported"],
    }
    if path is not None:
        operation["path"] = path
    if relative_path is not None:
        operation["file_ref"] = {
            "root_id": "approved-exports",
            "relative_path": relative_path,
        }
    return CadSpecV2.model_validate(
        {
            "cad_spec_version": "2.0",
            "intent": "Export one authority-bound artifact",
            "requirements": [
                {
                    "id": "exported",
                    "description": "artifact exists",
                    "assertion_ids": ["export_exists"],
                }
            ],
            "operations": [operation],
            "assertions": [
                {
                    "id": "export_exists",
                    "kind": "export_exists",
                    "target_ref": "part_body",
                }
            ],
        }
    )


def _import_spec(*, relative_path: str) -> CadSpecV2:
    return CadSpecV2.model_validate(
        {
            "cad_spec_version": "2.0",
            "intent": "Import one authority-bound artifact",
            "requirements": [
                {
                    "id": "imported",
                    "description": "component exists",
                    "assertion_ids": ["component_exists"],
                }
            ],
            "operations": [
                {
                    "id": "import_part",
                    "kind": "io.import",
                    "file_ref": {
                        "root_id": "approved-imports",
                        "relative_path": relative_path,
                    },
                    "format": "step",
                    "component_name": "ImportedPart",
                    "requirement_ids": ["imported"],
                }
            ],
            "assertions": [
                {
                    "id": "component_exists",
                    "kind": "entity_exists",
                    "target_ref": "ImportedPart",
                }
            ],
        }
    )


def _prepared_export(
    tmp_path: Path,
    *,
    clock=None,
    ttl: int = 1800,
) -> tuple[AuthorityBroker, object, Path]:
    policy, _import_root, export_root = _policy(tmp_path, ttl=ttl)
    broker = AuthorityBroker(
        policy,
        ledger=CapabilityLedger(tmp_path / "ledger"),
        **({"clock": clock} if clock is not None else {}),
    )
    graph = _prepare_graph(
        broker,
        _export_spec(relative_path="part.step"),
        session_id="session-one",
        provider="autodesk_http",
    )
    return broker, graph.operations[0], export_root / "part.step"


def test_mixed_windows_and_posix_separators_resolve_to_one_logical_path(
    tmp_path: Path,
) -> None:
    policy, _import_root, export_root = _policy(tmp_path)
    (export_root / "nested" / "folder").mkdir(parents=True)
    broker = AuthorityBroker(policy, ledger=CapabilityLedger(tmp_path / "ledger"))

    graph = _prepare_graph(
        broker,
        _export_spec(relative_path=r"nested\folder/part.step"),
        session_id="mixed-separators",
        provider="autodesk_http",
    )

    binding = graph.operations[0].host_path
    assert binding is not None
    assert binding.canonical_path == str(
        (export_root / "nested" / "folder" / "part.step").resolve()
    )
    assert binding.relative_path == "nested/folder/part.step"


@pytest.mark.parametrize(
    "requested_path",
    [
        r"nested\..//..\escape.step",
        r"C:drive-relative.step",
        r"\\?\C:\device.step",
        r"\\.\C:\device.step",
        r"\??\C:\device.step",
        "//?/C:/device.step",
    ],
)
def test_traversal_drive_relative_and_device_namespaces_are_denied(
    tmp_path: Path,
    requested_path: str,
) -> None:
    policy, _import_root, _export_root = _policy(tmp_path)
    broker = AuthorityBroker(policy, ledger=CapabilityLedger(tmp_path / "ledger"))

    with pytest.raises(AuthorityDeniedError):
        _prepare_graph(
            broker,
            _export_spec(path=requested_path),
            session_id="special-path-denied",
            provider="autodesk_http",
        )


def test_unapproved_unc_is_rejected_before_any_filesystem_resolution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy, _import_root, _export_root = _policy(tmp_path)
    broker = AuthorityBroker(policy, ledger=CapabilityLedger(tmp_path / "ledger"))

    def unexpected_resolution(*_args, **_kwargs):
        raise AssertionError("unapproved UNC reached filesystem canonicalization")

    monkeypatch.setattr(authority_module, "_canonical_target", unexpected_resolution)
    with pytest.raises(AuthorityDeniedError, match="UNC"):
        _prepare_graph(
            broker,
            _export_spec(path=r"\\fusion-agent-invalid\share\escape.step"),
            session_id="unc-denied",
            provider="autodesk_http",
        )


def test_symlink_escape_is_denied(tmp_path: Path) -> None:
    policy, _import_root, export_root = _policy(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = export_root / "escape-link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")
    broker = AuthorityBroker(policy, ledger=CapabilityLedger(tmp_path / "ledger"))

    with pytest.raises(AuthorityDeniedError, match="outside"):
        _prepare_graph(
            broker,
            _export_spec(relative_path="escape-link/part.step"),
            session_id="symlink-escape",
            provider="autodesk_http",
        )


@pytest.mark.skipif(
    os.name != "nt", reason="filesystem case folding is Windows-specific"
)
def test_existing_import_rejects_case_mismatch_on_windows(tmp_path: Path) -> None:
    policy, import_root, _export_root = _policy(tmp_path)
    source = import_root / "CaseSensitivePart.step"
    source.write_text("part", encoding="utf-8")
    broker = AuthorityBroker(policy, ledger=CapabilityLedger(tmp_path / "ledger"))

    with pytest.raises(AuthorityDeniedError, match="case"):
        _prepare_graph(
            broker,
            _import_spec(relative_path="casesensitivepart.step"),
            session_id="case-mismatch",
            provider="autodesk_http",
        )


def test_capability_expires_at_exact_ttl_boundary(tmp_path: Path) -> None:
    now = [100.0]
    broker, bound, _destination = _prepared_export(
        tmp_path,
        ttl=2,
        clock=lambda: now[0],
    )
    capability = bound.capability
    assert capability is not None

    now[0] = 102.0
    with pytest.raises(AuthorityDeniedError, match="expired"):
        broker.claim(bound)
    assert broker.ledger.state(capability.capability_id) == "expired"


@pytest.mark.parametrize(
    "field", ["spec_digest", "operation_digest", "session_id", "provider"]
)
def test_bound_graph_identity_mismatch_revokes_without_claim(
    tmp_path: Path,
    field: str,
) -> None:
    broker, bound, _destination = _prepared_export(tmp_path)
    capability = bound.capability
    assert capability is not None
    tampered = replace(bound, **{field: f"mismatched-{field}"})

    with pytest.raises(AuthorityDeniedError):
        broker.claim(tampered)
    assert broker.ledger.state(capability.capability_id) == "revoked"


@pytest.mark.parametrize(
    "field", ["spec_digest", "operation_digest", "session_id", "provider"]
)
def test_capability_identity_mismatch_revokes_stored_grant(
    tmp_path: Path,
    field: str,
) -> None:
    broker, bound, _destination = _prepared_export(tmp_path)
    capability = bound.capability
    assert capability is not None
    tampered_capability = replace(
        capability,
        **{field: f"mismatched-{field}"},
    )
    tampered = replace(bound, capability=tampered_capability)

    with pytest.raises(AuthorityDeniedError):
        broker.claim(tampered)
    assert broker.ledger.state(capability.capability_id) == "revoked"


def test_persistent_claim_is_atomic_across_broker_instances(tmp_path: Path) -> None:
    policy, _import_root, _export_root = _policy(tmp_path)
    ledger_root = tmp_path / "ledger"
    issuing_broker = AuthorityBroker(policy, ledger=CapabilityLedger(ledger_root))
    graph = _prepare_graph(
        issuing_broker,
        _export_spec(relative_path="part.step"),
        session_id="concurrent-claim",
        provider="autodesk_http",
    )
    bound = graph.operations[0]
    capability = bound.capability
    assert capability is not None
    competing_broker = AuthorityBroker(policy, ledger=CapabilityLedger(ledger_root))
    barrier = threading.Barrier(2)

    def claim(broker: AuthorityBroker) -> bool:
        barrier.wait(timeout=5)
        try:
            broker.claim(bound)
        except AuthorityDeniedError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(claim, (issuing_broker, competing_broker), timeout=10)
        )

    assert sorted(results) == [False, True]
    assert issuing_broker.ledger.state(capability.capability_id) == "claimed"
    issuing_broker.complete(bound, outcome="consumed")


def test_sink_revalidation_rejects_destination_created_after_claim(
    tmp_path: Path,
) -> None:
    broker, bound, destination = _prepared_export(tmp_path)
    capability = bound.capability
    binding = bound.host_path
    assert capability is not None
    assert binding is not None
    broker.claim(bound)
    destination.write_text("raced destination", encoding="utf-8")

    with pytest.raises(AuthorityDeniedError, match="existence changed"):
        revalidate_host_path(binding)
    assert broker.ledger.state(capability.capability_id) == "claimed"
    broker.fail(bound, outcome_unknown=False)
    assert broker.ledger.state(capability.capability_id) == "revoked"


def _different_volume_root(local_root: Path) -> Path | None:
    local_device = local_root.stat().st_dev
    candidates: list[Path]
    if os.name == "nt":
        candidates = [Path(f"{letter}:\\") for letter in string.ascii_uppercase]
    else:
        candidates = [Path("/dev/shm"), Path("/run"), Path("/mnt"), Path("/")]
    for candidate in candidates:
        try:
            if candidate.is_dir() and candidate.stat().st_dev != local_device:
                return candidate.resolve()
        except OSError:
            continue
    return None


def test_sink_revalidation_rejects_parent_swapped_to_different_volume(
    tmp_path: Path,
) -> None:
    policy, _import_root, export_root = _policy(tmp_path)
    stable_parent = export_root / "stable"
    stable_parent.mkdir()
    broker = AuthorityBroker(policy, ledger=CapabilityLedger(tmp_path / "ledger"))
    graph = _prepare_graph(
        broker,
        _export_spec(relative_path="stable/part.step"),
        session_id="volume-change",
        provider="autodesk_http",
    )
    bound = graph.operations[0]
    binding = bound.host_path
    assert binding is not None
    other_volume = _different_volume_root(export_root)
    if other_volume is None:
        pytest.skip("no accessible secondary filesystem volume")

    original_parent = export_root / "stable-original"
    stable_parent.rename(original_parent)
    try:
        try:
            stable_parent.symlink_to(other_volume, target_is_directory=True)
        except (NotImplementedError, OSError) as exc:
            pytest.skip(f"cross-volume directory symlink unavailable: {exc}")
        with pytest.raises(AuthorityDeniedError):
            revalidate_host_path(binding)
    finally:
        if stable_parent.is_symlink():
            stable_parent.unlink()
        original_parent.rename(stable_parent)
