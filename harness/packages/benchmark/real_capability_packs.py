"""Nightly CadSpec v2 capability packs for a real Autodesk Fusion runner.

Every case owns one disposable, unsaved Fusion document.  The module never
opens, saves, or mutates a user document: ``FusionRuntimeLifecycleBackend``
captures the original saved-document identity, creates a uniquely marked
fixture, and closes only that exact marker without saving.  Programmatic
readback is deliberately separate from the typed executor result.

The public builders are safe to import in offline tests.  Real Fusion is only
contacted by :func:`run_capability_pack_suite`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from agent_core.authority import HostOutputDisabledError, REAL_HOST_OUTPUT_POLICY
from agent_core.capability_executor import CapabilityExecutionResult
from benchmark.filesystem import (
    atomic_write_text,
    mkdir,
    path_exists,
    path_is_file,
    unlink,
)
from benchmark.fixtures import FixtureDefinition
from benchmark.models import BenchmarkCase
from benchmark.runner import TrialContext
from cad_spec.v2 import CadSpecV2, EXPERIMENTAL_CAPABILITIES
from fusion_agent_mcp.benchmark_bridge import (
    FixtureIdentity,
    FixtureSession,
    FusionRuntimeLifecycleBackend,
    _decode_script_payload,
)
from fusion_agent_mcp.runtime import FusionAgentRuntime, RuntimeConfiguration


RESULT_SCHEMA_VERSION = "fusion_real_capability_packs.v1"
CASE_SCHEMA_VERSION = "fusion_real_capability_pack_case.v1"
ORACLE_SCHEMA_VERSION = "fusion_real_capability_oracle.v1"
REQUIREMENT_ID = "independent_pack_contract"
ASSERTION_ID = "independent_pack_oracle"

# Capabilities explicitly promoted by the non-experimental 0.3 capability
# packs.  Basic sketch/body/component operations are prerequisites and are
# still present in the strict specs, but are not counted as a pack by
# themselves here.
NIGHTLY_PACK_CAPABILITIES = frozenset(
    {
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
        "joint",
        "joint_with_limits",
        "as_built_joint",
        "rigid_groups",
        "physical_properties",
        "interference",
    }
)


@dataclass(frozen=True, slots=True)
class CapabilityPackCase:
    """One isolated real-Fusion case and its independent readback contract."""

    id: str
    group: str
    target_capabilities: tuple[str, ...]
    spec: CadSpecV2
    oracle_expectations: dict[str, Any]
    execution_expectation: str = "success"


def _operation(
    operation_id: str,
    kind: str,
    *,
    depends_on: Iterable[str] = (),
    **values: Any,
) -> dict[str, Any]:
    return {
        "id": operation_id,
        "kind": kind,
        "depends_on": list(depends_on),
        "requirement_ids": [REQUIREMENT_ID],
        **values,
    }


def _contract(
    case_id: str,
    intent: str,
    operations: list[dict[str, Any]],
    oracle_expectations: dict[str, Any],
) -> CadSpecV2:
    return CadSpecV2.model_validate(
        {
            "cad_spec_version": "2.0",
            "intent": intent,
            "document_policy": {
                "modify_existing": False,
                "create_checkpoint": False,
            },
            "requirements": [
                {
                    "id": REQUIREMENT_ID,
                    "description": (
                        "Independent readback proves the promoted capability on the "
                        "uniquely marked disposable document"
                    ),
                    "oracle": "independent",
                    "assertion_ids": [ASSERTION_ID],
                }
            ],
            "operations": operations,
            "assertions": [
                {
                    "id": ASSERTION_ID,
                    "kind": "custom_oracle",
                    "target_ref": case_id,
                    "expected": oracle_expectations,
                }
            ],
        }
    )


def _case(
    case_id: str,
    group: str,
    target_capabilities: Iterable[str],
    intent: str,
    operations: list[dict[str, Any]],
    oracle_expectations: dict[str, Any],
    *,
    execution_expectation: str = "success",
) -> CapabilityPackCase:
    value = CapabilityPackCase(
        id=case_id,
        group=group,
        target_capabilities=tuple(sorted(set(target_capabilities))),
        spec=_contract(case_id, intent, operations, oracle_expectations),
        oracle_expectations=oracle_expectations,
        execution_expectation=execution_expectation,
    )
    missing = set(value.target_capabilities) - value.spec.capabilities
    if missing:
        raise ValueError(
            f"case {case_id} does not exercise declared capabilities: {sorted(missing)}"
        )
    experimental = set(value.spec.capabilities) & EXPERIMENTAL_CAPABILITIES
    experimental.update(
        capability
        for capability in value.spec.capabilities
        if capability.startswith("sheet_metal_") or capability.startswith("cam_")
    )
    if experimental:
        raise ValueError(
            f"nightly case {case_id} contains experimental capabilities: {sorted(experimental)}"
        )
    return value


def build_capability_pack_cases(
    artifact_root: Path | str,
) -> tuple[CapabilityPackCase, ...]:
    """Build all strict, non-experimental Autodesk nightly cases.

    I/O paths are resolved beneath ``artifact_root`` so the pack cannot write
    to a user-selected location.  The returned specs are deterministic for a
    given root and can be validated entirely offline.
    """

    root = Path(artifact_root).resolve()
    io_root = root / "io"
    denied_output_path = str((io_root / "deny-io-output.step").resolve())

    cases = [
        _case(
            "sketch_constraints_dimensions",
            "sketch_constraints_dimensions",
            {"sketch_constraints", "sketch_dimensions"},
            "Create a constrained and dimensioned sketch, then prove it by independent readback.",
            [
                _operation(
                    "create_contract_sketch",
                    "sketch.create",
                    component_ref="root",
                    plane="XY",
                    name="contract_sketch",
                ),
                _operation(
                    "draw_contract_rectangle",
                    "sketch.rectangle",
                    depends_on=["create_contract_sketch"],
                    sketch_ref="contract_sketch",
                    center=["0 mm", "0 mm"],
                    width="20 mm",
                    height="12 mm",
                    result_ref="contract_profile",
                ),
                _operation(
                    "constrain_contract_line",
                    "sketch.constraint",
                    depends_on=["draw_contract_rectangle"],
                    sketch_ref="contract_sketch",
                    constraint="horizontal",
                    entity_refs=["contract_sketch/line#0"],
                ),
                _operation(
                    "dimension_contract_line",
                    "sketch.dimension",
                    depends_on=["constrain_contract_line"],
                    sketch_ref="contract_sketch",
                    dimension="distance",
                    entity_refs=["contract_sketch/line#0"],
                    expression="20 mm",
                ),
                _operation(
                    "extrude_contract_body",
                    "feature.extrude",
                    depends_on=["dimension_contract_line"],
                    component_ref="root",
                    profile_ref="contract_profile",
                    distance="4 mm",
                    result_name="contract_body",
                ),
            ],
            {
                "sketches": ["contract_sketch"],
                "bodies": ["contract_body"],
                "features": ["extrude_contract_body"],
                "sketch_metrics": {
                    "contract_sketch": {"constraints_min": 1, "dimensions_min": 1}
                },
                "positive_physical_bodies": ["contract_body"],
            },
        ),
        _case(
            "feature_revolve",
            "revolve_sweep_loft",
            {"revolve"},
            "Create an offset closed profile and revolve it around a principal axis.",
            [
                _operation(
                    "create_revolve_profile",
                    "sketch.create",
                    component_ref="root",
                    plane="XY",
                    name="revolve_profile_sketch",
                ),
                _operation(
                    "draw_revolve_profile",
                    "sketch.rectangle",
                    depends_on=["create_revolve_profile"],
                    sketch_ref="revolve_profile_sketch",
                    center=["0 mm", "8 mm"],
                    width="4 mm",
                    height="4 mm",
                    result_ref="revolve_profile",
                ),
                _operation(
                    "revolve_profile_body",
                    "feature.revolve",
                    depends_on=["draw_revolve_profile"],
                    component_ref="root",
                    profile_ref="revolve_profile",
                    axis_ref="x",
                    angle="360 deg",
                    result_name="revolve_body",
                ),
            ],
            {
                "sketches": ["revolve_profile_sketch"],
                "bodies": ["revolve_body"],
                "features": ["revolve_profile_body"],
                "positive_physical_bodies": ["revolve_body"],
            },
        ),
        _case(
            "feature_sweep",
            "revolve_sweep_loft",
            {"sweep"},
            "Sweep a typed circular profile along a typed sketch-entity path.",
            [
                _operation(
                    "create_sweep_path",
                    "sketch.create",
                    component_ref="root",
                    plane="XY",
                    name="sweep_path",
                ),
                _operation(
                    "draw_sweep_path",
                    "sketch.rectangle",
                    depends_on=["create_sweep_path"],
                    sketch_ref="sweep_path",
                    center=["5 mm", "1 mm"],
                    width="10 mm",
                    height="2 mm",
                    result_ref="unused_sweep_path_profile",
                ),
                _operation(
                    "create_sweep_profile",
                    "sketch.create",
                    depends_on=["draw_sweep_path"],
                    component_ref="root",
                    plane="YZ",
                    name="sweep_profile_sketch",
                ),
                _operation(
                    "draw_sweep_profile",
                    "sketch.circle",
                    depends_on=["create_sweep_profile"],
                    sketch_ref="sweep_profile_sketch",
                    center=["0 mm", "0 mm"],
                    diameter="2 mm",
                    result_ref="sweep_profile",
                ),
                _operation(
                    "sweep_profile_body",
                    "feature.sweep",
                    depends_on=["draw_sweep_profile"],
                    component_ref="root",
                    profile_ref="sweep_profile",
                    path_ref="sweep_path/line#0",
                    orientation="perpendicular",
                    result_name="sweep_body",
                ),
            ],
            {
                "sketches": ["sweep_path", "sweep_profile_sketch"],
                "bodies": ["sweep_body"],
                "features": ["sweep_profile_body"],
                "positive_physical_bodies": ["sweep_body"],
            },
        ),
        _case(
            "feature_loft",
            "revolve_sweep_loft",
            {"loft"},
            "Loft between two independently named typed profiles.",
            [
                _operation(
                    "create_loft_profile_a",
                    "sketch.create",
                    component_ref="root",
                    plane="XY",
                    name="loft_profile_a_sketch",
                ),
                _operation(
                    "draw_loft_profile_a",
                    "sketch.circle",
                    depends_on=["create_loft_profile_a"],
                    sketch_ref="loft_profile_a_sketch",
                    center=["0 mm", "0 mm"],
                    diameter="8 mm",
                    result_ref="loft_profile_a",
                ),
                _operation(
                    "create_loft_profile_b",
                    "sketch.create",
                    depends_on=["draw_loft_profile_a"],
                    component_ref="root",
                    plane="XZ",
                    name="loft_profile_b_sketch",
                ),
                _operation(
                    "draw_loft_profile_b",
                    "sketch.circle",
                    depends_on=["create_loft_profile_b"],
                    sketch_ref="loft_profile_b_sketch",
                    center=["0 mm", "12 mm"],
                    diameter="4 mm",
                    result_ref="loft_profile_b",
                ),
                _operation(
                    "loft_profile_body",
                    "feature.loft",
                    depends_on=["draw_loft_profile_b"],
                    component_ref="root",
                    profile_refs=["loft_profile_a", "loft_profile_b"],
                    result_name="loft_body",
                ),
            ],
            {
                "sketches": ["loft_profile_a_sketch", "loft_profile_b_sketch"],
                "bodies": ["loft_body"],
                "features": ["loft_profile_body"],
                "positive_physical_bodies": ["loft_body"],
            },
        ),
        _pattern_case("rectangular"),
        _pattern_case("circular"),
        _pattern_case("path"),
        _mirror_case(),
        _boolean_case("join"),
        _boolean_case("split"),
        _assembly_case(),
        _analysis_case(),
        _output_denial_case(denied_output_path),
    ]

    ids = [case.id for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("capability pack case ids must be unique")
    covered = {capability for case in cases for capability in case.target_capabilities}
    if covered != set(NIGHTLY_PACK_CAPABILITIES):
        raise ValueError(
            "capability pack coverage mismatch: "
            f"missing={sorted(set(NIGHTLY_PACK_CAPABILITIES) - covered)} "
            f"extra={sorted(covered - set(NIGHTLY_PACK_CAPABILITIES))}"
        )
    return tuple(cases)


def _snake_token(value: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", value).lower()


def _base_body_operations(
    prefix: str, *, center: tuple[str, str] = ("8 mm", "0 mm")
) -> list[dict[str, Any]]:
    token = _snake_token(prefix)
    sketch = f"{token}_sketch"
    profile = f"{token}_profile"
    body = f"{token}_body"
    return [
        _operation(
            f"create_{token}_sketch",
            "sketch.create",
            component_ref="root",
            plane="XY",
            name=sketch,
        ),
        _operation(
            f"draw_{token}_profile",
            "sketch.circle",
            depends_on=[f"create_{token}_sketch"],
            sketch_ref=sketch,
            center=list(center),
            diameter="4 mm",
            result_ref=profile,
        ),
        _operation(
            f"extrude_{token}_body",
            "feature.extrude",
            depends_on=[f"draw_{token}_profile"],
            component_ref="root",
            profile_ref=profile,
            distance="4 mm",
            result_name=body,
        ),
    ]


def _pattern_case(pattern: str) -> CapabilityPackCase:
    prefix = pattern.capitalize()
    token = _snake_token(prefix)
    operations = _base_body_operations(prefix)
    pattern_values: dict[str, Any] = {
        "pattern": pattern,
        "target_refs": [f"{token}_body"],
        "count": 3,
    }
    if pattern == "rectangular":
        pattern_values.update({"spacing": "8 mm", "axis_ref": "x"})
    elif pattern == "circular":
        pattern_values.update({"axis_ref": "z"})
    else:
        operations.extend(
            [
                _operation(
                    "create_path_pattern_path",
                    "sketch.create",
                    depends_on=[operations[-1]["id"]],
                    component_ref="root",
                    plane="XY",
                    name="path_pattern_path",
                ),
                _operation(
                    "draw_path_pattern_path",
                    "sketch.rectangle",
                    depends_on=["create_path_pattern_path"],
                    sketch_ref="path_pattern_path",
                    center=["10 mm", "5 mm"],
                    width="20 mm",
                    height="10 mm",
                    result_ref="unused_path_pattern_profile",
                ),
            ]
        )
        pattern_values.update(
            {
                "spacing": "8 mm",
                "path_ref": "path_pattern_path/line#0",
            }
        )
    operation_id = f"{pattern}_body_pattern"
    operations.append(
        _operation(
            operation_id,
            "feature.pattern",
            depends_on=[operations[-1]["id"]],
            **pattern_values,
        )
    )
    return _case(
        f"pattern_{pattern}",
        "pattern_mirror_boolean_split",
        {f"pattern_{pattern}"},
        f"Create and independently verify a {pattern} body pattern.",
        operations,
        {
            "bodies": [f"{token}_body"],
            "features": [f"extrude_{token}_body", operation_id],
            "positive_physical_bodies": [f"{token}_body"],
        },
    )


def _mirror_case() -> CapabilityPackCase:
    operations = _base_body_operations("Mirror")
    operations.append(
        _operation(
            "mirror_body_feature",
            "feature.mirror",
            depends_on=[operations[-1]["id"]],
            target_refs=["mirror_body"],
            plane_ref="YZ",
            result_prefix="mirrored_body",
        )
    )
    return _case(
        "feature_mirror",
        "pattern_mirror_boolean_split",
        {"mirror"},
        "Mirror an offset body across a principal plane.",
        operations,
        {
            "bodies": ["mirror_body"],
            "features": ["extrude_mirror_body", "mirror_body_feature"],
            "positive_physical_bodies": ["mirror_body"],
        },
    )


def _boolean_case(operation: str) -> CapabilityPackCase:
    target_prefix = "JoinTarget" if operation == "join" else "SplitTarget"
    tool_prefix = "JoinTool" if operation == "join" else "SplitTool"
    operations = _base_body_operations(target_prefix, center=("0 mm", "0 mm"))
    operations.extend(_base_body_operations(tool_prefix, center=("2 mm", "0 mm")))
    operation_id = f"{operation}_body_feature"
    operations.append(
        _operation(
            operation_id,
            "feature.boolean",
            depends_on=[operations[-1]["id"]],
            operation=operation,
            target_ref=f"{_snake_token(target_prefix)}_body",
            tool_refs=[f"{_snake_token(tool_prefix)}_body"],
            keep_tools=True,
        )
    )
    capability = "split_body" if operation == "split" else "boolean"
    return _case(
        f"boolean_{operation}",
        "pattern_mirror_boolean_split",
        {capability},
        f"Execute and independently read back a typed {operation} feature.",
        operations,
        {
            "bodies": [f"{_snake_token(target_prefix)}_body"],
            "features": [operation_id],
            "positive_physical_bodies": [f"{_snake_token(target_prefix)}_body"],
        },
    )


def _assembly_case() -> CapabilityPackCase:
    components = [
        "rigid_parent",
        "rigid_child",
        "limited_parent",
        "limited_child",
        "as_built_parent",
        "as_built_child",
    ]
    operations: list[dict[str, Any]] = [
        _operation(
            "create_assembly_seed",
            "sketch.create",
            component_ref="root",
            plane="XY",
            name="assembly_seed",
        )
    ]
    previous = operations[-1]["id"]
    for component in components:
        operation_id = f"create_{component}"
        operations.append(
            _operation(
                operation_id,
                "component.create",
                depends_on=[previous],
                name=component,
            )
        )
        previous = operation_id
    operations.extend(
        [
            _operation(
                "create_rigid_joint",
                "assembly.joint",
                depends_on=[previous],
                name="rigid_joint",
                joint_type="rigid",
                parent_ref="rigid_parent",
                child_ref="rigid_child",
                axis="z",
            ),
            _operation(
                "create_limited_joint",
                "assembly.joint",
                depends_on=["create_rigid_joint"],
                name="limited_joint",
                joint_type="revolute",
                parent_ref="limited_parent",
                child_ref="limited_child",
                axis="z",
                limits={"minimum": "-45 deg", "maximum": "45 deg"},
            ),
            _operation(
                "create_as_built_joint",
                "assembly.joint",
                depends_on=["create_limited_joint"],
                name="as_built_joint",
                joint_type="as_built_rigid",
                parent_ref="as_built_parent",
                child_ref="as_built_child",
                axis="z",
            ),
            _operation(
                "create_rigid_group",
                "assembly.rigid_group",
                depends_on=["create_as_built_joint"],
                name="nightly_rigid_group",
                occurrence_refs=["rigid_parent", "rigid_child"],
            ),
        ]
    )
    return _case(
        "assembly_joints_rigid_groups",
        "joints_rigid_groups",
        {"joint", "joint_with_limits", "as_built_joint", "rigid_groups"},
        "Create native joints with typed limits and a native rigid group.",
        operations,
        {
            "components": components,
            "sketches": ["assembly_seed"],
            "joints": ["rigid_joint", "limited_joint", "as_built_joint"],
            "rigid_groups": ["nightly_rigid_group"],
        },
    )


def _analysis_case() -> CapabilityPackCase:
    operations = [
        _operation(
            "create_analysis_component",
            "component.create",
            name="analysis_component",
        ),
        _operation(
            "create_analysis_sketch",
            "sketch.create",
            depends_on=["create_analysis_component"],
            component_ref="analysis_component",
            plane="XY",
            name="analysis_sketch",
        ),
        _operation(
            "draw_analysis_profile",
            "sketch.circle",
            depends_on=["create_analysis_sketch"],
            sketch_ref="analysis_sketch",
            center=["0 mm", "0 mm"],
            diameter="4 mm",
            result_ref="analysis_profile",
        ),
        _operation(
            "extrude_analysis_body",
            "feature.extrude",
            depends_on=["draw_analysis_profile"],
            component_ref="analysis_component",
            profile_ref="analysis_profile",
            distance="4 mm",
            result_name="analysis_body",
        ),
        _operation(
            "measure_analysis_properties",
            "analysis.physical_properties",
            depends_on=["extrude_analysis_body"],
            target_refs=["analysis_component"],
            output_ref="analysis_properties",
        ),
        _operation(
            "analyze_fixture_interference",
            "analysis.interference",
            depends_on=["measure_analysis_properties"],
            target_refs=[],
            output_ref="analysis_interference",
        ),
    ]
    return _case(
        "analysis_physical_interference",
        "physical_properties_interference",
        {"physical_properties", "interference"},
        "Measure positive physical properties and independently prove interference state.",
        operations,
        {
            "components": ["analysis_component"],
            "bodies": ["analysis_body"],
            "features": ["extrude_analysis_body"],
            "positive_physical_bodies": ["analysis_body"],
            "interference_max": 0,
        },
    )


def _output_denial_case(output_path: str) -> CapabilityPackCase:
    operations = [
        _operation(
            "deny_real_output",
            "io.export",
            target_ref="root",
            path=output_path,
            format="step",
        )
    ]
    return _case(
        "output_deny_io_zero_dispatch",
        "host_output_deny_io",
        set(),
        "Prove that real Fusion host output is denied before transport dispatch.",
        operations,
        {"absent_files": [output_path]},
        execution_expectation="host_output_denied_zero_dispatch",
    )


def validate_real_runner_environment(
    configuration: RuntimeConfiguration,
) -> dict[str, str]:
    """Fail closed using the immutable process-start runtime configuration."""

    if configuration.backend != "autodesk_http":
        raise RuntimeError(
            "real capability packs require FUSION_AGENT_BACKEND=autodesk_http; "
            f"selected={configuration.backend}"
        )
    if configuration.default_mode != "real" or not configuration.require_real:
        raise RuntimeError(
            "real capability packs require FUSION_AGENT_DEFAULT_MODE=real and "
            "FUSION_AGENT_REQUIRE_REAL=1"
        )
    if configuration.allow_dry_run:
        raise RuntimeError("real capability packs refuse FUSION_AGENT_ALLOW_DRY_RUN=1")
    return {
        "backend": configuration.backend,
        "mode": configuration.default_mode,
        "require_real": "1" if configuration.require_real else "0",
        "allow_dry_run": "1" if configuration.allow_dry_run else "0",
    }


def _trial_context(case: CapabilityPackCase, run_id: str) -> TrialContext:
    trial_id = f"{case.id}_{uuid.uuid4().hex[:12]}"
    marker = f"fusion_agent_pack_{run_id}_{case.id}_{uuid.uuid4().hex[:10]}"
    benchmark_case = BenchmarkCase(
        id=case.id,
        prompt=case.spec.intent,
        category="cadspec_v2_capability_pack",
        risk="additive",
        timeout_seconds=900.0,
        fixture_id=f"empty_{case.id}",
        script_id=f"cadspec_v2_{case.id}",
        oracle_id=f"independent_{case.id}",
        execution_paths=["native_fast"],
    )
    return TrialContext(
        run_id=run_id,
        trial_id=trial_id,
        pair_id=trial_id,
        case=benchmark_case,
        fixture=FixtureDefinition(
            id=f"empty_{case.id}",
            state={"saved": False, "bodies": [], "features": []},
        ),
        execution_path="native_fast",
        mode="real",
        repetition=0,
        warmup=False,
        seed=42,
        project="fusion_real_capability_packs",
        dry_run=False,
        fixture_marker=marker,
    )


def _identity_matches(
    context: TrialContext,
    session: FixtureSession,
    identity: FixtureIdentity,
) -> bool:
    return bool(
        identity.document_id == session.fixture_document_id
        and identity.fixture_marker == context.fixture_marker
        and identity.fixture_marker == session.fixture_marker
        and identity.fixture_fingerprint == session.fixture_fingerprint
        and identity.unsaved
        and session.unsaved
    )


async def _safe_cleanup(
    lifecycle: FusionRuntimeLifecycleBackend,
    context: TrialContext,
    session: FixtureSession,
    baseline_open_ids: list[str],
) -> dict[str, Any]:
    closed = await asyncio.shield(
        lifecycle.close_fixture_without_save(context, session)
    )
    restored = await asyncio.shield(
        lifecycle.restore_original_document(context, session)
    )
    active_id = await lifecycle.read_active_document_id()
    open_ids = await lifecycle.list_open_document_ids()
    evidence: dict[str, Any] = {
        "closed_without_save": bool(closed),
        "restored": bool(restored),
        "identity_restored": active_id == session.original_document_id,
        "inventory_restored": sorted(open_ids) == sorted(baseline_open_ids),
    }
    evidence["passed"] = bool(
        evidence["closed_without_save"]
        and evidence["restored"]
        and evidence["identity_restored"]
        and evidence["inventory_restored"]
    )
    if not evidence["passed"]:
        raise RuntimeError("disposable fixture cleanup failed closed")
    return evidence


def build_independent_oracle_script(
    *,
    marker: str,
    fingerprint: str,
    expectations: dict[str, Any],
) -> str:
    """Return a fixed read-only Fusion script with exact fixture binding."""

    payload = json.dumps(
        {
            "marker": marker,
            "fingerprint": fingerprint,
            "expectations": expectations,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return f'''import json
import math
import os
import adsk.core
import adsk.fusion

PAYLOAD = json.loads({payload!r})
GROUP = "fusion_agent_benchmark"

def _items(collection):
    values = []
    if collection is None:
        return values
    for index in range(collection.count):
        value = collection.item(index)
        if value is not None:
            values.append(value)
    return values

def _matches(values, name):
    return [value for value in values if getattr(value, "name", None) == name]

def run(_context: str):
    app = adsk.core.Application.get()
    document = app.activeDocument
    design = adsk.fusion.Design.cast(app.activeProduct)
    checks = []

    def check(check_id, passed, expected=None, actual=None, failure_code=None):
        value = {{"id": check_id, "passed": bool(passed)}}
        if expected is not None:
            value["expected"] = expected
        if actual is not None:
            value["actual"] = actual
        if failure_code is not None:
            value["failure_code"] = failure_code
        checks.append(value)

    if design is None or document is None:
        check("active_fusion_design", False, expected=True, actual=False)
        print(json.dumps({{"ok": True, "schema_version": "{ORACLE_SCHEMA_VERSION}", "passed": False, "checks": checks}}, sort_keys=True, allow_nan=False))
        return

    root = design.rootComponent
    marker_attribute = root.attributes.itemByName(GROUP, "trial_marker")
    fingerprint_attribute = root.attributes.itemByName(GROUP, "fixture_fingerprint")
    marker_value = marker_attribute.value if marker_attribute is not None else None
    fingerprint_value = fingerprint_attribute.value if fingerprint_attribute is not None else None
    check("fixture_marker", marker_value == PAYLOAD["marker"], True, marker_value == PAYLOAD["marker"])
    check("fixture_fingerprint", fingerprint_value == PAYLOAD["fingerprint"], True, fingerprint_value == PAYLOAD["fingerprint"])
    check("fixture_unsaved", document.dataFile is None, True, document.dataFile is None)

    components = _items(design.allComponents)
    sketches = []
    bodies = []
    features = []
    for component in components:
        sketches.extend(_items(component.sketches))
        bodies.extend(_items(component.bRepBodies))
        features.extend(_items(component.features))

    occurrences = []
    def walk(collection):
        for occurrence in _items(collection):
            occurrences.append(occurrence)
            walk(occurrence.childOccurrences)
    walk(root.occurrences)

    joints = []
    joints.extend(_items(getattr(root, "joints", None)))
    joints.extend(_items(getattr(root, "asBuiltJoints", None)))
    rigid_groups = _items(getattr(root, "rigidGroups", None))
    expectations = PAYLOAD["expectations"]

    for label, values, key in [
        ("component", components, "components"),
        ("sketch", sketches, "sketches"),
        ("body", bodies, "bodies"),
        ("feature", features, "features"),
        ("joint", joints, "joints"),
        ("rigid_group", rigid_groups, "rigid_groups"),
    ]:
        for name in expectations.get(key) or []:
            matches = _matches(values, name)
            valid = len(matches) == 1 and bool(getattr(matches[0], "isValid", True))
            check(f"{{label}}:{{name}}", valid, {{"count": 1, "valid": True}}, {{"count": len(matches), "valid": valid}})

    for name, minima in (expectations.get("sketch_metrics") or {{}}).items():
        matches = _matches(sketches, name)
        if len(matches) != 1:
            check(f"sketch_metrics:{{name}}", False, minima, {{"match_count": len(matches)}})
            continue
        sketch = matches[0]
        actual = {{
            "constraints": int(sketch.geometricConstraints.count),
            "dimensions": int(sketch.sketchDimensions.count),
        }}
        passed = (
            actual["constraints"] >= int(minima.get("constraints_min", 0))
            and actual["dimensions"] >= int(minima.get("dimensions_min", 0))
        )
        check(f"sketch_metrics:{{name}}", passed, minima, actual)

    for name in expectations.get("positive_physical_bodies") or []:
        matches = _matches(bodies, name)
        actual = None
        failure_code = None
        try:
            if len(matches) != 1:
                raise RuntimeError(f"expected one body, found {{len(matches)}}")
            body = matches[0]
            volume = float(getattr(body, "volume", 0.0))
            mass = None
            try:
                mass = float(body.physicalProperties.mass)
            except Exception:
                mass = None
            finite = math.isfinite(volume) and (mass is None or math.isfinite(mass))
            actual = {{"volume_positive": volume > 0.0, "mass_positive": mass is None or mass > 0.0, "finite": finite}}
            passed = finite and volume > 0.0 and (mass is None or mass > 0.0)
        except Exception:
            passed = False
            failure_code = "PHYSICAL_EVIDENCE_UNAVAILABLE"
        check(f"physical_body:{{name}}", passed, {{"positive_finite": True}}, actual, failure_code)

    if "interference_max" in expectations:
        count = None
        failure_code = None
        try:
            collection = adsk.core.ObjectCollection.create()
            for body in bodies:
                collection.add(body)
            results = design.analyzeInterference(collection) if collection.count >= 2 else None
            count = int(results.count) if results is not None else 0
            passed = count <= int(expectations["interference_max"])
        except Exception:
            passed = False
            failure_code = "INTERFERENCE_EVIDENCE_UNAVAILABLE"
        check("interference", passed, {{"max": expectations["interference_max"]}}, {{"count": count}}, failure_code)

    for index, path in enumerate(expectations.get("files") or []):
        exists = os.path.isfile(path)
        size = os.path.getsize(path) if exists else 0
        check(f"file:{{index}}", exists and size > 0, {{"exists": True, "bytes_gt": 0}}, {{"exists": exists, "bytes": size}})

    for index, path in enumerate(expectations.get("absent_files") or []):
        exists = os.path.exists(path)
        check(f"absent_file:{{index}}", not exists, {{"exists": False}}, {{"exists": exists}})

    passed = bool(checks) and all(item["passed"] for item in checks)
    print(json.dumps({{
        "ok": True,
        "schema_version": "{ORACLE_SCHEMA_VERSION}",
        "passed": passed,
        "checks": checks,
        "check_count": len(checks),
        "requirement_ids": ["{REQUIREMENT_ID}"],
        "evidence_source": "independent_programmatic_readback",
        "supplemental_visual": False,
    }}, sort_keys=True, allow_nan=False))
'''


async def _run_independent_oracle(
    runtime: FusionAgentRuntime,
    case: CapabilityPackCase,
    context: TrialContext,
    session: FixtureSession,
) -> dict[str, Any]:
    operation_id = f"capability-pack:{context.trial_id}:independent-oracle"
    result = await runtime._call_trusted_native_real(
        "fusion_mcp_execute",
        {
            "featureType": "script",
            "object": {
                "script": build_independent_oracle_script(
                    marker=context.fixture_marker,
                    fingerprint=session.fixture_fingerprint,
                    expectations=case.oracle_expectations,
                )
            },
        },
        semantics="read_only",
        operation_id=operation_id,
    )
    payload = _decode_script_payload(result, operation_id=operation_id)
    if payload.get("schema_version") != ORACLE_SCHEMA_VERSION:
        raise RuntimeError("independent oracle returned an unsupported schema")
    return _project_oracle_payload(payload)


def _project_oracle_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate and shrink provider evidence before it reaches an artifact."""

    checks = payload.get("checks")
    if payload.get("ok") is not True or not isinstance(checks, list) or not checks:
        raise RuntimeError("independent oracle evidence is incomplete")
    projected_checks: list[dict[str, Any]] = []
    for index, item in enumerate(checks[:512]):
        if not isinstance(item, dict) or type(item.get("passed")) is not bool:
            raise RuntimeError("independent oracle check is malformed")
        check_id = str(item.get("id") or "")
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", check_id):
            check_id = f"check_{index:03d}"
        failure_code = item.get("failure_code")
        projected: dict[str, Any] = {"id": check_id, "passed": item["passed"]}
        if isinstance(failure_code, str) and re.fullmatch(
            r"[A-Z][A-Z0-9_]{2,79}", failure_code
        ):
            projected["failure_code"] = failure_code
        projected_checks.append(projected)
    passed = payload.get("passed")
    if type(passed) is not bool or passed != all(
        item["passed"] for item in projected_checks
    ):
        raise RuntimeError("independent oracle aggregate is inconsistent")
    return {
        "ok": True,
        "schema_version": ORACLE_SCHEMA_VERSION,
        "passed": passed,
        "checks": projected_checks,
        "check_count": len(projected_checks),
        "requirement_ids": [REQUIREMENT_ID],
        "evidence_source": "independent_programmatic_readback",
        "supplemental_visual": False,
    }


