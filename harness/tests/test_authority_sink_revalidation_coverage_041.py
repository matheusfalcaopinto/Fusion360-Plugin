from __future__ import annotations

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
    AuthorityRoot,
    BindingProof,
    BoundOperation,
    CadTargetBinding,
    CapabilityLedger,
    LegacyOutputOperation,
    OperationCapability,
)
from cad_spec.v2 import CadSpecV2, OPERATION_ADAPTER


_DOCUMENT = "d" * 64


def _operation(payload: dict[str, Any]) -> Any:
    return OPERATION_ADAPTER.validate_python(payload)


def _binding(
    reference_kind: str,
    requested_ref: str,
    *,
    document_identity: str = _DOCUMENT,
    entity_identity: str = "e" * 64,
    fingerprint: str = "f" * 64,
    producer_operation_id: str | None = None,
) -> CadTargetBinding:
    return CadTargetBinding(
        reference_kind=reference_kind,
        requested_ref=requested_ref,
        document_identity=document_identity,
        entity_identity=entity_identity,
        fingerprint=fingerprint,
        producer_operation_id=producer_operation_id,
    )


def _document_binding() -> CadTargetBinding:
    return _binding("active_document", "active_document")


def _regular_stat(
    *, inode: int = 2, size: int = 4, mtime_ns: int = 5
) -> SimpleNamespace:
    return SimpleNamespace(
        st_mode=stat.S_IFREG | 0o600,
        st_dev=1,
        st_ino=inode,
        st_size=size,
        st_mtime_ns=mtime_ns,
    )


def _spec(operations: list[dict[str, Any]]) -> CadSpecV2:
    normalized_operations = [
        {**operation, "requirement_ids": ["done"]} for operation in operations
    ]
    return CadSpecV2.model_validate(
        {
            "cad_spec_version": "2.0",
            "intent": "exercise authority graph binding",
            "requirements": [
                {
                    "id": "done",
                    "description": "operation completes",
                    "assertion_ids": ["exists"],
                }
            ],
            "operations": normalized_operations,
            "assertions": [
                {
                    "id": "exists",
                    "kind": "entity_exists",
                    "target_ref": "fixture",
                }
            ],
        }
    )


def _import_spec() -> CadSpecV2:
    return _spec(
        [
            {
                "id": "import_fixture",
                "kind": "io.import",
                "file_ref": {
                    "root_id": "approved-imports",
                    "relative_path": "fixture.step",
                },
                "format": "step",
                "component_name": "ImportedFixture",
            }
        ]
    )


def _import_broker(root: Path, *, clock: float = 10.0) -> AuthorityBroker:
    policy = AuthorityPolicy(
        schema_version="fusion_agent.authority_policy.v1",
        import_roots=(
            AuthorityRoot(
                id="approved-imports",
                canonical_path=str(root.resolve(strict=True)),
                formats=frozenset({"step"}),
                default=True,
            ),
        ),
        export_roots=(),
        capability_ttl_seconds=1800,
        digest="a" * 64,
    )
    return AuthorityBroker(policy, ledger=CapabilityLedger(), clock=lambda: clock)


def test_resource_fingerprint_rejects_open_failure_before_dispatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "fixture.step"
    path.write_bytes(b"STEP")
    dispatches: list[str] = []
    monkeypatch.setattr(
        authority.os,
        "open",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("denied")),
    )

    with pytest.raises(AuthorityDeniedError, match="opened safely"):
        authority._resource_fingerprint(path, direction="import", existed=True)

    assert dispatches == []


def test_resource_fingerprint_rejects_non_regular_resource_and_closes_handle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "fixture.step"
    path.write_bytes(b"STEP")
    closed: list[int] = []
    monkeypatch.setattr(authority.os, "open", lambda *_args, **_kwargs: 41)
    monkeypatch.setattr(
        authority.os,
        "fstat",
        lambda _descriptor: SimpleNamespace(st_mode=stat.S_IFDIR),
    )
    monkeypatch.setattr(authority.os, "close", closed.append)

    with pytest.raises(AuthorityDeniedError, match="regular file"):
        authority._resource_fingerprint(path, direction="import", existed=True)

    assert closed == [41]


