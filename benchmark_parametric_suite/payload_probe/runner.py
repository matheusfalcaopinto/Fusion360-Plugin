"""Fail-closed orchestration for the executor payload boundary experiment."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import random
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from benchmark.filesystem import atomic_write_text, mkdir_exclusive

from .calibration import CalibratedScript, PayloadScriptCalibrator
from .models import (
    CanaryContract,
    CleanupReceipt,
    DispatchReceipt,
    DispatcherCapabilities,
    PayloadProbeMatrix,
    ProbeClassification,
    ProbeDispatchRequest,
    ProbeReadback,
    ProbeRunConfig,
    ProbeRunReport,
    ProbeTrialContext,
    ProbeTrialFixture,
    ProbeTrialResult,
)


class PayloadProbeConfigurationError(ValueError):
    pass


class PayloadProbeAbort(RuntimeError):
    def __init__(
        self, message: str, *, report: ProbeRunReport, artifact_dir: Path
    ) -> None:
        super().__init__(message)
        self.report = report
        self.artifact_dir = artifact_dir


class ProbeDispatcher(Protocol):
    @property
    def capabilities(self) -> DispatcherCapabilities: ...

    async def dispatch_once(self, request: ProbeDispatchRequest) -> DispatchReceipt: ...


class ProbeLifecycle(Protocol):
    async def prepare_trial(self, context: ProbeTrialContext) -> ProbeTrialFixture: ...

    async def readback(
        self,
        context: ProbeTrialContext,
        fixture: ProbeTrialFixture,
    ) -> ProbeReadback: ...

    async def cleanup_trial(
        self,
        context: ProbeTrialContext,
        fixture: ProbeTrialFixture,
    ) -> CleanupReceipt: ...


@dataclass(frozen=True)
class _PlannedTrial:
    context: ProbeTrialContext
    calibrated: CalibratedScript


@dataclass
class _ProcessGenerationTracker:
    same_process_generation: str | None = None
    fresh_process_generations: set[str] | None = None

    def __post_init__(self) -> None:
        if self.fresh_process_generations is None:
            self.fresh_process_generations = set()

    def validate_and_record(
        self, context: ProbeTrialContext, fixture: ProbeTrialFixture
    ) -> None:
        if context.process_mode == "same_process":
            if self.same_process_generation is None:
                self.same_process_generation = fixture.process_generation
            elif fixture.process_generation != self.same_process_generation:
                raise PayloadProbeConfigurationError(
                    "same_process arm changed process_generation"
                )
            return
        assert self.fresh_process_generations is not None
        if fixture.process_generation in self.fresh_process_generations:
            raise PayloadProbeConfigurationError(
                "fresh_process arm reused process_generation"
            )
        self.fresh_process_generations.add(fixture.process_generation)


class PayloadProbeRunner:
    """Run injected adapters once per disposable trial, without retry."""

    def __init__(
        self,
        *,
        matrix: PayloadProbeMatrix,
        calibrator: PayloadScriptCalibrator,
        dispatcher: ProbeDispatcher,
        lifecycle: ProbeLifecycle,
        output_root: str | Path,
    ) -> None:
        self.matrix = matrix
        self.calibrator = calibrator
        self.dispatcher = dispatcher
        self.lifecycle = lifecycle
        self.output_root = Path(output_root).resolve()

    async def run(self, config: ProbeRunConfig | None = None) -> ProbeRunReport:
        run_config = config or ProbeRunConfig()
        capabilities = self.dispatcher.capabilities
        self._validate_preflight(run_config, capabilities)
        run_id = run_config.run_id or f"payload_probe_{uuid.uuid4().hex}"
        if not re.fullmatch(r"[A-Za-z0-9_.-]{8,96}", run_id):
            raise PayloadProbeConfigurationError(
                "run_id must be 8-96 safe filename characters"
            )
        repetitions = run_config.repetitions or self.matrix.repetitions
        warmups = (
            self.matrix.warmups if run_config.warmups is None else run_config.warmups
        )
        seed = self.matrix.seed if run_config.seed is None else run_config.seed
        if type(repetitions) is not int or repetitions <= 0:
            raise PayloadProbeConfigurationError(
                "repetitions must be a positive integer"
            )
        if type(seed) is not int:
            raise PayloadProbeConfigurationError("seed must be an integer")
        if type(warmups) is not int or warmups < 0:
            raise PayloadProbeConfigurationError(
                "warmups must be a non-negative integer"
            )
        for label, timeout in (
            ("prepare", run_config.prepare_timeout_seconds),
            ("dispatch", run_config.dispatch_timeout_seconds),
            ("readback", run_config.readback_timeout_seconds),
            ("cleanup", run_config.cleanup_timeout_seconds),
        ):
            if (
                isinstance(timeout, bool)
                or not isinstance(timeout, (int, float))
                or not math.isfinite(float(timeout))
                or timeout <= 0
            ):
                raise PayloadProbeConfigurationError(
                    f"{label} timeout must be positive"
                )

        plans = self._plan_all(
            run_id=run_id,
            warmups=warmups,
            repetitions=repetitions,
            seed=seed,
        )
        topology = {plan.calibrated.ast_topology_sha256 for plan in plans}
        if len(topology) != 1:
            raise PayloadProbeConfigurationError(
                "protected scripts do not share one constant AST topology"
            )

        artifact_dir = self.output_root / run_id
        try:
            mkdir_exclusive(artifact_dir)
        except FileExistsError as exc:
            raise PayloadProbeConfigurationError(
                "run artifact directory already exists"
            ) from exc
        report = ProbeRunReport(
            schema_version="fusion_executor_payload_probe.report.v1",
            run_id=run_id,
            experiment_id=self.matrix.experiment_id,
            seed=seed,
            warmups=warmups,
            repetitions=repetitions,
        )
        writer = _ArtifactWriter(artifact_dir=artifact_dir, matrix=self.matrix)
        writer.write(report)
        generations = _ProcessGenerationTracker()

        for plan in plans:
            try:
                result = await self._run_trial(plan, run_config, generations)
            except BaseException as exc:
                if isinstance(exc, asyncio.CancelledError):
                    report.abort_reason = "CALL_CANCELLED_OUTCOME_UNKNOWN"
                else:
                    report.abort_reason = "BENCHMARK_TRIAL_FAILED"
                report.status = "aborted"
                writer.write(report)
                raise PayloadProbeAbort(
                    report.abort_reason,
                    report=report,
                    artifact_dir=artifact_dir,
                ) from exc
            report.trials.append(result)
            writer.write(report)
            abort_reason = _trial_abort_reason(result)
            if abort_reason is not None:
                report.status = "aborted"
                report.abort_reason = abort_reason
                writer.write(report)
                raise PayloadProbeAbort(
                    abort_reason, report=report, artifact_dir=artifact_dir
                )

        report.status = "complete"
        writer.write(report)
        return report

    def _validate_preflight(
        self,
        config: ProbeRunConfig,
        capabilities: DispatcherCapabilities,
    ) -> None:
        if capabilities.retry_policy != "never":
            raise PayloadProbeConfigurationError(
                "dispatcher retry policy must be never"
            )
        if not capabilities.post_dispatch_replay_suppressed:
            raise PayloadProbeConfigurationError(
                "dispatcher must suppress replay after dispatch"
            )
        if (
            not capabilities.supports_fresh_process
            and "fresh_process" in self.matrix.process_modes
        ):
            raise PayloadProbeConfigurationError(
                "dispatcher does not support the fresh_process arm"
            )
        if (
            capabilities.configured_payload_limit_bytes
            < self.matrix.maximum_target_bytes
        ):
            raise PayloadProbeConfigurationError(
                "configured protected-payload gate is below the largest matrix point"
            )
        if capabilities.real:
            if not config.confirm_real_dispatch:
                raise PayloadProbeConfigurationError(
                    "real payload probe requires confirm_real_dispatch=true"
                )
            if not config.confirm_temporary_gate_elevation:
                raise PayloadProbeConfigurationError(
                    "real payload probe requires explicit confirmation of temporary gate elevation"
                )

    def _plan_all(
        self,
        *,
        run_id: str,
        warmups: int,
        repetitions: int,
        seed: int,
    ) -> list[_PlannedTrial]:
        contexts: list[ProbeTrialContext] = []
        sequence_index = 0
        for process_mode in self.matrix.process_modes:
            for warmup_index in range(warmups):
                target = self.matrix.targets[0]
                context = _make_context(
                    run_id=run_id,
                    process_mode=process_mode,
                    repetition=warmup_index,
                    warmup=True,
                    target_id=target.id,
                    target_bytes=target.target_protected_bytes,
                    sequence_index=sequence_index,
                )
                contexts.append(context)
                sequence_index += 1
            for repetition in range(repetitions):
                targets = list(self.matrix.targets)
                random.Random(f"{seed}:{process_mode}:{repetition}").shuffle(targets)
                for target in targets:
                    contexts.append(
                        _make_context(
                            run_id=run_id,
                            process_mode=process_mode,
                            repetition=repetition,
                            warmup=False,
                            target_id=target.id,
                            target_bytes=target.target_protected_bytes,
                            sequence_index=sequence_index,
                        )
                    )
                    sequence_index += 1
        return [
            _PlannedTrial(
                context=context,
                calibrated=self.calibrator.calibrate(
                    target_protected_bytes=context.target_protected_bytes,
                    canaries=context.canaries,
                ),
            )
            for context in contexts
        ]

    async def _run_trial(
        self,
        plan: _PlannedTrial,
        config: ProbeRunConfig,
        generations: _ProcessGenerationTracker,
    ) -> ProbeTrialResult:
        context = plan.context
        fixture = await asyncio.wait_for(
            self.lifecycle.prepare_trial(context),
            timeout=float(config.prepare_timeout_seconds),
        )
        validation_error: BaseException | None = None
        try:
            _validate_fixture(context, fixture)
            generations.validate_and_record(context, fixture)
        except BaseException as exc:
            validation_error = exc
        request = ProbeDispatchRequest(
            context=context,
            document_id=fixture.document_id,
            fixture_marker=fixture.fixture_marker,
            script=plan.calibrated.protected_script,
            original_payload_bytes=plan.calibrated.original_payload_bytes,
            protected_payload_bytes=plan.calibrated.protected_payload_bytes,
            original_payload_sha256=plan.calibrated.original_payload_sha256,
            protected_payload_sha256=plan.calibrated.protected_payload_sha256,
            ast_topology_sha256=plan.calibrated.ast_topology_sha256,
            operation_id=f"payload-probe:{context.trial_id}",
        )
        dispatch_invocations = 0
        dispatch_error: BaseException | None = None
        cancellation: asyncio.CancelledError | None = None
        if validation_error is None:
            try:
                dispatch_invocations += 1
                receipt = await asyncio.wait_for(
                    self.dispatcher.dispatch_once(request),
                    timeout=float(config.dispatch_timeout_seconds),
                )
            except asyncio.CancelledError as exc:
                cancellation = exc
                dispatch_error = exc
                receipt = DispatchReceipt(
                    mutating_dispatch_count=1,
                    transport_succeeded=False,
                    native_success=False,
                    outcome_unknown=True,
                    error_code="CALL_CANCELLED",
                )
            except BaseException as exc:
                dispatch_error = exc
                error_code = (
                    "MUTATION_OUTCOME_UNKNOWN"
                    if isinstance(exc, TimeoutError)
                    else "DISPATCH_FAILED"
                )
                receipt = DispatchReceipt(
                    mutating_dispatch_count=1,
                    transport_succeeded=False,
                    native_success=False,
                    outcome_unknown=True,
                    error_code=error_code,
                )
        else:
            receipt = DispatchReceipt(
                mutating_dispatch_count=0,
                transport_succeeded=False,
                native_success=False,
                outcome_unknown=False,
                error_code="FIXTURE_VALIDATION_FAILED",
            )

        try:
            _validate_dispatch_receipt(receipt)
        except (TypeError, ValueError) as exc:
            dispatch_error = dispatch_error or exc
            receipt = DispatchReceipt(
                mutating_dispatch_count=dispatch_invocations,
                transport_succeeded=False,
                native_success=False,
                outcome_unknown=dispatch_invocations > 0,
                duration_ms=0.0,
                error_code="INVALID_NUMERIC_EVIDENCE",
            )

        readback_error: BaseException | None = None
        try:
            readback = await asyncio.wait_for(
                self.lifecycle.readback(context, fixture),
                timeout=float(config.readback_timeout_seconds),
            )
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
            readback_error = exc
            readback = ProbeReadback(
                document_id=fixture.document_id,
                fixture_marker=fixture.fixture_marker,
                observed_trial_id=None,
                start_value=None,
                mutation_value=None,
                end_value=None,
                state_fingerprint="READBACK_CANCELLED",
                expected_change_complete=False,
                unexpected_drift=True,
                warnings=("CALL_CANCELLED",),
            )
        except BaseException as exc:
            readback_error = exc
            readback = ProbeReadback(
                document_id=fixture.document_id,
                fixture_marker=fixture.fixture_marker,
                observed_trial_id=None,
                start_value=None,
                mutation_value=None,
                end_value=None,
                state_fingerprint="READBACK_FAILED",
                expected_change_complete=False,
                unexpected_drift=True,
                warnings=("READBACK_FAILED",),
            )

        cleanup_error: BaseException | None = None
        try:
            cleanup = await asyncio.wait_for(
                self.lifecycle.cleanup_trial(context, fixture),
                timeout=float(config.cleanup_timeout_seconds),
            )
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
            cleanup_error = exc
            cleanup = CleanupReceipt(
                document_closed=False,
                saved=False,
                original_document_restored=False,
                restoration_fingerprint_matches=False,
                open_documents_match=False,
                errors=("CALL_CANCELLED",),
            )
        except BaseException as exc:
            cleanup_error = exc
            cleanup = CleanupReceipt(
                document_closed=False,
                saved=False,
                original_document_restored=False,
                restoration_fingerprint_matches=False,
                open_documents_match=False,
                errors=("CLEANUP_FAILED",),
            )

        classification, reasons = classify_probe_observation(
            context=context,
            fixture=fixture,
            receipt=receipt,
            readback=readback,
        )
        extra_reasons = list(reasons)
        if dispatch_error is not None:
            extra_reasons.append("dispatch raised; no retry was attempted")
        if readback_error is not None:
            extra_reasons.append("independent readback failed")
        if cleanup_error is not None:
            extra_reasons.append("cleanup/restoration adapter failed")
        if validation_error is not None:
            extra_reasons.append(
                "fixture/process generation validation failed before dispatch"
            )
        result = ProbeTrialResult(
            context=context,
            classification=classification,
            reasons=tuple(extra_reasons),
            original_payload_bytes=plan.calibrated.original_payload_bytes,
            protected_payload_bytes=plan.calibrated.protected_payload_bytes,
            original_payload_sha256=plan.calibrated.original_payload_sha256,
            protected_payload_sha256=plan.calibrated.protected_payload_sha256,
            ast_topology_sha256=plan.calibrated.ast_topology_sha256,
            dispatch_invocations_by_runner=dispatch_invocations,
            dispatch_receipt=receipt,
            readback=readback,
            cleanup=cleanup,
        )
        if cancellation is not None:
            raise cancellation
        return result


def _make_context(
    *,
    run_id: str,
    process_mode: str,
    repetition: int,
    warmup: bool,
    target_id: str,
    target_bytes: int,
    sequence_index: int,
) -> ProbeTrialContext:
    phase = "warmup" if warmup else "measured"
    digest = hashlib.sha256(
        f"{run_id}:{process_mode}:{phase}:{repetition}:{target_id}".encode("utf-8")
    ).hexdigest()[:24]
    trial_id = f"pp_{digest}"
    canaries = CanaryContract.for_trial(run_id=run_id, trial_id=trial_id)
    return ProbeTrialContext(
        run_id=run_id,
        trial_id=trial_id,
        target_id=target_id,
        target_protected_bytes=target_bytes,
        repetition=repetition,
        warmup=warmup,
        process_mode=process_mode,
        sequence_index=sequence_index,
        canaries=canaries,
    )


def classify_probe_observation(
    *,
    context: ProbeTrialContext,
    fixture: ProbeTrialFixture,
    receipt: DispatchReceipt,
    readback: ProbeReadback,
) -> tuple[ProbeClassification, tuple[str, ...]]:
    """Classify only independent state plus dispatch/replay transport facts."""

    canaries = context.canaries
    contamination: list[str] = []
    if receipt.mutating_dispatch_count != 1:
        contamination.append("adapter did not report exactly one mutating dispatch")
    if receipt.error_code == "INVALID_NUMERIC_EVIDENCE":
        contamination.append("dispatch receipt contained invalid numeric evidence")
    if readback.document_id != fixture.document_id:
        contamination.append("active document identity changed")
    if readback.fixture_marker != canaries.fixture_marker:
        contamination.append("fixture marker changed or disappeared")
    if readback.observed_trial_id not in (None, canaries.trial_id):
        contamination.append("foreign trial marker observed")
    expected_values = (
        ("start", readback.start_value, canaries.start_value),
        ("mutation", readback.mutation_value, canaries.mutation_value),
        ("end", readback.end_value, canaries.end_value),
    )
    for label, actual, expected in expected_values:
        if actual is not None and actual != expected:
            contamination.append(f"foreign {label} canary observed")
    if readback.unexpected_drift:
        contamination.append("independent readback detected unexpected drift")
    if readback.save_detected:
        contamination.append("save was detected")
    if contamination:
        return ProbeClassification.CONTAMINATED, tuple(contamination)

    start = readback.start_value == canaries.start_value
    mutation = readback.mutation_value == canaries.mutation_value
    end = readback.end_value == canaries.end_value
    trial = readback.observed_trial_id == canaries.trial_id
    if trial and start and mutation and end and readback.expected_change_complete:
        reasons = ["start, mutation and end attributes match independent readback"]
        if receipt.outcome_unknown:
            reasons.append(
                "state oracle proved completion after an unknown transport outcome"
            )
        return ProbeClassification.COMPLETE, tuple(reasons)

    no_trace = all(
        value is None
        for value in (
            readback.observed_trial_id,
            readback.start_value,
            readback.mutation_value,
            readback.end_value,
        )
    )
    unchanged = readback.state_fingerprint == fixture.baseline_fingerprint
    acknowledged = (
        receipt.transport_succeeded
        and receipt.native_success
        and not receipt.outcome_unknown
    )
    if (
        no_trace
        and unchanged
        and not readback.expected_change_complete
        and acknowledged
    ):
        return ProbeClassification.SILENT_NOOP, (
            "native call reported success but no canary or state change was observed",
        )

    reasons = ["canary/state contract was not completed"]
    if no_trace and unchanged:
        reasons.append(
            "no effect was observed but the call was not an acknowledged success"
        )
    elif start and not end:
        reasons.append("start canary exists without the end canary")
    elif (
        readback.state_fingerprint != fixture.baseline_fingerprint
        and not readback.expected_change_complete
    ):
        reasons.append("state changed without satisfying the independent oracle")
    return ProbeClassification.PARTIAL, tuple(reasons)


def _validate_fixture(context: ProbeTrialContext, fixture: ProbeTrialFixture) -> None:
    if fixture.trial_id != context.trial_id:
        raise PayloadProbeConfigurationError("lifecycle returned the wrong trial id")
    if fixture.fixture_marker != context.canaries.fixture_marker:
        raise PayloadProbeConfigurationError(
            "lifecycle returned the wrong fixture marker"
        )
    if (
        not fixture.document_id
        or not fixture.baseline_fingerprint
        or not fixture.process_generation
    ):
        raise PayloadProbeConfigurationError("lifecycle fixture identity is incomplete")
    if not fixture.baseline_canaries_clean:
        raise PayloadProbeConfigurationError(
            "fixture contains pre-existing probe canaries"
        )
    if not fixture.unsaved:
        raise PayloadProbeConfigurationError(
            "payload probe requires a new unsaved document"
        )


def _validate_dispatch_receipt(receipt: DispatchReceipt) -> None:
    if type(receipt.mutating_dispatch_count) is not int:
        raise TypeError("dispatch count must be an integer")
    duration = receipt.duration_ms
    if (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(float(duration))
        or duration < 0
    ):
        raise ValueError("INVALID_NUMERIC_EVIDENCE")


def _trial_abort_reason(result: ProbeTrialResult) -> str | None:
    if not result.cleanup.safe:
        return "RESTORATION_FAILED"
    if result.readback.unexpected_drift:
        return "DOCUMENT_DRIFT_DETECTED"
    if result.classification is ProbeClassification.PARTIAL:
        return "PARTIAL_CHANGE_DETECTED"
    if result.classification is ProbeClassification.CONTAMINATED:
        return "CONTAMINATED_TRIAL"
    return None


class _ArtifactWriter:
    def __init__(self, *, artifact_dir: Path, matrix: PayloadProbeMatrix) -> None:
        self.artifact_dir = artifact_dir
        self.matrix = matrix
        _atomic_json(
            artifact_dir / "matrix_snapshot.json",
            {
                **_project_matrix(matrix),
                "historical_observations_are_expectations": False,
                "historical_observations_are_oracles": False,
            },
        )

    def write(self, report: ProbeRunReport) -> None:
        _atomic_json(self.artifact_dir / "report.json", report.to_dict())
        trials = "".join(
            json.dumps(
                trial.to_dict(),
                sort_keys=True,
                ensure_ascii=False,
                allow_nan=False,
            )
            + "\n"
            for trial in report.trials
        )
        _atomic_text(self.artifact_dir / "trials.jsonl", trials)


def _atomic_json(path: Path, payload: object) -> None:
    _atomic_text(
        path,
        json.dumps(
            payload,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n",
    )


def _project_matrix(matrix: PayloadProbeMatrix) -> dict[str, object]:
    return {
        "schema_version": matrix.schema_version,
        "experiment_id": _safe_identifier(matrix.experiment_id, "payload_probe"),
        "payload_metric": _safe_identifier(matrix.payload_metric, "protected_bytes"),
        "warmups": matrix.warmups,
        "repetitions": matrix.repetitions,
        "seed": matrix.seed,
        "process_modes": list(matrix.process_modes),
        "targets": [
            {
                "id": _safe_identifier(target.id, "target"),
                "target_protected_bytes": target.target_protected_bytes,
            }
            for target in matrix.targets
        ],
        "historical_observations": [
            {
                "protected_payload_bytes": item.protected_payload_bytes,
                "eligible_as_expectation": False,
                "eligible_as_oracle": False,
            }
            for item in matrix.historical_observations[:64]
        ],
        "retry_policy": matrix.retry_policy,
        "mutating_dispatches_per_trial": matrix.mutating_dispatches_per_trial,
        "abort_on": [
            _safe_identifier(value, "abort_condition") for value in matrix.abort_on[:16]
        ],
    }


def _safe_identifier(value: str, fallback: str) -> str:
    normalized = str(value).strip()
    if re.fullmatch(r"[A-Za-z0-9_.-]{1,96}", normalized):
        return normalized
    return fallback


def _atomic_text(path: Path, value: str) -> None:
    atomic_write_text(path, value)
