from __future__ import annotations

import json
import math
import os
from pathlib import Path

import pytest

from benchmark.provenance import RevisionIdentity
from benchmark.filesystem import io_path, read_text
from benchmark_parametric_suite import run_reference_suite as reference_runner
from benchmark_parametric_suite.payload_probe.calibration import CalibratedScript
from benchmark_parametric_suite.payload_probe.models import (
    CanaryContract,
    CleanupReceipt,
    DispatchReceipt,
    DispatcherCapabilities,
    PayloadProbeMatrix,
    PayloadTarget,
    ProbeReadback,
    ProbeRunConfig,
    ProbeTrialContext,
    ProbeTrialFixture,
)
from benchmark_parametric_suite.payload_probe.runner import (
    PayloadProbeAbort,
    PayloadProbeRunner,
    _validate_dispatch_receipt,
    classify_probe_observation,
)
from fusion_agent_mcp.benchmark_bridge import _close_fixture_script


ROOT = Path(__file__).resolve().parents[1]
PRIVATE_CANARY = "PRIVATE_TOKEN=C:\\Users\\alice\\secret argv=--bearer-secret"
STRICT_ORACLES = [
    ROOT / "benchmark_parametric_ab" / "oracle_script.py",
    *sorted(
        (ROOT / "benchmark_parametric_suite" / "cases").glob("*/*oracle_script.py")
    ),
]


def test_close_fixture_requires_document_marker_and_fingerprint_identity() -> None:
    script = _close_fixture_script(
        "marker:expected", "expected-marker", "expected-fingerprint"
    )
    assert "if marker_matches and fingerprint_matches:" in script
    assert "if _stable_document_key(target) != _DOCUMENT_ID:" in script
    assert "if marker_matches or fingerprint_matches:" not in script
    assert "if marker_matches and _stable_document_key" not in script


def test_reference_oracle_wrapper_binds_exact_fixture_before_oracle_body() -> None:
    raw = "import json\n\ndef run(_context: str):\n    print(json.dumps({'passed': True}))\n"
    bound = reference_runner._bind_oracle_script(
        raw,
        document_id="marker:fixture",
        marker="fixture",
        fingerprint="f" * 64,
    )
    assert "ORACLE_FIXTURE_IDENTITY_MISMATCH" in bound
    assert '"marker:fixture"' in bound
    assert '"fixture"' in bound
    assert '"' + ("f" * 64) + '"' in bound
    assert bound.index("ORACLE_FIXTURE_IDENTITY_MISMATCH") < bound.index(
        "'passed': True"
    )


def test_b06_oracles_bind_body_ownership_and_joint_endpoints() -> None:
    initial = (
        ROOT
        / "benchmark_parametric_suite/cases/b06_robot_arm_assembly/oracle_script.py"
    ).read_text(encoding="utf-8")
    eco = (
        ROOT
        / "benchmark_parametric_suite/cases/b06_robot_arm_assembly/eco_oracle_script.py"
    ).read_text(encoding="utf-8")
    for source in (initial, eco):
        assert "expected_body_owners" in source
        assert "occurrenceOne" in source
        assert "occurrenceTwo" in source
        assert "expected_joint_endpoints" in source


def test_b05_oracles_require_one_connected_root_body_in_both_phases() -> None:
    case_root = ROOT / "benchmark_parametric_suite/cases/b05_spherical_lattice_radome"
    initial = (case_root / "oracle_script.py").read_text(encoding="utf-8")
    eco = (case_root / "eco_oracle_script.py").read_text(encoding="utf-8")

    assert "body = bodies[0] if len(bodies) == 1 else None" in initial
    assert 'body.name == "B01_Spherical_Lattice_Radome"' in initial
    assert "body = bodies[0] if len(bodies) == 1 else None" in eco
    assert "body.isValid and body.isSolid and body.lumps.count == 1" in eco