def _execution_contract(
    result: CapabilityExecutionResult, spec: CadSpecV2
) -> dict[str, Any]:
    failures: list[str] = []
    if result.provider != "autodesk_http":
        failures.append("provider_mismatch")
    if not result.success:
        failures.append("executor_reported_failure")
    if result.dry_run:
        failures.append("dry_run_is_not_real_evidence")
    if len(result.transactions) != len(spec.operations):
        failures.append("transaction_count_mismatch")
    transaction_ids = [
        str(item.get("operation_id") or "") for item in result.transactions
    ]
    expected_ids = [operation.id for operation in spec.operations]
    if transaction_ids != expected_ids:
        failures.append("transaction_order_or_identity_mismatch")
    if any(item.get("status") != "ok" for item in result.transactions):
        failures.append("non_ok_transaction")

    for operation in spec.operations:
        if operation.kind != "analysis.physical_properties":
            continue
        evidence = result.evidence.get(operation.id) or {}
        measured = evidence.get("physical_properties") or {}
        for target in operation.target_refs:
            value = measured.get(target) or {}
            if float(value.get("volume_mm3") or 0.0) <= 0.0:
                failures.append("physical_properties_not_positive")
    for operation in spec.operations:
        if operation.kind != "analysis.interference":
            continue
        evidence = result.evidence.get(operation.id) or {}
        interference = evidence.get("interference") or {}
        if "error" in interference or not isinstance(interference.get("count"), int):
            failures.append("interference_evidence_incomplete")

    return {
        "passed": not failures,
        "provider_verified": result.provider == "autodesk_http",
        "success": result.success,
        "dry_run": result.dry_run,
        "transaction_count": len(result.transactions),
        "expected_transaction_count": len(spec.operations),
        "required_capabilities": sorted(spec.capabilities),
        "available_required_capabilities": sorted(
            set(result.available_capabilities).intersection(spec.capabilities)
        ),
        "failures": failures,
    }


