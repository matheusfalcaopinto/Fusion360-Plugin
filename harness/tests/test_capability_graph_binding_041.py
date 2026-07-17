from __future__ import annotations

import hashlib
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from agent_core.authority import (
    AuthorityBroker,
    AuthorityDeniedError,
    AuthorityPolicy,
    CadTargetBinding,
    CapabilityLedger,
)
from agent_core.capability_executor import CapabilityExecutor
from cad_spec.v2 import CadSpecV2
from fusion_tool_facade.autodesk_typed_backend import _parameter_set_script


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    ).hexdigest()


def _document_binding(root_token: str = "root-token") -> dict[str, str]:
    document_identity = _digest(
        {
            "data_id": "current-document",
            "version_id": "current-v1",
            "root_token": root_token,
        }
    )
    facts = {
        "reference_kind": "active_document",
        "requested_ref": "active_document",
        "document_identity": document_identity,
        "entity_identity": hashlib.sha256(root_token.encode("utf-8")).hexdigest(),
    }
    return {**facts, "fingerprint": _digest(facts)}


def _parameter_binding(
    *, name: str, document_identity: str, token: str | None
) -> dict[str, str]:
    if token is None:
        reference_kind = "parameter_absent"
        entity_identity = _digest(
            {
                "document_identity": document_identity,
                "name": name,
                "state": "absent",
            }
        )
        object_type = ""
        state = "absent"
    else:
        reference_kind = "parameter_existing"
        entity_identity = hashlib.sha256(token.encode("utf-8")).hexdigest()
        object_type = "adsk::fusion::UserParameter"
        state = "existing"
    facts = {
        "reference_kind": reference_kind,
        "requested_ref": name,
        "document_identity": document_identity,
        "entity_identity": entity_identity,
        "name": name,
        "object_type": object_type,
        "state": state,
    }
    return {
        "reference_kind": reference_kind,
        "requested_ref": name,
        "document_identity": document_identity,
        "entity_identity": entity_identity,
        "fingerprint": _digest(facts),
    }


class _Parameter:
    objectType = "adsk::fusion::UserParameter"

    def __init__(self, name: str, token: str, mutations: list[tuple[str, str]]) -> None:
        self.name = name
        self.entityToken = token
        self._expression = "1 mm"
        self._mutations = mutations

    @property
    def expression(self) -> str:
        return self._expression

    @expression.setter
    def expression(self, value: str) -> None:
        self._mutations.append(("update", value))
        self._expression = value


class _Parameters:
    def __init__(
        self, values: list[_Parameter], mutations: list[tuple[str, str]]
    ) -> None:
        self.values = {value.name: value for value in values}
        self.mutations = mutations

    def itemByName(self, name: str) -> _Parameter | None:
        return self.values.get(name)

    def add(self, name: str, value: str, _unit: str, _comment: str) -> _Parameter:
        self.mutations.append(("add", value))
        parameter = _Parameter(name, f"created:{name}", self.mutations)
        parameter._expression = value
        self.values[name] = parameter
        return parameter


def _run_parameter_script(
    monkeypatch: pytest.MonkeyPatch,
    *,
    expected_binding: dict[str, str],
    parameters: list[_Parameter],
    mutations: list[tuple[str, str]],
) -> None:
    root = types.SimpleNamespace(entityToken="root-token")
    design = types.SimpleNamespace(
        rootComponent=root,
        userParameters=_Parameters(parameters, mutations),
        unitsManager=types.SimpleNamespace(defaultLengthUnits="mm"),
    )
    document = types.SimpleNamespace(
        dataFile=types.SimpleNamespace(id="current-document", versionId="current-v1")
    )
    core = types.ModuleType("adsk.core")
    core.Application = types.SimpleNamespace(
        get=lambda: types.SimpleNamespace(
            activeProduct=design,
            activeDocument=document,
        )
    )
    core.ValueInput = types.SimpleNamespace(createByString=lambda value: value)
    fusion = types.ModuleType("adsk.fusion")
    fusion.Design = types.SimpleNamespace(cast=lambda value: value)
    adsk = types.ModuleType("adsk")
    adsk.__path__ = []  # type: ignore[attr-defined]
    adsk.core = core
    adsk.fusion = fusion
    monkeypatch.setitem(sys.modules, "adsk", adsk)
    monkeypatch.setitem(sys.modules, "adsk.core", core)
    monkeypatch.setitem(sys.modules, "adsk.fusion", fusion)

    payload = {
        "name": "shaft_diameter",
        "expression": "10 mm",
        "comment": "bound parameter",
        "document_binding": _document_binding(),
        "target_bindings": [expected_binding],
    }
    namespace: dict[str, Any] = {}
    exec(compile(_parameter_set_script(payload), "<parameter-set>", "exec"), namespace)
    namespace["run"]("")