def test_b07_oracles_bind_body_ownership_and_exact_joint_graph() -> None:
    case_root = ROOT / "benchmark_parametric_suite/cases/b07_packaging_machine"
    for name in ("oracle_script.py", "eco_oracle_script.py"):
        source = (case_root / name).read_text(encoding="utf-8")
        assert "expected_body_owners" in source
        assert "ownership_errors" in source
        assert 'graph["endpoints"] == expected_joint_endpoints' in source


@pytest.mark.parametrize(
    "path", STRICT_ORACLES, ids=lambda path: path.parent.name + ":" + path.name
)
def test_reference_oracles_use_strict_json_numeric_serialization(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    assert "allow_nan=False" in source


def test_strict_json_control_rejects_non_finite_but_accepts_finite() -> None:
    with pytest.raises(ValueError):
        json.dumps({"passed": True, "metric": math.inf}, allow_nan=False)
    assert json.loads(json.dumps({"passed": True, "metric": 1.25}, allow_nan=False))[
        "passed"
    ]


def test_payload_probe_non_finite_receipt_is_never_complete() -> None:
    with pytest.raises(ValueError, match="INVALID_NUMERIC_EVIDENCE"):
        _validate_dispatch_receipt(
            DispatchReceipt(
                mutating_dispatch_count=1,
                transport_succeeded=True,
                native_success=True,
                duration_ms=math.inf,
            )
        )
    canaries = CanaryContract.for_trial(run_id="projection01", trial_id="trial01")
    context = ProbeTrialContext(
        run_id="projection01",
        trial_id="trial01",
        target_id="small",
        target_protected_bytes=1,
        repetition=0,
        warmup=False,
        process_mode="same_process",
        sequence_index=0,
        canaries=canaries,
    )
    fixture = ProbeTrialFixture(
        trial_id=context.trial_id,
        document_id="private-document-id",
        original_document_id=None,
        fixture_marker=canaries.fixture_marker,
        baseline_fingerprint="baseline",
        process_generation="generation",
        baseline_canaries_clean=True,
        unsaved=True,
    )
    readback = ProbeReadback(
        document_id=fixture.document_id,
        fixture_marker=canaries.fixture_marker,
        observed_trial_id=context.trial_id,
        start_value=canaries.start_value,
        mutation_value=canaries.mutation_value,
        end_value=canaries.end_value,
        state_fingerprint="changed",
        expected_change_complete=True,
    )
    classification, reasons = classify_probe_observation(
        context=context,
        fixture=fixture,
        receipt=DispatchReceipt(
            mutating_dispatch_count=1,
            transport_succeeded=False,
            native_success=False,
            outcome_unknown=True,
            error_code="INVALID_NUMERIC_EVIDENCE",
        ),
        readback=readback,
    )
    assert classification.value == "contaminated"
    assert "dispatch receipt contained invalid numeric evidence" in reasons


def test_tracked_reference_results_do_not_retain_private_runtime_payloads() -> None:
    tracked = list(
        (ROOT / "benchmark_parametric_suite").glob("cases/*/reference_result.json")
    ) + [ROOT / "benchmark_parametric_suite/reference_suite_result.json"]
    assert not [path for path in tracked if path.exists()]
    assert not (ROOT / "benchmark_parametric_ab/results.json").exists()


class _ProbeCalibrator:
    def calibrate(self, **_kwargs):  # noqa: ANN003
        return CalibratedScript(
            raw_script="def run(_context):\n    return None\n",
            protected_script="def run(_context):\n    return None\n",
            padding_bytes=0,
            original_payload_bytes=1,
            protected_payload_bytes=1,
            original_payload_sha256="a" * 64,
            protected_payload_sha256="b" * 64,
            ast_topology_sha256="c" * 64,
            padding_invariant_sha256="d" * 64,
        )


class _ProbeDispatcher:
    capabilities = DispatcherCapabilities(
        adapter_id="test",
        real=False,
        configured_payload_limit_bytes=1024,
    )

    async def dispatch_once(self, _request):  # noqa: ANN001
        raise RuntimeError(PRIVATE_CANARY)


class _ProbeLifecycle:
    async def prepare_trial(self, context):  # noqa: ANN001
        return ProbeTrialFixture(
            trial_id=context.trial_id,
            document_id="private-document-id",
            original_document_id="private-original-id",
            fixture_marker=context.canaries.fixture_marker,
            baseline_fingerprint="baseline",
            process_generation="generation",
            baseline_canaries_clean=True,
            unsaved=True,
        )

    async def readback(self, context, fixture):  # noqa: ANN001
        return ProbeReadback(
            document_id=fixture.document_id,
            fixture_marker=fixture.fixture_marker,
            observed_trial_id=context.trial_id,
            start_value=context.canaries.start_value,
            mutation_value=None,
            end_value=None,
            state_fingerprint=fixture.baseline_fingerprint,
            expected_change_complete=False,
            warnings=(PRIVATE_CANARY,),
        )

    async def cleanup_trial(self, _context, _fixture):  # noqa: ANN001
        return CleanupReceipt(
            document_closed=True,
            saved=False,
            original_document_restored=True,
            restoration_fingerprint_matches=True,
            open_documents_match=True,
            errors=(PRIVATE_CANARY,),
        )


@pytest.mark.asyncio
async def test_payload_probe_artifact_projects_errors_and_private_identity(
    tmp_path: Path,
) -> None:
    matrix = PayloadProbeMatrix(
        schema_version="fusion_executor_payload_probe.matrix.v1",
        experiment_id="security_projection",
        payload_metric="utf8_bytes",
        warmups=0,
        repetitions=1,
        seed=1,
        process_modes=("same_process",),
        targets=(PayloadTarget(id="small", target_protected_bytes=1),),
        historical_observations=(),
        retry_policy="never",
        mutating_dispatches_per_trial=1,
        abort_on=("partial",),
    )
    runner = PayloadProbeRunner(
        matrix=matrix,
        calibrator=_ProbeCalibrator(),
        dispatcher=_ProbeDispatcher(),
        lifecycle=_ProbeLifecycle(),
        output_root=tmp_path,
    )
    with pytest.raises(PayloadProbeAbort):
        await runner.run(ProbeRunConfig(run_id="projection01"))

    serialized = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (tmp_path / "projection01").glob("*")
        if path.is_file()
    )
    assert PRIVATE_CANARY not in serialized
    assert "private-document-id" not in serialized
    assert "private-original-id" not in serialized
    assert "fixture_" not in serialized


