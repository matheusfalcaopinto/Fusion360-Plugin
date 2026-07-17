from __future__ import annotations

from dataclasses import asdict, dataclass
from types import SimpleNamespace

import pytest

from agent_core import authority
from agent_core.authority import (
    AuthorityDeniedError,
    CadTargetBinding,
    LegacyOutputOperation,
    cad_graph_target_producers,
    cad_operation_produced_targets,
    cad_operation_target_requirements,
)


def _operation(kind: str, **fields: object) -> SimpleNamespace:
    return SimpleNamespace(kind=kind, **fields)


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        (
            _operation(
                "feature.pattern",
                target_refs=("body-a", "body-b"),
                axis_ref="axis-a",
                path_ref="path-a",
            ),
            (
                ("geometry", "body-a"),
                ("geometry", "body-b"),
                ("axis", "axis-a"),
                ("path", "path-a"),
            ),
        ),
        (
            _operation(
                "feature.mirror",
                target_refs=("body-a", "body-b"),
                plane_ref="plane-a",
            ),
            (
                ("geometry", "body-a"),
                ("geometry", "body-b"),
                ("plane", "plane-a"),
            ),
        ),
        (
            _operation(
                "feature.boolean",
                target_ref="body-a",
                tool_refs=("tool-a", "tool-b"),
            ),
            (("body", "body-a"), ("body", "tool-a"), ("body", "tool-b")),
        ),
        (
            _operation(
                "assembly.joint",
                parent_ref="occurrence-a",
                child_ref="occurrence-b",
            ),
            (("occurrence", "occurrence-a"), ("occurrence", "occurrence-b")),
        ),
        (
            _operation(
                "assembly.rigid_group",
                occurrence_refs=("occurrence-a", "occurrence-b"),
            ),
            (("occurrence", "occurrence-a"), ("occurrence", "occurrence-b")),
        ),
        (
            _operation("experimental.sheet_metal", target_ref="sheet-body"),
            (("geometry", "sheet-body"),),
        ),
        (
            _operation("experimental.cam", target_ref="cam-body"),
            (("geometry", "cam-body"),),
        ),
        (
            _operation("io.export", target_ref="export-body"),
            (("export_target", "export-body"),),
        ),
        (_operation("analysis.physical_properties"), ()),
    ],
)
def test_cad_target_requirement_registry_covers_remaining_operation_families(
    operation: SimpleNamespace,
    expected: tuple[tuple[str, str], ...],
) -> None:
    assert cad_operation_target_requirements(operation) == expected


def test_cad_target_requirement_registry_rejects_unknown_operation_kind() -> None:
    with pytest.raises(
        AuthorityDeniedError,
        match="operation target binding registry does not support feature.unknown",
    ):
        cad_operation_target_requirements(_operation("feature.unknown"))


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        (
            _operation("component.create", name="fixture"),
            (("component", "fixture"),),
        ),
        (
            _operation("sketch.create", name="profile-sketch"),
            (("sketch", "profile-sketch"),),
        ),
        (
            _operation("sketch.rectangle", result_ref="rectangle-profile"),
            (("profile", "rectangle-profile"),),
        ),
        (
            _operation("sketch.circle", result_ref="circle-profile"),
            (("profile", "circle-profile"),),
        ),
        (
            _operation(
                "feature.extrude",
                operation="new_body",
                result_name="extruded-body",
            ),
            (("body", "extruded-body"), ("geometry", "extruded-body")),
        ),
        (
            _operation(
                "feature.revolve",
                operation="new_body",
                result_name="revolved-body",
            ),
            (("body", "revolved-body"), ("geometry", "revolved-body")),
        ),
        (
            _operation(
                "feature.sweep",
                operation="new_body",
                result_name="swept-body",
            ),
            (("body", "swept-body"), ("geometry", "swept-body")),
        ),
        (
            _operation(
                "feature.loft",
                operation="new_body",
                result_name="lofted-body",
            ),
            (("body", "lofted-body"), ("geometry", "lofted-body")),
        ),
        (
            _operation("io.import", component_name="imported-component"),
            (("component", "imported-component"),),
        ),
        (
            _operation(
                "feature.extrude",
                operation="join",
                result_name="existing-body",
            ),
            (),
        ),
        (_operation("analysis.physical_properties"), ()),
    ],
)
def test_cad_target_producer_registry_is_exhaustive_for_declared_outputs(
    operation: SimpleNamespace,
    expected: tuple[tuple[str, str], ...],
) -> None:
    assert cad_operation_produced_targets(operation) == expected


def test_cad_graph_requires_declared_dependency_on_planned_target() -> None:
    spec = SimpleNamespace(
        operations=(
            _operation(
                "component.create",
                id="create-component",
                name="fixture",
                parent_ref=None,
                depends_on=(),
            ),
            _operation(
                "sketch.create",
                id="create-sketch",
                name="fixture-sketch",
                component_ref="fixture",
                depends_on=(),
            ),
        )
    )

    with pytest.raises(
        AuthorityDeniedError,
        match="without a declared dependency on create-component",
    ):
        cad_graph_target_producers(spec)


