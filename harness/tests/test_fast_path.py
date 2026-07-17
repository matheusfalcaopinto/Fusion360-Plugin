from __future__ import annotations

import ast
import base64
import json
import sys
import types

import pytest

from agent_core.fast_path import (
    FastPathService,
    GUARD_CONTROL_SCHEMA,
    _RecoveryRecord,
    _ROOT_COMPONENT_BINDING,
    _component_query_id,
    _guard_script,
    _mutation_baseline_issue,
    _validate_targets,
    evaluate_verification,
    lint_fusion_script,
    validate_fast_execute_request,
)
from agent_core.targeted_inspection import (
    build_targeted_inspection_script,
    validate_inspection_payload,
)
from fusion_mcp_adapter.errors import ErrorCode
from fusion_mcp_adapter import execute_guard
from fusion_mcp_adapter.execute_guard import (
    normalize_execute_script,
    protected_script_descriptor,
)
from fusion_mcp_adapter.tool_result import ToolResult


READ_SCRIPT = """import adsk.core

def run(_context: str):
    print(adsk.core.Application.get().version)
"""

ADDITIVE_SCRIPT = """import adsk.core
import adsk.fusion

def run(_context: str):
    root = target_components["root"]
    root.bRepBodies.add(None)
"""

ACTIVE_COMMAND_CLEAR = {
    "success": True,
    "complete": True,
    "activeCommandRead": True,
    "activeCommand": None,
}


def test_linter_accepts_bounded_scripts_and_blocks_unsafe_operations() -> None:
    assert lint_fusion_script(READ_SCRIPT, "read_only").allowed is True
    assert lint_fusion_script(ADDITIVE_SCRIPT, "additive").allowed is True

    unsafe = """import os
def run(_context: str):
    open('x', 'w')
"""
    decision = lint_fusion_script(unsafe, "additive")
    assert decision.allowed is False
    assert any("import is not allowlisted" in error for error in decision.errors)
    assert any("blocked" in error for error in decision.errors)

    save = """import adsk.core
def run(_context: str):
    adsk.core.Application.get().activeDocument.save('x')
"""
    assert lint_fusion_script(save, "scoped_update").allowed is False

    broad_except = """import adsk.core
def run(_context: str):
    try:
        print('x')
    except Exception:
        pass
"""
    assert lint_fusion_script(broad_except, "read_only").allowed is False


def test_linter_reserves_the_guarded_entrypoint_name() -> None:
    collision = """import adsk.core

def run(_context: str):
    print(adsk.core.Application.get().version)

def _fusion_agent_user_run(_context: str):
    print('shadowed')
"""

    decision = lint_fusion_script(collision, "read_only")

    assert decision.allowed is False
    assert any("reserved internal function name" in error for error in decision.errors)


def test_linter_keeps_sys_and_reflection_unavailable_to_model_script() -> None:
    import_sys = """import sys

def run(_context: str):
    print(sys.stdout)
"""
    reflective_access = """import adsk.core

def run(_context: str):
    print(getattr(adsk.core.Application.get(), '__class__'))
"""

    sys_decision = lint_fusion_script(import_sys, "read_only")
    reflection_decision = lint_fusion_script(reflective_access, "read_only")

    assert sys_decision.allowed is False
    assert any(
        "import is not allowlisted: sys" in error for error in sys_decision.errors
    )
    assert reflection_decision.allowed is False
    assert any("blocked" in error for error in reflection_decision.errors)


def test_guarded_entrypoint_normalizes_fusion_streams_before_harness_or_user_code() -> (
    None
):
    guarded = normalize_execute_script(
        _guard_script(
            READ_SCRIPT,
            {"name": "D", "id": "data-id", "runtime_id": "data:data-id"},
            guard_token="a" * 32,
            guard_binding_digest="b" * 64,
        )
    )
    parsed = ast.parse(guarded)
    entrypoint = next(
        node for node in parsed.body if getattr(node, "name", None) == "run"
    )
    first_lines = [getattr(node, "lineno", 0) for node in entrypoint.body[:5]]

    assert first_lines == sorted(first_lines)
    assert "import sys as _fusion_agent_runtime_sys" in guarded
    assert "object.__getattribute__" in guarded
    assert "object.__setattr__" in guarded
    assert "_NsSanitizedWriter" in guarded
    assert "range(512)" in guarded
    assert "_fusion_agent_runtime_sys.stdout = _fusion_agent_collapse_stream" in guarded
    assert "_fusion_agent_runtime_sys.stderr = _fusion_agent_collapse_stream" in guarded
    assert (
        "_fusion_agent_runtime_sys.stdout = _fusion_agent_runtime_sys.__stdout__"
        not in guarded
    )
    assert guarded.index("import sys as _fusion_agent_runtime_sys") < guarded.index(
        "import adsk.core", guarded.index("def run(")
    )
    assert guarded.index("del _fusion_agent_runtime_sys") < guarded.index(
        "return _fusion_agent_user_run(_context)"
    )


def test_document_guard_emits_operation_bound_control_envelope(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    adsk_module = types.ModuleType("adsk")
    core_module = types.ModuleType("adsk.core")
    fusion_module = types.ModuleType("adsk.fusion")
    document = types.SimpleNamespace(name="Other", dataFile=None)
    application = types.SimpleNamespace(
        activeCommand="", activeDocument=document, activeProduct=None
    )

    class Application:
        @staticmethod
        def get() -> object:
            return application

    core_module.Application = Application  # type: ignore[attr-defined]
    adsk_module.core = core_module  # type: ignore[attr-defined]
    adsk_module.fusion = fusion_module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "adsk", adsk_module)
    monkeypatch.setitem(sys.modules, "adsk.core", core_module)
    monkeypatch.setitem(sys.modules, "adsk.fusion", fusion_module)
    guarded = _guard_script(
        READ_SCRIPT,
        {"name": "Expected", "id": "", "runtime_id": ""},
        guard_token="a" * 32,
        guard_binding_digest="b" * 64,
    )
    namespace: dict[str, object] = {}
    exec(compile(guarded, "<guard-envelope-test>", "exec"), namespace)

    returned = namespace["run"]("")  # type: ignore[operator]
    emitted = json.loads(capsys.readouterr().out)

    assert returned == emitted
    assert emitted == {
        "fusion_agent_guard": {
            "schema": GUARD_CONTROL_SCHEMA,
            "token": "a" * 32,
            "binding_digest": "b" * 64,
            "status": "rejected_before_apply",
            "reason_code": "DOCUMENT_NAME_CHANGED",
        }
    }


