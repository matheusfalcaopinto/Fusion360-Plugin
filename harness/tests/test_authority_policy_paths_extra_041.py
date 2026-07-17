from __future__ import annotations

import json
import stat
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_core import authority
from agent_core.authority import (
    AuthorityBroker,
    AuthorityDeniedError,
    AuthorityPolicy,
    BindingProof,
    CadTargetBinding,
    CapabilityLedger,
)
from cad_spec.v2 import CadSpecV2


def _analysis_spec() -> CadSpecV2:
    return CadSpecV2.model_validate(
        {
            "cad_spec_version": "2.0",
            "intent": "Measure one authority-neutral fixture",
            "requirements": [
                {
                    "id": "measured",
                    "description": "The fixture mass is measured",
                    "assertion_ids": ["mass_range"],
                }
            ],
            "operations": [
                {
                    "id": "measure_fixture",
                    "kind": "analysis.physical_properties",
                    "target_refs": ["fixture"],
                    "output_ref": "mass_report",
                    "requirement_ids": ["measured"],
                }
            ],
            "assertions": [
                {
                    "id": "mass_range",
                    "kind": "physical_property_range",
                    "target_ref": "mass_report",
                    "expected": {"min_kg": 0.0, "max_kg": 10.0},
                }
            ],
        }
    )


def _import_spec() -> CadSpecV2:
    return CadSpecV2.model_validate(
        {
            "cad_spec_version": "2.0",
            "intent": "Import one approved fixture",
            "requirements": [
                {
                    "id": "imported",
                    "description": "The fixture is imported",
                    "assertion_ids": ["component_exists"],
                }
            ],
            "operations": [
                {
                    "id": "import_fixture",
                    "kind": "io.import",
                    "file_ref": {
                        "root_id": "approved-imports",
                        "relative_path": "fixture.step",
                    },
                    "format": "step",
                    "component_name": "ImportedFixture",
                    "requirement_ids": ["imported"],
                }
            ],
            "assertions": [
                {
                    "id": "component_exists",
                    "kind": "entity_exists",
                    "target_ref": "ImportedFixture",
                }
            ],
        }
    )


def _import_policy(tmp_path: Path) -> AuthorityPolicy:
    import_root = tmp_path / "imports"
    import_root.mkdir()
    (import_root / "fixture.step").write_bytes(b"STEP fixture")
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
                "export_roots": [],
                "allow_overwrite": False,
                "capability_ttl_seconds": 1800,
            }
        ),
        encoding="utf-8",
    )
    return AuthorityPolicy.load(policy_path)


def _document_binding() -> CadTargetBinding:
    return CadTargetBinding(
        reference_kind="active_document",
        requested_ref="active_document",
        document_identity="d" * 64,
        entity_identity="e" * 64,
        fingerprint="f" * 64,
    )


class _UnavailableLedgerRoot:
    def glob(self, pattern: str) -> tuple[Path, ...]:
        del pattern
        raise OSError("ledger storage unavailable")


def test_startup_reconciliation_ignores_untrusted_records_and_fails_closed(
    tmp_path: Path,
) -> None:
    ledger_root = tmp_path / "ledger"
    ledger_root.mkdir()
    (ledger_root / "directory.json").mkdir()
    malformed = ledger_root / "malformed.json"
    malformed.write_text("{not-json", encoding="utf-8")
    non_object = ledger_root / "non-object.json"
    non_object.write_text("[]", encoding="utf-8")

    ledger = CapabilityLedger(ledger_root)
    assert ledger.reconcile_startup() == 0
    assert malformed.read_text(encoding="utf-8") == "{not-json"
    assert non_object.read_text(encoding="utf-8") == "[]"

    unavailable = CapabilityLedger()
    unavailable.root = _UnavailableLedgerRoot()  # type: ignore[assignment]
    with pytest.raises(
        AuthorityDeniedError, match="ledger reconciliation is unavailable"
    ):
        unavailable.reconcile_startup()


def test_analysis_graph_remains_authority_neutral_and_rejects_injected_grants() -> None:
    broker = AuthorityBroker(AuthorityPolicy.deny_all(), ledger=CapabilityLedger())
    spec = _analysis_spec()

    graph = broker.prepare_graph(
        spec,
        session_id="analysis-session",
        provider="analysis-provider",
    )
    bound = graph.operations[0]
    assert bound.host_path is None
    assert bound.target_bindings == ()
    assert bound.proof is None
    assert bound.capability is None
    broker.validate(bound)

    just_in_time = broker.prepare_operation(
        spec,
        spec.operations[0],
        session_id="analysis-session",
        provider="analysis-provider",
    )
    assert just_in_time.proof is None
    assert just_in_time.capability is None

    broker.complete(bound, outcome="consumed")
    broker.revoke(bound)
    broker.fail(bound, outcome_unknown=True)
    with pytest.raises(AuthorityDeniedError, match="no capability"):
        broker.claim(bound)

    injected = replace(
        bound,
        proof=BindingProof(algorithm="sha256", digest="a" * 64),
    )
    with pytest.raises(AuthorityDeniedError, match="read-only operation"):
        broker.validate(injected)


