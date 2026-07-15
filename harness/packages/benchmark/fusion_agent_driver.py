"""Concrete trusted driver for Fusion Agent's public B02-B07 subject.

Mock trials run only code-owned fixture/action/oracle registry entries through
the canonical internal BenchmarkRunner.  Real trials use the
FusionRuntimeBenchmarkBridge and therefore fail before fixture creation unless
the runtime supplies every reviewed case-specific capability.  No manifest
field is interpreted as code or a command.
"""

from __future__ import annotations

import inspect
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from benchmark.models import BenchmarkCase, BenchmarkRunConfig, BenchmarkSuite
from benchmark.public import (
    AdapterExecution,
    AdapterPreflight,
    NormalizedPublicMetrics,
    PublicBenchmarkConfig,
    PublicBenchmarkTask,
)
from benchmark.registry import validate_case_registry
from benchmark.runner import BenchmarkExecutionError, BenchmarkRunner


@dataclass(frozen=True, slots=True)
class _TaskDefinition:
    task_id: str
    public_case_id: str
    fixture_path: str
    risk: str
    internal_case_id: str
    expected_status: str
    mutation_dispatch_count: int
    fault_id: str | None = None

    def benchmark_case(self) -> BenchmarkCase:
        return BenchmarkCase.model_validate(
            {
                "id": self.internal_case_id,
                "prompt": (
                    f"Run the code-owned normal-profile public contract for {self.task_id}; "
                    "do not execute manifest-supplied code."
                ),
                "category": "public_parametric_fault" if self.fault_id else "public_parametric_geometry",
                "risk": self.risk,
                "timeout_seconds": 900.0,
                "fixture_id": "public_fusion_disposable",
                "script_id": self.internal_case_id,
                "oracle_id": "public_fusion_contract",
                "execution_paths": ["safe_harness"],
                "expectations": {
                    "expected_status": self.expected_status,
                    "should_succeed": True,
                    "max_call_count": 24,
                    "mutation_dispatch_count": self.mutation_dispatch_count,
                },
            }
        )

    def matches(self, task: PublicBenchmarkTask) -> bool:
        return bool(
            task.task_id == self.task_id
            and task.case_id == self.public_case_id
            and task.fixture_path == self.fixture_path
            and task.risk == self.risk
            and task.fault_id == self.fault_id
            and task.expected_outcome == (self.expected_status if self.fault_id else None)
        )


def _normal(case_id: str, risk: str, dispatches: int) -> _TaskDefinition:
    return _TaskDefinition(
        task_id=f"{case_id}:normal",
        public_case_id=case_id,
        fixture_path=f"benchmark_parametric_suite/cases/{case_id}",
        risk=risk,
        internal_case_id=f"pub_{case_id.split('_', 1)[0]}",
        expected_status="applied_verified",
        mutation_dispatch_count=dispatches,
    )


def _fault(
    case_id: str,
    risk: str,
    fault_id: str,
    expected_status: str,
    dispatches: int,
    internal_case_id: str,
) -> _TaskDefinition:
    return _TaskDefinition(
        task_id=f"{case_id}:fault:{fault_id}",
        public_case_id=case_id,
        fixture_path=f"benchmark_parametric_suite/cases/{case_id}",
        risk=risk,
        internal_case_id=internal_case_id,
        expected_status=expected_status,
        mutation_dispatch_count=dispatches,
        fault_id=fault_id,
    )


_TASK_DEFINITIONS = (
    _normal("b02_vented_enclosure", "additive", 1),
    _normal("b03_split_pillow_block", "additive", 1),
    _normal("b04_offset_duct_adapter", "additive", 1),
    _normal("b05_spherical_lattice_radome", "scoped_update", 2),
    _normal("b06_robot_arm_assembly", "scoped_update", 2),
    _normal("b07_packaging_machine", "scoped_update", 2),
    _fault("b02_vented_enclosure", "additive", "timeout_before_dispatch", "blocked_before_dispatch", 0, "pub_b02_f01"),
    _fault("b02_vented_enclosure", "additive", "timeout_after_dispatch", "outcome_unknown_no_replay", 1, "pub_b02_f02"),
    _fault("b02_vented_enclosure", "additive", "transport_disconnect", "recover_by_readback", 1, "pub_b02_f03"),
    _fault("b05_spherical_lattice_radome", "scoped_update", "wrong_document", "zero_dispatch", 0, "pub_b05_f04"),
    _fault("b05_spherical_lattice_radome", "scoped_update", "ambiguous_target", "zero_dispatch", 0, "pub_b05_f05"),
    _fault("b05_spherical_lattice_radome", "scoped_update", "state_drift", "zero_dispatch", 0, "pub_b05_f06"),
    _fault("b06_robot_arm_assembly", "scoped_update", "incomplete_snapshot", "zero_dispatch", 0, "pub_b06_f07"),
    _fault("b07_packaging_machine", "scoped_update", "double_apply", "at_most_one_dispatch", 1, "pub_b07_f08"),
)
_TASKS_BY_ID = {definition.task_id: definition for definition in _TASK_DEFINITIONS}