def test_stream_preamble_preserves_current_writer_and_collapses_old_writer_chain() -> (
    None
):
    def wrap(delegate):
        class _NsSanitizedWriter:
            def __init__(self, original):
                self._original = original

        return _NsSanitizedWriter(delegate)

    base_out = object()
    base_err = object()
    outer_out = wrap(wrap(wrap(base_out)))
    outer_err = wrap(wrap(base_err))
    protected = normalize_execute_script(
        """def run(_context: str):
    import sys
    return sys.stdout, sys.stderr
"""
    )
    namespace: dict[str, object] = {}
    exec(compile(protected, "<executor-guard-test>", "exec"), namespace)
    previous = (sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__)
    try:
        sys.stdout = outer_out
        sys.stderr = outer_err
        sys.__stdout__ = base_out
        sys.__stderr__ = base_err
        observed_out, observed_err = namespace["run"]("")
    finally:
        sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__ = previous

    assert observed_out is outer_out
    assert observed_err is outer_err
    assert object.__getattribute__(outer_out, "_original") is base_out
    assert object.__getattribute__(outer_err, "_original") is base_err
    assert normalize_execute_script(protected) == protected


def test_stream_preamble_breaks_a_cyclic_writer_chain_to_the_base_fallback() -> None:
    class _NsSanitizedWriter:
        def __init__(self):
            self._original = self

    base_out = object()
    base_err = object()
    outer_out = _NsSanitizedWriter()
    outer_err = _NsSanitizedWriter()
    protected = normalize_execute_script(
        """def run(_context: str):
    import sys
    return sys.stdout, sys.stderr
"""
    )
    namespace: dict[str, object] = {}
    exec(compile(protected, "<executor-cycle-test>", "exec"), namespace)
    previous = (sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__)
    try:
        sys.stdout = outer_out
        sys.stderr = outer_err
        sys.__stdout__ = base_out
        sys.__stderr__ = base_err
        observed_out, observed_err = namespace["run"]("")
    finally:
        sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__ = previous

    assert observed_out is outer_out
    assert observed_err is outer_err
    assert object.__getattribute__(outer_out, "_original") is base_out
    assert object.__getattribute__(outer_err, "_original") is base_err


def test_default_executor_payload_gate_accepts_b07_range_and_blocks_above_28_kib(
    monkeypatch,
) -> None:
    monkeypatch.delenv("FUSION_AGENT_MAX_PROTECTED_SCRIPT_BYTES", raising=False)

    b07_sized = protected_script_descriptor("x" * 25_700)
    oversized = protected_script_descriptor("x" * ((28 * 1024) + 1))

    assert b07_sized["limit_bytes"] == 28 * 1024
    assert b07_sized["within_limit"] is True
    assert oversized["protected_payload_bytes"] == (28 * 1024) + 1
    assert oversized["within_limit"] is False


def test_linter_warns_on_manual_sketch_basis_projection() -> None:
    script = """import adsk.core

def run(_context: str):
    x_axis = sketch.xDirection
    point = adsk.core.Point3D.create(x_axis.x, x_axis.y, x_axis.z)
    print(point)
"""

    decision = lint_fusion_script(script, "read_only")

    assert any("modelToSketchSpace" in warning for warning in decision.warnings)


@pytest.mark.parametrize(
    "operation",
    [
        "doc.save()",
        "doc.saveAs('x')",
        "doc.close(False)",
        "entity.deleteMe()",
        "entity.remove()",
        "entity.moveToComponent(root)",
        "manager.importToTarget('x', root)",
        "manager.exportToArchive('x', root)",
        "body.transformBy(matrix)",
        "body.hide()",
        "root.createComponentFromBodies(None)",
        "app.executeTextCommand('Commands.Start SaveDocumentCommand')",
        "target_components['root'].occurrences.addExistingComponent(component, matrix)",
        "target_components['root'].occurrences.addByInsert(data_file, matrix, True)",
    ],
)
def test_linter_routes_destructive_and_global_operations_to_safe_harness(
    operation: str,
) -> None:
    script = f"""import adsk.core

def run(_context: str):
    {operation}
"""
    decision = lint_fusion_script(script, "scoped_update")

    assert decision.allowed is False
    assert any(
        "blocked" in error.lower() or "safe harness" in error.lower()
        for error in decision.errors
    )


def test_linter_blocks_module_level_execution_before_document_guard() -> None:
    script = """import adsk.core
app = adsk.core.Application.get()

def run(_context: str):
    print('late')
"""
    decision = lint_fusion_script(script, "additive")
    assert decision.allowed is False
    assert any("module-level" in error for error in decision.errors)


@pytest.mark.parametrize("statement", ["yield None", "yield from ()"])
def test_linter_blocks_generator_entrypoints_that_would_silently_defer_run(
    statement: str,
) -> None:
    script = f"""def run(_context: str):
    {statement}
"""
    decision = lint_fusion_script(script, "read_only")
    assert decision.allowed is False
    assert any("generator functions are blocked" in error for error in decision.errors)


def test_linter_blocks_declared_risk_divergence_and_allows_naming_new_entity() -> None:
    scoped_script = """import adsk.core

def run(_context: str):
    parameter = targets["parameter_target"]
    parameter.expression = '12 mm'
"""
    additive_named_script = """import adsk.core

def run(_context: str):
    root = target_components["root"]
    sketch = root.sketches.add(root.xYConstructionPlane)
    sketch.name = 'FusionAgentNamedFeature'
"""

    wrong_additive = lint_fusion_script(scoped_script, "additive")
    wrong_scoped = lint_fusion_script(additive_named_script, "scoped_update")
    correct_additive = lint_fusion_script(additive_named_script, "additive")

    assert wrong_additive.allowed is False
    assert wrong_additive.detected_change_class == "scoped_update"
    assert wrong_scoped.allowed is False
    assert wrong_scoped.detected_change_class == "additive"
    assert correct_additive.allowed is True
    assert correct_additive.detected_change_class == "additive"


def test_linter_blocks_unknown_fusion_mutators_and_command_execution() -> None:
    project = """import adsk.core

def run(_context: str):
    sketch.project(entity)
"""
    command_execute = """import adsk.core

def run(_context: str):
    app = adsk.core.Application.get()
    app.userInterface.commandDefinitions.itemById('SaveDocumentCommand').execute()
"""
    unknown = """import adsk.core

def run(_context: str):
    sketch.trim(curve, point)
"""

    project_decision = lint_fusion_script(project, "read_only")
    assert project_decision.allowed is False
    assert project_decision.detected_change_class == "additive"
    assert lint_fusion_script(command_execute, "read_only").allowed is False
    assert any(
        "unclassified Fusion call" in error
        for error in lint_fusion_script(unknown, "read_only").errors
    )