@pytest.mark.parametrize(
    "operation_update",
    [
        {"id": "outside-graph"},
        {"output_ref": "substituted_report"},
    ],
)
def test_just_in_time_operation_must_be_the_exact_validated_graph_member(
    operation_update: dict[str, str],
) -> None:
    broker = AuthorityBroker(AuthorityPolicy.deny_all(), ledger=CapabilityLedger())
    spec = _analysis_spec()
    substituted = spec.operations[0].model_copy(update=operation_update)

    with pytest.raises(AuthorityDeniedError, match="outside the validated graph"):
        broker.prepare_operation(
            spec,
            substituted,
            session_id="analysis-session",
            provider="analysis-provider",
        )


def test_import_capability_cannot_bypass_its_bound_host_path(tmp_path: Path) -> None:
    broker = AuthorityBroker(_import_policy(tmp_path), ledger=CapabilityLedger())
    spec = _import_spec()
    graph = broker.prepare_graph(
        spec,
        session_id="import-session",
        provider="import-provider",
        target_bindings_by_operation={"import_fixture": (_document_binding(),)},
    )
    bound = graph.operations[0]
    assert bound.host_path is not None
    assert bound.capability is not None
    assert bound.proof is not None
    broker.validate(bound)

    with pytest.raises(AuthorityDeniedError, match="requires a bound host path"):
        broker.validate(replace(bound, host_path=None))


def test_resource_fingerprint_rejects_open_and_non_regular_resource_bypasses(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "fixture.step"
    source.write_bytes(b"STEP fixture")
    assert (
        len(authority._resource_fingerprint(source, direction="import", existed=True))
        == 64
    )

    def unavailable_open(*_args: Any, **_kwargs: Any) -> int:
        raise OSError("open denied")

    monkeypatch.setattr(authority.os, "open", unavailable_open)
    with pytest.raises(AuthorityDeniedError, match="opened safely"):
        authority._resource_fingerprint(source, direction="import", existed=True)

    monkeypatch.setattr(authority.os, "open", lambda *_args, **_kwargs: 73)
    monkeypatch.setattr(
        authority.os,
        "fstat",
        lambda _descriptor: SimpleNamespace(st_mode=stat.S_IFDIR),
    )
    monkeypatch.setattr(authority.os, "close", lambda _descriptor: None)
    with pytest.raises(AuthorityDeniedError, match="regular file"):
        authority._resource_fingerprint(source, direction="import", existed=True)


def test_resource_fingerprint_rejects_path_loss_after_descriptor_binding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "fixture.step"
    source.write_bytes(b"STEP fixture")
    path_type = type(source)
    original_lstat = path_type.lstat

    def unavailable_lstat(path: Path, *args: Any, **kwargs: Any):
        if path == source:
            raise OSError("path disappeared")
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(path_type, "lstat", unavailable_lstat)
    with pytest.raises(AuthorityDeniedError, match="changed during binding"):
        authority._resource_fingerprint(source, direction="export", existed=True)


@pytest.mark.parametrize("drift", ["identity", "metadata"])
def test_resource_fingerprint_rejects_descriptor_identity_or_metadata_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    drift: str,
) -> None:
    source = tmp_path / "fixture.step"
    source.write_bytes(b"STEP fixture")
    baseline = source.stat()
    before = SimpleNamespace(
        st_mode=baseline.st_mode,
        st_dev=baseline.st_dev,
        st_ino=baseline.st_ino,
        st_size=baseline.st_size,
        st_mtime_ns=baseline.st_mtime_ns,
    )
    after = SimpleNamespace(
        st_mode=baseline.st_mode,
        st_dev=baseline.st_dev,
        st_ino=baseline.st_ino + (1 if drift == "identity" else 0),
        st_size=baseline.st_size + (1 if drift == "metadata" else 0),
        st_mtime_ns=baseline.st_mtime_ns,
    )
    snapshots = iter((before, after))
    monkeypatch.setattr(authority.os, "fstat", lambda _descriptor: next(snapshots))

    with pytest.raises(AuthorityDeniedError, match="changed during binding"):
        authority._resource_fingerprint(source, direction="export", existed=True)
