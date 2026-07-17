from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from benchmark_parametric_suite.payload_probe.calibration import PayloadScriptCalibrator
from benchmark_parametric_suite.payload_probe.cli import validate as validate_offline
from benchmark_parametric_suite.payload_probe.loader import (
    PayloadMatrixError,
    load_probe_matrix,
)
from benchmark_parametric_suite.payload_probe.models import (
    CanaryContract,
    CleanupReceipt,
    DispatchReceipt,
    DispatcherCapabilities,
    ProbeClassification,
    ProbeReadback,
    ProbeRunConfig,
    ProbeTrialContext,
    ProbeTrialFixture,
)
from benchmark_parametric_suite.payload_probe.runner import (
    PayloadProbeAbort,
    PayloadProbeConfigurationError,
    PayloadProbeRunner,
    classify_probe_observation,
)
from fusion_mcp_adapter.execute_guard import normalize_execute_script


ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "benchmark_parametric_suite" / "payload_probe_matrix.json"


def _context() -> ProbeTrialContext:
    canaries = CanaryContract.for_trial(
        run_id="run_payload_test", trial_id="pp_trial_0001"
    )
    return ProbeTrialContext(
        run_id="run_payload_test",
        trial_id=canaries.trial_id,
        target_id="p020480",
        target_protected_bytes=20480,
        repetition=0,
        warmup=False,
        process_mode="same_process",
        sequence_index=0,
        canaries=canaries,
    )


def _fixture(context: ProbeTrialContext) -> ProbeTrialFixture:
    return ProbeTrialFixture(
        trial_id=context.trial_id,
        document_id="doc-1",
        original_document_id="original-1",
        fixture_marker=context.canaries.fixture_marker,
        baseline_fingerprint="baseline",
        process_generation="process-1",
        baseline_canaries_clean=True,
        unsaved=True,
    )


def _readback(context: ProbeTrialContext, outcome: str) -> ProbeReadback:
    canaries = context.canaries
    values = {
        "complete": (
            canaries.trial_id,
            canaries.start_value,
            canaries.mutation_value,
            canaries.end_value,
            "changed",
            True,
        ),
        "silent_noop": (None, None, None, None, "baseline", False),
        "partial": (
            canaries.trial_id,
            canaries.start_value,
            None,
            None,
            "changed",
            False,
        ),
        "contaminated": ("foreign", None, None, None, "changed", False),
    }[outcome]
    return ProbeReadback(
        document_id="doc-1",
        fixture_marker=canaries.fixture_marker,
        observed_trial_id=values[0],
        start_value=values[1],
        mutation_value=values[2],
        end_value=values[3],
        state_fingerprint=values[4],
        expected_change_complete=values[5],
    )


def test_matrix_and_production_protector_calibrate_exact_canonical_sizes() -> None:
    matrix = load_probe_matrix(MATRIX)
    assert tuple(target.target_protected_bytes for target in matrix.targets) == (
        20480,
        24576,
        28672,
        31744,
        32512,
        32767,
        32768,
        32769,
        33024,
        36864,
        37976,
        40960,
    )
    assert matrix.warmups == 1
    assert all(
        not item.eligible_as_expectation and not item.eligible_as_oracle
        for item in matrix.historical_observations
    )
    calibrator = PayloadScriptCalibrator(normalize_execute_script)
    canaries = CanaryContract.for_trial(
        run_id="offline_validation", trial_id="pp_calibration"
    )
    scripts = [
        calibrator.calibrate(
            target_protected_bytes=item.target_protected_bytes, canaries=canaries
        )
        for item in matrix.targets
    ]
    assert [item.protected_payload_bytes for item in scripts] == [
        item.target_protected_bytes for item in matrix.targets
    ]
    assert len({item.ast_topology_sha256 for item in scripts}) == 1
    assert len({item.padding_invariant_sha256 for item in scripts}) == 1
    raw = scripts[-1].raw_script
    assert (
        raw.index("'start'")
        < raw.index("_payload_probe_padding")
        < raw.index("'mutation'")
        < raw.index("'end'")
    )