def test_parameter_same_name_replacement_is_rejected_at_sink_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mutations: list[tuple[str, str]] = []
    expected = _parameter_binding(
        name="shaft_diameter",
        document_identity=_document_binding()["document_identity"],
        token="reviewed-token",
    )
    replacement = _Parameter("shaft_diameter", "replacement-token", mutations)

    with pytest.raises(RuntimeError, match="parameter target binding changed"):
        _run_parameter_script(
            monkeypatch,
            expected_binding=expected,
            parameters=[replacement],
            mutations=mutations,
        )

    assert mutations == []


def test_parameter_exact_identity_is_a_legitimate_positive_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mutations: list[tuple[str, str]] = []
    reviewed = _Parameter("shaft_diameter", "reviewed-token", mutations)
    expected = _parameter_binding(
        name="shaft_diameter",
        document_identity=_document_binding()["document_identity"],
        token=reviewed.entityToken,
    )

    _run_parameter_script(
        monkeypatch,
        expected_binding=expected,
        parameters=[reviewed],
        mutations=mutations,
    )

    assert mutations == [("update", "10 mm")]


def test_parameter_absence_proof_rejects_creation_race_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mutations: list[tuple[str, str]] = []
    expected = _parameter_binding(
        name="shaft_diameter",
        document_identity=_document_binding()["document_identity"],
        token=None,
    )
    raced = _Parameter("shaft_diameter", "raced-token", mutations)

    with pytest.raises(RuntimeError, match="parameter target binding changed"):
        _run_parameter_script(
            monkeypatch,
            expected_binding=expected,
            parameters=[raced],
            mutations=mutations,
        )

    assert mutations == []


def test_parameter_absence_proof_allows_one_legitimate_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mutations: list[tuple[str, str]] = []
    expected = _parameter_binding(
        name="shaft_diameter",
        document_identity=_document_binding()["document_identity"],
        token=None,
    )

    _run_parameter_script(
        monkeypatch,
        expected_binding=expected,
        parameters=[],
        mutations=mutations,
    )

    assert mutations == [("add", "10 mm")]


def _chain_spec(*, include_dependencies: bool = True) -> CadSpecV2:
    dependencies = (
        {
            "create_sketch": ["create_component"],
            "draw_profile": ["create_sketch"],
            "extrude_profile": ["draw_profile"],
        }
        if include_dependencies
        else {}
    )
    return CadSpecV2.model_validate(
        {
            "cad_spec_version": "2.0",
            "intent": "Create a fully bound component profile and body",
            "requirements": [
                {
                    "id": "body_exists",
                    "description": "body exists",
                    "assertion_ids": ["body_created"],
                }
            ],
            "operations": [
                {
                    "id": "create_component",
                    "kind": "component.create",
                    "name": "fixture",
                    "requirement_ids": ["body_exists"],
                },
                {
                    "id": "create_sketch",
                    "kind": "sketch.create",
                    "component_ref": "fixture",
                    "plane": "XY",
                    "name": "fixture_profile",
                    "depends_on": dependencies.get("create_sketch", []),
                    "requirement_ids": ["body_exists"],
                },
                {
                    "id": "draw_profile",
                    "kind": "sketch.rectangle",
                    "sketch_ref": "fixture_profile",
                    "width": "10 mm",
                    "height": "5 mm",
                    "result_ref": "fixture_profile_ref",
                    "depends_on": dependencies.get("draw_profile", []),
                    "requirement_ids": ["body_exists"],
                },
                {
                    "id": "extrude_profile",
                    "kind": "feature.extrude",
                    "component_ref": "fixture",
                    "profile_ref": "fixture_profile_ref",
                    "distance": "4 mm",
                    "result_name": "FixtureBody",
                    "depends_on": dependencies.get("extrude_profile", []),
                    "requirement_ids": ["body_exists"],
                },
            ],
            "assertions": [
                {
                    "id": "body_created",
                    "kind": "entity_exists",
                    "target_ref": "FixtureBody",
                }
            ],
        }
    )