def test_linter_allows_pure_sketch_coordinate_conversions() -> None:
    script = """import adsk.core

def run(_context: str):
    root = target_components['root']
    sketch = root.sketches.add(root.yZConstructionPlane)
    sketch_point = sketch.modelToSketchSpace(adsk.core.Point3D.create(0, 1, 2))
    print(sketch.sketchToModelSpace(sketch_point).z)
"""

    decision = lint_fusion_script(
        script,
        "additive",
        allowed_target_ids=set(),
        allowed_component_paths={"root"},
    )

    assert decision.allowed is True
    assert decision.detected_change_class == "additive"


def test_linter_allows_fusion_runtime_type_introspection() -> None:
    script = """import adsk.core

def run(_context: str):
    print(adsk.core.Cylinder.classType())
"""

    decision = lint_fusion_script(script, "read_only")

    assert decision.allowed is True
    assert decision.detected_change_class == "read_only"


def test_linter_requires_declared_bindings_and_tracks_new_entity_helpers() -> None:
    helper = """import adsk.core

def make_sketch(component):
    sketch = component.sketches.add(component.xYConstructionPlane)
    sketch.name = 'BoundSketch'
    return sketch

def run(_context: str):
    make_sketch(target_components['root'])
"""
    scoped = """import adsk.core

def run(_context: str):
    parameter = targets['width']
    parameter.expression = '12 mm'
"""
    wrong_target = scoped.replace("targets['width']", "targets['height']")
    unbound = scoped.replace("parameter = targets['width']\n    ", "")
    shadowed = """import adsk.core

def run(_context: str):
    for targets in ({'width': object()},):
        targets['width'].expression = '12 mm'
"""
    overwritten = """import adsk.core

def run(_context: str):
    targets['width'] = adsk.core.Application.get().activeProduct
    targets['width'].name = 'Wrong'
"""

    assert (
        lint_fusion_script(
            helper,
            "additive",
            allowed_target_ids=set(),
            allowed_component_paths={"root"},
        ).allowed
        is True
    )
    assert (
        lint_fusion_script(
            scoped,
            "scoped_update",
            allowed_target_ids={"width"},
            allowed_component_paths=set(),
        ).allowed
        is True
    )
    assert (
        lint_fusion_script(
            wrong_target,
            "scoped_update",
            allowed_target_ids={"width"},
            allowed_component_paths=set(),
        ).allowed
        is False
    )
    assert (
        lint_fusion_script(
            unbound,
            "scoped_update",
            allowed_target_ids={"width"},
            allowed_component_paths=set(),
        ).allowed
        is False
    )
    assert (
        lint_fusion_script(
            shadowed,
            "scoped_update",
            allowed_target_ids={"width"},
            allowed_component_paths=set(),
        ).allowed
        is False
    )
    assert (
        lint_fusion_script(
            overwritten,
            "scoped_update",
            allowed_target_ids={"width"},
            allowed_component_paths=set(),
        ).allowed
        is False
    )


def test_targeted_inspection_validation_is_bounded_and_unambiguous() -> None:
    normalized = validate_inspection_payload(
        {
            "queries": [
                {
                    "id": "body",
                    "entity_type": "body",
                    "selector": {"component_path": "root", "name": "Body1"},
                    "fields": ["exists", "bounding_box_mm"],
                }
            ]
        }
    )
    assert normalized["limit_per_query"] == 20
    assert normalized["queries"][0]["selector"]["name"] == "Body1"
    with pytest.raises(ValueError, match="unique"):
        validate_inspection_payload(
            {
                "queries": [
                    {"id": "same", "entity_type": "body"},
                    {"id": "same", "entity_type": "body"},
                ]
            }
        )
    with pytest.raises(ValueError, match="component_path requires"):
        validate_inspection_payload(
            {
                "queries": [
                    {
                        "id": "bad",
                        "entity_type": "body",
                        "selector": {"component_path": "Arm:1"},
                    }
                ]
            }
        )


def test_mutating_baseline_rejects_any_inexact_query_match_count() -> None:
    issue = _mutation_baseline_issue(
        {"change_class": "additive", "target_query_ids": ["future_body"]},
        {
            "complete": True,
            "truncated": False,
            "counts_exact": True,
            "stop_reason": None,
            "results": [
                {
                    "query_id": "future_body",
                    "matches": [],
                    "ambiguous": False,
                    "truncated": False,
                    "match_count": 0,
                    "match_count_exact": True,
                },
                {
                    "query_id": "component_binding",
                    "matches": [],
                    "match_count_exact": False,
                },
            ],
        },
    )
    assert issue == "query_match_count_inexact:component_binding"


def test_targeted_inspection_generated_script_compiles_with_identity_and_full_paths() -> (
    None
):
    script = build_targeted_inspection_script(
        {
            "queries": [
                {
                    "id": "body",
                    "entity_type": "body",
                    "selector": {"path": "Assembly:1/Body1"},
                    "fields": ["exists", "bounding_box_mm"],
                }
            ]
        }
    )

    compile(script, "<fusion_agent_targeted_inspect>", "exec")
    assert 'stable_runtime_id = "data:" + data_id' in script
    assert '"runtime_id": stable_runtime_id' in script
    assert "str(id(doc))" not in script
    assert ".fullPathName" in script
    assert '"paths": paths' in script
    assert '"visible_body_bbox_mm": visible_bbox' in script
    assert "== component_path" in script
    assert "component_path in candidate_path" not in script
    assert "isinstance(found[1], bool)" in script
    assert 'return ["root"]' in script
    assert '("root", name)' in script
    assert "_global_state_fingerprint" in script