def test_matrix_rejects_using_history_as_an_expectation(tmp_path: Path) -> None:
    payload = json.loads(MATRIX.read_text(encoding="utf-8"))
    payload["historical_observations"][0]["eligible_as_expectation"] = True
    path = tmp_path / "matrix.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(PayloadMatrixError, match="cannot be an expectation"):
        load_probe_matrix(path)


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        ("complete", ProbeClassification.COMPLETE),
        ("silent_noop", ProbeClassification.SILENT_NOOP),
        ("partial", ProbeClassification.PARTIAL),
        ("contaminated", ProbeClassification.CONTAMINATED),
    ],
)
def test_classifier_emits_only_explicit_state_classes(
    outcome: str, expected: ProbeClassification
) -> None:
    context = _context()
    receipt = DispatchReceipt(
        mutating_dispatch_count=1,
        transport_succeeded=True,
        native_success=True,
    )
    actual, _ = classify_probe_observation(
        context=context,
        fixture=_fixture(context),
        receipt=receipt,
        readback=_readback(context, outcome),
    )
    assert actual is expected


class _Dispatcher:
    def __init__(self, *, real: bool = False, delay: float = 0.0) -> None:
        self.calls = []
        self.delay = delay
        self._capabilities = DispatcherCapabilities(
            adapter_id="mock",
            real=real,
            configured_payload_limit_bytes=40960,
        )

    @property
    def capabilities(self) -> DispatcherCapabilities:
        return self._capabilities

    async def dispatch_once(self, request):
        self.calls.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        return DispatchReceipt(
            mutating_dispatch_count=1,
            transport_succeeded=True,
            native_success=True,
        )


class _Lifecycle:
    def __init__(
        self,
        *,
        outcome: str = "complete",
        cleanup_safe: bool = True,
        reuse_fresh: bool = False,
    ) -> None:
        self.outcome = outcome
        self.cleanup_safe = cleanup_safe
        self.reuse_fresh = reuse_fresh
        self.prepared = []
        self.cleaned = []

    async def prepare_trial(self, context: ProbeTrialContext) -> ProbeTrialFixture:
        self.prepared.append(context)
        generation = "persistent-generation"
        if context.process_mode == "fresh_process":
            generation = (
                "reused-fresh" if self.reuse_fresh else f"fresh-{context.trial_id}"
            )
        return ProbeTrialFixture(
            trial_id=context.trial_id,
            document_id="doc-1",
            original_document_id="original-1",
            fixture_marker=context.canaries.fixture_marker,
            baseline_fingerprint="baseline",
            process_generation=generation,
            baseline_canaries_clean=True,
            unsaved=True,
        )

    async def readback(
        self, context: ProbeTrialContext, fixture: ProbeTrialFixture
    ) -> ProbeReadback:
        return _readback(context, self.outcome)

    async def cleanup_trial(
        self, context: ProbeTrialContext, fixture: ProbeTrialFixture
    ) -> CleanupReceipt:
        self.cleaned.append(context)
        return CleanupReceipt(
            document_closed=self.cleanup_safe,
            saved=False,
            original_document_restored=self.cleanup_safe,
            restoration_fingerprint_matches=self.cleanup_safe,
            open_documents_match=self.cleanup_safe,
        )


def _runner(tmp_path: Path, dispatcher: _Dispatcher, lifecycle: _Lifecycle):
    matrix = load_probe_matrix(MATRIX)
    matrix = replace(matrix, targets=matrix.targets[:1], repetitions=1, warmups=1)
    return PayloadProbeRunner(
        matrix=matrix,
        calibrator=PayloadScriptCalibrator(normalize_execute_script),
        dispatcher=dispatcher,
        lifecycle=lifecycle,
        output_root=tmp_path,
    )


