from __future__ import annotations

import json
import os
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
    CadTargetBinding,
    CapabilityLedger,
    HostPathBinding,
    OperationCapability,
)
from cad_spec.v2 import CadSpecV2


def _prepare_graph(broker: AuthorityBroker, spec: CadSpecV2, **kwargs):
    targets = {}
    for operation in spec.operations:
        if operation.kind == "io.export":
            targets[operation.id] = (
                CadTargetBinding(
                    reference_kind="export_target",
                    requested_ref=str(operation.target_ref),
                    document_identity="d" * 64,
                    entity_identity="e" * 64,
                    fingerprint="c" * 64,
                ),
            )
        elif not operation.kind.startswith("analysis."):
            targets[operation.id] = (
                CadTargetBinding(
                    reference_kind="active_document",
                    requested_ref="active_document",
                    document_identity="d" * 64,
                    entity_identity="e" * 64,
                    fingerprint="f" * 64,
                ),
            )
    return broker.prepare_graph(spec, target_bindings_by_operation=targets, **kwargs)


def _policy_payload(import_root: Path, export_root: Path) -> dict[str, Any]:
    return {
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
        "allow_overwrite": False,
        "capability_ttl_seconds": 1800,
    }


def _policy_file(tmp_path: Path, payload: Any | None = None) -> Path:
    import_root = tmp_path / "imports"
    export_root = tmp_path / "exports"
    import_root.mkdir(exist_ok=True)
    export_root.mkdir(exist_ok=True)
    path = tmp_path / "authority.json"
    path.write_text(
        json.dumps(
            _policy_payload(import_root, export_root) if payload is None else payload
        ),
        encoding="utf-8",
    )
    return path


