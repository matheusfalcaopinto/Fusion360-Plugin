"""Code-owned canonical fixtures and declarative script profiles."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from benchmark.models import ExecutionPath


@dataclass(frozen=True, slots=True)
class FixtureDefinition:
    id: str
    state: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RouteProfile:
    """Deterministic mock behavior for one code-reviewed canonical action."""

    status: str
    execution_success: bool
    duration_ms: float
    call_count: int
    script_count: int = 0
    initialize_count: int = 1
    reconnect_count: int = 0
    retry_count: int = 0
    mutation_dispatch_count: int = 0
    blocked_destructive: bool = False
    outcome_unknown: bool = False
    observation: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ScriptDefinition:
    id: str
    profiles: dict[ExecutionPath, RouteProfile]


FIXTURE_REGISTRY: dict[str, FixtureDefinition] = {
    "active_document": FixtureDefinition(
        id="active_document",
        state={
            "document": "benchmark_fixture",
            "saved": False,
            "marker": "fusion_agent_benchmark",
        },
    ),
    "empty_design": FixtureDefinition(
        id="empty_design",
        state={
            "document": "benchmark_empty",
            "bodies": [],
            "features": [],
            "saved": False,
        },
    ),
    "sample_design_medium": FixtureDefinition(
        id="sample_design_medium",
        state={
            "document": "benchmark_medium",
            "body_count": 8,
            "occurrence_count": 16,
            "saved": False,
        },
    ),
    "sample_design_large": FixtureDefinition(
        id="sample_design_large",
        state={
            "document": "benchmark_large",
            "body_count": 240,
            "occurrence_count": 480,
            "saved": False,
        },
    ),
    "parameterized_design": FixtureDefinition(
        id="parameterized_design",
        state={
            "document": "benchmark_parameter",
            "parameters": {"width": "20 mm"},
            "saved": False,
        },
    ),
}


def _profiles(
    *,
    status: str,
    success: bool,
    safe_ms: float,
    fast_ms: float,
    safe_calls: int,
    fast_calls: int,
    observation: dict[str, Any],
    scripts: tuple[int, int] = (0, 0),
    reconnects: tuple[int, int] = (0, 0),
    retries: tuple[int, int] = (0, 0),
    mutation_dispatches: tuple[int, int] = (0, 0),
    blocked_destructive: bool = False,
    outcome_unknown: bool = False,
    route_observations: tuple[dict[str, Any], dict[str, Any]] | None = None,
) -> dict[ExecutionPath, RouteProfile]:
    return {
        "safe_harness": RouteProfile(
            status=status,
            execution_success=success,
            duration_ms=safe_ms,
            call_count=safe_calls,
            script_count=scripts[0],
            reconnect_count=reconnects[0],
            retry_count=retries[0],
            mutation_dispatch_count=mutation_dispatches[0],
            blocked_destructive=blocked_destructive,
            outcome_unknown=outcome_unknown,
            observation=route_observations[0] if route_observations else observation,
        ),
        "native_fast": RouteProfile(
            status=status,
            execution_success=success,
            duration_ms=fast_ms,
            call_count=fast_calls,
            script_count=scripts[1],
            reconnect_count=reconnects[1],
            retry_count=retries[1],
            mutation_dispatch_count=mutation_dispatches[1],
            blocked_destructive=blocked_destructive,
            outcome_unknown=outcome_unknown,
            observation=route_observations[1] if route_observations else observation,
        ),
    }


SCRIPT_REGISTRY: dict[str, ScriptDefinition] = {
    "persistent_cold_first_read": ScriptDefinition(
        "persistent_cold_first_read",
        _profiles(
            status="read_succeeded",
            success=True,
            safe_ms=1250,
            fast_ms=480,
            safe_calls=3,
            fast_calls=1,
            observation={},
            route_observations=(
                {
                    "transport": {
                        "initialize_count": 1,
                        "tools_list_count": 1,
                        "cold_first_read_ms": 1250,
                        "reconnect_count": 0,
                    }
                },
                {
                    "transport": {
                        "initialize_count": 1,
                        "tools_list_count": 1,
                        "cold_first_read_ms": 480,
                        "reconnect_count": 0,
                    }
                },
            ),
        ),
    ),
    "read_api_documentation": ScriptDefinition(
        "read_api_documentation",
        _profiles(
            status="read_succeeded",
            success=True,
            safe_ms=110,
            fast_ms=35,
            safe_calls=4,
            fast_calls=1,
            observation={"api_documentation": {"matches": 1, "class": "Application"}},
        ),
    ),
    "read_document_summary": ScriptDefinition(
        "read_document_summary",
        _profiles(
            status="read_succeeded",
            success=True,
            safe_ms=140,
            fast_ms=42,
            safe_calls=5,
            fast_calls=1,
            observation={"document": {"name": "benchmark_fixture", "body_count": 8}},
        ),
    ),
    "inspect_medium": ScriptDefinition(
        "inspect_medium",
        _profiles(
            status="read_succeeded",
            success=True,
            safe_ms=320,
            fast_ms=85,
            safe_calls=7,
            fast_calls=1,
            observation={
                "inspection": {"matched": 8, "ambiguous": False, "truncated": False}
            },
        ),
    ),
    "inspect_large": ScriptDefinition(
        "inspect_large",
        _profiles(
            status="read_succeeded",
            success=True,
            safe_ms=900,
            fast_ms=260,
            safe_calls=12,
            fast_calls=2,
            observation={
                "inspection": {"matched": 100, "ambiguous": False, "truncated": True}
            },
        ),
    ),
    "inspect_large_bounded": ScriptDefinition(
        "inspect_large_bounded",
        _profiles(
            status="read_succeeded",
            success=True,
            safe_ms=1450,
            fast_ms=620,
            safe_calls=4,
            fast_calls=1,
            observation={},
            route_observations=tuple(
                {
                    "inspection": {
                        "complete": False,
                        "truncated": True,
                        "visited_entities": 1000,
                        "elapsed_ms": elapsed_ms,
                        "response_bytes": 800000,
                        "stop_reason": "entity_budget",
                        "physical_properties_access_count": 0,
                    }
                }
                for elapsed_ms in (1450, 620)
            ),
        ),
    ),
    "inspect_large_by_token": ScriptDefinition(
        "inspect_large_by_token",
        _profiles(
            status="read_succeeded",
            success=True,
            safe_ms=650,
            fast_ms=110,
            safe_calls=4,
            fast_calls=1,
            observation={},
            route_observations=tuple(
                {
                    "inspection": {
                        "complete": True,
                        "truncated": False,
                        "matched": 1,
                        "ambiguous": False,
                        "visited_entities": 1,
                        "lookup_strategy": "entity_token",
                        "global_scan_count": 0,
                        "elapsed_ms": elapsed_ms,
                    }
                }
                for elapsed_ms in (650, 110)
            ),
        ),
    ),
    "capture_screenshot": ScriptDefinition(
        "capture_screenshot",
        _profiles(
            status="read_succeeded",
            success=True,
            safe_ms=250,
            fast_ms=100,
            safe_calls=5,
            fast_calls=1,
            observation={
                "screenshot": {
                    "mime_type": "image/png",
                    "verified": True,
                    "bytes": 4096,
                }
            },
        ),
    ),
    "create_cube": ScriptDefinition(
        "create_cube",
        _profiles(
            status="applied_verified",
            success=True,
            safe_ms=700,
            fast_ms=300,
            safe_calls=10,
            fast_calls=3,
            scripts=(4, 1),
            mutation_dispatches=(4, 1),
            observation={
                "feature": {
                    "name": "benchmark_cube",
                    "health": "ok",
                    "bbox_mm": [10, 10, 10],
                }
            },
        ),
    ),
    "create_plate": ScriptDefinition(
        "create_plate",
        _profiles(
            status="applied_verified",
            success=True,
            safe_ms=1200,
            fast_ms=520,
            safe_calls=16,
            fast_calls=4,
            scripts=(7, 1),
            mutation_dispatches=(7, 1),
            observation={
                "feature": {"name": "benchmark_plate", "health": "ok"},
                "hole_count": 4,
            },
        ),
    ),
    "update_parameter": ScriptDefinition(
        "update_parameter",
        _profiles(
            status="applied_verified",
            success=True,
            safe_ms=500,
            fast_ms=230,
            safe_calls=8,
            fast_calls=3,
            scripts=(2, 1),
            mutation_dispatches=(2, 1),
            observation={"parameters": {"width": "25 mm"}, "feature_health": "ok"},
        ),
    ),
    "block_destructive": ScriptDefinition(
        "block_destructive",
        _profiles(
            status="blocked_before_apply",
            success=True,
            safe_ms=80,
            fast_ms=45,
            safe_calls=2,
            fast_calls=1,
            observation={
                "blocked": True,
                "blocked_reason": "destructive_requires_safe_harness_preview",
                "mutation_dispatch_count": 0,
                "save_count": 0,
            },
            blocked_destructive=True,
        ),
    ),
    "simulate_mutation_timeout": ScriptDefinition(
        "simulate_mutation_timeout",
        _profiles(
            status="outcome_unknown",
            success=False,
            safe_ms=250,
            fast_ms=210,
            safe_calls=3,
            fast_calls=2,
            scripts=(1, 1),
            mutation_dispatches=(1, 1),
            outcome_unknown=True,
            observation={
                "error_code": "MUTATION_OUTCOME_UNKNOWN",
                "replayed": False,
                "mutation_dispatch_count": 1,
                "duplicate_count": 0,
            },
        ),
    ),
    "simulate_manifest_drift": ScriptDefinition(
        "simulate_manifest_drift",
        _profiles(
            status="manifest_drift",
            success=False,
            safe_ms=170,
            fast_ms=125,
            safe_calls=3,
            fast_calls=2,
            reconnects=(1, 1),
            observation={
                "error_code": "MANIFEST_DRIFT",
                "blocked_before_retry": True,
                "reconnect_count": 1,
            },
        ),
    ),
}


# Public B02-B07 comparison fixtures are code-owned mock contracts.  They do
# not load or execute the reference suite's Python build scripts.  Real runs
# use the runtime bridge and must advertise reviewed case-specific
# capabilities before the first fixture or mutation is dispatched.
FIXTURE_REGISTRY["public_fusion_disposable"] = FixtureDefinition(
    id="public_fusion_disposable",
    state={
        "document": "public_fusion_disposable",
        "saved": False,
        "isolated": True,
        "execution_profile": "normal_equivalent",
        "arbitrary_code_allowed": False,
    },
)

_PUBLIC_NORMAL_CASE_PROFILES = {
    "b02_vented_enclosure": (780.0, 8, 1),
    "b03_split_pillow_block": (920.0, 10, 1),
    "b04_offset_duct_adapter": (1_080.0, 11, 1),
    "b05_spherical_lattice_radome": (1_360.0, 14, 2),
    "b06_robot_arm_assembly": (1_520.0, 16, 2),
    "b07_packaging_machine": (1_740.0, 18, 2),
}

for _case_id, (
    _duration_ms,
    _call_count,
    _dispatch_count,
) in _PUBLIC_NORMAL_CASE_PROFILES.items():
    _internal_case_id = f"pub_{_case_id.split('_', 1)[0]}"
    SCRIPT_REGISTRY[_internal_case_id] = ScriptDefinition(
        _internal_case_id,
        _profiles(
            status="applied_verified",
            success=True,
            safe_ms=_duration_ms,
            fast_ms=_duration_ms,
            safe_calls=_call_count,
            fast_calls=_call_count,
            mutation_dispatches=(_dispatch_count, _dispatch_count),
            observation={
                "public_contract": {
                    "internal_case_id": _internal_case_id,
                    "public_case_id": _case_id,
                    "task_kind": "normal",
                    "expected_outcome": "applied_verified",
                    "expected_dispatch_count": _dispatch_count,
                    "safety_contract_passed": True,
                    "contract_coverage": 1.0,
                    "geometry_valid": True,
                    "constraint_health": "healthy",
                    "backend_id": "fusion_agent_internal_mock",
                    "backend_version": "public_registry.v1",
                    "replay_count": 0,
                    "recovery_status": "not_needed",
                }
            },
        ),
    )


_PUBLIC_FAULT_PROFILES = {
    "pub_b02_f01": {
        "case_id": "b02_vented_enclosure",
        "fault_id": "timeout_before_dispatch",
        "outcome": "blocked_before_dispatch",
        "dispatches": 0,
        "geometry_valid": True,
        "constraint_health": "preserved",
        "recovery_status": "not_needed",
    },
    "pub_b02_f02": {
        "case_id": "b02_vented_enclosure",
        "fault_id": "timeout_after_dispatch",
        "outcome": "outcome_unknown_no_replay",
        "dispatches": 1,
        "geometry_valid": None,
        "constraint_health": "unknown",
        "recovery_status": "readback_required",
    },
    "pub_b02_f03": {
        "case_id": "b02_vented_enclosure",
        "fault_id": "transport_disconnect",
        "outcome": "recover_by_readback",
        "dispatches": 1,
        "geometry_valid": True,
        "constraint_health": "healthy",
        "recovery_status": "readback_confirmed",
    },
    "pub_b05_f04": {
        "case_id": "b05_spherical_lattice_radome",
        "fault_id": "wrong_document",
        "outcome": "zero_dispatch",
        "dispatches": 0,
        "geometry_valid": True,
        "constraint_health": "preserved",
        "recovery_status": "blocked_before_dispatch",
    },
    "pub_b05_f05": {
        "case_id": "b05_spherical_lattice_radome",
        "fault_id": "ambiguous_target",
        "outcome": "zero_dispatch",
        "dispatches": 0,
        "geometry_valid": True,
        "constraint_health": "preserved",
        "recovery_status": "blocked_before_dispatch",
    },
    "pub_b05_f06": {
        "case_id": "b05_spherical_lattice_radome",
        "fault_id": "state_drift",
        "outcome": "zero_dispatch",
        "dispatches": 0,
        "geometry_valid": True,
        "constraint_health": "preserved",
        "recovery_status": "stale_preview",
    },
    "pub_b06_f07": {
        "case_id": "b06_robot_arm_assembly",
        "fault_id": "incomplete_snapshot",
        "outcome": "zero_dispatch",
        "dispatches": 0,
        "geometry_valid": True,
        "constraint_health": "preserved",
        "recovery_status": "inspection_incomplete",
    },
    "pub_b07_f08": {
        "case_id": "b07_packaging_machine",
        "fault_id": "double_apply",
        "outcome": "at_most_one_dispatch",
        "dispatches": 1,
        "geometry_valid": True,
        "constraint_health": "healthy",
        "recovery_status": "single_consumer_won",
    },
}

for _internal_case_id, _fault in _PUBLIC_FAULT_PROFILES.items():
    _dispatch_count = int(_fault["dispatches"])
    SCRIPT_REGISTRY[_internal_case_id] = ScriptDefinition(
        _internal_case_id,
        _profiles(
            status=str(_fault["outcome"]),
            success=True,
            safe_ms=240.0,
            fast_ms=240.0,
            safe_calls=3,
            fast_calls=3,
            mutation_dispatches=(_dispatch_count, _dispatch_count),
            outcome_unknown=_fault["outcome"] == "outcome_unknown_no_replay",
            observation={
                "public_contract": {
                    "internal_case_id": _internal_case_id,
                    "public_case_id": _fault["case_id"],
                    "task_kind": "fault",
                    "fault_id": _fault["fault_id"],
                    "expected_outcome": _fault["outcome"],
                    "expected_dispatch_count": _dispatch_count,
                    "safety_contract_passed": True,
                    "contract_coverage": 1.0,
                    "geometry_valid": _fault["geometry_valid"],
                    "constraint_health": _fault["constraint_health"],
                    "backend_id": "fusion_agent_internal_mock",
                    "backend_version": "public_registry.v1",
                    "replay_count": 0,
                    "recovery_status": _fault["recovery_status"],
                }
            },
        ),
    )


# Compatibility name only; it is not a fallback suite and is never loaded when
# a v2 JSON suite is missing or invalid.
V0_PARAMETRIC_PARTS: list[Any] = []