@pytest.mark.parametrize(
    ("session_id", "provider", "message"),
    [
        (" ", "autodesk_http", "session id is required"),
        ("session-041", " ", "provider is required"),
    ],
)
def test_just_in_time_authority_rejects_blank_owner_fields_before_binding(
    tmp_path: Path,
    session_id: str,
    provider: str,
    message: str,
) -> None:
    spec = _chain_spec()
    broker = AuthorityBroker(
        AuthorityPolicy.deny_all(), ledger=CapabilityLedger(tmp_path)
    )

    with pytest.raises(AuthorityDeniedError, match=message):
        broker.prepare_operation(
            spec,
            spec.operations[0],
            session_id=session_id,
            provider=provider,
        )

    assert list(tmp_path.iterdir()) == []


class _MaterializingBackend:
    provider = "materializing-test"
    capabilities = {"components", "sketch_create", "sketch_rectangle", "extrude"}

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []
        self.materialized: set[tuple[str, str]] = set()
        self.bound: dict[str, Any] = {}
        self.document_identity = hashlib.sha256(b"document").hexdigest()
        self.omit_proof_for: set[str] = set()
        self.mismatched_proof_for: set[str] = set()

    def _target_binding(
        self, kind: str, ref: str, *, produced: bool = False
    ) -> CadTargetBinding:
        identity_ref = (
            f"different:{kind}:{ref}"
            if produced and ref in self.mismatched_proof_for
            else f"{kind}:{ref}"
        )
        return CadTargetBinding(
            reference_kind=kind,
            requested_ref=ref,
            document_identity=self.document_identity,
            entity_identity=hashlib.sha256(identity_ref.encode()).hexdigest(),
            fingerprint=hashlib.sha256(f"proof:{identity_ref}".encode()).hexdigest(),
        )

    def preflight_operations(self, operations) -> None:
        self.events.append(("preflight", ",".join(op.id for op in operations)))
        assert [operation.id for operation in operations] == [
            "create_component",
            "create_sketch",
            "draw_profile",
            "extrude_profile",
        ]

    async def resolve_document_binding(self) -> CadTargetBinding:
        self.events.append(("resolve", "document"))
        return CadTargetBinding(
            reference_kind="active_document",
            requested_ref="active_document",
            document_identity=self.document_identity,
            entity_identity=hashlib.sha256(b"root").hexdigest(),
            fingerprint=hashlib.sha256(b"document-binding").hexdigest(),
        )

    async def resolve_operation_target_bindings(
        self, operation, *, requirements=None
    ) -> tuple[CadTargetBinding, ...]:
        requested = tuple(requirements or ())
        self.events.append(
            ("resolve", f"{operation.id}:{','.join(ref for _kind, ref in requested)}")
        )
        for requirement in requested:
            if requirement not in self.materialized and requirement != (
                "component",
                "root",
            ):
                raise RuntimeError(f"target not materialized: {requirement}")
        return tuple(self._target_binding(kind, ref) for kind, ref in requested)

    def bind_bound_operation(self, bound) -> None:
        self.events.append(("bind", bound.operation.id))
        self.bound[bound.operation.id] = bound

    async def execute_bound_operation(self, bound) -> dict[str, Any]:
        assert self.bound.get(bound.operation.id) == bound
        self.events.append(("mutate", bound.operation.id))
        operation = bound.operation
        produced: list[CadTargetBinding] = []
        if operation.kind == "component.create":
            self.materialized.add(("component", operation.name))
            produced.append(
                self._target_binding("component", operation.name, produced=True)
            )
        elif operation.kind == "sketch.create":
            self.materialized.add(("sketch", operation.name))
            produced.append(
                self._target_binding("sketch", operation.name, produced=True)
            )
        elif operation.kind == "sketch.rectangle":
            self.materialized.add(("profile", operation.result_ref))
            produced.append(
                self._target_binding("profile", operation.result_ref, produced=True)
            )
        elif operation.kind == "feature.extrude":
            self.materialized.add(("body", operation.result_name))
            produced.extend(
                (
                    self._target_binding("body", operation.result_name, produced=True),
                    self._target_binding(
                        "geometry", operation.result_name, produced=True
                    ),
                )
            )
        if operation.id in self.omit_proof_for:
            produced = []
        return {
            "success": True,
            "produced_target_bindings": [
                {
                    "reference_kind": binding.reference_kind,
                    "requested_ref": binding.requested_ref,
                    "document_identity": binding.document_identity,
                    "entity_identity": binding.entity_identity,
                    "fingerprint": binding.fingerprint,
                }
                for binding in produced
            ],
        }