def test_resource_fingerprint_rejects_lstat_failure_after_closing_handle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "fixture.step"
    path.write_bytes(b"STEP")
    closed: list[int] = []
    before = _regular_stat()
    stats = iter((before, before))
    monkeypatch.setattr(authority.os, "open", lambda *_args, **_kwargs: 42)
    monkeypatch.setattr(authority.os, "fstat", lambda _descriptor: next(stats))
    monkeypatch.setattr(authority.os, "read", lambda *_args, **_kwargs: b"")
    monkeypatch.setattr(authority.os, "close", closed.append)
    monkeypatch.setattr(
        authority.Path,
        "lstat",
        lambda _path: (_ for _ in ()).throw(OSError("vanished")),
    )

    with pytest.raises(AuthorityDeniedError, match="changed during binding"):
        authority._resource_fingerprint(path, direction="import", existed=True)

    assert closed == [42]


@pytest.mark.parametrize("changed_identity", ["descriptor", "directory_entry"])
def test_resource_fingerprint_rejects_identity_replacement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    changed_identity: str,
) -> None:
    path = tmp_path / "fixture.step"
    path.write_bytes(b"STEP")
    before = _regular_stat(inode=2)
    after = _regular_stat(inode=3 if changed_identity == "descriptor" else 2)
    current = _regular_stat(inode=3 if changed_identity == "directory_entry" else 2)
    stats = iter((before, after))
    monkeypatch.setattr(authority.os, "open", lambda *_args, **_kwargs: 43)
    monkeypatch.setattr(authority.os, "fstat", lambda _descriptor: next(stats))
    monkeypatch.setattr(authority.os, "read", lambda *_args, **_kwargs: b"")
    monkeypatch.setattr(authority.os, "close", lambda _descriptor: None)
    monkeypatch.setattr(authority.Path, "lstat", lambda _path: current)

    with pytest.raises(AuthorityDeniedError, match="changed during binding"):
        authority._resource_fingerprint(path, direction="import", existed=True)


@pytest.mark.parametrize("changed_metadata", ["size", "mtime"])
def test_resource_fingerprint_rejects_metadata_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    changed_metadata: str,
) -> None:
    path = tmp_path / "fixture.step"
    path.write_bytes(b"STEP")
    before = _regular_stat(size=4, mtime_ns=5)
    after = _regular_stat(
        size=5 if changed_metadata == "size" else 4,
        mtime_ns=6 if changed_metadata == "mtime" else 5,
    )
    stats = iter((before, after))
    monkeypatch.setattr(authority.os, "open", lambda *_args, **_kwargs: 44)
    monkeypatch.setattr(authority.os, "fstat", lambda _descriptor: next(stats))
    monkeypatch.setattr(authority.os, "read", lambda *_args, **_kwargs: b"")
    monkeypatch.setattr(authority.os, "close", lambda _descriptor: None)
    monkeypatch.setattr(authority.Path, "lstat", lambda _path: before)

    with pytest.raises(AuthorityDeniedError, match="changed during binding"):
        authority._resource_fingerprint(path, direction="import", existed=True)


def test_case_check_is_a_noop_off_windows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(authority.os, "name", "posix")

    authority._require_windows_case_exact(
        tmp_path / "Approved",
        tmp_path / "different" / "fixture.step",
        final_exists=True,
    )


