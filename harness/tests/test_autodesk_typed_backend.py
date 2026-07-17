from __future__ import annotations

import ast
import hashlib
import json
import os
import stat
import sys
import types
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agent_core.authority import (
    AuthorityBroker,
    AuthorityPolicy,
    CadTargetBinding,
    CapabilityLedger,
    HostOutputDisabledError,
)
from agent_core.capability_executor import CapabilityExecutor
from cad_spec.v2 import CadSpecV2
from fusion_mcp_adapter.tool_result import ToolDefinition, ToolManifest, ToolResult
from fusion_tool_facade import vendor_facade as vendor_facade_module
from fusion_tool_facade.autodesk_typed_backend import (
    AutodeskTypedBackend,
    _parameter_set_script,
)
from fusion_tool_facade.vendor_facade import (
    _crud_analyze_interference_script,
    _crud_capture_viewport_script,
    _crud_create_component_script,
    _crud_create_sketch_script,
    _crud_draw_circle_script,
    _crud_draw_rectangle_script,
    _crud_extrude_script,
    _crud_measure_physical_properties_script,
    _typed_export_script,
    _typed_import_script,
)


def _fake_target_binding(reference_kind: str, requested_ref: str) -> dict[str, str]:
    document_identity = _document_binding(
        data_id="current-document",
        version_id="current-v1",
        root_token="current-root-token",
    )["document_identity"]
    identity_ref = f"{reference_kind}:{requested_ref}"
    return {
        "reference_kind": reference_kind,
        "requested_ref": requested_ref,
        "document_identity": document_identity,
        "entity_identity": hashlib.sha256(identity_ref.encode("utf-8")).hexdigest(),
        "fingerprint": hashlib.sha256(
            f"proof:{identity_ref}".encode("utf-8")
        ).hexdigest(),
    }


def test_analysis_scripts_emit_typed_complete_evidence_without_raw_errors() -> None:
    scripts = [
        _crud_analyze_interference_script({"target": "fixture"}),
        _crud_measure_physical_properties_script({"targets": ["fixture"]}),
    ]
    for script in scripts:
        compile(script, "<analysis-evidence>", "exec")
        assert "fusion_agent.evidence.v1" in script
        assert "binding_identity" not in script
        assert "str(exc)" not in script
        assert "allow_nan=False" in script


class Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def list_tools(self):
        raise AssertionError("manifest is already supplied")

    async def call_tool(self, name, arguments, *, options=None):
        self.calls.append((name, arguments))
        script = str(((arguments.get("object") or {}).get("script")) or "")
        if "binding = _active_document_binding(design)" in script:
            return ToolResult.success(
                message=json.dumps(
                    {
                        "success": True,
                        "binding": _document_binding(
                            data_id="current-document",
                            version_id="current-v1",
                            root_token="current-root-token",
                        ),
                    }
                )
            )
        if "binding = _parameter_binding(design, PAYLOAD" in script:
            payload_line = next(
                line for line in script.splitlines() if line.startswith("PAYLOAD = ")
            )
            literal = payload_line.removeprefix("PAYLOAD = json.loads(")
            encoded = ast.literal_eval(literal[:-1])
            parameter_name = json.loads(encoded)["name"]
            document_identity = _document_binding(
                data_id="current-document",
                version_id="current-v1",
                root_token="current-root-token",
            )["document_identity"]
            absence_facts = {
                "reference_kind": "parameter_absent",
                "requested_ref": parameter_name,
                "document_identity": document_identity,
                "entity_identity": hashlib.sha256(
                    json.dumps(
                        {
                            "document_identity": document_identity,
                            "name": parameter_name,
                            "state": "absent",
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
                "name": parameter_name,
                "object_type": "",
                "state": "absent",
            }
            binding = {
                key: value
                for key, value in absence_facts.items()
                if key not in {"name", "object_type", "state"}
            }
            binding["fingerprint"] = hashlib.sha256(
                json.dumps(absence_facts, sort_keys=True, separators=(",", ":")).encode(
                    "utf-8"
                )
            ).hexdigest()
            return ToolResult.success(
                message=json.dumps({"success": True, "bindings": [binding]})
            )
        if "bindings = [" in script and "target_binding_descriptors" in script:
            payload_line = next(
                line for line in script.splitlines() if line.startswith("PAYLOAD = ")
            )
            literal = payload_line.removeprefix("PAYLOAD = json.loads(")
            encoded = ast.literal_eval(literal[:-1])
            descriptors = json.loads(encoded)["target_binding_descriptors"]
            bindings = [
                _fake_target_binding(
                    descriptor["reference_kind"], descriptor["requested_ref"]
                )
                for descriptor in descriptors
            ]
            return ToolResult.success(
                message=json.dumps({"success": True, "bindings": bindings})
            )
        if script:
            payload_line = next(
                (line for line in script.splitlines() if line.startswith("PAYLOAD = ")),
                None,
            )
            payload: dict[str, Any] = {}
            if payload_line is not None:
                literal = payload_line.removeprefix("PAYLOAD = json.loads(")
                payload = json.loads(ast.literal_eval(literal[:-1]))
            produced: list[dict[str, str]] = []
            produced_profile_resolver: dict[str, Any] | None = None
            if "occurrences.addNewComponent" in script:
                produced.append(_fake_target_binding("component", payload["name"]))
            elif "component.sketches.add" in script:
                produced.append(_fake_target_binding("sketch", payload["name"]))
            elif "addCenterPointRectangle" in script or "addByCenterRadius" in script:
                produced.append(_fake_target_binding("profile", payload["result_ref"]))
                produced_profile_resolver = {
                    "sketch": payload["sketch"],
                    "index": 0,
                }
            elif "extrudeFeatures.addSimple" in script:
                if payload.get("operation") == "new_body":
                    produced.extend(
                        _fake_target_binding(kind, payload["body_name"])
                        for kind in ("body", "geometry")
                    )
            response: dict[str, Any] = {
                "success": True,
                "produced_target_bindings": produced,
            }
            if produced_profile_resolver is not None:
                response.update(
                    {
                        "profile_ref": payload["result_ref"],
                        "produced_profile_resolver": produced_profile_resolver,
                    }
                )
            return ToolResult.success(message=json.dumps(response))
        return ToolResult.success(message='{"success":true}')


class PreexistingProfileClient(Client):
    def __init__(self, *, produced_profile_index: int) -> None:
        super().__init__()
        self.produced_profile_index = produced_profile_index
        self.profile_resolver_indices: list[int] = []
        self.produced_profile = _fake_target_binding("profile", "fixture_profile_ref")
        self.preexisting_profile = {
            **self.produced_profile,
            "entity_identity": hashlib.sha256(b"preexisting-profile-token").hexdigest(),
            "fingerprint": hashlib.sha256(b"preexisting-profile-proof").hexdigest(),
        }

    async def call_tool(self, name, arguments, *, options=None):
        script = str(((arguments.get("object") or {}).get("script")) or "")
        if "bindings = [" in script and "target_binding_descriptors" in script:
            self.calls.append((name, arguments))
            payload_line = next(
                line for line in script.splitlines() if line.startswith("PAYLOAD = ")
            )
            literal = payload_line.removeprefix("PAYLOAD = json.loads(")
            descriptors = json.loads(ast.literal_eval(literal[:-1]))[
                "target_binding_descriptors"
            ]
            bindings = []
            for descriptor in descriptors:
                if descriptor["reference_kind"] != "profile":
                    bindings.append(
                        _fake_target_binding(
                            descriptor["reference_kind"],
                            descriptor["requested_ref"],
                        )
                    )
                    continue
                index = int(descriptor["resolver"]["reference"]["index"])
                self.profile_resolver_indices.append(index)
                bindings.append(
                    self.produced_profile if index == 1 else self.preexisting_profile
                )
            return ToolResult.success(
                message=json.dumps({"success": True, "bindings": bindings})
            )
        if "addCenterPointRectangle" in script:
            self.calls.append((name, arguments))
            return ToolResult.success(
                message=json.dumps(
                    {
                        "success": True,
                        "profile_ref": "fixture_profile_ref",
                        "produced_profile_resolver": {
                            "sketch": "fixture_profile",
                            "index": self.produced_profile_index,
                        },
                        "produced_target_bindings": [self.produced_profile],
                    }
                )
            )
        return await super().call_tool(name, arguments, options=options)


def _manifest(*names: str) -> ToolManifest:
    return ToolManifest(
        source="autodesk-test",
        tools=[ToolDefinition(name=name) for name in names],
    )


def _backend(client: Client | None = None) -> AutodeskTypedBackend:
    return AutodeskTypedBackend.from_client(
        client or Client(),
        _manifest("fusion_mcp_read", "fusion_mcp_execute"),
    )


def _contract(operations: list[dict]) -> CadSpecV2:
    return CadSpecV2.model_validate(
        {
            "cad_spec_version": "2.0",
            "intent": "Exercise fixed Autodesk capability packs",
            "requirements": [
                {
                    "id": "typed_result",
                    "description": "Typed result exists",
                    "assertion_ids": ["result_exists"],
                }
            ],
            "operations": operations,
            "assertions": [
                {
                    "id": "result_exists",
                    "kind": "entity_exists",
                    "target_ref": "result",
                }
            ],
        }
    )


def _capability_operations() -> list[dict]:
    return [
        {
            "id": "create_profile_a",
            "kind": "sketch.create",
            "component_ref": "root",
            "name": "profile_a_sketch",
        },
        {
            "id": "draw_profile_a",
            "kind": "sketch.rectangle",
            "sketch_ref": "profile_a_sketch",
            "width": "10 mm",
            "height": "5 mm",
            "result_ref": "profile_a",
        },
        {
            "id": "constrain_profile_a",
            "kind": "sketch.constraint",
            "sketch_ref": "profile_a_sketch",
            "constraint": "horizontal",
            "entity_refs": ["line#0"],
        },
        {
            "id": "dimension_profile_a",
            "kind": "sketch.dimension",
            "sketch_ref": "profile_a_sketch",
            "dimension": "distance",
            "entity_refs": ["line#0"],
            "expression": "10 mm",
        },
        {
            "id": "create_profile_b",
            "kind": "sketch.create",
            "component_ref": "root",
            "name": "profile_b_sketch",
        },
        {
            "id": "draw_profile_b",
            "kind": "sketch.circle",
            "sketch_ref": "profile_b_sketch",
            "diameter": "4 mm",
            "result_ref": "profile_b",
        },
        {
            "id": "create_path",
            "kind": "sketch.create",
            "component_ref": "root",
            "name": "path_sketch",
        },
        {
            "id": "draw_path_lines",
            "kind": "sketch.rectangle",
            "sketch_ref": "path_sketch",
            "width": "20 mm",
            "height": "10 mm",
            "result_ref": "unused_path_profile",
        },
        {
            "id": "revolve_profile",
            "kind": "feature.revolve",
            "component_ref": "root",
            "profile_ref": "profile_a",
            "axis_ref": "x_axis",
            "angle": "360 deg",
            "result_name": "RevolvedBody",
        },
        {
            "id": "sweep_profile",
            "kind": "feature.sweep",
            "component_ref": "root",
            "profile_ref": "profile_b",
            "path_ref": "path_sketch/line#0",
            "orientation": "parallel",
            "result_name": "SweptBody",
        },
        {
            "id": "loft_profiles",
            "kind": "feature.loft",
            "component_ref": "root",
            "profile_refs": ["profile_a", "profile_b"],
            "result_name": "LoftedBody",
        },
        {
            "id": "rectangular_pattern",
            "kind": "feature.pattern",
            "pattern": "rectangular",
            "target_refs": ["RevolvedBody"],
            "count": 3,
            "spacing": "12 mm",
        },
        {
            "id": "circular_pattern",
            "kind": "feature.pattern",
            "pattern": "circular",
            "target_refs": ["SweptBody"],
            "count": 4,
            "axis_ref": "z",
        },
        {
            "id": "path_pattern",
            "kind": "feature.pattern",
            "pattern": "path",
            "target_refs": ["LoftedBody"],
            "count": 2,
            "spacing": "8 mm",
            "path_ref": "path_sketch/line#1",
        },
        {
            "id": "mirror_body",
            "kind": "feature.mirror",
            "target_refs": ["RevolvedBody"],
            "plane_ref": "YZ_plane",
            "result_prefix": "MirroredBody",
        },
        {
            "id": "combine_bodies",
            "kind": "feature.boolean",
            "operation": "join",
            "target_ref": "RevolvedBody",
            "tool_refs": ["SweptBody"],
        },
        {
            "id": "split_body",
            "kind": "feature.boolean",
            "operation": "split",
            "target_ref": "LoftedBody",
            "tool_refs": ["SweptBody"],
        },
        {
            "id": "make_rigid",
            "kind": "assembly.rigid_group",
            "name": "CarriageGroup",
            "occurrence_refs": ["Carriage:1", "Motor:1"],
        },
        {
            "id": "import_fixture",
            "kind": "io.import",
            "path": r"C:\fixtures\fixture.step",
            "format": "step",
            "component_name": "ImportedFixture",
        },
        {
            "id": "export_fixture",
            "kind": "io.export",
            "target_ref": "RevolvedBody",
            "path": r"C:\exports\fixture.iges",
            "format": "iges",
        },
    ]


def test_autodesk_preflight_compiles_every_fixed_capability_script() -> None:
    backend = _backend()
    spec = _contract(
        [
            operation
            for operation in _capability_operations()
            if operation["kind"] != "io.export"
        ]
    )

    backend.preflight_operations(list(spec.operations))

    expected = {
        "sketch_constraints",
        "sketch_dimensions",
        "revolve",
        "sweep",
        "loft",
        "pattern_rectangular",
        "pattern_circular",
        "pattern_path",
        "mirror",
        "boolean",
        "split_body",
        "rigid_groups",
        "import_step",
    }
    assert expected <= backend.capabilities
    assert not any(name.startswith("export_") for name in backend.capabilities)
    assert set(backend._prepared) == {
        operation.id
        for operation in spec.operations
        if operation.kind
        in {
            "sketch.constraint",
            "sketch.dimension",
            "feature.revolve",
            "feature.sweep",
            "feature.loft",
            "feature.pattern",
            "feature.mirror",
            "feature.boolean",
            "assembly.rigid_group",
            "io.import",
        }
    }
    for operation_id, plan in backend._prepared.items():
        compile(plan.script, f"<{operation_id}>", "exec")
        # Host I/O scripts include the descriptor-backed promotion boundary;
        # keep a finite envelope while allowing the fixed security helpers.
        assert len(plan.script.encode("utf-8")) < 40 * 1024
        assert "execute_code" not in plan.script
        assert "eval(" not in plan.script
        assert "exec(" not in plan.script


@pytest.mark.asyncio
async def test_late_malformed_reference_blocks_every_dispatch() -> None:
    client = Client()
    backend = _backend(client)
    spec = _contract(
        [
            {
                "id": "valid_constraint",
                "kind": "sketch.constraint",
                "sketch_ref": "ExistingSketch",
                "constraint": "fixed",
                "entity_refs": ["line#0"],
            },
            {
                "id": "invalid_path_pattern",
                "kind": "feature.pattern",
                "pattern": "path",
                "target_refs": ["ExistingBody"],
                "count": 2,
                "spacing": "5 mm",
                "path_ref": "ExistingSketch/point#0",
            },
        ]
    )

    with pytest.raises(ValueError, match="line, arc, or curve"):
        await CapabilityExecutor(backend).execute(spec)
    assert client.calls == []
    assert backend._prepared == {}


@pytest.mark.asyncio
async def test_prepared_script_integrity_rejects_substitution_without_dispatch() -> (
    None
):
    client = Client()
    backend = _backend(client)
    spec = _contract(
        [
            {
                "id": "fixed_line",
                "kind": "sketch.constraint",
                "sketch_ref": "ExistingSketch",
                "constraint": "fixed",
                "entity_refs": ["line#0"],
            }
        ]
    )
    binding_payload = _document_binding(
        data_id="current-document",
        version_id="current-v1",
        root_token="current-root-token",
    )
    binding = CadTargetBinding(**binding_payload)
    sketch_binding = CadTargetBinding(
        **_cad_entity_binding(
            reference_kind="sketch",
            requested_ref="ExistingSketch",
            token="existing-sketch-token",
            document_binding=binding_payload,
            name="ExistingSketch",
            object_type="adsk::fusion::Sketch",
        )
    )
    line_binding = CadTargetBinding(
        **_cad_entity_binding(
            reference_kind="sketch_entity",
            requested_ref="ExistingSketch::line#0",
            token="existing-line-token",
            document_binding=binding_payload,
            name="",
            object_type="adsk::fusion::SketchLine",
        )
    )
    broker = AuthorityBroker(AuthorityPolicy.deny_all(), ledger=CapabilityLedger())
    graph = broker.prepare_graph(
        spec,
        session_id="integrity-test",
        provider=backend.provider,
        target_bindings_by_operation={
            "fixed_line": (binding, sketch_binding, line_binding)
        },
    )
    bound = graph.operations[0]
    backend.preflight_bound_operations([bound])
    plan = backend._prepared["fixed_line"]
    backend._prepared["fixed_line"] = replace(
        plan,
        script=plan.script + "\n# substituted code",
    )

    broker.claim(bound)
    with pytest.raises(RuntimeError, match="integrity"):
        await backend.execute_bound_operation(bound)
    broker.fail(bound, outcome_unknown=False)
    assert client.calls == []


@pytest.mark.asyncio
async def test_fixed_operation_dispatches_only_autodesk_crud_tool() -> None:
    client = Client()
    backend = _backend(client)
    spec = _contract(
        [
            {
                "id": "fixed_line",
                "kind": "sketch.constraint",
                "sketch_ref": "ExistingSketch",
                "constraint": "fixed",
                "entity_refs": ["line#0"],
            }
        ]
    )

    result = await CapabilityExecutor(backend).execute(spec)

    assert result.success is True
    assert [name for name, _arguments in client.calls] == [
        "fusion_mcp_execute",
        "fusion_mcp_execute",
        "fusion_mcp_execute",
    ]
    resolver_script = client.calls[1][1]["object"]["script"]
    assert "target_binding_descriptors" in resolver_script
    assert "ExistingSketch::line#0" in resolver_script
    script = client.calls[2][1]["object"]["script"]
    assert "PAYLOAD = json.loads" in script
    assert ".isFixed = True" in script
    assert "target_bindings" in script
    assert "ExistingSketch::line#0" in script
    assert "execute_code" not in script


@pytest.mark.asyncio
async def test_parameter_set_resolves_absence_and_carries_proof_into_fixed_sink() -> (
    None
):
    client = Client()
    backend = _backend(client)
    spec = _contract(
        [
            {
                "id": "set_shaft_diameter",
                "kind": "parameter.set",
                "name": "shaft_diameter",
                "expression": "10 mm",
            }
        ]
    )

    result = await CapabilityExecutor(backend).execute(spec)

    assert result.success is True
    assert [name for name, _arguments in client.calls] == [
        "fusion_mcp_execute",
        "fusion_mcp_execute",
        "fusion_mcp_execute",
    ]
    resolver_script = client.calls[1][1]["object"]["script"]
    mutation_script = client.calls[2][1]["object"]["script"]
    assert "_parameter_binding(design, PAYLOAD" in resolver_script
    assert '"parameter_absent"' in mutation_script
    assert (
        "parameter target binding changed after capability issuance" in mutation_script
    )
    assert "create_parameter" not in [name for name, _arguments in client.calls]


@pytest.mark.asyncio
async def test_autodesk_created_targets_are_not_resolved_before_their_producers() -> (
    None
):
    client = Client()
    backend = _backend(client)
    spec = _contract(
        [
            {
                "id": "create_fixture",
                "kind": "component.create",
                "name": "fixture",
            },
            {
                "id": "create_profile_sketch",
                "kind": "sketch.create",
                "component_ref": "fixture",
                "name": "fixture_profile",
                "depends_on": ["create_fixture"],
            },
            {
                "id": "draw_fixture_profile",
                "kind": "sketch.rectangle",
                "sketch_ref": "fixture_profile",
                "width": "10 mm",
                "height": "5 mm",
                "result_ref": "fixture_profile_ref",
                "depends_on": ["create_profile_sketch"],
            },
            {
                "id": "extrude_fixture",
                "kind": "feature.extrude",
                "component_ref": "fixture",
                "profile_ref": "fixture_profile_ref",
                "distance": "4 mm",
                "result_name": "FixtureBody",
                "depends_on": ["draw_fixture_profile"],
            },
        ]
    )

    result = await CapabilityExecutor(backend).execute(spec)

    assert result.success is True
    scripts = [arguments["object"]["script"] for _name, arguments in client.calls]
    component_mutation = next(
        index
        for index, script in enumerate(scripts)
        if "occurrences.addNewComponent" in script
    )
    component_resolution = next(
        index
        for index, script in enumerate(scripts)
        if '"requested_ref":"fixture"' in script
        and "target_binding_descriptors" in script
    )
    assert component_resolution > component_mutation
    rectangle_mutation = next(
        index
        for index, script in enumerate(scripts)
        if "addCenterPointRectangle" in script
    )
    profile_resolution = next(
        index
        for index, script in enumerate(scripts)
        if '"requested_ref":"fixture_profile_ref"' in script
        and "target_binding_descriptors" in script
    )
    assert profile_resolution > rectangle_mutation


@pytest.mark.asyncio
async def test_planned_profile_consumer_uses_actual_produced_profile_index() -> None:
    client = PreexistingProfileClient(produced_profile_index=1)
    backend = _backend(client)
    spec = _contract(
        [
            {
                "id": "create_fixture",
                "kind": "component.create",
                "name": "fixture",
            },
            {
                "id": "create_profile_sketch",
                "kind": "sketch.create",
                "component_ref": "fixture",
                "name": "fixture_profile",
                "depends_on": ["create_fixture"],
            },
            {
                "id": "draw_fixture_profile",
                "kind": "sketch.rectangle",
                "sketch_ref": "fixture_profile",
                "width": "10 mm",
                "height": "5 mm",
                "result_ref": "fixture_profile_ref",
                "depends_on": ["create_profile_sketch"],
            },
            {
                "id": "extrude_fixture",
                "kind": "feature.extrude",
                "component_ref": "fixture",
                "profile_ref": "fixture_profile_ref",
                "distance": "4 mm",
                "result_name": "FixtureBody",
                "depends_on": ["draw_fixture_profile"],
            },
        ]
    )

    result = await CapabilityExecutor(backend).execute(spec)

    assert result.success is True
    assert client.profile_resolver_indices == [1]
    scripts = [arguments["object"]["script"] for _name, arguments in client.calls]
    assert any("extrudeFeatures.addSimple" in script for script in scripts)


@pytest.mark.asyncio
async def test_planned_profile_locator_mismatch_blocks_consumer_dispatch() -> None:
    client = PreexistingProfileClient(produced_profile_index=0)
    backend = _backend(client)
    spec = _contract(
        [
            {
                "id": "create_fixture",
                "kind": "component.create",
                "name": "fixture",
            },
            {
                "id": "create_profile_sketch",
                "kind": "sketch.create",
                "component_ref": "fixture",
                "name": "fixture_profile",
                "depends_on": ["create_fixture"],
            },
            {
                "id": "draw_fixture_profile",
                "kind": "sketch.rectangle",
                "sketch_ref": "fixture_profile",
                "width": "10 mm",
                "height": "5 mm",
                "result_ref": "fixture_profile_ref",
                "depends_on": ["create_profile_sketch"],
            },
            {
                "id": "extrude_fixture",
                "kind": "feature.extrude",
                "component_ref": "fixture",
                "profile_ref": "fixture_profile_ref",
                "distance": "4 mm",
                "result_name": "FixtureBody",
                "depends_on": ["draw_fixture_profile"],
            },
        ]
    )

    result = await CapabilityExecutor(backend).execute(spec)

    assert result.success is False
    assert client.profile_resolver_indices == [0]
    scripts = [arguments["object"]["script"] for _name, arguments in client.calls]
    assert not any("extrudeFeatures.addSimple" in script for script in scripts)


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch_target", [False, True])
async def test_extrude_cut_requires_exact_produced_target_before_dispatch(
    mismatch_target: bool,
) -> None:
    class CutTargetClient(Client):
        async def call_tool(self, name, arguments, *, options=None):
            script = str(((arguments.get("object") or {}).get("script")) or "")
            if "bindings = [" in script and "target_binding_descriptors" in script:
                payload_line = next(
                    line
                    for line in script.splitlines()
                    if line.startswith("PAYLOAD = ")
                )
                literal = payload_line.removeprefix("PAYLOAD = json.loads(")
                descriptors = json.loads(ast.literal_eval(literal[:-1]))[
                    "target_binding_descriptors"
                ]
                if any(item["reference_kind"] == "body" for item in descriptors):
                    self.calls.append((name, arguments))
                    bindings = [
                        _fake_target_binding(
                            descriptor["reference_kind"],
                            descriptor["requested_ref"],
                        )
                        for descriptor in descriptors
                    ]
                    if mismatch_target:
                        body_index = next(
                            index
                            for index, descriptor in enumerate(descriptors)
                            if descriptor["reference_kind"] == "body"
                        )
                        bindings[body_index] = {
                            **bindings[body_index],
                            "entity_identity": hashlib.sha256(
                                b"replacement-target-body"
                            ).hexdigest(),
                            "fingerprint": hashlib.sha256(
                                b"replacement-target-proof"
                            ).hexdigest(),
                        }
                    return ToolResult.success(
                        message=json.dumps({"success": True, "bindings": bindings})
                    )
            return await super().call_tool(name, arguments, options=options)

    client = CutTargetClient()
    backend = _backend(client)
    spec = _contract(
        [
            {"id": "create_fixture", "kind": "component.create", "name": "fixture"},
            {
                "id": "create_plate_sketch",
                "kind": "sketch.create",
                "component_ref": "fixture",
                "name": "plate_sketch",
                "depends_on": ["create_fixture"],
            },
            {
                "id": "draw_plate",
                "kind": "sketch.rectangle",
                "sketch_ref": "plate_sketch",
                "width": "20 mm",
                "height": "10 mm",
                "result_ref": "plate_profile",
                "depends_on": ["create_plate_sketch"],
            },
            {
                "id": "extrude_plate",
                "kind": "feature.extrude",
                "component_ref": "fixture",
                "profile_ref": "plate_profile",
                "distance": "4 mm",
                "result_name": "plate_body",
                "depends_on": ["draw_plate"],
            },
            {
                "id": "create_hole_sketch",
                "kind": "sketch.create",
                "component_ref": "fixture",
                "name": "hole_sketch",
                "depends_on": ["extrude_plate"],
            },
            {
                "id": "draw_hole",
                "kind": "sketch.circle",
                "sketch_ref": "hole_sketch",
                "diameter": "3 mm",
                "result_ref": "hole_profile",
                "depends_on": ["create_hole_sketch"],
            },
            {
                "id": "cut_hole",
                "kind": "feature.extrude",
                "component_ref": "fixture",
                "profile_ref": "hole_profile",
                "distance": "4 mm",
                "operation": "cut",
                "target_body_ref": "plate_body",
                "result_name": "plate_body",
                "depends_on": ["draw_hole"],
            },
        ]
    )

    result = await CapabilityExecutor(backend).execute(spec)

    cut_dispatches = []
    for _tool_name, arguments in client.calls:
        script = str(((arguments.get("object") or {}).get("script")) or "")
        if "extrudeFeatures.addSimple" not in script:
            continue
        payload_line = next(
            line for line in script.splitlines() if line.startswith("PAYLOAD = ")
        )
        literal = payload_line.removeprefix("PAYLOAD = json.loads(")
        payload = json.loads(ast.literal_eval(literal[:-1]))
        if payload.get("operation") == "cut":
            cut_dispatches.append(arguments)

    assert result.success is (not mismatch_target)
    assert len(cut_dispatches) == (0 if mismatch_target else 1)


@pytest.mark.asyncio
async def test_autodesk_already_existing_component_cannot_authorize_consumer() -> None:
    class ExistingComponentClient(Client):
        async def call_tool(self, name, arguments, *, options=None):
            script = str(((arguments.get("object") or {}).get("script")) or "")
            if "occurrences.addNewComponent" in script:
                self.calls.append((name, arguments))
                return ToolResult.success(
                    message=json.dumps(
                        {
                            "success": True,
                            "component": {
                                "name": "fixture",
                                "already_exists": True,
                            },
                            "produced_target_bindings": [],
                        }
                    )
                )
            return await super().call_tool(name, arguments, options=options)

    client = ExistingComponentClient()
    backend = _backend(client)
    spec = _contract(
        [
            {
                "id": "create_fixture",
                "kind": "component.create",
                "name": "fixture",
            },
            {
                "id": "create_profile_sketch",
                "kind": "sketch.create",
                "component_ref": "fixture",
                "name": "fixture_profile",
                "depends_on": ["create_fixture"],
            },
        ]
    )

    result = await CapabilityExecutor(backend).execute(spec)

    assert result.success is False
    scripts = [arguments["object"]["script"] for _name, arguments in client.calls]
    assert any("occurrences.addNewComponent" in script for script in scripts)
    assert not any("component.sketches.add" in script for script in scripts)


def test_capabilities_require_complete_crud_pair() -> None:
    execute_only = AutodeskTypedBackend.from_client(
        Client(),
        _manifest("fusion_mcp_execute"),
    )
    direct_only = AutodeskTypedBackend.from_client(
        Client(),
        _manifest("create_parameter", "export_step", "export_stl"),
    )

    assert "revolve" not in execute_only.capabilities
    assert direct_only.capabilities == set()


@pytest.mark.parametrize("modifier", ["cut", "intersect"])
def test_autodesk_preflight_accepts_only_explicitly_bound_extrude_modifiers(
    modifier: str,
) -> None:
    client = Client()
    backend = _backend(client)
    spec = _contract(
        [
            {
                "id": "create_profile_sketch",
                "kind": "sketch.create",
                "component_ref": "root",
                "name": "profile_sketch",
            },
            {
                "id": "draw_profile",
                "kind": "sketch.circle",
                "sketch_ref": "profile_sketch",
                "diameter": "5 mm",
                "result_ref": "profile_ref",
            },
            {
                "id": "modify_body",
                "kind": "feature.extrude",
                "component_ref": "root",
                "profile_ref": "profile_ref",
                "distance": "6 mm",
                "operation": modifier,
                "target_body_ref": "fixture_body",
                "result_name": "fixture_body",
            },
        ]
    )

    backend.preflight_operations(list(spec.operations))

    assert client.calls == []


def test_extrude_modifier_missing_target_fails_before_any_provider_dispatch() -> None:
    client = Client()

    with pytest.raises(ValueError, match="requires target_body_ref"):
        _contract(
            [
                {
                    "id": "cut_body",
                    "kind": "feature.extrude",
                    "component_ref": "root",
                    "profile_ref": "profile_ref",
                    "distance": "6 mm",
                    "operation": "cut",
                    "result_name": "fixture_body",
                }
            ]
        )

    assert client.calls == []


@pytest.mark.parametrize(
    "operation",
    [
        {
            "id": "join_body",
            "kind": "feature.extrude",
            "component_ref": "root",
            "profile_ref": "profile_ref",
            "distance": "6 mm",
            "operation": "join",
            "target_body_ref": "fixture_body",
            "result_name": "fixture_body",
        },
        {
            "id": "negative_body",
            "kind": "feature.extrude",
            "component_ref": "root",
            "profile_ref": "profile_ref",
            "distance": "6 mm",
            "direction": "negative",
            "result_name": "fixture_body",
        },
        {
            "id": "cut_revolve",
            "kind": "feature.revolve",
            "component_ref": "root",
            "profile_ref": "profile_ref",
            "axis_ref": "x_axis",
            "operation": "cut",
            "target_body_ref": "fixture_body",
            "result_name": "fixture_body",
        },
        {
            "id": "cut_sweep",
            "kind": "feature.sweep",
            "component_ref": "root",
            "profile_ref": "profile_ref",
            "path_ref": "path_sketch/line#0",
            "operation": "cut",
            "target_body_ref": "fixture_body",
            "result_name": "fixture_body",
        },
        {
            "id": "cut_loft",
            "kind": "feature.loft",
            "component_ref": "root",
            "profile_refs": ["profile_ref", "second_profile_ref"],
            "operation": "cut",
            "target_body_ref": "fixture_body",
            "result_name": "fixture_body",
        },
    ],
)
def test_autodesk_unsupported_feature_modifiers_fail_preflight_without_dispatch(
    operation: dict[str, Any],
) -> None:
    client = Client()
    backend = _backend(client)
    spec = _contract(
        [
            {
                "id": "create_profile_sketch",
                "kind": "sketch.create",
                "component_ref": "root",
                "name": "profile_sketch",
            },
            {
                "id": "draw_profile",
                "kind": "sketch.circle",
                "sketch_ref": "profile_sketch",
                "diameter": "5 mm",
                "result_ref": "profile_ref",
            },
            operation,
        ]
    )

    with pytest.raises(ValueError, match="lossless|positive"):
        backend.preflight_operations(list(spec.operations))

    assert client.calls == []


def test_relative_or_mismatched_io_path_fails_during_preflight() -> None:
    backend = _backend()
    relative = _contract(
        [
            {
                "id": "import_relative",
                "kind": "io.import",
                "path": "fixture.step",
                "format": "step",
                "component_name": "Fixture",
            }
        ]
    )
    mismatched = _contract(
        [
            {
                "id": "export_mismatch",
                "kind": "io.export",
                "target_ref": "Fixture",
                "path": r"C:\exports\fixture.stl",
                "format": "iges",
            }
        ]
    )

    with pytest.raises(ValueError, match="must be absolute"):
        backend.preflight_operations(list(relative.operations))
    with pytest.raises(ValueError, match="does not match"):
        backend.preflight_operations(list(mismatched.operations))


@pytest.mark.parametrize(
    ("direction", "format_name", "extension"),
    [
        ("import", "step", "step"),
        ("import", "stp", "stp"),
        ("import", "iges", "iges"),
        ("import", "igs", "igs"),
        ("import", "sat", "sat"),
        ("import", "f3d", "f3d"),
    ],
)
def test_declared_import_formats_compile_fixed_scripts(
    direction: str,
    format_name: str,
    extension: str,
) -> None:
    backend = _backend()
    assert direction == "import"
    operation = {
        "id": f"import_{format_name}",
        "kind": "io.import",
        "path": rf"C:\fixtures\fixture.{extension}",
        "format": format_name,
        "component_name": "ImportedFixture",
    }
    spec = _contract([operation])

    backend.preflight_operations(list(spec.operations))

    plan = backend._prepared[operation["id"]]
    compile(plan.script, f"<{operation['id']}>", "exec")
    assert f'"format":"{format_name}"' in plan.script


class _Collection:
    def __init__(self, values: list[Any]) -> None:
        self.values = values

    @property
    def count(self) -> int:
        return len(self.values)

    def item(self, index: int) -> Any:
        return self.values[index]


class _SketchLines:
    def __init__(self, mutations: list[str], profiles: _Collection) -> None:
        self.mutations = mutations
        self.profiles = profiles

    def addCenterPointRectangle(self, _center: object, _corner: object) -> None:
        self.mutations.append("rectangle")
        index = self.profiles.count
        self.profiles.values.append(
            types.SimpleNamespace(
                name=f"profile-{index}",
                entityToken=f"profile-token:{index}",
                objectType="adsk::fusion::Profile",
            )
        )


class _SketchCircles:
    def __init__(self, mutations: list[str], profiles: _Collection) -> None:
        self.mutations = mutations
        self.profiles = profiles

    def addByCenterRadius(self, _center: object, _radius: float) -> None:
        self.mutations.append("circle")
        index = self.profiles.count
        self.profiles.values.append(
            types.SimpleNamespace(
                name=f"profile-{index}",
                entityToken=f"profile-token:{index}",
                objectType="adsk::fusion::Profile",
            )
        )


class _Sketch:
    def __init__(
        self,
        name: str,
        mutations: list[str],
        *,
        entity_token: str | None = None,
    ) -> None:
        self.name = name
        self.entityToken = entity_token or f"sketch-token:{name}"
        self.objectType = "adsk::fusion::Sketch"
        self.profiles = _Collection([])
        self.sketchCurves = types.SimpleNamespace(
            sketchLines=_SketchLines(mutations, self.profiles),
            sketchCircles=_SketchCircles(mutations, self.profiles),
        )


class _SketchCollection(_Collection):
    def __init__(self, values: list[_Sketch], mutations: list[str]) -> None:
        super().__init__(values)
        self.mutations = mutations

    def add(self, _plane: object) -> _Sketch:
        self.mutations.append("sketch")
        sketch = _Sketch("", self.mutations)
        self.values.append(sketch)
        return sketch


class _Component:
    def __init__(
        self, name: str, sketches: list[_Sketch], mutations: list[str]
    ) -> None:
        self.name = name
        self.entityToken = f"component-token:{name}"
        self.objectType = "adsk::fusion::Component"
        self.sketches = _SketchCollection(sketches, mutations)
        self.bRepBodies = _Collection([])
        self.occurrences = _Collection([])
        self.xYConstructionPlane = object()
        self.xZConstructionPlane = object()
        self.yZConstructionPlane = object()


class _BoundExtrudeInput:
    def __init__(self, profile: object, operation: object) -> None:
        self.profile = profile
        self.operation = operation
        self.participantBodies: list[object] = []
        self.extent: tuple[bool, object] | None = None

    def setDistanceExtent(self, symmetric: bool, distance: object) -> None:
        self.extent = (symmetric, distance)


class _BoundExtrudeFeatures:
    def __init__(self) -> None:
        self.inputs: list[_BoundExtrudeInput] = []
        self.added: list[_BoundExtrudeInput] = []

    def createInput(self, profile: object, operation: object) -> _BoundExtrudeInput:
        value = _BoundExtrudeInput(profile, operation)
        self.inputs.append(value)
        return value

    def add(self, value: _BoundExtrudeInput) -> object:
        self.added.append(value)
        return types.SimpleNamespace(name="", bodies=_Collection([]))

    def addSimple(self, *_args: object) -> object:
        raise AssertionError("modifier must not use unscoped addSimple")


def _run_crud_script(
    monkeypatch: pytest.MonkeyPatch,
    script: str,
    components: list[_Component],
    *,
    execute_run: bool = True,
    user_parameters: Any | None = None,
    import_manager: Any | None = None,
) -> dict[str, Any]:
    design = types.SimpleNamespace(
        allComponents=_Collection(components), rootComponent=components[0]
    )
    if user_parameters is not None:
        design.userParameters = user_parameters
    data_file = types.SimpleNamespace(id="current-document", versionId="current-v1")
    document = types.SimpleNamespace(dataFile=data_file)
    application = types.SimpleNamespace(
        activeProduct=design,
        activeDocument=document,
    )
    if import_manager is not None:
        application.importManager = import_manager
    core = types.ModuleType("adsk.core")
    core.Application = types.SimpleNamespace(get=lambda: application)
    core.Point3D = types.SimpleNamespace(create=lambda x, y, z: (x, y, z))
    core.Matrix3D = types.SimpleNamespace(create=lambda: object())
    fusion = types.ModuleType("adsk.fusion")
    fusion.Design = types.SimpleNamespace(cast=lambda value: value)
    adsk = types.ModuleType("adsk")
    adsk.__path__ = []  # type: ignore[attr-defined]
    adsk.core = core
    adsk.fusion = fusion
    monkeypatch.setitem(sys.modules, "adsk", adsk)
    monkeypatch.setitem(sys.modules, "adsk.core", core)
    monkeypatch.setitem(sys.modules, "adsk.fusion", fusion)
    namespace: dict[str, Any] = {}
    exec(compile(script, "<autodesk-crud-test>", "exec"), namespace)
    if execute_run:
        namespace["run"]("")
    return namespace


def _document_binding(
    *, data_id: str, version_id: str, root_token: str
) -> dict[str, str]:
    document_identity = hashlib.sha256(
        json.dumps(
            {
                "data_id": data_id,
                "version_id": version_id,
                "root_token": root_token,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    facts = {
        "reference_kind": "active_document",
        "requested_ref": "active_document",
        "document_identity": document_identity,
        "entity_identity": hashlib.sha256(root_token.encode("utf-8")).hexdigest(),
    }
    return {
        **facts,
        "fingerprint": hashlib.sha256(
            json.dumps(facts, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }


def _cad_entity_binding(
    *,
    reference_kind: str,
    requested_ref: str,
    token: str,
    document_binding: dict[str, str],
    name: str,
    object_type: str,
) -> dict[str, str]:
    facts = {
        "reference_kind": reference_kind,
        "requested_ref": requested_ref,
        "document_identity": document_binding["document_identity"],
        "entity_identity": hashlib.sha256(token.encode("utf-8")).hexdigest(),
        "name": name,
        "object_type": object_type,
    }
    return {
        key: value
        for key, value in {
            "reference_kind": reference_kind,
            "requested_ref": requested_ref,
            "document_identity": facts["document_identity"],
            "entity_identity": facts["entity_identity"],
            "fingerprint": hashlib.sha256(
                json.dumps(facts, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
        }.items()
    }


def _host_path_binding(
    path,
    root,
    *,
    direction: str,
    existed: bool,
    overwrite: bool = False,
) -> dict[str, Any]:
    if direction == "import" or existed:
        stat = path.stat()
        facts = {
            "device": int(stat.st_dev),
            "inode": int(stat.st_ino),
            "size": int(stat.st_size),
            "mtime_ns": int(stat.st_mtime_ns),
        }
        if direction == "import":
            facts["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    else:
        stat = root.stat()
        facts = {
            "parent_device": int(stat.st_dev),
            "parent_inode": int(stat.st_ino),
            "destination_absent": True,
        }
    fingerprint = hashlib.sha256(
        json.dumps(facts, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "direction": direction,
        "canonical_root": str(root.resolve()),
        "canonical_path": str(path.resolve(strict=False)),
        "existed_at_issue": existed,
        "overwrite": overwrite,
        "resource_fingerprint": fingerprint,
    }


def test_crud_missing_component_fails_before_native_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mutations: list[str] = []
    root = _Component("ActualRoot", [], mutations)
    script = _crud_create_sketch_script(
        {
            "component": "Missing",
            "plane": "XY",
            "name": "profile",
            "target_bindings": [
                {
                    "reference_kind": "component",
                    "requested_ref": "Missing",
                    "document_identity": "unreachable",
                    "entity_identity": "unreachable",
                    "fingerprint": "unreachable",
                }
            ],
        }
    )

    with pytest.raises(RuntimeError, match="component binding"):
        _run_crud_script(monkeypatch, script, [root])

    assert mutations == []


def test_crud_duplicate_sketch_fails_before_native_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mutations: list[str] = []
    root = _Component(
        "ActualRoot",
        [_Sketch("profile", mutations), _Sketch("profile", mutations)],
        mutations,
    )
    script = _crud_draw_rectangle_script(
        {
            "sketch": "profile",
            "center_x": 0.0,
            "center_y": 0.0,
            "width": 1.0,
            "height": 1.0,
            "target_bindings": [
                {
                    "reference_kind": "sketch",
                    "requested_ref": "profile",
                    "document_identity": "unreachable",
                    "entity_identity": "unreachable",
                    "fingerprint": "unreachable",
                }
            ],
        }
    )

    with pytest.raises(RuntimeError, match="sketch binding"):
        _run_crud_script(monkeypatch, script, [root])

    assert mutations == []


def test_crud_unique_component_is_a_legitimate_positive_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mutations: list[str] = []
    root = _Component("ActualRoot", [], mutations)
    fixture = _Component("Fixture", [], mutations)
    document_binding = _document_binding(
        data_id="current-document",
        version_id="current-v1",
        root_token=root.entityToken,
    )
    target_binding = _cad_entity_binding(
        reference_kind="component",
        requested_ref="Fixture",
        token=fixture.entityToken,
        document_binding=document_binding,
        name=fixture.name,
        object_type=fixture.objectType,
    )
    script = _crud_create_sketch_script(
        {
            "component": "Fixture",
            "plane": "XY",
            "name": "profile",
            "document_binding": document_binding,
            "target_bindings": [target_binding],
        }
    )

    _run_crud_script(monkeypatch, script, [root, fixture])

    assert mutations == ["sketch"]


def test_component_already_exists_returns_no_produced_identity(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mutations: list[str] = []
    root = _Component("Fixture", [], mutations)

    _run_crud_script(
        monkeypatch,
        _crud_create_component_script({"name": "Fixture"}),
        [root],
    )

    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["component"]["already_exists"] is True
    assert payload["produced_target_bindings"] == []


def test_new_component_returns_its_exact_created_identity(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mutations: list[str] = []
    root = _Component("ActualRoot", [], mutations)

    _run_crud_script(
        monkeypatch,
        _crud_create_component_script({"name": "Fixture"}),
        [root],
    )

    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    expected_document = _document_binding(
        data_id="current-document",
        version_id="current-v1",
        root_token=root.entityToken,
    )
    assert payload["produced_target_bindings"] == [
        _cad_entity_binding(
            reference_kind="component",
            requested_ref="Fixture",
            token=root.entityToken,
            document_binding=expected_document,
            name="Fixture",
            object_type=root.objectType,
        )
    ]


@pytest.mark.parametrize(
    ("builder", "shape", "geometry"),
    [
        (_crud_draw_rectangle_script, "rectangle", {"width": 1.0, "height": 1.0}),
        (_crud_draw_circle_script, "circle", {"radius": 0.5}),
    ],
)
def test_profile_producer_returns_new_identity_not_preexisting_profile_zero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    builder: Any,
    shape: str,
    geometry: dict[str, float],
) -> None:
    mutations: list[str] = []
    sketch = _Sketch("profile", mutations, entity_token="reviewed-sketch-token")
    sketch.profiles.values.append(
        types.SimpleNamespace(
            name="profile-0",
            entityToken="preexisting-profile-token",
            objectType="adsk::fusion::Profile",
        )
    )
    root = _Component("ActualRoot", [sketch], mutations)
    document_binding = _document_binding(
        data_id="current-document",
        version_id="current-v1",
        root_token=root.entityToken,
    )
    sketch_binding = _cad_entity_binding(
        reference_kind="sketch",
        requested_ref="profile",
        token=sketch.entityToken,
        document_binding=document_binding,
        name=sketch.name,
        object_type=sketch.objectType,
    )
    payload = {
        "sketch": "profile",
        "center_x": 0.0,
        "center_y": 0.0,
        "result_ref": f"profile:{shape}:result",
        "document_binding": document_binding,
        "target_bindings": [sketch_binding],
        **geometry,
    }

    _run_crud_script(monkeypatch, builder(payload), [root])

    result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    expected = _cad_entity_binding(
        reference_kind="profile",
        requested_ref=payload["result_ref"],
        token="profile-token:1",
        document_binding=document_binding,
        name="profile-1",
        object_type="adsk::fusion::Profile",
    )
    assert result["produced_target_bindings"] == [expected]
    assert result["produced_profile_resolver"] == {"sketch": "profile", "index": 1}
    assert (
        result["produced_target_bindings"][0]["entity_identity"]
        != hashlib.sha256(b"preexisting-profile-token").hexdigest()
    )
    assert mutations == [shape]


@pytest.mark.parametrize("target_matches_component", [True, False])
def test_crud_extrude_modifier_uses_only_exact_bound_participant_body(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    target_matches_component: bool,
) -> None:
    mutations: list[str] = []
    sketch = _Sketch("profile_sketch", mutations)
    profile = types.SimpleNamespace(
        name="profile-0",
        entityToken="profile-token:0",
        objectType="adsk::fusion::Profile",
    )
    sketch.profiles.values.append(profile)
    root = _Component("ActualRoot", [sketch], mutations)
    foreign = _Component("Foreign", [], mutations)
    target = types.SimpleNamespace(
        name="fixture_body",
        entityToken="fixture-body-token",
        objectType="adsk::fusion::BRepBody",
        parentComponent=root if target_matches_component else foreign,
    )
    root.bRepBodies.values.append(target)
    extrudes = _BoundExtrudeFeatures()
    root.features = types.SimpleNamespace(extrudeFeatures=extrudes)
    document_binding = _document_binding(
        data_id="current-document",
        version_id="current-v1",
        root_token=root.entityToken,
    )
    target_bindings = [
        _cad_entity_binding(
            reference_kind="component",
            requested_ref="root",
            token=root.entityToken,
            document_binding=document_binding,
            name=root.name,
            object_type=root.objectType,
        ),
        _cad_entity_binding(
            reference_kind="profile",
            requested_ref="profile_ref",
            token=profile.entityToken,
            document_binding=document_binding,
            name=profile.name,
            object_type=profile.objectType,
        ),
        _cad_entity_binding(
            reference_kind="body",
            requested_ref="fixture_body",
            token=target.entityToken,
            document_binding=document_binding,
            name=target.name,
            object_type=target.objectType,
        ),
    ]
    descriptors = [
        {
            "reference_kind": "component",
            "requested_ref": "root",
            "resolver": {"kind": "component", "reference": "root"},
        },
        {
            "reference_kind": "profile",
            "requested_ref": "profile_ref",
            "resolver": {
                "kind": "profile",
                "reference": {"sketch": "profile_sketch", "index": 0},
            },
        },
        {
            "reference_kind": "body",
            "requested_ref": "fixture_body",
            "resolver": {"kind": "body", "reference": "fixture_body"},
        },
    ]
    script = _crud_extrude_script(
        {
            "sketch": "profile_sketch",
            "distance": "6 mm",
            "operation": "cut",
            "feature_name": "cut_fixture",
            "body_name": "fixture_body",
            "target_body_ref": "fixture_body",
            "document_binding": document_binding,
            "target_bindings": target_bindings,
            "target_binding_descriptors": descriptors,
        }
    )
    namespace = _run_crud_script(
        monkeypatch,
        script,
        [root, foreign],
        execute_run=False,
    )
    sys.modules["adsk.core"].ValueInput = types.SimpleNamespace(
        createByString=lambda value: value
    )
    sys.modules["adsk.fusion"].FeatureOperations = types.SimpleNamespace(
        NewBodyFeatureOperation="new_body",
        CutFeatureOperation="cut",
        IntersectFeatureOperation="intersect",
    )

    if not target_matches_component:
        with pytest.raises(RuntimeError, match="does not belong to the component"):
            namespace["run"]("")
        assert extrudes.inputs == []
        assert extrudes.added == []
        return

    namespace["run"]("")

    result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert len(extrudes.inputs) == 1
    assert extrudes.added == extrudes.inputs
    assert extrudes.inputs[0].participantBodies == [target]
    assert extrudes.inputs[0].extent == (False, "6 mm")
    assert result["body"] == {"name": "fixture_body"}
    assert result["produced_target_bindings"] == []


def test_crud_document_drift_fails_at_sink_before_native_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mutations: list[str] = []
    root = _Component("ActualRoot", [], mutations)
    fixture = _Component("Fixture", [], mutations)
    stale_binding = _document_binding(
        data_id="previewed-document",
        version_id="previewed-v1",
        root_token="previewed-root-token",
    )
    script = _crud_create_sketch_script(
        {
            "component": "Fixture",
            "plane": "XY",
            "name": "profile",
            "document_binding": stale_binding,
        }
    )

    with pytest.raises(RuntimeError, match="document binding changed"):
        _run_crud_script(monkeypatch, script, [root, fixture])

    assert mutations == []


def test_crud_document_binding_positive_control_mutates_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mutations: list[str] = []
    root = _Component("ActualRoot", [], mutations)
    fixture = _Component("Fixture", [], mutations)
    binding = _document_binding(
        data_id="current-document",
        version_id="current-v1",
        root_token=root.entityToken,
    )
    target_binding = _cad_entity_binding(
        reference_kind="component",
        requested_ref="Fixture",
        token=fixture.entityToken,
        document_binding=binding,
        name=fixture.name,
        object_type=fixture.objectType,
    )
    script = _crud_create_sketch_script(
        {
            "component": "Fixture",
            "plane": "XY",
            "name": "profile",
            "document_binding": binding,
            "target_bindings": [target_binding],
        }
    )

    _run_crud_script(monkeypatch, script, [root, fixture])

    assert mutations == ["sketch"]


def test_crud_same_name_replacement_fails_entity_binding_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mutations: list[str] = []
    replacement = _Sketch("profile", mutations, entity_token="replacement-sketch-token")
    root = _Component("ActualRoot", [replacement], mutations)
    document_binding = _document_binding(
        data_id="current-document",
        version_id="current-v1",
        root_token=root.entityToken,
    )
    reviewed_binding = _cad_entity_binding(
        reference_kind="sketch",
        requested_ref="profile",
        token="reviewed-sketch-token",
        document_binding=document_binding,
        name="profile",
        object_type="adsk::fusion::Sketch",
    )
    script = _crud_draw_rectangle_script(
        {
            "sketch": "profile",
            "center_x": 0.0,
            "center_y": 0.0,
            "width": 1.0,
            "height": 1.0,
            "document_binding": document_binding,
            "target_bindings": [reviewed_binding],
        }
    )

    with pytest.raises(RuntimeError, match="CAD target binding changed"):
        _run_crud_script(monkeypatch, script, [root])

    assert mutations == []


def test_crud_exact_entity_binding_is_a_legitimate_positive_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mutations: list[str] = []
    sketch = _Sketch("profile", mutations, entity_token="reviewed-sketch-token")
    root = _Component("ActualRoot", [sketch], mutations)
    document_binding = _document_binding(
        data_id="current-document",
        version_id="current-v1",
        root_token=root.entityToken,
    )
    binding = _cad_entity_binding(
        reference_kind="sketch",
        requested_ref="profile",
        token=sketch.entityToken,
        document_binding=document_binding,
        name=sketch.name,
        object_type=sketch.objectType,
    )
    script = _crud_draw_rectangle_script(
        {
            "sketch": "profile",
            "center_x": 0.0,
            "center_y": 0.0,
            "width": 1.0,
            "height": 1.0,
            "document_binding": document_binding,
            "target_bindings": [binding],
        }
    )

    _run_crud_script(monkeypatch, script, [root])

    assert mutations == ["rectangle"]


def test_crud_mutates_the_exact_entity_reviewed_at_sink_not_a_second_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reviewed_mutations: list[str] = []
    replacement_mutations: list[str] = []
    reviewed = _Sketch(
        "profile", reviewed_mutations, entity_token="reviewed-sketch-token"
    )
    replacement = _Sketch(
        "profile", replacement_mutations, entity_token="replacement-sketch-token"
    )

    class _FlippingSketchCollection(_SketchCollection):
        def __init__(self) -> None:
            super().__init__([reviewed], reviewed_mutations)
            self.lookups = 0

        def item(self, index: int) -> _Sketch:
            assert index == 0
            self.lookups += 1
            return reviewed if self.lookups == 1 else replacement

    root = _Component("ActualRoot", [], reviewed_mutations)
    sketches = _FlippingSketchCollection()
    root.sketches = sketches
    document_binding = _document_binding(
        data_id="current-document",
        version_id="current-v1",
        root_token=root.entityToken,
    )
    binding = _cad_entity_binding(
        reference_kind="sketch",
        requested_ref="profile",
        token=reviewed.entityToken,
        document_binding=document_binding,
        name=reviewed.name,
        object_type=reviewed.objectType,
    )
    script = _crud_draw_rectangle_script(
        {
            "sketch": "profile",
            "center_x": 0.0,
            "center_y": 0.0,
            "width": 1.0,
            "height": 1.0,
            "document_binding": document_binding,
            "target_bindings": [binding],
        }
    )

    _run_crud_script(monkeypatch, script, [root])

    assert sketches.lookups == 1
    assert reviewed_mutations == ["rectangle"]
    assert replacement_mutations == []


def test_parameter_set_mutates_the_exact_parameter_reviewed_at_sink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Parameter:
        objectType = "adsk::fusion::UserParameter"

        def __init__(self, token: str, expression: str) -> None:
            self.entityToken = token
            self.expression = expression

    reviewed = _Parameter("reviewed-parameter-token", "1 mm")
    replacement = _Parameter("replacement-parameter-token", "99 mm")

    class _FlippingParameters:
        def __init__(self) -> None:
            self.lookups = 0

        def itemByName(self, name: str) -> _Parameter:
            assert name == "Width"
            self.lookups += 1
            return reviewed if self.lookups == 1 else replacement

    root = _Component("ActualRoot", [], [])
    document_binding = _document_binding(
        data_id="current-document",
        version_id="current-v1",
        root_token=root.entityToken,
    )
    parameter_facts = {
        "reference_kind": "parameter_existing",
        "requested_ref": "Width",
        "document_identity": document_binding["document_identity"],
        "entity_identity": hashlib.sha256(
            reviewed.entityToken.encode("utf-8")
        ).hexdigest(),
        "name": "Width",
        "object_type": reviewed.objectType,
        "state": "existing",
    }
    parameter_binding = {
        key: value
        for key, value in parameter_facts.items()
        if key not in {"name", "object_type", "state"}
    }
    parameter_binding["fingerprint"] = hashlib.sha256(
        json.dumps(parameter_facts, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    parameters = _FlippingParameters()
    script = _parameter_set_script(
        {
            "name": "Width",
            "expression": "2 mm",
            "document_binding": document_binding,
            "target_bindings": [parameter_binding],
        }
    )

    _run_crud_script(
        monkeypatch,
        script,
        [root],
        user_parameters=parameters,
    )

    assert parameters.lookups == 1
    assert reviewed.expression == "2 mm"
    assert replacement.expression == "99 mm"


def test_typed_import_rechecks_resource_identity_inside_fusion_sink(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    source = tmp_path / "fixture.step"
    source.write_bytes(b"reviewed-fixture")
    binding = _host_path_binding(source, tmp_path, direction="import", existed=True)
    source.write_bytes(b"substituted-fixture-with-different-size")
    mutations: list[str] = []
    root = _Component("ActualRoot", [], mutations)
    script = _typed_import_script(
        {
            "path": str(source.resolve()),
            "format": "step",
            "component_name": "ImportedFixture",
            "host_path_binding": binding,
        }
    )

    with pytest.raises(RuntimeError, match="host resource changed"):
        _run_crud_script(monkeypatch, script, [root])

    assert mutations == []


def test_import_post_provider_revalidation_failure_deletes_partial_occurrence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "fixture.step"
    source.write_bytes(b"reviewed-import-fixture")
    binding = _host_path_binding(source, tmp_path, direction="import", existed=True)

    class _Occurrence:
        def __init__(self) -> None:
            self.component = _Component("", [], [])
            self.deleted = 0

        def deleteMe(self) -> None:
            self.deleted += 1

    occurrence = _Occurrence()

    class _Occurrences:
        def addNewComponent(self, _transform: object) -> _Occurrence:
            return occurrence

    class _ImportManager:
        def __init__(self) -> None:
            self.calls = 0

        def createSTEPImportOptions(self, path: str) -> str:
            return path

        def importToTarget2(self, _options: str, _component: object) -> _Collection:
            self.calls += 1
            return _Collection([object()])

    manager = _ImportManager()
    root = _Component("ActualRoot", [], [])
    root.occurrences = _Occurrences()
    namespace = _run_crud_script(
        monkeypatch,
        _typed_import_script(
            {
                "path": str(source.resolve()),
                "format": "step",
                "component_name": "ImportedFixture",
                "host_path_binding": binding,
            }
        ),
        [root],
        execute_run=False,
        import_manager=manager,
    )
    original_claim = namespace["_claim_import_stage"]

    def _post_provider_claim(stage: dict[str, Any]) -> str:
        if manager.calls:
            raise RuntimeError("authorized import staging content changed")
        return original_claim(stage)

    namespace["_claim_import_stage"] = _post_provider_claim

    with pytest.raises(RuntimeError, match="staging content changed"):
        namespace["run"]("")

    assert manager.calls == 1
    assert occurrence.deleted == 1


def test_import_post_provider_digest_drift_is_detected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "fixture.step"
    source.write_bytes(b"reviewed-import-fixture")
    binding = _host_path_binding(source, tmp_path, direction="import", existed=True)
    root = _Component("ActualRoot", [], [])
    namespace = _run_crud_script(
        monkeypatch,
        _typed_import_script(
            {
                "path": str(source.resolve()),
                "format": "step",
                "component_name": "ImportedFixture",
                "host_path_binding": binding,
            }
        ),
        [root],
        execute_run=False,
    )
    payload = namespace["PAYLOAD"]
    bound_path = namespace["_require_host_path_binding"](payload, "import")
    stage = namespace["_stage_bound_import"](payload, bound_path)
    try:
        descriptor = stage["descriptor"]
        os.lseek(descriptor, 0, os.SEEK_SET)
        with pytest.raises(OSError):
            os.write(descriptor, b"attacker-controlled")
        provider_path = namespace["_claim_import_stage"](stage)
        assert Path(provider_path).read_bytes() == b"reviewed-import-fixture"
    finally:
        namespace["_discard_host_stage"](stage)


def test_typed_export_rejects_destination_created_after_capability_issue(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    destination = tmp_path / "fixture.step"
    binding = _host_path_binding(
        destination, tmp_path, direction="export", existed=False
    )
    destination.write_bytes(b"unexpected-existing-destination")
    mutations: list[str] = []
    root = _Component("ActualRoot", [], mutations)
    script = _typed_export_script(
        {
            "path": str(destination.resolve()),
            "format": "step",
            "target": "ActualRoot",
            "binding": {},
            "host_path_binding": binding,
        }
    )

    with pytest.raises(RuntimeError, match="path existence changed"):
        _run_crud_script(monkeypatch, script, [root])

    assert mutations == []


def test_import_staging_is_immutable_after_the_bound_source_is_replaced(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    reviewed = b"reviewed-import-fixture"
    source = tmp_path / "fixture.step"
    source.write_bytes(reviewed)
    binding = _host_path_binding(source, tmp_path, direction="import", existed=True)
    root = _Component("ActualRoot", [], [])
    script = _typed_import_script(
        {
            "path": str(source.resolve()),
            "format": "step",
            "component_name": "ImportedFixture",
            "host_path_binding": binding,
        }
    )
    namespace = _run_crud_script(monkeypatch, script, [root], execute_run=False)

    bound_path = namespace["_require_host_path_binding"](namespace["PAYLOAD"], "import")
    stage = namespace["_stage_bound_import"](namespace["PAYLOAD"], bound_path)
    staged_path = str(stage["path"])
    try:
        source.write_bytes(b"attacker-replacement-after-staging")
        provider_path = namespace["_claim_import_stage"](stage)
        assert Path(provider_path).read_bytes() == reviewed
        if os.name == "nt":
            assert Path(staged_path).resolve().parent == tmp_path.resolve()
        else:
            assert stage["path"] is None
            assert provider_path.startswith(("/proc/self/fd/", "/dev/fd/"))
    finally:
        namespace["_discard_host_stage"](stage)


def test_import_stage_hardlink_swap_before_api_consumes_only_reviewed_inode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reviewed = b"reviewed-import-fixture"
    external = tmp_path / "external.step"
    external.write_bytes(b"attacker-controlled-import")
    source = tmp_path / "fixture.step"
    source.write_bytes(reviewed)
    binding = _host_path_binding(source, tmp_path, direction="import", existed=True)
    root = _Component("ActualRoot", [], [])
    namespace = _run_crud_script(
        monkeypatch,
        _typed_import_script(
            {
                "path": str(source.resolve()),
                "format": "step",
                "component_name": "ImportedFixture",
                "host_path_binding": binding,
            }
        ),
        [root],
        execute_run=False,
    )
    payload = namespace["PAYLOAD"]
    bound_path = namespace["_require_host_path_binding"](payload, "import")
    stage = namespace["_stage_bound_import"](payload, bound_path)
    stage_path = str(stage["path"] if isinstance(stage, dict) else stage)
    alias = tmp_path / "import-stage-alias.step"
    try:
        claim = namespace.get("_claim_import_stage")
        provider_path = claim(stage) if callable(claim) else stage_path
        if os.name == "nt":
            with pytest.raises(PermissionError):
                os.unlink(stage_path)
        else:
            with pytest.raises(OSError):
                os.link(provider_path, alias, follow_symlinks=True)
        assert Path(provider_path).read_bytes() == reviewed
        assert external.read_bytes() == b"attacker-controlled-import"
        assert not alias.exists()
    finally:
        discard = namespace.get("_discard_host_stage")
        if callable(discard) and isinstance(stage, dict):
            discard(stage)
        elif os.path.lexists(stage_path):
            os.unlink(stage_path)
        if alias.exists():
            alias.unlink()


@pytest.mark.skipif(
    not (os.name == "posix" and sys.platform.startswith("linux")),
    reason="anonymous Linux output staging",
)
def test_export_hardlink_swap_after_final_validation_cannot_modify_external_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    destination = tmp_path / "fixture.step"
    outside = tmp_path / "external.step"
    outside.write_bytes(b"external-must-not-change")
    binding = _host_path_binding(
        destination, tmp_path, direction="export", existed=False
    )
    root = _Component("ActualRoot", [], [])
    script = _typed_export_script(
        {
            "path": str(destination.resolve()),
            "format": "step",
            "target": "ActualRoot",
            "binding": {},
            "host_path_binding": binding,
        }
    )
    namespace = _run_crud_script(monkeypatch, script, [root], execute_run=False)
    payload = namespace["PAYLOAD"]
    final_path = namespace["_require_host_path_binding"](payload, "export")
    stage = namespace["_prepare_export_stage"](payload, final_path)
    Path(namespace["_claim_output_stage"](stage, require_empty=True)).write_bytes(
        b"reviewed-export"
    )
    namespace["_require_host_path_binding"](payload, "export")
    os.link(outside, destination)
    try:
        with pytest.raises(RuntimeError, match="existence changed"):
            namespace["_promote_written_export"](payload, stage, final_path)
        assert outside.read_bytes() == b"external-must-not-change"
        assert destination.read_bytes() == b"external-must-not-change"
    finally:
        namespace["_discard_host_stage"](stage)
        if os.path.lexists(destination):
            os.unlink(destination)


@pytest.mark.skipif(
    not (os.name == "posix" and sys.platform.startswith("linux")),
    reason="anonymous Linux output staging",
)
def test_export_staging_promotes_atomically_on_legitimate_absent_destination(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    destination = tmp_path / "fixture.step"
    binding = _host_path_binding(
        destination, tmp_path, direction="export", existed=False
    )
    root = _Component("ActualRoot", [], [])
    script = _typed_export_script(
        {
            "path": str(destination.resolve()),
            "format": "step",
            "target": "ActualRoot",
            "binding": {},
            "host_path_binding": binding,
        }
    )
    namespace = _run_crud_script(monkeypatch, script, [root], execute_run=False)
    payload = namespace["PAYLOAD"]
    final_path = namespace["_require_host_path_binding"](payload, "export")
    stage = namespace["_prepare_export_stage"](payload, final_path)
    assert stage["path"] is None
    Path(namespace["_claim_output_stage"](stage, require_empty=True)).write_bytes(
        b"reviewed-export"
    )

    promoted = namespace["_promote_written_export"](payload, stage, final_path)

    assert promoted == str(destination.resolve())
    assert destination.read_bytes() == b"reviewed-export"
    assert list(tmp_path.glob(".fa-export-*")) == []
    assert list(tmp_path.glob(".fa-promote-*")) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows output boundary")
def test_windows_promotion_snapshot_is_exclusive_and_avoids_path_replace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    destination = tmp_path / "fixture.step"
    binding = _host_path_binding(
        destination, tmp_path, direction="export", existed=False
    )
    root = _Component("ActualRoot", [], [])
    namespace = _run_crud_script(
        monkeypatch,
        _typed_export_script(
            {
                "path": str(destination.resolve()),
                "format": "step",
                "target": "ActualRoot",
                "binding": {},
                "host_path_binding": binding,
            }
        ),
        [root],
        execute_run=False,
    )
    payload = namespace["PAYLOAD"]
    final_path = namespace["_require_host_path_binding"](payload, "export")
    with pytest.raises(
        RuntimeError,
        match="secure path-only output staging is unavailable on Windows",
    ):
        namespace["_prepare_export_stage"](payload, final_path)
    assert not destination.exists()
    assert list(tmp_path.glob(".fa-export-*")) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows output boundary")
def test_windows_authorized_overwrite_promotes_exact_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    destination = tmp_path / "fixture.step"
    destination.write_bytes(b"reviewed-old-destination")
    binding = _host_path_binding(
        destination,
        tmp_path,
        direction="export",
        existed=True,
        overwrite=True,
    )
    root = _Component("ActualRoot", [], [])
    namespace = _run_crud_script(
        monkeypatch,
        _typed_export_script(
            {
                "path": str(destination.resolve()),
                "format": "step",
                "target": "ActualRoot",
                "binding": {},
                "host_path_binding": binding,
            }
        ),
        [root],
        execute_run=False,
    )
    payload = namespace["PAYLOAD"]
    final_path = namespace["_require_host_path_binding"](payload, "export")
    with pytest.raises(
        RuntimeError,
        match="secure path-only output staging is unavailable on Windows",
    ):
        namespace["_prepare_export_stage"](payload, final_path)
    assert destination.read_bytes() == b"reviewed-old-destination"
    assert list(tmp_path.glob(".fa-export-*")) == []


@pytest.mark.skipif(
    not (os.name == "posix" and sys.platform.startswith("linux")),
    reason="anonymous Linux output staging",
)
def test_export_stage_hardlink_swap_before_api_never_writes_external_inode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    destination = tmp_path / "fixture.step"
    external = tmp_path / "external.step"
    external.write_bytes(b"external-must-not-change")
    binding = _host_path_binding(
        destination, tmp_path, direction="export", existed=False
    )
    root = _Component("ActualRoot", [], [])
    namespace = _run_crud_script(
        monkeypatch,
        _typed_export_script(
            {
                "path": str(destination.resolve()),
                "format": "step",
                "target": "ActualRoot",
                "binding": {},
                "host_path_binding": binding,
            }
        ),
        [root],
        execute_run=False,
    )
    payload = namespace["PAYLOAD"]
    final_path = namespace["_require_host_path_binding"](payload, "export")
    stage = namespace["_prepare_export_stage"](payload, final_path)
    alias = tmp_path / "output-stage-alias.step"
    try:
        claim = namespace.get("_claim_output_stage")
        provider_path = (
            claim(stage, require_empty=True) if callable(claim) else str(stage)
        )
        with pytest.raises(OSError):
            os.link(provider_path, alias, follow_symlinks=True)
        Path(provider_path).write_bytes(b"provider-export")
        assert external.read_bytes() == b"external-must-not-change"
        assert not alias.exists()
    finally:
        discard = namespace.get("_discard_host_stage")
        if callable(discard) and isinstance(stage, dict):
            discard(stage)
        if alias.exists():
            alias.unlink()


def _capture_script_namespace(
    monkeypatch: pytest.MonkeyPatch,
    *,
    destination: Path,
    root: Path,
    viewport: Any,
    overwrite: bool = False,
) -> dict[str, Any]:
    mutations: list[str] = []
    component = _Component("ActualRoot", [], mutations)
    component.attributes = types.SimpleNamespace(add=lambda *_args: None)
    document_binding = _document_binding(
        data_id="current-document",
        version_id="current-v1",
        root_token=component.entityToken,
    )
    script = _crud_capture_viewport_script(
        {
            "name": "fixture",
            "path": str(destination.resolve(strict=False)),
            "view": "isometric",
            "width": 800,
            "height": 600,
            "document_binding": document_binding,
            "host_path_binding": _host_path_binding(
                destination,
                root,
                direction="export",
                existed=destination.exists(),
                overwrite=overwrite,
            ),
        }
    )
    namespace = _run_crud_script(
        monkeypatch,
        script,
        [component],
        execute_run=False,
    )
    core = sys.modules["adsk.core"]
    application = core.Application.get()
    core.ViewOrientations = types.SimpleNamespace(
        FrontViewOrientation="front",
        TopViewOrientation="top",
        RightViewOrientation="right",
        IsoTopRightViewOrientation="iso",
    )
    core.Application.get = lambda: types.SimpleNamespace(
        activeProduct=application.activeProduct,
        activeDocument=application.activeDocument,
        activeViewport=viewport,
    )
    return namespace


@pytest.mark.skipif(
    not (os.name == "posix" and sys.platform.startswith("linux")),
    reason="anonymous Linux output staging",
)
def test_capture_never_passes_final_destination_to_fusion_during_hardlink_swap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    destination = tmp_path / "capture.png"
    outside = tmp_path.parent / f"{tmp_path.name}-outside.png"
    outside.write_bytes(b"external-must-not-change")

    class SwappingViewport:
        viewOrientation: str | None = None

        def fit(self) -> None:
            pass

        def saveAsImageFile(self, path: str, _width: int, _height: int) -> bool:
            os.link(outside, destination)
            Path(path).write_bytes(b"captured-image")
            return True

    namespace = _capture_script_namespace(
        monkeypatch,
        destination=destination,
        root=tmp_path,
        viewport=SwappingViewport(),
    )

    try:
        with pytest.raises(RuntimeError, match="existence changed"):
            namespace["run"]("")
        assert outside.read_bytes() == b"external-must-not-change"
        assert destination.read_bytes() == b"external-must-not-change"
    finally:
        if os.path.lexists(destination):
            os.unlink(destination)
        if os.path.lexists(outside):
            os.unlink(outside)


@pytest.mark.skipif(
    not (os.name == "posix" and sys.platform.startswith("linux")),
    reason="anonymous Linux output staging",
)
def test_capture_stages_and_atomically_promotes_legitimate_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    destination = tmp_path / "capture.png"
    fusion_paths: list[Path] = []

    class RecordingViewport:
        viewOrientation: str | None = None

        def fit(self) -> None:
            pass

        def saveAsImageFile(self, path: str, _width: int, _height: int) -> bool:
            fusion_path = Path(path)
            fusion_paths.append(fusion_path)
            fusion_path.write_bytes(b"captured-image")
            return True

    namespace = _capture_script_namespace(
        monkeypatch,
        destination=destination,
        root=tmp_path,
        viewport=RecordingViewport(),
    )

    namespace["run"]("")

    assert destination.read_bytes() == b"captured-image"
    assert len(fusion_paths) == 1
    assert fusion_paths[0] != destination
    assert str(fusion_paths[0]).startswith(("/proc/self/fd/", "/dev/fd/"))
    assert not fusion_paths[0].exists()


@pytest.mark.skipif(
    not (os.name == "posix" and sys.platform.startswith("linux")),
    reason="anonymous Linux output staging",
)
def test_capture_stage_swap_inside_provider_cannot_write_external_inode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    destination = tmp_path / "capture.png"
    external = tmp_path / "external.png"
    external.write_bytes(b"external-must-not-change")
    alias = tmp_path / "capture-stage-alias.png"

    class SwappingViewport:
        viewOrientation: str | None = None

        def fit(self) -> None:
            pass

        def saveAsImageFile(self, path: str, _width: int, _height: int) -> bool:
            with pytest.raises(OSError):
                os.link(path, alias, follow_symlinks=True)
            Path(path).write_bytes(b"captured-image")
            return True

    namespace = _capture_script_namespace(
        monkeypatch,
        destination=destination,
        root=tmp_path,
        viewport=SwappingViewport(),
    )

    try:
        namespace["run"]("")
        assert external.read_bytes() == b"external-must-not-change"
        assert destination.read_bytes() == b"captured-image"
        assert not alias.exists()
    finally:
        if os.path.lexists(destination):
            os.unlink(destination)
        if alias.exists():
            alias.unlink()


@pytest.mark.skipif(os.name != "nt", reason="Windows path-only provider boundary")
def test_windows_capture_output_is_fail_closed_before_provider_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    destination = tmp_path / "capture.png"

    class RecordingViewport:
        viewOrientation: str | None = None

        def __init__(self) -> None:
            self.fit_calls = 0
            self.save_calls = 0

        def fit(self) -> None:
            self.fit_calls += 1

        def saveAsImageFile(self, _path: str, _width: int, _height: int) -> bool:
            self.save_calls += 1
            return True

    viewport = RecordingViewport()
    namespace = _capture_script_namespace(
        monkeypatch,
        destination=destination,
        root=tmp_path,
        viewport=viewport,
    )

    with pytest.raises(
        RuntimeError,
        match="secure path-only output staging is unavailable on Windows",
    ):
        namespace["run"]("")

    assert viewport.fit_calls == 0
    assert viewport.save_calls == 0
    assert not destination.exists()
    assert list(tmp_path.glob(".fa-export-*")) == []


@pytest.mark.asyncio
async def test_capture_overwrite_is_revalidated_before_mcp_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    destination = tmp_path / "capture.png"
    destination.write_bytes(b"existing")
    client = Client()
    facade = _backend(client).facade
    preflight_calls: list[tuple[str, bool]] = []

    def emulate_linux_host_io_preflight(
        direction: str, *, overwrite: bool = False
    ) -> None:
        preflight_calls.append((direction, overwrite))
        if direction == "export" and overwrite:
            raise RuntimeError("secure POSIX overwrite is unavailable")

    monkeypatch.setattr(
        vendor_facade_module,
        "_require_secure_host_io_platform",
        emulate_linux_host_io_preflight,
    )

    with pytest.raises(RuntimeError, match="secure POSIX overwrite is unavailable"):
        await facade.capture_viewport(
            name="existing",
            path=destination,
            view="isometric",
            host_path_binding=_host_path_binding(
                destination,
                tmp_path,
                direction="export",
                existed=True,
                overwrite=True,
            ),
        )

    assert preflight_calls == [("export", True)]
    assert client.calls == []


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="Windows path-only provider boundary")
async def test_windows_host_output_is_rejected_before_any_fusion_mcp_call(
    tmp_path: Path,
) -> None:
    client = Client()
    facade = _backend(client).facade
    destination = tmp_path / "fixture.step"
    plan = facade.prepare_typed_operation(
        "export",
        {
            "path": str(destination.resolve()),
            "format": "step",
            "target": "ActualRoot",
            "binding": {},
            "host_path_binding": _host_path_binding(
                destination, tmp_path, direction="export", existed=False
            ),
        },
    )

    with pytest.raises(HostOutputDisabledError, match="disabled by deny_io"):
        await facade.execute_prepared_typed_operation(
            plan, operation_id="zero-dispatch-export"
        )
    with pytest.raises(HostOutputDisabledError, match="disabled by deny_io"):
        await facade.capture_viewport(
            name="zero-dispatch-capture",
            path=tmp_path / "capture.png",
            view="isometric",
        )

    assert client.calls == []
    assert list(tmp_path.glob(".fa-*")) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows path-only provider boundary")
def test_windows_export_output_is_fail_closed_before_provider_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    destination = tmp_path / "fixture.step"
    binding = _host_path_binding(
        destination, tmp_path, direction="export", existed=False
    )
    root = _Component("ActualRoot", [], [])

    class RecordingExportManager:
        def __init__(self) -> None:
            self.option_calls = 0
            self.execute_calls = 0

        def createSTEPExportOptions(self, _path: str, _target: object) -> object:
            self.option_calls += 1
            return object()

        def execute(self, _options: object) -> bool:
            self.execute_calls += 1
            return True

    manager = RecordingExportManager()
    namespace = _run_crud_script(
        monkeypatch,
        _typed_export_script(
            {
                "path": str(destination.resolve()),
                "format": "step",
                "target": "ActualRoot",
                "binding": {},
                "host_path_binding": binding,
            }
        ),
        [root],
        execute_run=False,
    )
    design = sys.modules["adsk.core"].Application.get().activeProduct
    design.exportManager = manager
    namespace["_cad_target_binding"] = lambda *_args: (root, {})

    with pytest.raises(
        RuntimeError,
        match="secure path-only output staging is unavailable on Windows",
    ):
        namespace["run"]("")

    assert manager.option_calls == 0
    assert manager.execute_calls == 0
    assert not destination.exists()
    assert list(tmp_path.glob(".fa-export-*")) == []


@pytest.mark.skipif(
    os.name not in {"nt", "posix"}, reason="sealed host import platforms"
)
def test_import_provider_cannot_transiently_rewrite_and_restore_sealed_stage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reviewed = b"reviewed-import-fixture"
    transient = b"attacker-controlled-bytes"
    source = tmp_path / "fixture.step"
    source.write_bytes(reviewed)
    binding = _host_path_binding(source, tmp_path, direction="import", existed=True)

    class _Occurrence:
        def __init__(self) -> None:
            self.component = _Component("", [], [])
            self.deleted = 0

        def deleteMe(self) -> None:
            self.deleted += 1

    occurrence = _Occurrence()

    class _Occurrences:
        def addNewComponent(self, _transform: object) -> _Occurrence:
            return occurrence

    class _ImportManager:
        def __init__(self) -> None:
            self.calls = 0
            self.write_blocked = False
            self.consumed = b""

        def createSTEPImportOptions(self, path: str) -> str:
            return path

        def importToTarget2(self, path: str, _component: object) -> _Collection:
            self.calls += 1
            attack_path = Path(path)
            alias = tmp_path / "transient-import-alias.step"
            try:
                if os.name == "nt":
                    os.link(path, alias)
                    attack_path = alias
                os.chmod(attack_path, stat.S_IWRITE | stat.S_IREAD)
                attack_path.write_bytes(transient)
                attack_path.write_bytes(reviewed)
            except (OSError, PermissionError):
                self.write_blocked = True
            finally:
                if alias.exists():
                    alias.unlink()
            self.consumed = Path(path).read_bytes()
            return _Collection([object()])

    manager = _ImportManager()
    root = _Component("ActualRoot", [], [])
    root.occurrences = _Occurrences()
    namespace = _run_crud_script(
        monkeypatch,
        _typed_import_script(
            {
                "path": str(source.resolve()),
                "format": "step",
                "component_name": "ImportedFixture",
                "host_path_binding": binding,
            }
        ),
        [root],
        execute_run=False,
        import_manager=manager,
    )

    if os.name == "posix" and not sys.platform.startswith("linux"):
        with pytest.raises(
            RuntimeError, match="sealed host import staging is unavailable"
        ):
            namespace["run"]("")
        assert manager.calls == 0
    else:
        namespace["run"]("")
        assert manager.calls == 1
        assert manager.write_blocked is True
        assert manager.consumed == reviewed
        assert occurrence.deleted == 0

    assert source.read_bytes() == reviewed
    assert list(tmp_path.glob(".fa-import-*")) == []