@pytest.mark.asyncio
async def test_produced_references_are_materialized_and_bound_just_in_time() -> None:
    backend = _MaterializingBackend()
    broker = AuthorityBroker(AuthorityPolicy.deny_all(), ledger=CapabilityLedger())

    result = await CapabilityExecutor(backend, authority_broker=broker).execute(
        _chain_spec(), session_id="materialized-chain"
    )

    assert result.success is True
    first_mutation = backend.events.index(("mutate", "create_component"))
    component_resolution = next(
        index
        for index, event in enumerate(backend.events)
        if event == ("resolve", "create_sketch:fixture")
    )
    assert component_resolution > first_mutation
    sketch_mutation = backend.events.index(("mutate", "create_sketch"))
    sketch_resolution = next(
        index
        for index, event in enumerate(backend.events)
        if event == ("resolve", "draw_profile:fixture_profile")
    )
    assert sketch_resolution > sketch_mutation
    profile_mutation = backend.events.index(("mutate", "draw_profile"))
    extrude_resolution = next(
        index
        for index, event in enumerate(backend.events)
        if event
        == (
            "resolve",
            "extrude_profile:fixture,fixture_profile_ref",
        )
    )
    assert extrude_resolution > profile_mutation

    create_sketch = backend.bound["create_sketch"]
    assert create_sketch.target_bindings[1].producer_operation_id == "create_component"
    extrude = backend.bound["extrude_profile"]
    assert [
        binding.producer_operation_id for binding in extrude.target_bindings[1:]
    ] == [
        "create_component",
        "draw_profile",
    ]
    assert all(
        broker.ledger.state(bound.capability.capability_id) == "consumed"
        for bound in backend.bound.values()
        if bound.capability is not None
    )


@pytest.mark.asyncio
async def test_declared_output_without_returned_identity_is_zero_consumer_dispatch() -> (
    None
):
    backend = _MaterializingBackend()
    backend.omit_proof_for.add("create_component")

    result = await CapabilityExecutor(backend).execute(
        _chain_spec(), session_id="missing-produced-identity"
    )

    assert result.success is False
    assert ("mutate", "create_component") in backend.events
    assert ("mutate", "create_sketch") not in backend.events
    assert "create_sketch" not in backend.bound


@pytest.mark.asyncio
async def test_returned_identity_must_match_materialized_consumer_target() -> None:
    backend = _MaterializingBackend()
    backend.mismatched_proof_for.add("fixture")

    result = await CapabilityExecutor(backend).execute(
        _chain_spec(), session_id="mismatched-produced-identity"
    )

    assert result.success is False
    assert ("mutate", "create_component") in backend.events
    assert ("mutate", "create_sketch") not in backend.events
    assert "create_sketch" not in backend.bound


@pytest.mark.asyncio
async def test_planned_reference_without_dependency_is_zero_dispatch() -> None:
    backend = _MaterializingBackend()

    with pytest.raises(AuthorityDeniedError, match="declared dependency"):
        await CapabilityExecutor(backend).execute(
            _chain_spec(include_dependencies=False)
        )

    assert backend.events == []