def test_target_bindings_reject_every_authority_mismatch_before_dispatch() -> None:
    analysis_operation = _operation(
        {
            "id": "inspect",
            "kind": "analysis.physical_properties",
            "target_refs": ["fixture"],
            "output_ref": "properties",
        }
    )
    export_operation = _operation(
        {
            "id": "export",
            "kind": "io.export",
            "target_ref": "fixture",
            "path": "fixture.step",
            "format": "step",
        }
    )
    component_operation = _operation(
        {"id": "component", "kind": "component.create", "name": "fixture"}
    )
    sketch_operation = _operation(
        {
            "id": "sketch",
            "kind": "sketch.create",
            "component_ref": "fixture",
            "plane": "XY",
            "name": "profile",
        }
    )
    valid_export = _binding("export_target", "fixture")
    valid_component = _binding(
        "component", "fixture", producer_operation_id="component"
    )
    cases = (
        (
            analysis_operation,
            (_document_binding(),),
            None,
            "read-only operation carries CAD target authority",
        ),
        (export_operation, (), None, "exactly one resolved CAD target binding"),
        (
            export_operation,
            (_binding("body", "fixture"),),
            None,
            "does not match export reference",
        ),
        (
            component_operation,
            (),
            None,
            "lacks exact document/entity target bindings",
        ),
        (
            component_operation,
            (_binding("active_document", "other"),),
            None,
            "do not match the operation references",
        ),
        (
            sketch_operation,
            (
                _document_binding(),
                replace(valid_component, document_identity="a" * 64),
            ),
            {("component", "fixture"): "component"},
            "do not belong to the bound document",
        ),
        (
            sketch_operation,
            (_document_binding(), replace(valid_component, producer_operation_id=None)),
            {("component", "fixture"): "component"},
            "producer proof does not match",
        ),
        (
            export_operation,
            (replace(valid_export, document_identity=""),),
            None,
            "identity proof is incomplete",
        ),
        (
            export_operation,
            (replace(valid_export, fingerprint="g" * 64),),
            None,
            "binding proof is invalid",
        ),
    )

    for operation, bindings, expected_producers, message in cases:
        dispatches: list[str] = []
        with pytest.raises(AuthorityDeniedError, match=message):
            authority._validated_target_bindings(
                operation,
                bindings,
                expected_producers=expected_producers,
            )
            dispatches.append(operation.id)
        assert dispatches == []


def test_target_binding_positive_controls_preserve_exact_producer_and_parameter_state() -> (
    None
):
    sketch_operation = _operation(
        {
            "id": "sketch",
            "kind": "sketch.create",
            "component_ref": "fixture",
            "plane": "XY",
            "name": "profile",
        }
    )
    component_binding = _binding(
        "component", "fixture", producer_operation_id="component"
    )
    sketch_bindings = (_document_binding(), component_binding)
    parameter_operation = _operation(
        {
            "id": "parameter",
            "kind": "parameter.set",
            "name": "width",
            "expression": "10 mm",
        }
    )
    parameter_binding = _binding("parameter_absent", "width")
    parameter_bindings = (_document_binding(), parameter_binding)
    dispatches: list[str] = []

    assert (
        authority._validated_target_bindings(
            sketch_operation,
            sketch_bindings,
            expected_producers={("component", "fixture"): "component"},
        )
        == sketch_bindings
    )
    dispatches.append(sketch_operation.id)
    assert (
        authority._validated_target_bindings(
            parameter_operation,
            parameter_bindings,
            expected_producers={},
        )
        == parameter_bindings
    )
    dispatches.append(parameter_operation.id)
    assert not authority._binding_matches_requirement(
        parameter_binding, ("parameter_target", "height")
    )
    assert authority._binding_matches_requirement(
        parameter_binding, ("parameter_target", "width")
    )
    assert dispatches == ["sketch", "parameter"]


def test_claim_revalidates_import_and_revokes_changed_resource_before_dispatch(
    tmp_path: Path,
) -> None:
    import_root = tmp_path / "imports"
    import_root.mkdir()
    source = import_root / "fixture.step"
    source.write_bytes(b"STEP-v1")
    broker = _import_broker(import_root)
    graph = broker.prepare_graph(
        _import_spec(),
        session_id="session",
        provider="provider",
        target_bindings_by_operation={"import_fixture": (_document_binding(),)},
    )
    bound = graph.operations[0]
    assert bound.capability is not None
    source.write_bytes(b"STEP-v2-with-drift")
    dispatches: list[str] = []

    with pytest.raises(AuthorityDeniedError, match="resource changed before dispatch"):
        broker.claim(bound)
        dispatches.append(bound.operation.id)

    assert dispatches == []
    assert broker.ledger.state(bound.capability.capability_id) == "revoked"


def test_claim_positive_control_consumes_one_revalidated_import_capability(
    tmp_path: Path,
) -> None:
    import_root = tmp_path / "imports"
    import_root.mkdir()
    (import_root / "fixture.step").write_bytes(b"STEP-v1")
    broker = _import_broker(import_root)
    bound = broker.prepare_graph(
        _import_spec(),
        session_id="session",
        provider="provider",
        target_bindings_by_operation={"import_fixture": (_document_binding(),)},
    ).operations[0]
    assert bound.capability is not None
    dispatches: list[str] = []

    broker.claim(bound)
    dispatches.append(bound.operation.id)
    broker.complete(bound, outcome="consumed")

    assert dispatches == ["import_fixture"]
    assert broker.ledger.state(bound.capability.capability_id) == "consumed"