def test_reference_revision_mismatch_blocks_before_runtime_creation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_root = tmp_path
    for index in range(5):
        output_root /= f"reference-run-root-{index}-" + ("r" * 55)
    assert len(str(output_root.resolve())) > 320

    identity = RevisionIdentity(
        expected_git_commit="a" * 40,
        observed_git_commit="c" * 40,
        expected_source_manifest_sha256="b" * 64,
        observed_source_manifest_sha256="b" * 64,
        tracked_state="clean",
    )
    monkeypatch.setattr(
        reference_runner, "collect_workspace_revision", lambda *_a, **_k: identity
    )
    runtime_calls = 0

    def runtime_factory(**_kwargs):  # noqa: ANN003
        nonlocal runtime_calls
        runtime_calls += 1
        raise AssertionError("runtime must not be created")

    monkeypatch.setattr(reference_runner, "FusionAgentRuntime", runtime_factory)
    with pytest.raises(ValueError, match="revision"):
        reference_runner.asyncio.run(
            reference_runner._main(
                ["b02_vented_enclosure"],
                git_commit="a" * 40,
                source_manifest_sha256="b" * 64,
                nightly_run_identity="1-1",
                output_root=output_root,
            )
        )
    assert runtime_calls == 0
    run_directories = [
        Path(entry.path)
        for entry in os.scandir(io_path(output_root))
        if entry.is_dir(follow_symlinks=False)
    ]
    assert len(run_directories) == 1
    report = json.loads(read_text(run_directories[0] / "reference_suite_result.json"))
    assert report["status"] == "aborted"
    assert report["error"]["code"] == "REVISION_IDENTITY_MISMATCH"