def _policy(tmp_path: Path, *, allow_overwrite: bool = False) -> AuthorityPolicy:
    path = _policy_file(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["allow_overwrite"] = allow_overwrite
    path.write_text(json.dumps(payload), encoding="utf-8")
    return AuthorityPolicy.load(path)


def _export_spec(
    *,
    relative_path: str = "part.step",
    root_id: str = "approved-exports",
    overwrite: bool = False,
) -> CadSpecV2:
    return CadSpecV2.model_validate(
        {
            "cad_spec_version": "2.0",
            "intent": "exercise an authority-bound export",
            "requirements": [
                {
                    "id": "exported",
                    "description": "artifact exists",
                    "assertion_ids": ["export_exists"],
                }
            ],
            "operations": [
                {
                    "id": "export_part",
                    "kind": "io.export",
                    "target_ref": "part_body",
                    "file_ref": {
                        "root_id": root_id,
                        "relative_path": relative_path,
                    },
                    "format": "step",
                    "overwrite": overwrite,
                    "requirement_ids": ["exported"],
                }
            ],
            "assertions": [
                {
                    "id": "export_exists",
                    "kind": "export_exists",
                    "target_ref": "part_body",
                }
            ],
        }
    )


def _component_spec() -> CadSpecV2:
    return CadSpecV2.model_validate(
        {
            "cad_spec_version": "2.0",
            "intent": "exercise an operation without host I/O",
            "requirements": [
                {
                    "id": "created",
                    "description": "component exists",
                    "assertion_ids": ["component_exists"],
                }
            ],
            "operations": [
                {
                    "id": "create_component",
                    "kind": "component.create",
                    "name": "fixture_component",
                    "requirement_ids": ["created"],
                }
            ],
            "assertions": [
                {
                    "id": "component_exists",
                    "kind": "entity_exists",
                    "target_ref": "fixture_component",
                }
            ],
        }
    )


def _capability(
    capability_id: str,
    *,
    expires_at: float = 10.0,
) -> OperationCapability:
    return OperationCapability(
        capability_id=capability_id,
        direction="export",
        root_id="approved-exports",
        canonical_path="C:/approved/part.step",
        spec_digest="a" * 64,
        operation_digest="b" * 64,
        session_id="session",
        provider="provider",
        overwrite=False,
        issued_at=1.0,
        expires_at=expires_at,
        binding_digest="c" * 64,
    )


@pytest.mark.parametrize(
    ("kind", "message"),
    [
        ("missing", "unavailable"),
        ("directory", "must name a file"),
        ("oversized", "too large"),
        ("invalid-json", "valid UTF-8 JSON"),
        ("invalid-utf8", "valid UTF-8 JSON"),
        ("array", "JSON object"),
    ],
)
def test_policy_file_boundary_rejects_unavailable_or_malformed_input(
    tmp_path: Path, kind: str, message: str
) -> None:
    path = tmp_path / "policy"
    if kind == "missing":
        pass
    elif kind == "directory":
        path.mkdir()
    elif kind == "oversized":
        path.write_bytes(b"{" + b" " * (1024 * 1024))
    elif kind == "invalid-json":
        path.write_text("{not-json", encoding="utf-8")
    elif kind == "invalid-utf8":
        path.write_bytes(b"\xff\xfe")
    else:
        path.write_text("[]", encoding="utf-8")

    with pytest.raises(AuthorityDeniedError, match=message):
        AuthorityPolicy.load(path)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("unknown", True, "unknown fields"),
        ("schema_version", "v0", "schema_version"),
        ("allow_overwrite", 1, "must be boolean"),
        ("capability_ttl_seconds", True, "integer from 1"),
        ("capability_ttl_seconds", 0, "integer from 1"),
        ("capability_ttl_seconds", 86401, "integer from 1"),
        ("capability_ttl_seconds", "1800", "integer from 1"),
    ],
)
def test_policy_schema_rejects_unknown_or_ambiguous_fields(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    import_root = tmp_path / "imports"
    export_root = tmp_path / "exports"
    import_root.mkdir()
    export_root.mkdir()
    payload = _policy_payload(import_root, export_root)
    payload[field] = value

    with pytest.raises(AuthorityDeniedError, match=message):
        AuthorityPolicy.load(_policy_file(tmp_path, payload))


def test_policy_requires_globally_unique_root_ids(tmp_path: Path) -> None:
    import_root = tmp_path / "imports"
    export_root = tmp_path / "exports"
    import_root.mkdir()
    export_root.mkdir()
    payload = _policy_payload(import_root, export_root)
    payload["export_roots"][0]["id"] = "approved-imports"

    with pytest.raises(AuthorityDeniedError, match="globally unique"):
        AuthorityPolicy.load(_policy_file(tmp_path, payload))


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("not-array", "must be an array"),
        ("not-object", "must be an object"),
        ("unknown-field", "unknown fields"),
        ("bad-id", "valid root id"),
        ("empty-path", "non-empty string"),
        ("relative-path", "must be absolute"),
        ("missing-path", "is unavailable"),
        ("file-path", "must be a directory"),
        ("empty-formats", "non-empty array"),
        ("nonstring-format", "only strings"),
        ("unsupported-format", "unsupported formats"),
        ("nonbool-default", "default must be boolean"),
        ("duplicate-id", "ids must be unique"),
        ("multiple-defaults", "at most one default"),
    ],
)
def test_root_schema_rejects_every_ambiguous_authority_declaration(
    tmp_path: Path, case: str, message: str
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    root_item: dict[str, Any] = {
        "id": "root-one",
        "path": str(root),
        "formats": ["step"],
        "default": True,
    }
    value: Any = [root_item]
    if case == "not-array":
        value = {}
    elif case == "not-object":
        value = ["root"]
    elif case == "unknown-field":
        root_item["secret"] = True
    elif case == "bad-id":
        root_item["id"] = "Root_One"
    elif case == "empty-path":
        root_item["path"] = " "
    elif case == "relative-path":
        root_item["path"] = "relative/root"
    elif case == "missing-path":
        root_item["path"] = str(tmp_path / "missing")
    elif case == "file-path":
        file_path = tmp_path / "not-a-directory"
        file_path.write_text("data", encoding="utf-8")
        root_item["path"] = str(file_path)
    elif case == "empty-formats":
        root_item["formats"] = []
    elif case == "nonstring-format":
        root_item["formats"] = [7]
    elif case == "unsupported-format":
        root_item["formats"] = ["stl"]
    elif case == "nonbool-default":
        root_item["default"] = 1
    elif case == "duplicate-id":
        value = [root_item, {**root_item, "path": str(other), "default": False}]
    elif case == "multiple-defaults":
        value = [
            root_item,
            {**root_item, "id": "root-two", "path": str(other)},
        ]

    with pytest.raises(AuthorityDeniedError, match=message):
        authority._load_roots(value, "import", frozenset({"step"}))


@pytest.mark.parametrize(
    "device_path", [r"\\?\C:\root", r"\\.\C:\root", r"\??\C:\root", r"\\??\C:\root"]
)
def test_root_schema_rejects_windows_device_namespaces(device_path: str) -> None:
    with pytest.raises(AuthorityDeniedError, match="device paths"):
        authority._load_roots(
            [
                {
                    "id": "root",
                    "path": device_path,
                    "formats": ["step"],
                }
            ],
            "import",
            frozenset({"step"}),
        )


def test_policy_environment_and_safe_summary_expose_ids_not_paths(
    tmp_path: Path,
) -> None:
    path = _policy_file(tmp_path)
    policy = AuthorityPolicy.from_environment(
        {"FUSION_AGENT_AUTHORITY_POLICY_PATH": str(path)}
    )
    broker = AuthorityBroker.from_environment(
        environment={"FUSION_AGENT_AUTHORITY_POLICY_PATH": str(path)}
    )

    assert broker.policy == policy
    assert policy.io_enabled
    assert policy.root_ids == {"import": ("approved-imports",)}
    assert str(tmp_path) not in json.dumps(policy.safe_summary())


def test_export_only_policy_is_parsed_but_does_not_enable_real_io(
    tmp_path: Path,
) -> None:
    payload = _policy_payload(tmp_path / "imports", tmp_path / "exports")
    payload["import_roots"] = []
    path = _policy_file(tmp_path, payload)

    policy = AuthorityPolicy.load(path)

    assert [root.id for root in policy.export_roots] == ["approved-exports"]
    assert policy.io_enabled is False
    assert policy.root_ids == {"import": ()}
    assert policy.safe_summary()["output_enabled"] is False


def test_memory_ledger_enforces_collision_expiry_replay_and_terminal_states() -> None:
    ledger = CapabilityLedger()
    first = _capability("first")
    ledger.issue(first)
    assert ledger.state("first") == "issued"
    with pytest.raises(AuthorityDeniedError, match="collision"):
        ledger.issue(first)

    with pytest.raises(AuthorityDeniedError, match="transition denied"):
        ledger.transition(first, "consumed")
    with pytest.raises(ValueError, match="terminal transition"):
        ledger.transition(first, "claimed")

    ledger.revoke_active("first")
    assert ledger.state("first") == "revoked"
    ledger.revoke_active("first")
    with pytest.raises(AuthorityDeniedError, match="replay"):
        ledger.claim(first, now=2.0)

    claimed = _capability("claimed")
    ledger.issue(claimed)
    ledger.claim(claimed, now=2.0)
    assert ledger.state("claimed") == "claimed"
    with pytest.raises(AuthorityDeniedError, match="replay"):
        ledger.claim(claimed, now=2.0)
    ledger.transition(claimed, "consumed")
    assert ledger.state("claimed") == "consumed"

    expired = _capability("expired", expires_at=5.0)
    ledger.issue(expired)
    with pytest.raises(AuthorityDeniedError, match="expired"):
        ledger.claim(expired, now=5.0)
    assert ledger.state("expired") == "expired"
    with pytest.raises(AuthorityDeniedError, match="unknown"):
        ledger.state("not-issued")


def test_disk_ledger_rejects_collision_corruption_expiry_and_invalid_state(
    tmp_path: Path,
) -> None:
    ledger = CapabilityLedger(tmp_path / "ledger")
    collision = _capability("collision")
    ledger.issue(collision)
    with pytest.raises(AuthorityDeniedError, match="collision"):
        ledger.issue(collision)

    invalid_state = _capability("invalid-state")
    ledger.issue(invalid_state)
    record = ledger._disk_record("invalid-state")
    ledger._write_disk_state(record, "revoked")
    with pytest.raises(AuthorityDeniedError, match="state revoked"):
        ledger.claim(invalid_state, now=2.0)

    denied_transition = _capability("denied-transition")
    ledger.issue(denied_transition)
    with pytest.raises(AuthorityDeniedError, match="transition denied"):
        ledger.transition(denied_transition, "consumed")

    expired = _capability("disk-expired", expires_at=5.0)
    ledger.issue(expired)
    with pytest.raises(AuthorityDeniedError, match="expired"):
        ledger.claim(expired, now=5.0)
    assert ledger.state("disk-expired") == "expired"

    with pytest.raises(AuthorityDeniedError, match="unavailable"):
        ledger._disk_record("missing")
    corrupt_path = ledger._record_path("corrupt")
    corrupt_path.write_text("{bad", encoding="utf-8")
    with pytest.raises(AuthorityDeniedError, match="unavailable"):
        ledger._disk_record("corrupt")
    invalid_path = ledger._record_path("invalid")
    invalid_path.write_text("[]", encoding="utf-8")
    with pytest.raises(AuthorityDeniedError, match="invalid"):
        ledger._disk_record("invalid")


def test_disk_ledger_startup_reconciliation_is_fail_closed_and_persistent(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ledger"
    ledger = CapabilityLedger(root)

    claimed = _capability("claimed-before-restart")
    claimed_without_marker = _capability("claimed-without-marker")
    interrupted_claim = _capability("claim-marker-before-state")
    unclaimed = _capability("unclaimed")
    consumed = _capability("consumed")
    for capability in (
        claimed,
        claimed_without_marker,
        interrupted_claim,
        unclaimed,
        consumed,
    ):
        ledger.issue(capability)

    ledger.claim(claimed, now=2.0)
    ledger.claim(claimed_without_marker, now=2.0)
    ledger._claim_path(claimed_without_marker.capability_id).unlink()
    ledger._claim_path(interrupted_claim.capability_id).touch()
    ledger.claim(consumed, now=2.0)
    ledger.transition(consumed, "consumed")
    outside = tmp_path / "outside.json"
    outside.write_text("sentinel", encoding="utf-8")
    forged = ledger._disk_record(unclaimed.capability_id)
    forged.update({"capability_id": "../outside", "state": "claimed"})
    (root / "forged.json").write_text(json.dumps(forged), encoding="utf-8")

    restarted = CapabilityLedger(root)
    assert restarted.reconcile_startup() == 3

    assert restarted.state(claimed.capability_id) == "unknown"
    assert restarted.state(claimed_without_marker.capability_id) == "unknown"
    assert restarted.state(interrupted_claim.capability_id) == "unknown"
    assert restarted.state(unclaimed.capability_id) == "issued"
    assert restarted.state(consumed.capability_id) == "consumed"
    assert outside.read_text(encoding="utf-8") == "sentinel"

    # The terminal reconciliation itself is durable and can never resurrect a
    # capability on a subsequent restart or through a replayed claim.
    reopened = CapabilityLedger(root)
    assert reopened.reconcile_startup() == 0
    assert reopened.state(claimed.capability_id) == "unknown"
    with pytest.raises(AuthorityDeniedError, match="replay"):
        reopened.claim(claimed, now=3.0)


def test_ledger_private_boundaries_fail_closed_without_storage() -> None:
    ledger = CapabilityLedger()
    assert ledger.reconcile_startup() == 0
    with pytest.raises(RuntimeError, match="memory-only"):
        ledger._record_path("id")
    with pytest.raises(RuntimeError, match="memory-only"):
        ledger._claim_path("id")
    with pytest.raises(AuthorityDeniedError, match="ledger record"):
        CapabilityLedger._validate_record(
            {"capability_id": "other"}, _capability("expected")
        )


def test_broker_non_io_graph_has_document_authority_and_requires_identity(
    tmp_path: Path,
) -> None:
    broker = AuthorityBroker(_policy(tmp_path), ledger=CapabilityLedger())
    with pytest.raises(AuthorityDeniedError, match="session id"):
        _prepare_graph(broker, _component_spec(), session_id=" ", provider="provider")
    with pytest.raises(AuthorityDeniedError, match="provider"):
        _prepare_graph(broker, _component_spec(), session_id="session", provider=" ")

    graph = _prepare_graph(
        broker, _component_spec(), session_id="session", provider="provider"
    )
    bound = graph.operations[0]
    assert bound.host_path is None
    assert bound.capability is not None
    assert bound.capability.direction == "cad"
    assert bound.proof is not None
    assert bound.target_bindings[0].reference_kind == "active_document"
    broker.validate(bound)
    broker.claim(bound)
    broker.complete(bound, outcome="consumed")
    broker.revoke(bound)
    broker.fail(bound, outcome_unknown=False)
    with pytest.raises(AuthorityDeniedError, match="replay"):
        broker.claim(bound)

    forged = replace(
        bound,
        host_path=HostPathBinding(
            direction="export",
            root_id="root",
            canonical_root=str(tmp_path),
            canonical_path=str(tmp_path / "part.step"),
            relative_path="part.step",
            format="step",
            overwrite=False,
            existed_at_issue=False,
            resource_fingerprint="x",
        ),
    )
    with pytest.raises(AuthorityDeniedError, match="CAD-only"):
        broker.validate(forged)


def test_broker_rejects_target_proofs_for_operations_outside_the_validated_graph(
    tmp_path: Path,
) -> None:
    broker = AuthorityBroker(_policy(tmp_path), ledger=CapabilityLedger())

    with pytest.raises(AuthorityDeniedError, match="unknown operations"):
        broker.prepare_graph(
            _component_spec(),
            session_id="session",
            provider="provider",
            target_bindings_by_operation={"not_in_spec": ()},
        )


def test_mutating_cad_operation_requires_single_use_document_capability(
    tmp_path: Path,
) -> None:
    broker = AuthorityBroker(_policy(tmp_path), ledger=CapabilityLedger())
    spec = _component_spec()
    binding = CadTargetBinding(
        reference_kind="active_document",
        requested_ref="active_document",
        document_identity="d" * 64,
        entity_identity="e" * 64,
        fingerprint="f" * 64,
    )

    graph = broker.prepare_graph(
        spec,
        session_id="document-bound-session",
        provider="provider",
        target_bindings_by_operation={"create_component": (binding,)},
    )
    bound = graph.operations[0]

    assert bound.host_path is None
    assert bound.target_bindings == (binding,)
    assert bound.capability is not None
    assert bound.capability.direction == "cad"
    assert bound.proof is not None
    broker.claim(bound)
    broker.complete(bound, outcome="consumed")
    assert broker.ledger.state(bound.capability.capability_id) == "consumed"


def test_broker_lifecycle_covers_issued_claimed_consumed_and_revoked(
    tmp_path: Path,
) -> None:
    broker = AuthorityBroker(_policy(tmp_path), ledger=CapabilityLedger())

    issued = _prepare_graph(
        broker,
        _export_spec(relative_path="issued.step"),
        session_id="issued",
        provider="provider",
    ).operations[0]
    assert issued.capability is not None
    broker.fail(issued, outcome_unknown=False)
    assert broker.ledger.state(issued.capability.capability_id) == "revoked"

    revoked = _prepare_graph(
        broker,
        _export_spec(relative_path="revoke.step"),
        session_id="revoke",
        provider="provider",
    ).operations[0]
    assert revoked.capability is not None
    broker.revoke(revoked)
    assert broker.ledger.state(revoked.capability.capability_id) == "revoked"

    consumed = _prepare_graph(
        broker,
        _export_spec(relative_path="consumed.step"),
        session_id="consumed",
        provider="provider",
    ).operations[0]
    assert consumed.capability is not None
    broker.claim(consumed)
    broker.complete(consumed, outcome="consumed")
    assert broker.ledger.state(consumed.capability.capability_id) == "consumed"


class _RevocationFailureLedger(CapabilityLedger):
    def revoke_active(self, capability_id: str) -> None:
        del capability_id
        raise AuthorityDeniedError("revocation storage unavailable")


def test_claim_preserves_validation_error_when_best_effort_revocation_fails(
    tmp_path: Path,
) -> None:
    broker = AuthorityBroker(_policy(tmp_path), ledger=_RevocationFailureLedger())
    bound = _prepare_graph(
        broker, _export_spec(), session_id="session", provider="provider"
    ).operations[0]
    assert bound.proof is not None
    tampered = replace(
        bound,
        proof=BindingProof(algorithm="sha512", digest=bound.proof.digest),
    )

    with pytest.raises(AuthorityDeniedError, match="proof does not match"):
        broker.claim(tampered)


def test_validate_rejects_missing_or_changed_io_binding(tmp_path: Path) -> None:
    broker = AuthorityBroker(_policy(tmp_path), ledger=CapabilityLedger())
    bound = _prepare_graph(
        broker, _export_spec(), session_id="session", provider="provider"
    ).operations[0]
    with pytest.raises(AuthorityDeniedError, match="complete bound capability"):
        broker.validate(replace(bound, capability=None))

    changed_operation = bound.operation.model_copy(update={"overwrite": True})
    with pytest.raises(AuthorityDeniedError, match="operation changed"):
        broker.validate(replace(bound, operation=changed_operation))


def _manual_policy(
    *,
    import_roots: tuple[AuthorityRoot, ...] = (),
    export_roots: tuple[AuthorityRoot, ...] = (),
    allow_overwrite: bool = False,
) -> AuthorityPolicy:
    return AuthorityPolicy(
        schema_version="fusion_agent.authority_policy.v1",
        import_roots=import_roots,
        export_roots=export_roots,
        allow_overwrite=allow_overwrite,
        digest="d" * 64,
    )


def _root(root_id: str, path: Path, *, default: bool = False) -> AuthorityRoot:
    return AuthorityRoot(
        id=root_id,
        canonical_path=str(path.resolve()),
        formats=frozenset({"step"}),
        default=default,
    )


def test_resolve_host_path_rejects_missing_unknown_and_ambiguous_roots(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    nested = first / "nested"
    first.mkdir()
    second.mkdir()
    nested.mkdir()
    roots = (_root("first", first), _root("second", second))
    policy = _manual_policy(export_roots=roots)

    with pytest.raises(AuthorityDeniedError, match="requires root_id"):
        authority._resolve_host_path(
            policy,
            direction="export",
            root_id=None,
            requested_path="part.step",
            format_name="step",
            overwrite=False,
            explicit_ref=True,
        )
    with pytest.raises(AuthorityDeniedError, match="unknown approved"):
        authority._resolve_host_path(
            policy,
            direction="export",
            root_id="unknown",
            requested_path="part.step",
            format_name="step",
            overwrite=False,
            explicit_ref=True,
        )
    with pytest.raises(AuthorityDeniedError, match="one default"):
        authority._resolve_host_path(
            policy,
            direction="export",
            root_id=None,
            requested_path="part.step",
            format_name="step",
            overwrite=False,
            explicit_ref=False,
        )

    nested_policy = _manual_policy(
        export_roots=(_root("outer", first), _root("inner", nested))
    )
    with pytest.raises(AuthorityDeniedError, match="exactly one"):
        authority._resolve_host_path(
            nested_policy,
            direction="export",
            root_id=None,
            requested_path=str(nested / "part.step"),
            format_name="step",
            overwrite=False,
            explicit_ref=False,
        )


def test_resolve_host_path_legacy_default_format_extension_and_file_rules(
    tmp_path: Path,
) -> None:
    export_root = tmp_path / "exports"
    import_root = tmp_path / "imports"
    export_root.mkdir()
    import_root.mkdir()
    export_policy = _manual_policy(export_roots=(_root("export", export_root),))
    import_policy = _manual_policy(import_roots=(_root("import", import_root),))

    binding = authority._resolve_host_path(
        export_policy,
        direction="export",
        root_id=None,
        requested_path="part.step",
        format_name="step",
        overwrite=False,
        explicit_ref=False,
    )
    assert binding.root_id == "export"

    with pytest.raises(AuthorityDeniedError, match="not allowed"):
        authority._resolve_host_path(
            export_policy,
            direction="export",
            root_id="export",
            requested_path="part.stl",
            format_name="stl",
            overwrite=False,
            explicit_ref=True,
        )
    with pytest.raises(AuthorityDeniedError, match="extension"):
        authority._resolve_host_path(
            export_policy,
            direction="export",
            root_id="export",
            requested_path="part.stl",
            format_name="step",
            overwrite=False,
            explicit_ref=True,
        )
    with pytest.raises(AuthorityDeniedError, match="unavailable"):
        authority._resolve_host_path(
            import_policy,
            direction="import",
            root_id="import",
            requested_path="missing.step",
            format_name="step",
            overwrite=False,
            explicit_ref=True,
        )

    import_directory = import_root / "directory.step"
    import_directory.mkdir()
    with pytest.raises(AuthorityDeniedError, match="existing file"):
        authority._resolve_host_path(
            import_policy,
            direction="import",
            root_id="import",
            requested_path="directory.step",
            format_name="step",
            overwrite=False,
            explicit_ref=True,
        )
    export_directory = export_root / "directory.step"
    export_directory.mkdir()
    with pytest.raises(AuthorityDeniedError, match="file destination"):
        authority._resolve_host_path(
            export_policy,
            direction="export",
            root_id="export",
            requested_path="directory.step",
            format_name="step",
            overwrite=False,
            explicit_ref=True,
        )


def test_resolve_host_path_allows_double_opt_in_overwrite(tmp_path: Path) -> None:
    export_root = tmp_path / "exports"
    export_root.mkdir()
    destination = export_root / "part.step"
    destination.write_text("existing", encoding="utf-8")
    policy = _manual_policy(
        export_roots=(_root("export", export_root),),
        allow_overwrite=True,
    )

    binding = authority._resolve_host_path(
        policy,
        direction="export",
        root_id="export",
        requested_path="part.step",
        format_name="step",
        overwrite=True,
        explicit_ref=True,
    )
    assert binding.existed_at_issue
    assert binding.overwrite


@pytest.mark.parametrize(
    "path", ["", " part.step", "part.step ", "part\n.step", "part\x7f.step"]
)
def test_requested_text_rejects_empty_whitespace_and_control_characters(
    path: str,
) -> None:
    with pytest.raises(AuthorityDeniedError):
        authority._validate_requested_text(path)


@pytest.mark.parametrize(
    "path",
    ["/absolute.step", r"C:\absolute.step", r"C:drive.step", "../escape.step", "."],
)
def test_relative_path_normalization_rejects_absolute_traversal_and_empty(
    path: str,
) -> None:
    with pytest.raises(AuthorityDeniedError):
        authority._normalize_relative_path(path)


def test_unc_matching_is_scoped_to_approved_share() -> None:
    roots = (
        AuthorityRoot(
            id="local",
            canonical_path=r"C:\exports",
            formats=frozenset({"step"}),
        ),
        AuthorityRoot(
            id="unc",
            canonical_path=r"\\server\share\exports",
            formats=frozenset({"step"}),
        ),
    )
    assert authority._is_unc_path(r"//server/share/exports/part.step")
    assert authority._unc_matches_approved_root(
        r"\\server\share\exports\part.step", roots
    )
    assert not authority._unc_matches_approved_root(r"\\server\other\part.step", roots)


def test_canonical_target_rejects_missing_parent_and_file_parent(
    tmp_path: Path,
) -> None:
    with pytest.raises(AuthorityDeniedError, match="unavailable"):
        authority._canonical_target(tmp_path / "missing" / "part.step", "export")

    file_parent = tmp_path / "not-directory"
    file_parent.write_text("data", encoding="utf-8")
    with pytest.raises(AuthorityDeniedError, match="existing directory"):
        authority._canonical_target(file_parent / "part.step", "export")


def test_revalidation_rejects_missing_or_changed_canonical_identity(
    tmp_path: Path,
) -> None:
    missing_root = HostPathBinding(
        direction="export",
        root_id="root",
        canonical_root=str(tmp_path / "missing"),
        canonical_path=str(tmp_path / "missing" / "part.step"),
        relative_path="part.step",
        format="step",
        overwrite=False,
        existed_at_issue=False,
        resource_fingerprint="unused",
    )
    with pytest.raises(AuthorityDeniedError, match="approved root changed"):
        authority.revalidate_host_path(missing_root)

    root = tmp_path / "root"
    sub = root / "sub"
    root.mkdir()
    sub.mkdir()
    canonical_mismatch = replace(
        missing_root,
        canonical_root=str(sub / ".."),
        canonical_path=str(root / "part.step"),
    )
    with pytest.raises(AuthorityDeniedError, match="canonical identity changed"):
        authority.revalidate_host_path(canonical_mismatch)

    fingerprint = authority._resource_fingerprint(
        root / "part.step", direction="export", existed=False
    )
    path_mismatch = replace(
        missing_root,
        canonical_root=str(root.resolve()),
        canonical_path=str(sub / ".." / "part.step"),
        resource_fingerprint=fingerprint,
    )
    with pytest.raises(AuthorityDeniedError, match="authorized path changed"):
        authority.revalidate_host_path(path_mismatch)


@pytest.mark.skipif(os.name != "nt", reason="Windows case validation only")
def test_windows_case_revalidation_covers_exact_mismatch_and_scan_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "Root"
    root.mkdir()

    mismatched_root = Path(str(root).replace("Root", "root")) / "part.step"
    with pytest.raises(AuthorityDeniedError, match="approved root"):
        authority._require_windows_case_exact(
            root,
            mismatched_root,
            final_exists=False,
        )

    candidate = root / "Folder" / "part.step"
    monkeypatch.setattr(
        authority.os,
        "scandir",
        lambda _path: (_ for _ in ()).throw(OSError("scan failed")),
    )
    with pytest.raises(AuthorityDeniedError, match="could not be revalidated"):
        authority._require_windows_case_exact(root, candidate, final_exists=True)

    scans = iter(
        [
            [SimpleNamespace(name="folder")],
            [SimpleNamespace(name="part.step")],
        ]
    )
    monkeypatch.setattr(authority.os, "scandir", lambda _path: next(scans))
    with pytest.raises(AuthorityDeniedError, match="filesystem"):
        authority._require_windows_case_exact(root, candidate, final_exists=True)

    monkeypatch.setattr(
        authority.os,
        "scandir",
        lambda _path: [SimpleNamespace(name="Other")],
    )
    with pytest.raises(AuthorityDeniedError, match="changed during case validation"):
        authority._require_windows_case_exact(root, candidate, final_exists=True)


def test_containment_rejects_outside_and_cross_volume_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(AuthorityDeniedError, match="outside"):
        authority._require_contained(root, tmp_path / "outside.step")

    monkeypatch.setattr(
        authority.os.path,
        "commonpath",
        lambda _paths: (_ for _ in ()).throw(ValueError("different drives")),
    )
    assert not authority._is_contained(root, root / "part.step")