class FakeNative:
    def __init__(self) -> None:
        self.mutating_calls = 0
        self.inspections = 0

    async def __call__(self, name, arguments, *, semantics, operation_id):
        if name == "fusion_mcp_read":
            if arguments["queryType"] == "screenshot":
                encoded = base64.b64encode(b"\x89PNG\r\n\x1a\nmock").decode("ascii")
                return {
                    "ok": True,
                    "data": {"type": "image", "mimeType": "image/png", "data": encoded},
                    "_meta": {"native_trace": "screenshot"},
                }
        if name == "fusion_mcp_update":
            self.mutating_calls += 1
            return {"ok": True, "data": {"success": True}}
        if name == "fusion_mcp_execute" and semantics == "read_only":
            if "fusion_agent_active_command" in arguments["object"]["script"]:
                return {
                    "ok": True,
                    "data": {"message": json.dumps(ACTIVE_COMMAND_CLEAR)},
                }
            self.inspections += 1
            exists = self.mutating_calls > 0
            payload = {
                "success": True,
                "complete": True,
                "truncated": False,
                "counts_exact": True,
                "stop_reason": None,
                "document": {"name": "Untitled", "id": "", "runtime_id": "marker:fake"},
                "summary": {
                    "components": 1,
                    "occurrences": 0,
                    "bodies": 1 if exists else 0,
                    "state_fingerprint": "state-after" if exists else "state-before",
                    "state_fingerprint_truncated": False,
                },
                "results": [
                    {
                        "query_id": "body_target",
                        "matches": [{"name": "Body1", "exists": True}]
                        if exists
                        else [],
                        "ambiguous": False,
                        "truncated": False,
                        "match_count": 1 if exists else 0,
                        "match_count_exact": True,
                    },
                    {
                        "query_id": "__fusion_agent_component_4813494d137e1631",
                        "matches": [
                            {
                                "entity_type": "component",
                                "name": "root",
                                "path": "root",
                                "paths": ["root"],
                                "entity_token": "component-root-token",
                                "visible": True,
                                "is_referenced_component": False,
                                "occurrence_count_for_component": 0,
                            }
                        ],
                        "ambiguous": False,
                        "truncated": False,
                        "match_count": 1,
                        "match_count_exact": True,
                    },
                ],
                "warnings": [],
            }
            return {"ok": True, "data": {"message": json.dumps(payload)}}
        if name == "fusion_mcp_execute":
            self.mutating_calls += 1
            assert (
                f'_expected_components = {{"root": "{_ROOT_COMPONENT_BINDING}"}}'
                in arguments["object"]["script"]
            )
            assert "return _design.rootComponent" in arguments["object"]["script"]
            assert arguments["object"]["script"].count("def run(") == 1
            assert "def _fusion_agent_user_run(" in arguments["object"]["script"]
            assert "isinstance(_found[1], bool)" in arguments["object"]["script"]
            assert (
                "_items = list(_found) if _found is not None else []"
                in arguments["object"]["script"]
            )
            assert "except TypeError:" in arguments["object"]["script"]
            return {"ok": True, "data": {"message": "done"}}
        raise AssertionError((name, arguments, semantics, operation_id))


@pytest.mark.asyncio
async def test_fast_execute_uses_one_mutation_between_baseline_and_readback() -> None:
    native = FakeNative()
    service = FastPathService(native, manifest_fingerprint=lambda: "manifest-hash")
    request = {
        "intent": "Create Body1",
        "change_class": "additive",
        "script": ADDITIVE_SCRIPT,
        "api_references": ["adsk.fusion.BRepBodies.add"],
        "target_query_ids": ["body_target"],
        "verification": {
            "queries": [
                {
                    "id": "body_target",
                    "entity_type": "body",
                    "selector": {"component_path": "root", "name": "Body1"},
                    "fields": ["exists"],
                }
            ],
            "assertions": [
                {
                    "id": "body_exists",
                    "query_id": "body_target",
                    "field": "exists",
                    "operator": "eq",
                    "expected": True,
                }
            ],
            "requirements": [
                {
                    "id": "body_created",
                    "assertion_ids": ["body_exists"],
                    "required": True,
                }
            ],
        },
    }
    validate_fast_execute_request(request)
    response = await service.fast_execute(request)

    assert response.payload["status"] == "applied_verified"
    assert response.payload["script_sha256"]
    assert native.mutating_calls == 1
    assert native.inspections == 3
    assert response.payload["bindings"]["target_components"] == ["root"]
    assert response.payload["transport_mutating_dispatch_count"] == 1
    assert response.payload["declared_mutation_count"] == 1
    assert response.payload["executor_guard"]["within_limit"] is True
    assert response.payload["executor_guard"]["stream_normalization"] == (
        "preserve_current_ns_writer_collapse_original_chain"
    )
    assert (
        response.payload["executor_guard"]["fallback_streams"]
        == "sys.__stdout__/sys.__stderr__"
    )


@pytest.mark.asyncio
async def test_post_dispatch_timeout_stays_unknown_even_when_readback_assertions_pass() -> (
    None
):
    class UnknownOutcomeNative(FakeNative):
        async def __call__(self, name, arguments, *, semantics, operation_id):
            result = await super().__call__(
                name,
                arguments,
                semantics=semantics,
                operation_id=operation_id,
            )
            if name == "fusion_mcp_execute" and semantics == "mutating":
                return {
                    "ok": False,
                    "error_code": ErrorCode.MUTATION_OUTCOME_UNKNOWN.value,
                    "error_message": "timeout after dispatch",
                    "data": {"dispatched": True},
                    "_meta": {
                        "fusion_agent_transport": {
                            "operation_id": operation_id,
                            "dispatched": True,
                            "mutation_outcome": "unknown",
                            "post_dispatch_replay_suppressed": True,
                        }
                    },
                }
            return result

    response = await FastPathService(UnknownOutcomeNative()).fast_execute(
        {
            "intent": "Create Body1",
            "change_class": "additive",
            "script": ADDITIVE_SCRIPT,
            "target_query_ids": ["body_target"],
            "verification": {
                "queries": [
                    {
                        "id": "body_target",
                        "entity_type": "body",
                        "selector": {"component_path": "root", "name": "Body1"},
                        "fields": ["exists"],
                    }
                ],
                "assertions": [
                    {
                        "id": "body_exists",
                        "query_id": "body_target",
                        "field": "exists",
                        "operator": "eq",
                        "expected": True,
                    }
                ],
                "requirements": [
                    {
                        "id": "body_created",
                        "assertion_ids": ["body_exists"],
                        "required": True,
                    }
                ],
            },
        }
    )

    assert response.payload["status"] == "mutation_outcome_unknown"
    assert response.payload["error_code"] == ErrorCode.MUTATION_OUTCOME_UNKNOWN.value
    assert response.payload["mutation_outcome"] == "unknown"
    assert response.payload["mutation_status"] == "outcome_unknown"
    assert response.payload["verification"]["contract_verified"] is False
    assert response.payload["verification"]["assertion_status"] == "passed"
    assert (
        response.payload["verification"]["drift_conclusion"]
        == "no_drift_in_observed_scope"
    )
    assert response.payload["post_dispatch_replay_suppressed"] is True
    assert response.is_error is True