def test_cad_graph_rejects_duplicate_target_producers() -> None:
    spec = SimpleNamespace(
        operations=(
            _operation(
                "component.create",
                id="create-component-a",
                name="fixture",
                parent_ref=None,
                depends_on=(),
            ),
            _operation(
                "component.create",
                id="create-component-b",
                name="fixture",
                parent_ref=None,
                depends_on=(),
            ),
        )
    )

    with pytest.raises(
        AuthorityDeniedError,
        match="CAD graph target 'fixture' has multiple producers",
    ):
        cad_graph_target_producers(spec)


def test_cad_graph_tracks_transitive_producers_for_declared_dependencies() -> None:
    spec = SimpleNamespace(
        operations=(
            _operation(
                "component.create",
                id="create-component",
                name="fixture",
                parent_ref=None,
                depends_on=(),
            ),
            _operation(
                "sketch.create",
                id="create-sketch",
                name="fixture-sketch",
                component_ref="fixture",
                depends_on=("create-component",),
            ),
            _operation(
                "sketch.rectangle",
                id="create-profile",
                sketch_ref="fixture-sketch",
                result_ref="fixture-profile",
                depends_on=("create-sketch",),
            ),
        )
    )

    producers = cad_graph_target_producers(spec)

    assert producers["create-sketch"] == {("component", "fixture"): "create-component"}
    assert producers["create-profile"] == {
        ("sketch", "fixture-sketch"): "create-sketch"
    }


def _legacy_operation(kind: str) -> LegacyOutputOperation:
    return LegacyOutputOperation(
        id="legacy-output",
        kind=kind,
        path="output.step" if kind == "legacy.export" else "capture.png",
        format="step" if kind == "legacy.export" else "png",
        target_identity="export-body" if kind == "legacy.export" else "ignored",
    )


def _legacy_binding(
    *,
    reference_kind: str = "export_target",
    requested_ref: str = "export-body",
    document_identity: str = "d" * 64,
    entity_identity: str = "e" * 64,
    fingerprint: str = "f" * 64,
) -> CadTargetBinding:
    return CadTargetBinding(
        reference_kind=reference_kind,
        requested_ref=requested_ref,
        document_identity=document_identity,
        entity_identity=entity_identity,
        fingerprint=fingerprint,
    )


@pytest.mark.parametrize(
    ("operation", "binding"),
    [
        (_legacy_operation("legacy.export"), _legacy_binding()),
        (
            _legacy_operation("legacy.capture"),
            _legacy_binding(
                reference_kind="active_document",
                requested_ref="active_document",
            ),
        ),
    ],
)
def test_legacy_target_binding_accepts_exact_export_and_capture_identity(
    operation: LegacyOutputOperation,
    binding: CadTargetBinding,
) -> None:
    bindings = (binding,)

    assert authority._validated_legacy_target_bindings(operation, bindings) is bindings


@pytest.mark.parametrize("bindings", [(), (_legacy_binding(), _legacy_binding())])
def test_legacy_target_binding_requires_exactly_one_binding(
    bindings: tuple[CadTargetBinding, ...],
) -> None:
    with pytest.raises(
        AuthorityDeniedError,
        match="legacy host output requires exactly one target binding",
    ):
        authority._validated_legacy_target_bindings(
            _legacy_operation("legacy.export"), bindings
        )


@pytest.mark.parametrize(
    "binding",
    [
        _legacy_binding(reference_kind="active_document"),
        _legacy_binding(requested_ref="different-body"),
    ],
)
def test_legacy_target_binding_rejects_operation_mismatch(
    binding: CadTargetBinding,
) -> None:
    with pytest.raises(
        AuthorityDeniedError,
        match="legacy output target binding does not match the operation",
    ):
        authority._validated_legacy_target_bindings(
            _legacy_operation("legacy.export"), (binding,)
        )


@pytest.mark.parametrize(
    "binding",
    [
        _legacy_binding(document_identity=""),
        _legacy_binding(entity_identity=" "),
        _legacy_binding(fingerprint=""),
    ],
)
def test_legacy_target_binding_rejects_incomplete_identity_proof(
    binding: CadTargetBinding,
) -> None:
    with pytest.raises(
        AuthorityDeniedError,
        match="legacy output target identity proof is incomplete",
    ):
        authority._validated_legacy_target_bindings(
            _legacy_operation("legacy.export"), (binding,)
        )


@pytest.mark.parametrize(
    ("binding", "message"),
    [
        (
            _legacy_binding(document_identity="g" * 64),
            "legacy document binding fingerprint is invalid",
        ),
        (
            _legacy_binding(entity_identity="g" * 64),
            "legacy entity binding fingerprint is invalid",
        ),
        (
            _legacy_binding(fingerprint="g" * 64),
            "legacy target binding fingerprint is invalid",
        ),
    ],
)
def test_legacy_target_binding_rejects_invalid_fingerprint_format(
    binding: CadTargetBinding,
    message: str,
) -> None:
    with pytest.raises(AuthorityDeniedError, match=message):
        authority._validated_legacy_target_bindings(
            _legacy_operation("legacy.export"), (binding,)
        )


@dataclass(frozen=True)
class _DigestFixture:
    name: str
    count: int


def test_model_digest_serializes_dataclasses_with_the_shared_json_contract() -> None:
    fixture = _DigestFixture(name="fixture", count=2)

    assert authority._model_digest(fixture) == authority._json_digest(asdict(fixture))
