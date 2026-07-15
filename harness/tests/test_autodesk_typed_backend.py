from __future__ import annotations

from dataclasses import replace

import pytest

from agent_core.capability_executor import CapabilityExecutor
from cad_spec.v2 import CadSpecV2
from fusion_mcp_adapter.tool_result import ToolDefinition, ToolManifest, ToolResult
from fusion_tool_facade.autodesk_typed_backend import AutodeskTypedBackend


class Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def list_tools(self):
        raise AssertionError("manifest is already supplied")

    async def call_tool(self, name, arguments, *, options=None):
        self.calls.append((name, arguments))
        return ToolResult.success(message='{"success":true}')


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
    spec = _contract(_capability_operations())

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
        "export_iges",
    }
    assert expected <= backend.capabilities
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
            "io.export",
        }
    }
    for operation_id, plan in backend._prepared.items():
        compile(plan.script, f"<{operation_id}>", "exec")
        assert len(plan.script.encode("utf-8")) < 28 * 1024
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
async def test_prepared_script_integrity_rejects_substitution_without_dispatch() -> None:
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
    backend.preflight_operations(list(spec.operations))
    plan = backend._prepared["fixed_line"]
    backend._prepared["fixed_line"] = replace(
        plan,
        script=plan.script + "\n# substituted code",
    )

    with pytest.raises(RuntimeError, match="integrity"):
        await backend.execute_operation(spec.operations[0])
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
    assert [name for name, _arguments in client.calls] == ["fusion_mcp_execute"]
    script = client.calls[0][1]["object"]["script"]
    assert "PAYLOAD = json.loads" in script
    assert ".isFixed = True" in script
    assert "execute_code" not in script


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
    assert direct_only.capabilities == {
        "parameters",
        "export_step",
        "export_stp",
        "export_stl",
    }


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
        ("export", "step", "step"),
        ("export", "stp", "stp"),
        ("export", "stl", "stl"),
        ("export", "iges", "iges"),
        ("export", "igs", "igs"),
        ("export", "f3d", "f3d"),
    ],
)
def test_all_declared_io_formats_compile_fixed_scripts(
    direction: str,
    format_name: str,
    extension: str,
) -> None:
    backend = _backend()
    if direction == "import":
        operation = {
            "id": f"import_{format_name}",
            "kind": "io.import",
            "path": rf"C:\fixtures\fixture.{extension}",
            "format": format_name,
            "component_name": "ImportedFixture",
        }
    else:
        operation = {
            "id": f"export_{format_name}",
            "kind": "io.export",
            "target_ref": "FixtureBody",
            "path": rf"C:\exports\fixture.{extension}",
            "format": format_name,
        }
    spec = _contract([operation])

    backend.preflight_operations(list(spec.operations))

    plan = backend._prepared[operation["id"]]
    compile(plan.script, f"<{operation['id']}>", "exec")
    assert f'"format":"{format_name}"' in plan.script