@pytest.mark.asyncio
async def test_fast_execute_never_verifies_from_partial_readback() -> None:
    class PartialReadbackNative(FakeNative):
        async def __call__(self, name, arguments, *, semantics, operation_id):
            result = await super().__call__(
                name,
                arguments,
                semantics=semantics,
                operation_id=operation_id,
            )
            if (
                name == "fusion_mcp_execute"
                and semantics == "read_only"
                and "fusion_agent_active_command" not in arguments["object"]["script"]
                and self.inspections == 3
            ):
                payload = json.loads(result["data"]["message"])
                # The requested assertion is still present and true, but the
                # bounded oracle cannot prove the rest of the readback.
                payload.update(
                    {
                        "complete": False,
                        "truncated": True,
                        "counts_exact": False,
                        "stop_reason": "response_limit",
                    }
                )
                result["data"]["message"] = json.dumps(payload)
            return result

    native = PartialReadbackNative()
    response = await FastPathService(native).fast_execute(
        {
            "intent": "Create Body1",
            "change_class": "additive",
            "script": ADDITIVE_SCRIPT,
            "target_query_ids": ["body_target"],
            "verification": {
                "queries": [
                    {
                        "id": "body_target",
                        "entity_type": "body",
                        "selector": {"component_path": "root", "name": "Body1"},
                        "fields": ["exists"],
                    }
                ],
                "assertions": [
                    {
                        "id": "body_exists",
                        "query_id": "body_target",
                        "field": "exists",
                        "operator": "eq",
                        "expected": True,
                    }
                ],
            },
        }
    )

    assert native.mutating_calls == 1
    assert response.payload["status"] == "applied_unverified"
    assert response.payload["verification"]["passed"] is False
    assert response.payload["verification"]["assertion_status"] == "incomplete"
    assert response.payload["verification"]["readback_complete"] is False
    assert response.payload["verification"]["readback_issue"] == "complete_not_true"
    assert response.payload["verification"]["source"] == "partial_readback"
    assert response.payload["recovery_instruction"].startswith("Do not save")


@pytest.mark.asyncio
async def test_fast_execute_blocks_truncated_additive_baseline_before_dispatch() -> (
    None
):
    class TruncatedBaselineNative:
        def __init__(self) -> None:
            self.mutations = 0
            self.inspections = 0

        async def __call__(self, name, arguments, *, semantics, operation_id):
            del operation_id
            if name == "fusion_mcp_execute" and semantics == "read_only":
                if "fusion_agent_active_command" in arguments["object"]["script"]:
                    return {
                        "ok": True,
                        "data": {"message": json.dumps(ACTIVE_COMMAND_CLEAR)},
                    }
                self.inspections += 1
                payload = {
                    "success": True,
                    "complete": False,
                    "truncated": True,
                    "counts_exact": False,
                    "stop_reason": "max_entities_visited",
                    "document": {
                        "name": "D",
                        "id": "data-file",
                        "runtime_id": "data:data-file",
                    },
                    "summary": {
                        "state_fingerprint": None,
                        "state_fingerprint_truncated": True,
                    },
                    "results": [
                        {
                            "query_id": "body_target",
                            "matches": [],
                            "ambiguous": False,
                            "truncated": True,
                            "match_count": 0,
                            "match_count_exact": False,
                        }
                    ],
                }
                return {"ok": True, "data": {"message": json.dumps(payload)}}
            if name == "fusion_mcp_execute":
                self.mutations += 1
                return {"ok": True, "data": {"message": "must not execute"}}
            raise AssertionError((name, arguments, semantics))

    native = TruncatedBaselineNative()
    response = await FastPathService(native).fast_execute(
        {
            "intent": "Create Body1",
            "change_class": "additive",
            "script": ADDITIVE_SCRIPT,
            "target_query_ids": ["body_target"],
            "verification": {
                "queries": [
                    {
                        "id": "body_target",
                        "entity_type": "body",
                        "selector": {"component_path": "root", "name": "Body1"},
                    }
                ],
                "assertions": [
                    {
                        "query_id": "body_target",
                        "field": "exists",
                        "operator": "eq",
                        "expected": True,
                    }
                ],
            },
        }
    )

    assert response.payload["status"] == "blocked_before_apply"
    assert response.payload["reason"] == "incomplete_baseline"
    assert response.payload["baseline_issue"] == "complete_not_true"
    assert response.payload["transport_mutating_dispatch_count"] == 0
    assert native.mutations == 0
    assert native.inspections == 1


@pytest.mark.asyncio
async def test_fast_execute_blocks_oversized_protected_payload_before_dispatch(
    monkeypatch,
) -> None:
    monkeypatch.setattr(execute_guard, "DEFAULT_PROTECTED_SCRIPT_LIMIT_BYTES", 1)
    native = FakeNative()
    response = await FastPathService(native).fast_execute(
        {
            "intent": "Create Body1",
            "change_class": "additive",
            "script": ADDITIVE_SCRIPT,
            "target_query_ids": ["body_target"],
            "verification": {
                "queries": [
                    {
                        "id": "body_target",
                        "entity_type": "body",
                        "selector": {"component_path": "root", "name": "Body1"},
                    }
                ],
                "assertions": [
                    {
                        "query_id": "body_target",
                        "field": "exists",
                        "operator": "eq",
                        "expected": True,
                    }
                ],
            },
        }
    )

    assert response.payload["status"] == "blocked_before_apply"
    assert response.payload["reason"] == "protected_script_size_limit"
    assert response.payload["error_code"] == ErrorCode.SCRIPT_SIZE_LIMIT_EXCEEDED.value
    assert response.payload["executor_guard"]["limit_bytes"] == 1
    assert response.payload["executor_guard"]["protected_payload_bytes"] > 1
    assert response.payload["executor_guard"]["within_limit"] is False
    assert response.payload["transport_mutating_dispatch_count"] == 0
    assert response.payload["mutating_call_count"] == 0
    assert native.mutating_calls == 0
    assert native.inspections == 1


