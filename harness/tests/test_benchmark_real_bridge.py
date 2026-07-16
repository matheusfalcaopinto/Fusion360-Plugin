from __future__ import annotations

import json
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from benchmark.models import BenchmarkRunConfig, ExecutionObservation
from benchmark.runner import (
    BenchmarkExecutionError,
    BenchmarkRunner,
    IndependentEvidence,
)
from fusion_agent_mcp import server
from fusion_agent_mcp.benchmark_bridge import (
    CANONICAL_ALL_CAPABILITY,
    COMMON_CAPABILITIES,
    ROUTE_CAPABILITIES,
    ContainmentAudit,
    FixtureIdentity,
    FixtureSession,
    FusionRuntimeLifecycleBackend,
    FusionRuntimeBenchmarkBridge,
)
from fusion_agent_mcp.runtime import FusionAgentRuntime
from fusion_mcp_adapter.tool_result import ToolResult


def _suite(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": "benchmark_suite.v2",
                "suite_id": "real_bridge_test",
                "title": "Real bridge containment test",
                "description": "One read-only case for fake-runtime lifecycle tests.",
                "cases": [
                    {
                        "id": "document_summary",
                        "prompt": "Read the isolated benchmark document.",
                        "category": "native_read",
                        "risk": "read_only",
                        "timeout_seconds": 30.0,
                        "fixture_id": "sample_design_medium",
                        "script_id": "read_document_summary",
                        "oracle_id": "document_summary",
                        "execution_paths": ["safe_harness", "native_fast"],
                        "expectations": {
                            "expected_status": "read_succeeded",
                            "should_succeed": True,
                            "max_call_count": 5,
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


class FakeRealBackend:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.active_document_id: str | None = "original-doc"
        self.open_document_ids = {"original-doc"}
        self.identity_marker_override: str | None = None
        self.close_ok = True
        self.restore_ok = True
        self.leave_fixture_open = False

    def capabilities(self) -> set[str]:
        return (
            set(COMMON_CAPABILITIES)
            | set(ROUTE_CAPABILITIES.values())
            | {CANONICAL_ALL_CAPABILITY}
        )

    async def prepare_fixture(self, context) -> FixtureSession:
        self.calls.append(f"prepare:{context.execution_path}")
        self.active_document_id = f"fixture:{context.trial_id}"
        self.open_document_ids.add(self.active_document_id)
        return FixtureSession(
            original_document_id="original-doc",
            fixture_document_id=self.active_document_id,
            fixture_marker=context.fixture_marker,
            fixture_fingerprint=f"sha256:{context.trial_id}",
            unsaved=True,
        )

    async def read_fixture_identity(self, context, session) -> FixtureIdentity:
        self.calls.append("identity")
        return FixtureIdentity(
            document_id=self.active_document_id,
            fixture_marker=self.identity_marker_override or context.fixture_marker,
            fixture_fingerprint=session.fixture_fingerprint,
            unsaved=True,
        )

    async def execute_safe_harness(self, context, session) -> ExecutionObservation:
        del context, session
        self.calls.append("execute:safe_harness")
        return self._executor_claim()

    async def execute_native_fast(self, context, session) -> ExecutionObservation:
        del context, session
        self.calls.append("execute:native_fast")
        return self._executor_claim()

    async def observe_oracle(self, context, session) -> IndependentEvidence:
        del context, session
        self.calls.append("observe")
        # Independent evidence intentionally contradicts the executor claim.
        return IndependentEvidence(
            observation={"document": {"name": "benchmark_fixture", "body_count": 8}},
            metrics={"call_count": 1},
            trace={"source": "fake-independent-runtime"},
        )

    async def close_fixture_without_save(self, context, session) -> bool:
        del context
        self.calls.append("close")
        if self.close_ok:
            self.active_document_id = None
            if not self.leave_fixture_open:
                self.open_document_ids.discard(session.fixture_document_id)
        return self.close_ok

    async def restore_original_document(self, context, session) -> bool:
        del context
        self.calls.append("restore")
        if self.restore_ok:
            self.active_document_id = session.original_document_id
            if session.original_document_id is not None:
                self.open_document_ids.add(session.original_document_id)
        return self.restore_ok

    async def read_active_document_id(self) -> str | None:
        self.calls.append("restore_readback")
        return self.active_document_id

    async def list_open_document_ids(self) -> list[str]:
        self.calls.append("list_open")
        return sorted(self.open_document_ids)

    async def containment_audit(self, context, session) -> ContainmentAudit:
        del context, session
        self.calls.append("audit")
        return ContainmentAudit()

    @staticmethod
    def _executor_claim() -> ExecutionObservation:
        return ExecutionObservation(
            status="read_succeeded",
            execution_success=True,
            duration_ms=12,
            call_count=1,
            # These claims are deliberately false. The lifecycle must replace
            # them and the independent oracle must ignore observation below.
            fixture_marker_verified=False,
            fingerprint_verified=False,
            closed_without_save=False,
            restored=False,
            save_count=99,
            observation={"document": {"name": "executor_claim", "body_count": 0}},
        )


def _real_runner(tmp_path: Path, backend: FakeRealBackend) -> BenchmarkRunner:
    runtime = FusionAgentRuntime(
        manifest_root=tmp_path / "manifests",
        outputs_root=tmp_path / "runtime_outputs",
        real_benchmark_backend=backend,
    )
    bridge = FusionRuntimeBenchmarkBridge(runtime)
    return BenchmarkRunner(
        output_dir=tmp_path / "outputs",
        route_executors=bridge.route_executors,
        oracle_observer=bridge,
        real_lifecycle=bridge,
    )


@pytest.mark.asyncio
async def test_real_bridge_runs_both_routes_and_uses_independent_oracle(
    tmp_path: Path,
) -> None:
    backend = FakeRealBackend()
    runner = _real_runner(tmp_path, backend)

    run = await runner.run_suite(
        _suite(tmp_path / "suite.json"),
        config=BenchmarkRunConfig(
            mode="real",
            driver="internal",
            execution_paths=["safe_harness", "native_fast"],
        ),
    )

    assert {trial.execution_path for trial in run.report.trials} == {
        "safe_harness",
        "native_fast",
    }
    assert all(trial.oracle.passed for trial in run.report.trials)
    assert backend.calls.count("execute:safe_harness") == 1
    assert backend.calls.count("execute:native_fast") == 1
    assert backend.calls.count("observe") == 2
    assert backend.calls.count("close") == 2
    assert backend.calls.count("restore") == 2
    for trial in run.report.trials:
        assert trial.metrics["save_count"] == 0
        assert trial.metrics["closed_without_save"] is True
        assert trial.metrics["restored"] is True
        assert trial.metrics["execution_ms"] > 0
        assert trial.metrics["call_ms"] == 0
        assert trial.metrics["independent_metric_fields"] == ["call_count"]
        assert 0 <= trial.metrics["restoration_ms"] <= trial.metrics["teardown_ms"]
        accounted = sum(
            trial.metrics[name]
            for name in (
                "queue_wait_ms",
                "setup_ms",
                "execution_ms",
                "verification_ms",
                "teardown_ms",
            )
        )
        assert trial.metrics["duration_ms"] >= accounted


@pytest.mark.asyncio
async def test_marker_mismatch_blocks_route_but_still_closes_and_restores(
    tmp_path: Path,
) -> None:
    backend = FakeRealBackend()
    backend.identity_marker_override = "wrong-marker"
    runner = _real_runner(tmp_path, backend)

    with pytest.raises(
        BenchmarkExecutionError,
        match="blocked before route dispatch.*fixture marker mismatch",
    ):
        await runner.run_suite(
            _suite(tmp_path / "suite.json"),
            config=BenchmarkRunConfig(mode="real", execution_paths=["safe_harness"]),
        )

    assert not any(call.startswith("execute:") for call in backend.calls)
    assert "observe" not in backend.calls
    assert backend.calls[-5:] == [
        "close",
        "restore",
        "restore_readback",
        "list_open",
        "audit",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_field", "message"),
    [
        ("close_ok", "not closed without save"),
        ("restore_ok", "not restored"),
    ],
)
async def test_close_or_restore_failure_aborts_suite(
    tmp_path: Path,
    failure_field: str,
    message: str,
) -> None:
    backend = FakeRealBackend()
    setattr(backend, failure_field, False)
    runner = _real_runner(tmp_path, backend)

    with pytest.raises(BenchmarkExecutionError, match=message):
        await runner.run_suite(
            _suite(tmp_path / "suite.json"),
            config=BenchmarkRunConfig(
                mode="real",
                execution_paths=["safe_harness", "native_fast"],
            ),
        )

    assert sum(call.startswith("execute:") for call in backend.calls) == 1


@pytest.mark.asyncio
async def test_close_boolean_is_rejected_when_fixture_remains_in_list_open(
    tmp_path: Path,
) -> None:
    backend = FakeRealBackend()
    backend.leave_fixture_open = True
    runner = _real_runner(tmp_path, backend)

    with pytest.raises(BenchmarkExecutionError, match="not closed without save"):
        await runner.run_suite(
            _suite(tmp_path / "suite.json"),
            config=BenchmarkRunConfig(mode="real", execution_paths=["safe_harness"]),
        )

    assert "list_open" in backend.calls


@pytest.mark.asyncio
async def test_stock_runtime_fails_capability_preflight_before_dispatch(
    tmp_path: Path,
) -> None:
    runtime = FusionAgentRuntime(
        manifest_root=tmp_path / "manifests", outputs_root=tmp_path / "runtime"
    )
    bridge = FusionRuntimeBenchmarkBridge(runtime)
    runner = BenchmarkRunner(
        output_dir=tmp_path / "outputs",
        route_executors=bridge.route_executors,
        oracle_observer=bridge,
        real_lifecycle=bridge,
    )

    with pytest.raises(
        BenchmarkExecutionError,
        match="REAL_BENCHMARK_CAPABILITY_MISSING:.*canonical_real_action:read_document_summary:safe_harness.*no real benchmark action",
    ):
        await runner.run_suite(
            _suite(tmp_path / "suite.json"),
            config=BenchmarkRunConfig(mode="real", execution_paths=["safe_harness"]),
        )
    assert runtime.real_client.connection_generation == 0
    await runtime.close()


class FakeLifecycleRuntime:
    def __init__(self, marker: str) -> None:
        self.marker = marker
        self.fingerprint = hashlib.sha256(marker.encode("utf-8")).hexdigest()
        self.active_id: str | None = "data:original-doc"
        self.document_present = True
        self.open_ids = ["data:original-doc"]
        self.calls: list[tuple[str, str, str]] = []

    async def _call_trusted_native_real(
        self,
        name: str,
        arguments: dict,
        *,
        semantics: str,
        operation_id: str,
    ) -> ToolResult:
        return self._respond(name, arguments, semantics, operation_id)

    async def _call_native_real(
        self,
        name: str,
        arguments: dict,
        *,
        semantics: str,
        operation_id: str,
    ) -> ToolResult:
        return self._respond(name, arguments, semantics, operation_id)

    def _respond(
        self,
        name: str,
        arguments: dict,
        semantics: str,
        operation_id: str,
    ) -> ToolResult:
        script = arguments["object"]["script"]
        self.calls.append((operation_id, semantics, script))
        if operation_id.endswith(":prepare"):
            self.active_id = "fixture-doc"
            self.open_ids.append("fixture-doc")
            return ToolResult.success(
                ok=True,
                original_document_id="data:original-doc",
                original_open_document_ids=["data:original-doc"],
                fixture_document_id="fixture-doc",
                fixture_marker=self.marker,
                fixture_fingerprint=self.fingerprint,
                unsaved=True,
            )
        if operation_id.endswith(":identity"):
            return ToolResult.success(
                ok=True,
                document_id=self.active_id,
                fixture_marker=self.marker,
                fixture_fingerprint=self.fingerprint,
                unsaved=True,
            )
        if operation_id.endswith(":close"):
            self.open_ids.remove("fixture-doc")
            self.active_id = None
            return ToolResult.success(ok=True, found=True, closed=True)
        if operation_id.endswith(":restore"):
            self.active_id = "data:original-doc"
            return ToolResult.success(ok=True, restored=True)
        if operation_id.endswith(":list-open"):
            return ToolResult.success(ok=True, document_ids=list(self.open_ids))
        if operation_id.endswith(":active-document"):
            return ToolResult.success(
                ok=True,
                document_present=self.document_present,
                document_id=self.active_id,
            )
        raise AssertionError(f"unexpected lifecycle operation: {operation_id}")


@pytest.mark.asyncio
async def test_stock_lifecycle_backend_uses_exact_runtime_identity_and_close_without_save() -> (
    None
):
    marker = "fusion_agent_trial_bench_stock_lifecycle_001"
    runtime = FakeLifecycleRuntime(marker)
    backend = FusionRuntimeLifecycleBackend(runtime)
    context = SimpleNamespace(
        trial_id="stock_lifecycle_001",
        fixture_marker=marker,
        fixture=SimpleNamespace(id="empty_design"),
    )

    assert CANONICAL_ALL_CAPABILITY not in backend.capabilities()
    assert not set(ROUTE_CAPABILITIES.values()).intersection(backend.capabilities())
    session = await backend.prepare_fixture(context)
    assert session.original_document_id == "data:original-doc"
    assert session.metadata["original_open_document_ids"] == ["data:original-doc"]
    assert session.fixture_document_id == "fixture-doc"
    identity = await backend.read_fixture_identity(context, session)
    assert identity.document_id == "fixture-doc"
    assert identity.fixture_marker == marker
    assert await backend.close_fixture_without_save(context, session) is True
    assert await backend.restore_original_document(context, session) is True
    assert await backend.read_active_document_id() == "data:original-doc"
    assert await backend.list_open_document_ids() == ["data:original-doc"]

    mutation_scripts = [
        script for _, semantics, script in runtime.calls if semantics == "mutating"
    ]
    assert any(
        "attributes.add" in script and "_stable_document_key(created)" in script
        for script in mutation_scripts
    )
    assert any(
        "finally:" in script and "created.close(False)" in script
        for script in mutation_scripts
    )
    assert any(
        '"found": False, "closed": False' in script for script in mutation_scripts
    )
    assert any("fixture_fingerprint" in script for script in mutation_scripts)
    assert all("str(id(" not in script for _, _, script in runtime.calls)
    assert any("target.close(False)" in script for script in mutation_scripts)
    assert any("target.activate()" in script for script in mutation_scripts)


@pytest.mark.asyncio
async def test_stock_lifecycle_blocks_unidentified_unsaved_original_before_creation() -> (
    None
):
    marker = "fusion_agent_trial_unstable_original"
    runtime = FakeLifecycleRuntime(marker)
    runtime.active_id = None
    runtime.document_present = True
    backend = FusionRuntimeLifecycleBackend(runtime)
    context = SimpleNamespace(
        trial_id="unstable_original",
        fixture_marker=marker,
        fixture=SimpleNamespace(id="empty_design"),
    )

    with pytest.raises(BenchmarkExecutionError, match="no stable data identity"):
        await backend.prepare_fixture(context)

    assert not any(
        operation_id.endswith(":prepare") for operation_id, _, _ in runtime.calls
    )


@pytest.mark.asyncio
async def test_stock_lifecycle_rejects_stale_marker_identity_as_original() -> None:
    marker = "fusion_agent_trial_stale_marker"
    runtime = FakeLifecycleRuntime(marker)
    runtime.active_id = f"marker:{marker}"
    backend = FusionRuntimeLifecycleBackend(runtime)

    with pytest.raises(
        BenchmarkExecutionError, match="not backed by a saved Fusion data file"
    ):
        await backend.read_active_document_id()


@pytest.mark.asyncio
async def test_server_injects_shared_runtime_real_bridge(
    monkeypatch, tmp_path: Path
) -> None:
    suite = _suite(tmp_path / "suite.json")
    monkeypatch.setattr(server, "_default_benchmark_suite", lambda: suite)
    monkeypatch.setattr(server, "OUTPUTS_ROOT", tmp_path / "outputs")
    backend = FakeRealBackend()
    runtime = FusionAgentRuntime(
        manifest_root=tmp_path / "manifests",
        outputs_root=tmp_path / "runtime",
        real_benchmark_backend=backend,
    )

    result = await server.execute_tool(
        "fusion_agent_run_benchmark",
        {
            "driver": "internal",
            "mode": "real",
            "execution_paths": ["safe_harness", "native_fast"],
            "repetitions": 1,
        },
        runtime=runtime,
    )

    assert result["trial_count"] == 2
    assert backend.calls.count("execute:safe_harness") == 1
    assert backend.calls.count("execute:native_fast") == 1
    await runtime.close()
