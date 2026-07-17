"""Typed contracts for the executor protected-payload experiment."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


_PUBLIC_REASON_ALLOWLIST = {
    "adapter did not report exactly one mutating dispatch",
    "active document identity changed",
    "fixture marker changed or disappeared",
    "foreign trial marker observed",
    "foreign start canary observed",
    "foreign mutation canary observed",
    "foreign end canary observed",
    "independent readback detected unexpected drift",
    "save was detected",
    "dispatch receipt contained invalid numeric evidence",
    "start, mutation and end attributes match independent readback",
    "state oracle proved completion after an unknown transport outcome",
    "native call reported success but no canary or state change was observed",
    "canary/state contract was not completed",
    "no effect was observed but the call was not an acknowledged success",
    "start canary exists without the end canary",
    "state changed without satisfying the independent oracle",
    "dispatch raised; no retry was attempted",
    "independent readback failed",
    "cleanup/restoration adapter failed",
    "fixture/process generation validation failed before dispatch",
}


class ProbeClassification(str, Enum):
    """The only four state classifications emitted by the probe."""

    SILENT_NOOP = "silent_noop"
    PARTIAL = "partial"
    COMPLETE = "complete"
    CONTAMINATED = "contaminated"


@dataclass(frozen=True)
class PayloadTarget:
    id: str
    target_protected_bytes: int


@dataclass(frozen=True)
class HistoricalObservation:
    protected_payload_bytes: int
    observation_label: str
    source: str
    eligible_as_expectation: bool = False
    eligible_as_oracle: bool = False


@dataclass(frozen=True)
class PayloadProbeMatrix:
    schema_version: str
    experiment_id: str
    payload_metric: str
    warmups: int
    repetitions: int
    seed: int
    process_modes: tuple[str, ...]
    targets: tuple[PayloadTarget, ...]
    historical_observations: tuple[HistoricalObservation, ...]
    retry_policy: str
    mutating_dispatches_per_trial: int
    abort_on: tuple[str, ...]

    @property
    def maximum_target_bytes(self) -> int:
        return max(target.target_protected_bytes for target in self.targets)


@dataclass(frozen=True)
class CanaryContract:
    """Unique fixed-length values written as Fusion attributes by one trial."""

    group: str
    fixture_marker: str
    trial_id: str
    start_value: str
    mutation_value: str
    end_value: str

    @classmethod
    def for_trial(cls, *, run_id: str, trial_id: str) -> "CanaryContract":
        def token(label: str, length: int = 32) -> str:
            digest = hashlib.sha256(
                f"{run_id}:{trial_id}:{label}".encode("utf-8")
            ).hexdigest()
            return digest[:length]

        return cls(
            group="fusion_agent_payload_probe",
            fixture_marker=f"fixture_{token('fixture')}",
            trial_id=trial_id,
            start_value=f"start_{token('start')}",
            mutation_value=f"mutation_{token('mutation')}",
            end_value=f"end_{token('end')}",
        )


@dataclass(frozen=True)
class DispatcherCapabilities:
    adapter_id: str
    real: bool
    configured_payload_limit_bytes: int
    retry_policy: str = "never"
    post_dispatch_replay_suppressed: bool = True
    supports_fresh_process: bool = True


@dataclass(frozen=True)
class ProbeRunConfig:
    """Run-time authority; real dispatch is denied unless both flags are true."""

    run_id: str | None = None
    warmups: int | None = None
    repetitions: int | None = None
    seed: int | None = None
    confirm_real_dispatch: bool = False
    confirm_temporary_gate_elevation: bool = False
    prepare_timeout_seconds: float = 30.0
    dispatch_timeout_seconds: float = 300.0
    readback_timeout_seconds: float = 120.0
    cleanup_timeout_seconds: float = 10.0


@dataclass(frozen=True)
class ProbeTrialContext:
    run_id: str
    trial_id: str
    target_id: str
    target_protected_bytes: int
    repetition: int
    warmup: bool
    process_mode: str
    sequence_index: int
    canaries: CanaryContract


@dataclass(frozen=True)
class ProbeTrialFixture:
    """Disposable fixture prepared before the sole mutating dispatch."""

    trial_id: str
    document_id: str
    original_document_id: str | None
    fixture_marker: str
    baseline_fingerprint: str
    process_generation: str
    baseline_canaries_clean: bool
    unsaved: bool


@dataclass(frozen=True)
class ProbeDispatchRequest:
    context: ProbeTrialContext
    document_id: str
    fixture_marker: str
    script: str
    original_payload_bytes: int
    protected_payload_bytes: int
    original_payload_sha256: str
    protected_payload_sha256: str
    ast_topology_sha256: str
    semantics: str = "mutating"
    operation_id: str = ""
    maximum_dispatches: int = 1


@dataclass(frozen=True)
class DispatchReceipt:
    """Adapter receipt for its one and only dispatch attempt."""

    mutating_dispatch_count: int
    transport_succeeded: bool
    native_success: bool
    outcome_unknown: bool = False
    duration_ms: float = 0.0
    error_code: str | None = None


@dataclass(frozen=True)
class ProbeReadback:
    """Independent post-dispatch observation; never executor self-report."""

    document_id: str
    fixture_marker: str | None
    observed_trial_id: str | None
    start_value: str | None
    mutation_value: str | None
    end_value: str | None
    state_fingerprint: str
    expected_change_complete: bool
    unexpected_drift: bool = False
    save_detected: bool = False
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class CleanupReceipt:
    document_closed: bool
    saved: bool
    original_document_restored: bool
    restoration_fingerprint_matches: bool
    open_documents_match: bool
    errors: tuple[str, ...] = ()

    @property
    def safe(self) -> bool:
        return (
            self.document_closed
            and not self.saved
            and self.original_document_restored
            and self.restoration_fingerprint_matches
            and self.open_documents_match
            and not self.errors
        )


@dataclass(frozen=True)
class ProbeTrialResult:
    context: ProbeTrialContext
    classification: ProbeClassification
    reasons: tuple[str, ...]
    original_payload_bytes: int
    protected_payload_bytes: int
    original_payload_sha256: str
    protected_payload_sha256: str
    ast_topology_sha256: str
    dispatch_invocations_by_runner: int
    dispatch_receipt: DispatchReceipt
    readback: ProbeReadback
    cleanup: CleanupReceipt

    def to_dict(self) -> dict[str, Any]:
        return {
            "context": {
                "run_id": self.context.run_id,
                "trial_id": self.context.trial_id,
                "target_id": self.context.target_id,
                "target_protected_bytes": self.context.target_protected_bytes,
                "repetition": self.context.repetition,
                "warmup": self.context.warmup,
                "process_mode": self.context.process_mode,
                "sequence_index": self.context.sequence_index,
            },
            "classification": self.classification.value,
            "reasons": [_public_reason(item) for item in self.reasons[:32]],
            "original_payload_bytes": self.original_payload_bytes,
            "protected_payload_bytes": self.protected_payload_bytes,
            "original_payload_sha256": self.original_payload_sha256,
            "protected_payload_sha256": self.protected_payload_sha256,
            "ast_topology_sha256": self.ast_topology_sha256,
            "dispatch_invocations_by_runner": self.dispatch_invocations_by_runner,
            "dispatch_receipt": {
                "mutating_dispatch_count": self.dispatch_receipt.mutating_dispatch_count,
                "transport_succeeded": self.dispatch_receipt.transport_succeeded,
                "native_success": self.dispatch_receipt.native_success,
                "outcome_unknown": self.dispatch_receipt.outcome_unknown,
                "duration_ms": self.dispatch_receipt.duration_ms,
                "error_code": _public_code(
                    self.dispatch_receipt.error_code,
                    fallback="DISPATCH_ERROR_REDACTED",
                ),
            },
            "readback": {
                "trial_id_matches": (
                    self.readback.observed_trial_id == self.context.trial_id
                ),
                "start_observed": self.readback.start_value is not None,
                "mutation_observed": self.readback.mutation_value is not None,
                "end_observed": self.readback.end_value is not None,
                "expected_change_complete": self.readback.expected_change_complete,
                "unexpected_drift": self.readback.unexpected_drift,
                "save_detected": self.readback.save_detected,
                "warnings": [
                    _public_code(item, fallback="READBACK_WARNING_REDACTED")
                    for item in self.readback.warnings[:32]
                ],
            },
            "cleanup": {
                "document_closed": self.cleanup.document_closed,
                "saved": self.cleanup.saved,
                "original_document_restored": self.cleanup.original_document_restored,
                "restoration_fingerprint_matches": (
                    self.cleanup.restoration_fingerprint_matches
                ),
                "open_documents_match": self.cleanup.open_documents_match,
                "error_codes": [
                    _public_code(item, fallback="CLEANUP_ERROR_REDACTED")
                    for item in self.cleanup.errors[:32]
                ],
                "safe": self.cleanup.safe,
            },
        }


@dataclass
class ProbeRunReport:
    schema_version: str
    run_id: str
    experiment_id: str
    seed: int
    warmups: int
    repetitions: int
    status: str = "running"
    abort_reason: str | None = None
    trials: list[ProbeTrialResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "experiment_id": self.experiment_id,
            "seed": self.seed,
            "warmups": self.warmups,
            "repetitions": self.repetitions,
            "status": self.status,
            "abort_reason": self.abort_reason,
            "trial_count": len(self.trials),
            "warmup_trial_count": sum(
                1 for trial in self.trials if trial.context.warmup
            ),
            "measured_trial_count": sum(
                1 for trial in self.trials if not trial.context.warmup
            ),
            "classification_counts": {
                value.value: sum(
                    1
                    for trial in self.trials
                    if not trial.context.warmup and trial.classification is value
                )
                for value in ProbeClassification
            },
            "trials": [trial.to_dict() for trial in self.trials],
        }


def _public_code(value: str | None, *, fallback: str) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    if re.fullmatch(r"[A-Z][A-Z0-9_]{2,79}", normalized):
        return normalized
    return fallback


def _public_reason(value: str) -> str:
    normalized = str(value).strip()
    if normalized in _PUBLIC_REASON_ALLOWLIST:
        return normalized
    return "benchmark trial diagnostic redacted"