@pytest.mark.asyncio
async def test_scoped_update_is_bound_to_the_inspected_entity_token() -> None:
    class ScopedNative:
        def __init__(self) -> None:
            self.inspections = 0
            self.mutations = 0

        async def __call__(self, name, arguments, *, semantics, operation_id):
            del operation_id
            if name == "fusion_mcp_execute" and semantics == "read_only":
                script = arguments["object"]["script"]
                if "fusion_agent_active_command" in script:
                    return {
                        "ok": True,
                        "data": {"message": json.dumps(ACTIVE_COMMAND_CLEAR)},
                    }
                self.inspections += 1
                expression = "12 mm" if self.mutations else "10 mm"
                payload = {
                    "success": True,
                    "complete": True,
                    "truncated": False,
                    "counts_exact": True,
                    "stop_reason": None,
                    "document": {
                        "name": "D",
                        "id": "data-file",
                        "runtime_id": "runtime",
                    },
                    "summary": {
                        "components": 1,
                        "parameters": 1,
                        "state_fingerprint": "state-after"
                        if self.mutations
                        else "state-before",
                        "state_fingerprint_truncated": False,
                    },
                    "results": [
                        {
                            "query_id": "parameter_target",
                            "matches": [
                                {
                                    "entity_type": "parameter",
                                    "name": "Width",
                                    "entity_token": "parameter-width-token",
                                    "expression": expression,
                                    "visible": True,
                                    "is_referenced_component": False,
                                    "occurrence_count_for_component": 1,
                                }
                            ],
                            "ambiguous": False,
                            "truncated": False,
                            "match_count": 1,
                            "match_count_exact": True,
                        }
                    ],
                    "warnings": [],
                }
                return {"ok": True, "data": {"message": json.dumps(payload)}}
            if name == "fusion_mcp_execute":
                self.mutations += 1
                guarded = arguments["object"]["script"]
                assert (
                    '_expected_targets = {"parameter_target": "parameter-width-token"}'
                    in guarded
                )
                assert "parameter = targets['parameter_target']" in guarded
                return {"ok": True, "data": {"message": "updated"}}
            raise AssertionError((name, arguments, semantics))

    native = ScopedNative()
    response = await FastPathService(native).fast_execute(
        {
            "intent": "Update Width",
            "change_class": "scoped_update",
            "script": """import adsk.core

def run(_context: str):
    parameter = targets['parameter_target']
    parameter.expression = '12 mm'
""",
            "target_query_ids": ["parameter_target"],
            "verification": {
                "queries": [
                    {
                        "id": "parameter_target",
                        "entity_type": "parameter",
                        "selector": {"name": "Width"},
                        "fields": ["exists", "expression"],
                    }
                ],
                "assertions": [
                    {
                        "id": "width_updated",
                        "query_id": "parameter_target",
                        "field": "expression",
                        "operator": "eq",
                        "expected": "12 mm",
                    }
                ],
                "requirements": [
                    {
                        "id": "requested_width",
                        "assertion_ids": ["width_updated"],
                        "required": True,
                    }
                ],
            },
        }
    )

    assert response.payload["status"] == "applied_verified"
    assert response.payload["bindings"]["targets"] == ["parameter_target"]
    assert native.mutations == 1


@pytest.mark.asyncio
async def test_native_screenshot_preserves_image_content_without_json_base64() -> None:
    service = FastPathService(FakeNative())
    response = await service.native_read(
        {"query_type": "screenshot", "width": 128, "height": 128}
    )

    assert response.payload["status"] == "read_succeeded"
    assert response.content[0]["type"] == "image"
    assert "data" not in response.payload["data"]
    assert response.payload["data"]["image_in_content"] is True
    assert response.meta == {"native_trace": "screenshot"}


@pytest.mark.asyncio
async def test_active_command_blocks_before_mutation() -> None:
    class BusyNative(FakeNative):
        async def __call__(self, name, arguments, *, semantics, operation_id):
            if (
                name == "fusion_mcp_execute"
                and "fusion_agent_active_command" in arguments["object"]["script"]
            ):
                return {
                    "ok": True,
                    "data": {
                        "message": json.dumps(
                            {
                                "success": True,
                                "complete": True,
                                "activeCommandRead": True,
                                "activeCommand": {
                                    "id": "Extrude",
                                    "isDefaultCommand": False,
                                },
                            }
                        )
                    },
                }
            return await super().__call__(
                name, arguments, semantics=semantics, operation_id=operation_id
            )

    native = BusyNative()
    service = FastPathService(native)
    request = {
        "intent": "Create Body1",
        "change_class": "additive",
        "script": ADDITIVE_SCRIPT,
        "target_query_ids": ["body_target"],
        "verification": {
            "queries": [
                {
                    "id": "body_target",
                    "entity_type": "body",
                    "selector": {"component_path": "root", "name": "Body1"},
                }
            ],
            "assertions": [
                {
                    "query_id": "body_target",
                    "field": "exists",
                    "operator": "eq",
                    "expected": True,
                }
            ],
        },
    }
    response = await service.fast_execute(request)

    assert response.payload["status"] == "blocked_before_apply"
    assert native.mutating_calls == 0


@pytest.mark.asyncio
async def test_recovery_active_command_blocks_before_update_dispatch() -> None:
    class BusyRecoveryNative:
        def __init__(self) -> None:
            self.updates = 0

        async def __call__(self, name, arguments, *, semantics, operation_id):
            del semantics, operation_id
            if (
                name == "fusion_mcp_execute"
                and "fusion_agent_active_command" in arguments["object"]["script"]
            ):
                return {
                    "ok": True,
                    "data": {
                        "message": json.dumps(
                            {
                                "success": True,
                                "complete": True,
                                "activeCommandRead": True,
                                "activeCommand": {
                                    "id": "Extrude",
                                    "isDefaultCommand": False,
                                },
                            }
                        )
                    },
                }
            if name == "fusion_mcp_update":
                self.updates += 1
            raise AssertionError((name, arguments))

    native = BusyRecoveryNative()
    service = FastPathService(native)
    service._recovery_records["op"] = _RecoveryRecord(
        operation_id="op",
        document={"id": "doc"},
        state_fingerprint="state",
        inspection_args={"queries": []},
        after={},
    )

    response = await service.recover_change(
        {
            "action": "undo",
            "operation_id": "op",
            "confirm": True,
            "verification": {
                "queries": [
                    {"id": "body", "entity_type": "body", "selector": {"name": "Body1"}}
                ],
                "assertions": [
                    {
                        "query_id": "body",
                        "field": "exists",
                        "operator": "eq",
                        "expected": False,
                    }
                ],
            },
        }
    )

    assert response.payload["status"] == "blocked_before_apply"
    assert response.payload["reason"] == "active_command"
    assert native.updates == 0