class FusionAgentCodexPublicDriver:
    """Trusted internal driver for our own public benchmark subject only."""

    def __init__(
        self,
        *,
        output_dir: Path | str,
        manifest_dir: Path | str = "manifests",
        runtime_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.manifest_dir = Path(manifest_dir)
        self.runtime_factory = runtime_factory or self._default_runtime

    async def preflight(self, context, config: PublicBenchmarkConfig) -> AdapterPreflight:  # noqa: ANN001
        context_error = _context_error(context)
        if context_error:
            return AdapterPreflight(ready=False, reason=context_error)
        try:
            cases = [definition.benchmark_case() for definition in _TASK_DEFINITIONS]
            for case in cases:
                validate_case_registry(case)
        except Exception as exc:  # code/registry packaging error; never dispatch
            return AdapterPreflight(
                ready=False,
                reason=_bounded_reason(f"internal_public_registry_invalid:{type(exc).__name__}:{exc}"),
            )

        if config.mode == "mock":
            return AdapterPreflight(
                ready=True,
                observed_revision=_observed_revision(),
                environment={
                    "backend_id": "fusion_agent_internal_mock",
                    "backend_version": "public_registry.v1",
                    "execution_profile": "normal_equivalent",
                    "fixture_isolation": "code_owned_mock",
                },
            )

        runtime = self.runtime_factory()
        try:
            bridge = _runtime_bridge(runtime)
            await bridge.preflight(["safe_harness"], cases)
        except Exception as exc:
            return AdapterPreflight(
                ready=False,
                observed_revision=_observed_revision(),
                environment={
                    "backend_id": "fusion_agent_autodesk_runtime",
                    "execution_profile": "normal_equivalent",
                    "fixture_isolation": "preflight_only_no_dispatch",
                },
                reason=_bounded_reason(
                    "real_public_capabilities_unavailable:"
                    "no real benchmark action was dispatched:"
                    f"{type(exc).__name__}:{exc}"
                ),
            )
        finally:
            await _close_runtime(runtime)
        return AdapterPreflight(
            ready=True,
            observed_revision=_observed_revision(),
            environment={
                "backend_id": "fusion_agent_autodesk_runtime",
                "execution_profile": "normal_equivalent",
                "fixture_isolation": "runtime_bridge_verified",
            },
        )

    async def execute(
        self,
        context,
        task: PublicBenchmarkTask,
        config: PublicBenchmarkConfig,
    ) -> AdapterExecution:  # noqa: ANN001
        context_error = _context_error(context)
        if context_error:
            return AdapterExecution(state="not_run", reason=context_error)
        definition = _TASKS_BY_ID.get(task.task_id)
        if definition is None or not definition.matches(task):
            return AdapterExecution(state="not_run", reason="task_not_in_code_owned_public_registry")
        if config.mode == "mock":
            return await self._run_definition(definition, config, bridge=None)

        runtime = self.runtime_factory()
        try:
            bridge = _runtime_bridge(runtime)
            # Recheck immediately before BenchmarkRunner can prepare a fixture.
            await bridge.preflight(["safe_harness"], [definition.benchmark_case()])
            return await self._run_definition(definition, config, bridge=bridge)
        except BenchmarkExecutionError as exc:
            if "REAL_BENCHMARK_CAPABILITY_MISSING" in str(exc):
                return AdapterExecution(
                    state="not_run",
                    reason=_bounded_reason(
                        "real_public_capabilities_unavailable:"
                        f"no real benchmark action was dispatched:{exc}"
                    ),
                )
            raise
        finally:
            await _close_runtime(runtime)

    async def _run_definition(
        self,
        definition: _TaskDefinition,
        config: PublicBenchmarkConfig,
        *,
        bridge: Any | None,
    ) -> AdapterExecution:
        suite = BenchmarkSuite(
            schema_version="benchmark_suite.v2",
            suite_id=f"fusion_public_{definition.internal_case_id}",
            title=f"Fusion Agent public contract {definition.task_id}",
            description="One code-owned, normal-profile-equivalent public comparison task.",
            cases=[definition.benchmark_case()],
        )
        internal_output = self.output_dir / "fusion_agent_internal"
        runner = BenchmarkRunner(
            output_dir=internal_output,
            manifest_dir=self.manifest_dir,
            route_executors=bridge.route_executors if bridge is not None else None,
            oracle_observer=bridge if bridge is not None else None,
            real_lifecycle=bridge,
            environment_metadata={
                "public_subject": "fusion_agent_codex",
                "execution_profile": "normal_equivalent",
                "arbitrary_code_allowed": False,
            },
        )
        with tempfile.TemporaryDirectory(prefix="fusion-agent-public-suite-") as temporary:
            suite_path = Path(temporary) / "benchmark_suite_v2.json"
            suite_path.write_text(suite.model_dump_json(indent=2), encoding="utf-8")
            run = await runner.run_suite(
                suite_path,
                config=BenchmarkRunConfig(
                    mode=config.mode,
                    driver="internal",
                    execution_paths=["safe_harness"],
                    repetitions=1,
                    warmups=0,
                    seed=42,
                    confirm_real_benchmark=config.mode == "real",
                    project="fusion_agent_public_benchmark",
                ),
            )

        trials = [trial for trial in run.report.trials if not trial.warmup]
        if len(trials) != 1:
            raise BenchmarkExecutionError(
                f"public internal runner returned {len(trials)} trials; expected exactly one"
            )
        trial = trials[0]
        oracle_metrics = trial.oracle.metrics
        task_success = bool(trial.final_success and trial.metrics.get("expectations_met") is True)
        metrics = NormalizedPublicMetrics(
            task_success=task_success,
            oracle_passed=trial.oracle.passed,
            contract_coverage=_optional_float(oracle_metrics.get("contract_coverage")),
            geometry_valid=_optional_bool(oracle_metrics.get("geometry_valid")),
            constraint_health=_optional_string(oracle_metrics.get("constraint_health")),
            backend_id=_optional_string(oracle_metrics.get("backend_id")),
            backend_version=_optional_string(oracle_metrics.get("backend_version")),
            latency_ms=float(trial.metrics["duration_ms"]),
            tool_calls=int(trial.metrics["call_count"]),
            mutation_dispatch_count=int(trial.metrics["mutation_dispatch_count"]),
            replay_count=int(oracle_metrics.get("replay_count") or 0),
            recovery_status=_optional_string(oracle_metrics.get("recovery_status")),
            payload_bytes=int(trial.metrics["bytes_transferred"]),
            install_status="workspace_pinned_internal",
        )
        return AdapterExecution(
            state="completed",
            metrics=metrics,
            evidence={
                "internal_run_id": run.report.run_id,
                "internal_trial_id": trial.trial_id,
                "internal_status": trial.status,
                "internal_report_path": str(run.report_path),
                "execution_path": trial.execution_path,
                "execution_profile": "normal_equivalent",
                "arbitrary_code_allowed": False,
                "geometry_valid": metrics.geometry_valid,
                "backend_id": metrics.backend_id,
                "expected_outcome": oracle_metrics.get("expected_outcome"),
            },
        )

    def _default_runtime(self) -> Any:
        from fusion_agent_mcp.runtime import FusionAgentRuntime

        return FusionAgentRuntime(
            manifest_root=self.manifest_dir,
            outputs_root=self.output_dir / "fusion_agent_real",
        )


def _runtime_bridge(runtime: Any) -> Any:
    from fusion_agent_mcp.benchmark_bridge import FusionRuntimeBenchmarkBridge

    return FusionRuntimeBenchmarkBridge(runtime)


async def _close_runtime(runtime: Any) -> None:
    close = getattr(runtime, "close", None)
    if close is None:
        return
    value = close(timeout_seconds=2.0)
    if inspect.isawaitable(value):
        await value


def _context_error(context: Any) -> str | None:
    if getattr(context, "adapter_id", None) != "fusion_agent_codex":
        return "driver_subject_mismatch"
    if getattr(context, "execution_profile", None) != "normal_equivalent":
        return "normal_profile_equivalence_required"
    if getattr(context, "arbitrary_code_allowed", None) is not False:
        return "arbitrary_code_is_forbidden"
    return None


def _observed_revision() -> str:
    try:
        installed_version = version("fusion-agent-harness")
    except PackageNotFoundError:
        installed_version = "source-tree"
    return f"fusion-agent-harness@{installed_version}"


def _bounded_reason(value: str) -> str:
    return value[:500]


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _optional_float(value: Any) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return None