def test_capability_helpers_fail_closed_for_read_only_unbound_operation() -> None:
    read_only_spec = _spec(
        [
            {
                "id": "inspect",
                "kind": "analysis.physical_properties",
                "target_refs": ["fixture"],
                "output_ref": "properties",
            }
        ]
    )
    broker = AuthorityBroker(AuthorityPolicy.deny_all(), ledger=CapabilityLedger())
    bound = broker.prepare_operation(
        read_only_spec,
        read_only_spec.operations[0],
        session_id="session",
        provider="provider",
    )
    dispatches: list[str] = []

    with pytest.raises(AuthorityDeniedError, match="no capability"):
        broker.claim(bound)
        dispatches.append(bound.operation.id)
    broker.complete(bound, outcome="consumed")
    broker.revoke(bound)
    broker.fail(bound, outcome_unknown=True)
    with pytest.raises(AuthorityDeniedError, match="read-only operation"):
        broker.validate(replace(bound, target_bindings=(_document_binding(),)))

    assert dispatches == []


def test_validate_rejects_host_io_capability_without_bound_path_before_dispatch() -> (
    None
):
    import_operation = _import_spec().operations[0]
    binding = _document_binding()
    proof = BindingProof(algorithm="sha256", digest="b" * 64)
    capability = OperationCapability(
        capability_id="missing-host",
        direction="import",
        root_id="approved-imports",
        canonical_path="fixture.step",
        spec_digest="a" * 64,
        operation_digest=authority._model_digest(import_operation),
        session_id="session",
        provider="provider",
        overwrite=False,
        issued_at=1.0,
        expires_at=2.0,
        binding_digest=proof.digest,
    )
    bound = BoundOperation(
        operation=import_operation,
        spec_digest=capability.spec_digest,
        operation_digest=capability.operation_digest,
        session_id=capability.session_id,
        provider=capability.provider,
        target_bindings=(binding,),
        capability=capability,
        proof=proof,
    )
    dispatches: list[str] = []
    broker = AuthorityBroker(AuthorityPolicy.deny_all(), ledger=CapabilityLedger())

    with pytest.raises(AuthorityDeniedError, match="requires a bound host path"):
        broker.validate(bound)
        dispatches.append(bound.operation.id)

    assert dispatches == []


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {
                "id": "constraint",
                "kind": "sketch.constraint",
                "sketch_ref": "profile",
                "constraint": "horizontal",
                "entity_refs": ["edge-a", "edge-b"],
            },
            (
                ("sketch", "profile"),
                ("sketch_entity", "profile::edge-a"),
                ("sketch_entity", "profile::edge-b"),
            ),
        ),
        (
            {
                "id": "revolve",
                "kind": "feature.revolve",
                "component_ref": "component",
                "profile_ref": "profile",
                "axis_ref": "axis",
                "operation": "cut",
                "target_body_ref": "body",
                "result_name": "body",
            },
            (
                ("component", "component"),
                ("profile", "profile"),
                ("axis", "axis"),
                ("body", "body"),
            ),
        ),
        (
            {
                "id": "sweep",
                "kind": "feature.sweep",
                "component_ref": "component",
                "profile_ref": "profile",
                "path_ref": "path",
                "operation": "cut",
                "target_body_ref": "body",
                "result_name": "body",
            },
            (
                ("component", "component"),
                ("profile", "profile"),
                ("path", "path"),
                ("body", "body"),
            ),
        ),
        (
            {
                "id": "loft",
                "kind": "feature.loft",
                "component_ref": "component",
                "profile_refs": ["profile-a", "profile-b"],
                "guide_refs": ["guide"],
                "operation": "cut",
                "target_body_ref": "body",
                "result_name": "body",
            },
            (
                ("component", "component"),
                ("profile", "profile-a"),
                ("profile", "profile-b"),
                ("path", "guide"),
                ("body", "body"),
            ),
        ),
        (
            {
                "id": "pattern",
                "kind": "feature.pattern",
                "pattern": "circular",
                "target_refs": ["body"],
                "count": 2,
                "axis_ref": "axis",
            },
            (("geometry", "body"), ("axis", "axis")),
        ),
        (
            {
                "id": "mirror",
                "kind": "feature.mirror",
                "target_refs": ["body"],
                "plane_ref": "plane",
            },
            (("geometry", "body"), ("plane", "plane")),
        ),
        (
            {
                "id": "boolean",
                "kind": "feature.boolean",
                "operation": "cut",
                "target_ref": "body",
                "tool_refs": ["tool-a", "tool-b"],
            },
            (("body", "body"), ("body", "tool-a"), ("body", "tool-b")),
        ),
        (
            {
                "id": "joint",
                "kind": "assembly.joint",
                "name": "fixture_joint",
                "joint_type": "rigid",
                "parent_ref": "parent",
                "child_ref": "child",
            },
            (("occurrence", "parent"), ("occurrence", "child")),
        ),
        (
            {
                "id": "rigid",
                "kind": "assembly.rigid_group",
                "name": "fixture_group",
                "occurrence_refs": ["one", "two"],
            },
            (("occurrence", "one"), ("occurrence", "two")),
        ),
        (
            {
                "id": "sheet",
                "kind": "experimental.sheet_metal",
                "operation": "create_flange",
                "target_ref": "body",
            },
            (("geometry", "body"),),
        ),
        (
            {
                "id": "analysis",
                "kind": "analysis.interference",
                "target_refs": [],
                "output_ref": "result",
            },
            (),
        ),
    ],
)
def test_target_requirement_registry_covers_exact_mutator_references(
    payload: dict[str, Any], expected: tuple[tuple[str, str], ...]
) -> None:
    assert authority.cad_operation_target_requirements(_operation(payload)) == expected