@pytest.mark.asyncio
async def test_document_runtime_identity_guard_blocks_switched_unsaved_document() -> (
    None
):
    class SwitchedDocumentNative:
        def __init__(self) -> None:
            self.inspections = 0
            self.mutating_calls = 0

        async def __call__(self, name, arguments, *, semantics, operation_id):
            del operation_id
            if name == "fusion_mcp_execute" and semantics == "read_only":
                if "fusion_agent_active_command" in arguments["object"]["script"]:
                    return {
                        "ok": True,
                        "data": {"message": json.dumps(ACTIVE_COMMAND_CLEAR)},
                    }
                self.inspections += 1
                runtime_id = (
                    "doc-runtime-a" if self.inspections <= 2 else "doc-runtime-b"
                )
                payload = {
                    "success": True,
                    "complete": True,
                    "truncated": False,
                    "counts_exact": True,
                    "stop_reason": None,
                    "document": {
                        "name": "Untitled",
                        "id": "",
                        "runtime_id": runtime_id,
                    },
                    "summary": {
                        "components": 1,
                        "bodies": 0,
                        "state_fingerprint": "state-after"
                        if self.mutating_calls
                        else "state-before",
                        "state_fingerprint_truncated": False,
                    },
                    "results": [
                        {
                            "query_id": "body_target",
                            "matches": [],
                            "ambiguous": False,
                            "truncated": False,
                            "match_count": 0,
                            "match_count_exact": True,
                        },
                        {
                            "query_id": "__fusion_agent_component_4813494d137e1631",
                            "matches": [
                                {
                                    "entity_type": "component",
                                    "name": "root",
                                    "path": "root",
                                    "paths": ["root"],
                                    "entity_token": "component-root-token",
                                    "visible": True,
                                    "is_referenced_component": False,
                                    "occurrence_count_for_component": 0,
                                }
                            ],
                            "ambiguous": False,
                            "truncated": False,
                            "match_count": 1,
                            "match_count_exact": True,
                        },
                    ],
                    "warnings": [],
                }
                return {"ok": True, "data": {"message": json.dumps(payload)}}
            if name == "fusion_mcp_execute":
                self.mutating_calls += 1
                assert (
                    '_expected_runtime_id = "doc-runtime-a"'
                    in arguments["object"]["script"]
                )
                assignments = {
                    node.targets[0].id: ast.literal_eval(node.value)
                    for node in ast.walk(ast.parse(arguments["object"]["script"]))
                    if isinstance(node, ast.Assign)
                    and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id
                    in {
                        "_fusion_agent_guard_token",
                        "_fusion_agent_guard_binding_digest",
                    }
                }
                control = {
                    "fusion_agent_guard": {
                        "schema": GUARD_CONTROL_SCHEMA,
                        "token": assignments["_fusion_agent_guard_token"],
                        "binding_digest": assignments[
                            "_fusion_agent_guard_binding_digest"
                        ],
                        "status": "rejected_before_apply",
                        "reason_code": "DOCUMENT_RUNTIME_ID_CHANGED",
                    }
                }
                return ToolResult.from_mcp(
                    {
                        "content": [{"type": "text", "text": json.dumps(control)}],
                        "_meta": {
                            "fusion_agent_transport": {
                                "dispatched": True,
                                "mutation_outcome": "known",
                                "post_dispatch_replay_suppressed": True,
                            }
                        },
                    }
                )
            raise AssertionError((name, arguments, semantics))

    native = SwitchedDocumentNative()
    response = await FastPathService(native).fast_execute(
        {
            "intent": "Create Body1",
            "change_class": "additive",
            "script": ADDITIVE_SCRIPT,
            "target_query_ids": ["body_target"],
            "verification": {
                "queries": [
                    {
                        "id": "body_target",
                        "entity_type": "body",
                        "selector": {"component_path": "root", "name": "Body1"},
                    }
                ],
                "assertions": [
                    {
                        "query_id": "body_target",
                        "field": "exists",
                        "operator": "eq",
                        "expected": True,
                    }
                ],
            },
        }
    )

    assert response.payload["status"] == "blocked_before_apply"
    assert response.payload["verification"]["source"] == "document_identity_guard"
    assert native.mutating_calls == 1


@pytest.mark.parametrize(
    ("match_overrides", "result_overrides", "expected_reason"),
    [
        (
            {"visible": False},
            {},
            "hidden_target_requires_safe_harness:parameter_target",
        ),
        (
            {"is_referenced_component": True},
            {},
            "referenced_target_requires_safe_harness:parameter_target",
        ),
        (
            {"occurrence_count_for_component": 2},
            {},
            "shared_component_requires_safe_harness:parameter_target",
        ),
        (
            {},
            {"ambiguous": True, "match_count": 2},
            "ambiguous_target:parameter_target",
        ),
    ],
)
@pytest.mark.asyncio
async def test_fast_execute_blocks_ineligible_or_ambiguous_targets_before_dispatch(
    match_overrides: dict,
    result_overrides: dict,
    expected_reason: str,
) -> None:
    class TargetPolicyNative:
        def __init__(self) -> None:
            self.mutating_calls = 0

        async def __call__(self, name, arguments, *, semantics, operation_id):
            del operation_id
            if name == "fusion_mcp_execute" and semantics == "read_only":
                script = arguments["object"]["script"]
                if "fusion_agent_active_command" in script:
                    return {
                        "ok": True,
                        "data": {"message": json.dumps(ACTIVE_COMMAND_CLEAR)},
                    }
                match = {
                    "entity_type": "parameter",
                    "name": "Width",
                    "exists": True,
                    "expression": "10 mm",
                    "visible": True,
                    "is_referenced_component": False,
                    "occurrence_count_for_component": 1,
                    "entity_token": "parameter-width-token",
                    **match_overrides,
                }
                result = {
                    "query_id": "parameter_target",
                    "matches": [match],
                    "ambiguous": False,
                    "truncated": False,
                    "match_count": 1,
                    "match_count_exact": True,
                    **result_overrides,
                }
                if result.get("ambiguous") is True and result.get("match_count") == 2:
                    result["matches"] = [
                        match,
                        {**match, "entity_token": "parameter-width-token-duplicate"},
                    ]
                payload = {
                    "success": True,
                    "complete": True,
                    "truncated": False,
                    "counts_exact": True,
                    "stop_reason": None,
                    "document": {"name": "Untitled", "id": "trial-doc"},
                    "summary": {
                        "components": 1,
                        "parameters": 1,
                        "state_fingerprint": "state-target-policy",
                        "state_fingerprint_truncated": False,
                    },
                    "results": [result],
                    "warnings": [],
                }
                return {"ok": True, "data": {"message": json.dumps(payload)}}
            if name == "fusion_mcp_execute":
                self.mutating_calls += 1
                return {"ok": True, "data": {"message": "unexpected"}}
            raise AssertionError((name, arguments, semantics))

    native = TargetPolicyNative()
    service = FastPathService(native)
    request = {
        "intent": "Update Width",
        "change_class": "scoped_update",
        "script": """import adsk.core

def run(_context: str):
    parameter = targets["parameter_target"]
    parameter.expression = '12 mm'
""",
        "target_query_ids": ["parameter_target"],
        "verification": {
            "queries": [
                {
                    "id": "parameter_target",
                    "entity_type": "parameter",
                    "selector": {"name": "Width"},
                    "fields": ["exists", "expression"],
                }
            ],
            "assertions": [
                {
                    "query_id": "parameter_target",
                    "field": "expression",
                    "operator": "eq",
                    "expected": "12 mm",
                }
            ],
        },
    }

    response = await service.fast_execute(request)

    assert response.payload["status"] == "blocked_before_apply"
    assert response.payload["reason"] == expected_reason
    assert native.mutating_calls == 0