@pytest.mark.asyncio
async def test_runner_separates_warmups_and_dispatches_once_per_trial(
    tmp_path: Path,
) -> None:
    dispatcher = _Dispatcher()
    lifecycle = _Lifecycle()
    report = await _runner(tmp_path, dispatcher, lifecycle).run(
        ProbeRunConfig(run_id="payload_complete_001")
    )
    assert report.status == "complete"
    assert len(report.trials) == 4  # one warmup + one measured point, per process mode
    assert len(dispatcher.calls) == len(report.trials)
    assert all(item.maximum_dispatches == 1 for item in dispatcher.calls)
    serialized = report.to_dict()
    assert serialized["warmup_trial_count"] == 2
    assert serialized["measured_trial_count"] == 2
    assert serialized["classification_counts"]["complete"] == 2
    assert (tmp_path / "payload_complete_001" / "report.json").exists()


@pytest.mark.asyncio
async def test_partial_aborts_after_one_dispatch_without_retry_and_cleans_up(
    tmp_path: Path,
) -> None:
    dispatcher = _Dispatcher()
    lifecycle = _Lifecycle(outcome="partial")
    with pytest.raises(PayloadProbeAbort) as caught:
        await _runner(tmp_path, dispatcher, lifecycle).run(
            ProbeRunConfig(run_id="payload_partial_001")
        )
    assert len(dispatcher.calls) == 1
    assert len(lifecycle.cleaned) == 1
    assert caught.value.report.status == "aborted"
    assert caught.value.report.trials[0].classification is ProbeClassification.PARTIAL


@pytest.mark.asyncio
async def test_dispatch_timeout_is_never_retried_and_restoration_is_attempted(
    tmp_path: Path,
) -> None:
    dispatcher = _Dispatcher(delay=0.05)
    lifecycle = _Lifecycle(outcome="silent_noop")
    with pytest.raises(PayloadProbeAbort) as caught:
        await _runner(tmp_path, dispatcher, lifecycle).run(
            ProbeRunConfig(run_id="payload_timeout_001", dispatch_timeout_seconds=0.001)
        )
    assert len(dispatcher.calls) == 1
    assert len(lifecycle.cleaned) == 1
    assert caught.value.report.trials[0].dispatch_receipt.outcome_unknown is True


@pytest.mark.asyncio
async def test_restore_failure_aborts_suite(tmp_path: Path) -> None:
    dispatcher = _Dispatcher()
    lifecycle = _Lifecycle(cleanup_safe=False)
    with pytest.raises(PayloadProbeAbort, match="RESTORATION_FAILED"):
        await _runner(tmp_path, dispatcher, lifecycle).run(
            ProbeRunConfig(run_id="payload_restore_001")
        )
    assert len(dispatcher.calls) == 1


@pytest.mark.asyncio
async def test_fresh_process_generation_must_be_unique_and_invalid_trial_is_not_dispatched(
    tmp_path: Path,
) -> None:
    dispatcher = _Dispatcher()
    lifecycle = _Lifecycle(reuse_fresh=True)
    with pytest.raises(PayloadProbeAbort):
        await _runner(tmp_path, dispatcher, lifecycle).run(
            ProbeRunConfig(run_id="payload_process_001")
        )
    assert len(lifecycle.cleaned) == len(lifecycle.prepared)
    assert len(dispatcher.calls) == len(lifecycle.prepared) - 1


@pytest.mark.asyncio
async def test_real_adapter_requires_both_confirmations(tmp_path: Path) -> None:
    dispatcher = _Dispatcher(real=True)
    runner = _runner(tmp_path, dispatcher, _Lifecycle())
    with pytest.raises(PayloadProbeConfigurationError, match="confirm_real_dispatch"):
        await runner.run(ProbeRunConfig(run_id="payload_real_0001"))
    with pytest.raises(PayloadProbeConfigurationError, match="gate elevation"):
        await runner.run(
            ProbeRunConfig(run_id="payload_real_0002", confirm_real_dispatch=True)
        )
    assert dispatcher.calls == []


def test_offline_cli_validation_has_zero_dispatches() -> None:
    payload = validate_offline(MATRIX)
    assert payload["dispatch_count"] == 0
    assert payload["target_protected_bytes"][-3:] == [36864, 37976, 40960]