def test_binding_graph_rejects_undeclared_dependency_and_duplicate_producer() -> None:
    missing_dependency = _spec(
        [
            {"id": "component", "kind": "component.create", "name": "fixture"},
            {
                "id": "sketch",
                "kind": "sketch.create",
                "component_ref": "fixture",
                "plane": "XY",
                "name": "profile",
            },
        ]
    )
    duplicate_producer = _spec(
        [
            {"id": "first", "kind": "component.create", "name": "fixture"},
            {"id": "second", "kind": "component.create", "name": "fixture"},
        ]
    )
    dispatches: list[str] = []

    with pytest.raises(AuthorityDeniedError, match="without a declared dependency"):
        authority.cad_graph_target_producers(missing_dependency)
        dispatches.append("missing-dependency")
    with pytest.raises(AuthorityDeniedError, match="multiple producers"):
        authority.cad_graph_target_producers(duplicate_producer)
        dispatches.append("duplicate-producer")

    assert dispatches == []


def test_binding_graph_positive_control_preserves_transitive_producer_proof() -> None:
    graph = _spec(
        [
            {"id": "component", "kind": "component.create", "name": "fixture"},
            {
                "id": "sketch",
                "kind": "sketch.create",
                "component_ref": "fixture",
                "plane": "XY",
                "name": "profile",
                "depends_on": ["component"],
            },
        ]
    )

    assert authority.cad_graph_target_producers(graph)["sketch"] == {
        ("component", "fixture"): "component"
    }


def test_unknown_mutator_kind_and_legacy_binding_proofs_fail_closed() -> None:
    dispatches: list[str] = []
    with pytest.raises(AuthorityDeniedError, match="does not support future.mutate"):
        authority.cad_operation_target_requirements(
            SimpleNamespace(kind="future.mutate")  # type: ignore[arg-type]
        )
        dispatches.append("unknown")

    operation = LegacyOutputOperation(
        id="legacy",
        kind="legacy.export",
        path="fixture.step",
        format="step",
        target_identity="fixture",
    )
    valid = _binding("export_target", "fixture")
    assert authority._validated_legacy_target_bindings(operation, (valid,)) == (valid,)
    for bindings, message in (
        ((), "exactly one"),
        ((_binding("body", "fixture"),), "does not match"),
        ((replace(valid, entity_identity=""),), "identity proof is incomplete"),
        ((replace(valid, document_identity="g" * 64),), "document binding"),
        ((replace(valid, entity_identity="g" * 64),), "entity binding"),
        ((replace(valid, fingerprint="g" * 64),), "target binding"),
    ):
        with pytest.raises(AuthorityDeniedError, match=message):
            authority._validated_legacy_target_bindings(operation, bindings)
            dispatches.append(message)

    assert dispatches == []