def test_read_only_fast_execute_can_omit_mutation_verification_contract() -> None:
    normalized = validate_fast_execute_request(
        {
            "intent": "Read the Fusion version",
            "change_class": "read_only",
            "script": READ_SCRIPT,
        }
    )

    assert normalized["target_query_ids"] == []
    assert normalized["verification"]["assertions"] == []
    assert normalized["verification"]["queries"][0]["entity_type"] == "document"


def test_passing_assertions_without_requirement_coverage_are_not_contract_verified() -> (
    None
):
    baseline = {
        "complete": True,
        "truncated": False,
        "counts_exact": True,
        "stop_reason": None,
        "document": {"id": "doc"},
        "summary": {"components": 1},
        "results": [
            {
                "query_id": "target",
                "matches": [],
                "ambiguous": False,
                "truncated": False,
                "match_count": 0,
                "match_count_exact": True,
            }
        ],
    }
    after = {
        "complete": True,
        "truncated": False,
        "counts_exact": True,
        "stop_reason": None,
        "document": {"id": "doc"},
        "summary": {"components": 1},
        "results": [
            {
                "query_id": "target",
                "matches": [{"name": "Body1"}],
                "ambiguous": False,
                "truncated": False,
                "match_count": 1,
                "match_count_exact": True,
            }
        ],
    }
    result = evaluate_verification(
        baseline,
        after,
        [
            {
                "id": "exists",
                "query_id": "target",
                "field": "exists",
                "operator": "eq",
                "expected": True,
            }
        ],
        "scoped_update",
        [{"id": "intent", "required": True, "assertion_ids": []}],
    )

    assert result["passed"] is True
    assert result["assertion_status"] == "passed"
    assert result["intent_coverage"] == "none"
    assert result["verification_level"] == "contract"
    assert result["contract_verified"] is False


def test_independent_oracle_label_cannot_self_elevate_contract_assertions() -> None:
    baseline = {
        "complete": True,
        "truncated": False,
        "counts_exact": True,
        "stop_reason": None,
        "document": {"id": "doc"},
        "summary": {"components": 1},
        "results": [
            {
                "query_id": "target",
                "matches": [],
                "ambiguous": False,
                "truncated": False,
                "match_count": 0,
                "match_count_exact": True,
            }
        ],
    }
    after = {
        "complete": True,
        "truncated": False,
        "counts_exact": True,
        "stop_reason": None,
        "document": {"id": "doc"},
        "summary": {"components": 1},
        "results": [
            {
                "query_id": "target",
                "matches": [{"name": "Body1"}],
                "ambiguous": False,
                "truncated": False,
                "match_count": 1,
                "match_count_exact": True,
            }
        ],
    }
    result = evaluate_verification(
        baseline,
        after,
        [
            {
                "id": "exists",
                "query_id": "target",
                "field": "exists",
                "operator": "eq",
                "expected": True,
            }
        ],
        "scoped_update",
        [
            {
                "id": "intent",
                "required": True,
                "assertion_ids": ["exists"],
                "oracle": "independent_oracle",
            }
        ],
    )

    assert result["assertion_status"] == "passed"
    assert result["verification_level"] == "independent_oracle"
    assert result["intent_coverage"] == "none"
    assert result["requirements"][0]["covered"] is False
    assert result["contract_verified"] is False
    assert result["requirements"][0]["oracle_evidence"] == "not_available"


@pytest.mark.parametrize(
    ("component_overrides", "expected"),
    [
        ({"visible": False}, "hidden_target_requires_safe_harness:component:root"),
        (
            {"is_referenced_component": True},
            "referenced_target_requires_safe_harness:component:root",
        ),
        (
            {"occurrence_count_for_component": 2},
            "shared_component_requires_safe_harness:component:root",
        ),
    ],
)
def test_additive_component_binding_is_exact_visible_local_and_unshared(
    component_overrides: dict,
    expected: str,
) -> None:
    request = validate_fast_execute_request(
        {
            "intent": "Create Body1",
            "change_class": "additive",
            "script": ADDITIVE_SCRIPT,
            "target_query_ids": ["body_target"],
            "verification": {
                "queries": [
                    {
                        "id": "body_target",
                        "entity_type": "body",
                        "selector": {"component_path": "root", "name": "Body1"},
                    }
                ],
                "assertions": [
                    {
                        "query_id": "body_target",
                        "field": "exists",
                        "operator": "eq",
                        "expected": True,
                    }
                ],
            },
        }
    )
    component = {
        "entity_type": "component",
        "name": "root",
        "paths": ["root"],
        "entity_token": "component-root-token",
        "visible": True,
        "is_referenced_component": False,
        "occurrence_count_for_component": 0,
        **component_overrides,
    }
    snapshot = {
        "document": {"name": "D", "runtime_id": "runtime"},
        "results": [
            {"query_id": "body_target", "matches": [], "ambiguous": False},
            {
                "query_id": _component_query_id("root"),
                "matches": [component],
                "ambiguous": False,
            },
        ],
    }

    error, bindings = _validate_targets(request, snapshot)

    assert error == expected
    assert bindings["target_components"] == {}


def test_root_component_binding_does_not_depend_on_unresolvable_entity_token() -> None:
    request = validate_fast_execute_request(
        {
            "intent": "Create Body1",
            "change_class": "additive",
            "script": ADDITIVE_SCRIPT,
            "target_query_ids": ["body_target"],
            "verification": {
                "queries": [
                    {
                        "id": "body_target",
                        "entity_type": "body",
                        "selector": {"component_path": "root", "name": "Body1"},
                    }
                ],
                "assertions": [
                    {
                        "query_id": "body_target",
                        "field": "exists",
                        "operator": "eq",
                        "expected": True,
                    }
                ],
            },
        }
    )
    snapshot = {
        "document": {"name": "D", "runtime_id": "marker:trial"},
        "results": [
            {"query_id": "body_target", "matches": [], "ambiguous": False},
            {
                "query_id": _component_query_id("root"),
                "matches": [
                    {
                        "entity_type": "component",
                        "name": "root",
                        "paths": ["root"],
                        "entity_token": "",
                        "visible": True,
                        "is_referenced_component": False,
                        "occurrence_count_for_component": 0,
                    }
                ],
                "ambiguous": False,
            },
        ],
    }

    error, bindings = _validate_targets(request, snapshot)

    assert error is None
    assert bindings["target_components"] == {"root": _ROOT_COMPONENT_BINDING}