def _prepare_case_artifacts(case: CapabilityPackCase, artifact_root: Path) -> None:
    root = artifact_root.resolve()
    values = [
        *(case.oracle_expectations.get("files") or []),
        *(case.oracle_expectations.get("absent_files") or []),
    ]
    for value in values:
        path = Path(value).resolve()
        if not path.is_relative_to(root):
            raise RuntimeError(f"capability pack artifact escapes nightly root: {path}")
        mkdir(path.parent)
        if path_exists(path):
            if not path_is_file(path):
                raise RuntimeError(
                    f"capability pack output is not a regular file: {path}"
                )
            unlink(path)


def _spec_sha256(spec: CadSpecV2) -> str:
    return hashlib.sha256(
        json.dumps(
            spec.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _transport_call_count(runtime: Any) -> int | None:
    diagnostics = getattr(runtime, "diagnostics", None)
    if not callable(diagnostics):
        return None
    try:
        value = diagnostics().get("call_count")
    except BaseException:
        return None
    return value if type(value) is int and value >= 0 else None


async def _exercise_output_denial(
    runtime: FusionAgentRuntime,
    case: CapabilityPackCase,
) -> dict[str, Any]:
    before = _transport_call_count(runtime)
    denied = False
    error_code = None
    try:
        await runtime.execute_cad_spec_v2(case.spec, mode="real")
    except HostOutputDisabledError as exc:
        denied = True
        error_code = exc.error_code
    after = _transport_call_count(runtime)
    delta = after - before if before is not None and after is not None else None
    absent_files = case.oracle_expectations.get("absent_files") or []
    files_absent = bool(absent_files) and all(
        not path_exists(Path(value)) for value in absent_files
    )
    passed = bool(
        denied and error_code == "HOST_OUTPUT_DISABLED" and delta == 0 and files_absent
    )
    return {
        "passed": passed,
        "policy": REAL_HOST_OUTPUT_POLICY,
        "error_code": error_code,
        "transport_call_count_before": before,
        "transport_call_count_after": after,
        "transport_dispatch_delta": delta,
        "checked_absent_file_count": len(absent_files),
        "files_absent": files_absent,
        "output_executed": not denied,
    }


async def _run_case(
    runtime: FusionAgentRuntime,
    lifecycle: FusionRuntimeLifecycleBackend,
    case: CapabilityPackCase,
    run_id: str,
    artifact_root: Path,
) -> dict[str, Any]:
    context = _trial_context(case, run_id)
    baseline_open_ids = await lifecycle.list_open_document_ids()
    session: FixtureSession | None = None
    started = time.perf_counter()
    failure: BaseException | None = None
    result: dict[str, Any] = {
        "schema_version": CASE_SCHEMA_VERSION,
        "case_id": case.id,
        "group": case.group,
        "target_capabilities": list(case.target_capabilities),
        "spec_sha256": _spec_sha256(case.spec),
        "trial_identity_sha256": hashlib.sha256(
            f"{context.trial_id}|{context.fixture_marker}".encode("utf-8")
        ).hexdigest(),
        "status": "running",
        "execution_attempted": False,
        "executor_completed": False,
        "automatic_replay_suppressed": True,
    }
    try:
        _prepare_case_artifacts(case, artifact_root)
        session = await lifecycle.prepare_fixture(context)
        identity = await lifecycle.read_fixture_identity(context, session)
        identity_verified = _identity_matches(context, session, identity)
        identity_material = "|".join(
            (
                session.fixture_document_id,
                session.fixture_marker,
                session.fixture_fingerprint,
            )
        )
        result["fixture"] = {
            "identity_verified": identity_verified,
            "identity_sha256": hashlib.sha256(
                identity_material.encode("utf-8")
            ).hexdigest(),
            "document_bound": bool(session.fixture_document_id),
            "marker_bound": bool(session.fixture_marker),
            "fingerprint_bound": bool(session.fixture_fingerprint),
            "unsaved": session.unsaved,
        }
        if not identity_verified:
            raise RuntimeError("disposable fixture identity mismatch")

        result["execution_attempted"] = True
        if case.execution_expectation == "host_output_denied_zero_dispatch":
            result["output_denial"] = await _exercise_output_denial(runtime, case)
            result["mutation_outcome"] = "not_dispatched"
            result["oracle"] = await _run_independent_oracle(
                runtime,
                case,
                context,
                session,
            )
            result["status"] = (
                "passed"
                if result["output_denial"]["passed"]
                and result["oracle"].get("passed") is True
                else "failed"
            )
        else:
            try:
                execution = await runtime.execute_cad_spec_v2(case.spec, mode="real")
            except BaseException:
                # CapabilityExecutor may have failed before dispatch or after one
                # of several typed operations.  Without correlated per-operation
                # transport evidence, the conservative aggregate outcome is
                # unknown.  Never replay; read back, then destroy the disposable
                # fixture.
                result["mutation_outcome"] = "unknown"
                result["post_failure_readback_attempted"] = True
                try:
                    result["oracle"] = await _run_independent_oracle(
                        runtime,
                        case,
                        context,
                        session,
                    )
                    result["oracle_context"] = "post_executor_failure_readback"
                except BaseException:
                    result["oracle_error"] = _public_error(
                        "INDEPENDENT_ORACLE_FAILED",
                        correlation_material=f"{run_id}:{case.id}:post-failure-oracle",
                    )
                raise
            result["executor_completed"] = True
            execution_contract = _execution_contract(execution, case.spec)
            result["execution_contract"] = execution_contract
            result["oracle"] = await _run_independent_oracle(
                runtime,
                case,
                context,
                session,
            )
            result["mutation_outcome"] = (
                "observed_in_independent_readback"
                if result["oracle"].get("passed") is True
                else "executor_completed_but_readback_failed"
            )
            result["status"] = (
                "passed"
                if execution_contract["passed"]
                and result["oracle"].get("passed") is True
                else "failed"
            )
        if result["status"] != "passed":
            raise RuntimeError(
                "capability pack did not satisfy executor and independent-oracle contracts"
            )
    except BaseException as exc:
        failure = exc
        result["status"] = "failed"
        result["error"] = _public_error(
            "CAPABILITY_PACK_CASE_FAILED",
            correlation_material=f"{run_id}:{case.id}:case",
        )
    finally:
        if session is not None:
            try:
                result["cleanup"] = await _safe_cleanup(
                    lifecycle,
                    context,
                    session,
                    baseline_open_ids,
                )
                result["disposable_fixture_destroyed"] = True
            except BaseException as cleanup_error:
                result["cleanup"] = {
                    "passed": False,
                    "error": _public_error(
                        "FIXTURE_CLEANUP_FAILED",
                        correlation_material=f"{run_id}:{case.id}:cleanup",
                    ),
                }
                result["status"] = "failed"
                result["cleanup_error"] = result["cleanup"]["error"]
                result["disposable_fixture_destroyed"] = False
                failure = cleanup_error
        result["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
    if failure is not None:
        return result
    return result


async def _restoration_evidence(
    lifecycle: FusionRuntimeLifecycleBackend,
    original_document_id: str | None,
    original_open_ids: list[str],
) -> dict[str, Any]:
    try:
        active_id = await lifecycle.read_active_document_id()
        open_ids = await lifecycle.list_open_document_ids()
        passed = active_id == original_document_id and sorted(open_ids) == sorted(
            original_open_ids
        )
        return {
            "passed": passed,
            "identity_restored": active_id == original_document_id,
            "inventory_restored": sorted(open_ids) == sorted(original_open_ids),
        }
    except BaseException:
        return {
            "passed": False,
            "identity_restored": False,
            "inventory_restored": False,
            "error": _public_error(
                "RESTORATION_READBACK_FAILED",
                correlation_material="capability-packs:restoration",
            ),
        }


def _group_summaries(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for case in cases:
        groups.setdefault(str(case["group"]), []).append(case)
    return [
        {
            "group": group,
            "status": "passed"
            if all(item.get("status") == "passed" for item in items)
            else "failed",
            "case_ids": [str(item["case_id"]) for item in items],
            "target_capabilities": sorted(
                {
                    capability
                    for item in items
                    for capability in item.get("target_capabilities") or []
                }
            ),
        }
        for group, items in sorted(groups.items())
    ]


def _write_result(path: Path, value: dict[str, Any]) -> None:
    atomic_write_text(
        path,
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n",
    )


def _public_error(code: str, *, correlation_material: str) -> dict[str, Any]:
    return {
        "code": code,
        "generic_message": "The capability-pack operation failed. Inspect private local diagnostics.",
        "correlation_id": hashlib.sha256(
            correlation_material.encode("utf-8")
        ).hexdigest()[:16],
        "retryable": False,
    }


def _public_environment(environment: dict[str, str]) -> dict[str, Any]:
    projected: dict[str, Any] = {}
    for key, value in sorted(environment.items()):
        safe_key = str(key)
        safe_value = str(value)
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,79}", safe_key):
            continue
        projected[safe_key] = (
            safe_value
            if re.fullmatch(r"[A-Za-z0-9_.:+-]{1,160}", safe_value)
            else {"redacted": True}
        )
    return projected


async def run_capability_pack_suite(
    *,
    runtime: FusionAgentRuntime,
    lifecycle: FusionRuntimeLifecycleBackend,
    cases: tuple[CapabilityPackCase, ...],
    artifact_root: Path | str,
    environment: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run every selected case, persisting honest state after each fixture."""

    root = Path(artifact_root).resolve()
    output_path = root / "capability-packs.json"
    run_id = "packs_" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    suite: dict[str, Any] = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "run_id": run_id,
        "status": "running",
        "evidence_mode": "autodesk_real",
        "fixture_policy": "disposable_unsaved_only",
        "save_user_documents": False,
        "screenshot_role": "supplemental_visual_only",
        "environment": _public_environment(dict(environment or {})),
        "requested_case_ids": [case.id for case in cases],
        "target_capabilities": sorted(
            {capability for case in cases for capability in case.target_capabilities}
        ),
        "cases": [],
    }
    _write_result(output_path, suite)

    try:
        original_document_id = await lifecycle.read_active_document_id()
        original_open_ids = await lifecycle.list_open_document_ids()
    except BaseException:
        suite["status"] = "failed"
        suite["error"] = _public_error(
            "RESTORATION_BASELINE_FAILED",
            correlation_material=f"{run_id}:baseline",
        )
        suite["restoration"] = {"passed": False, "reason": "baseline_not_captured"}
        _write_result(output_path, suite)
        return suite

    unsafe_to_continue = False
    for index, case in enumerate(cases):
        if unsafe_to_continue:
            suite["cases"].append(
                {
                    "schema_version": CASE_SCHEMA_VERSION,
                    "case_id": case.id,
                    "group": case.group,
                    "target_capabilities": list(case.target_capabilities),
                    "status": "not_run",
                    "reason": "prior_fixture_restoration_not_proven",
                }
            )
            continue
        case_result = await _run_case(runtime, lifecycle, case, run_id, root)
        suite["cases"].append(case_result)
        restoration = await _restoration_evidence(
            lifecycle,
            original_document_id,
            original_open_ids,
        )
        case_result["suite_restoration_after_case"] = restoration
        if not restoration["passed"] or not (case_result.get("cleanup") or {}).get(
            "passed"
        ):
            unsafe_to_continue = True
            suite["unsafe_after_case_index"] = index
        suite["groups"] = _group_summaries(suite["cases"])
        _write_result(output_path, suite)

    suite["restoration"] = await _restoration_evidence(
        lifecycle,
        original_document_id,
        original_open_ids,
    )
    completed = len(suite["cases"]) == len(cases)
    passed = bool(
        completed
        and cases
        and all(case.get("status") == "passed" for case in suite["cases"])
        and suite["restoration"].get("passed") is True
    )
    suite["status"] = "passed" if passed else "failed"
    suite["groups"] = _group_summaries(suite["cases"])
    suite["completed_at_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _write_result(output_path, suite)
    return suite


async def run_real_capability_packs(
    artifact_root: Path | str,
    *,
    configuration: RuntimeConfiguration,
) -> dict[str, Any]:
    """Validated CLI entry point used by the Windows real-Fusion workflow."""

    environment = validate_real_runner_environment(configuration)
    root = Path(artifact_root).resolve()
    cases = build_capability_pack_cases(root)
    runtime = FusionAgentRuntime(
        manifest_root=root / "manifests",
        outputs_root=root / "outputs",
        configuration=configuration,
    )
    lifecycle = FusionRuntimeLifecycleBackend(runtime)
    try:
        return await run_capability_pack_suite(
            runtime=runtime,
            lifecycle=lifecycle,
            cases=cases,
            artifact_root=root,
            environment=environment,
        )
    finally:
        await runtime.close(timeout_seconds=5.0)


__all__ = [
    "ASSERTION_ID",
    "CASE_SCHEMA_VERSION",
    "CapabilityPackCase",
    "NIGHTLY_PACK_CAPABILITIES",
    "ORACLE_SCHEMA_VERSION",
    "REQUIREMENT_ID",
    "RESULT_SCHEMA_VERSION",
    "build_capability_pack_cases",
    "build_independent_oracle_script",
    "run_capability_pack_suite",
    "run_real_capability_packs",
    "validate_real_runner_environment",
]
