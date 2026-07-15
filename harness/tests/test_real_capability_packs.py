from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_core.capability_executor import CapabilityExecutionResult
from benchmark.real_capability_packs import (
    NIGHTLY_PACK_CAPABILITIES,
    ORACLE_SCHEMA_VERSION,
    build_capability_pack_cases,
    build_independent_oracle_script,
    run_capability_pack_suite,
    validate_real_runner_environment,
)
from fusion_agent_mcp.benchmark_bridge import FixtureIdentity, FixtureSession
from fusion_mcp_adapter.tool_result import ToolDefinition, ToolManifest, ToolResult
from fusion_tool_facade.autodesk_typed_backend import AutodeskTypedBackend


ROOT = Path(__file__).resolve().parents[2]


def test_builders_cover_every_nonexperimental_promoted_pack(tmp_path: Path) -> None:
    cases = build_capability_pack_cases(tmp_path)

    assert len(cases) == 13
    assert len({case.id for case in cases}) == len(cases)
    assert {
        capability for case in cases for capability in case.target_capabilities
    } == set(NIGHTLY_PACK_CAPABILITIES)
    assert {case.group for case in cases} == {
        "sketch_constraints_dimensions",
        "revolve_sweep_loft",
        "pattern_mirror_boolean_split",
        "joints_rigid_groups",
        "physical_properties_interference",
        "import_export",
    }
    for case in cases:
        assert case.spec.cad_spec_version == "2.0"
        assert case.spec.document_policy.modify_existing is False
        assert case.spec.document_policy.create_checkpoint is False
        assert not any(
            capability.startswith(("sheet_metal_", "cam_"))
            for capability in case.spec.capabilities
        )
        assert all(operation.requirement_ids for operation in case.spec.operations)
        assert case.spec.requirements[0].oracle == "independent"
        assert case.spec.assertions[0].kind == "custom_oracle"


def test_io_pack_is_confined_to_requested_artifact_root(tmp_path: Path) -> None:
    io_case = next(
        case
        for case in build_capability_pack_cases(tmp_path)
        if case.id == "io_step_roundtrip"
    )

    paths = [Path(path).resolve() for path in io_case.oracle_expectations["files"]]
    assert paths
    assert all(path.is_relative_to(tmp_path.resolve()) for path in paths)
    assert {path.suffix for path in paths} == {".step", ".stl"}


def test_every_pack_completes_autodesk_fixed_script_preflight(tmp_path: Path) -> None:
    class Client:
        calls: list[tuple[str, dict]] = []

        async def call_tool(self, name, arguments, *, options=None):
            del options
            self.calls.append((name, arguments))
            return ToolResult.success(message='{"success":true}')

    client = Client()
    manifest = ToolManifest(
        source="offline-autodesk-pack-test",
        tools=[
            ToolDefinition(name="fusion_mcp_read"),
            ToolDefinition(name="fusion_mcp_execute"),
        ],
    )
    backend = AutodeskTypedBackend.from_client(client, manifest)

    for case in build_capability_pack_cases(tmp_path):
        backend.preflight_operations(list(case.spec.operations))

    assert client.calls == []


def test_independent_oracle_is_exactly_bound_and_read_only() -> None:
    script = build_independent_oracle_script(
        marker="nightly_marker",
        fingerprint="f" * 64,
        expectations={"bodies": ["test_body"]},
    )

    compile(script, "<nightly-independent-oracle>", "exec")
    assert "nightly_marker" in script
    assert "f" * 64 in script
    assert ORACLE_SCHEMA_VERSION in script
    assert "document.close" not in script
    assert ".save" not in script
    assert "deleteMe" not in script
    assert "execute_code" not in script


def test_real_environment_guard_rejects_mock_or_dry_run(monkeypatch) -> None:
    monkeypatch.setenv("FUSION_AGENT_BACKEND", "autodesk_http")
    monkeypatch.setenv("FUSION_AGENT_DEFAULT_MODE", "real")
    monkeypatch.setenv("FUSION_AGENT_REQUIRE_REAL", "1")
    monkeypatch.setenv("FUSION_AGENT_ALLOW_DRY_RUN", "0")
    assert validate_real_runner_environment()["backend"] == "autodesk_http"

    monkeypatch.setenv("FUSION_AGENT_ALLOW_DRY_RUN", "1")
    with pytest.raises(RuntimeError, match="refuse"):
        validate_real_runner_environment()


def test_nightly_workflow_runs_pack_runner_and_uploads_failure_evidence() -> None:
    workflow = (ROOT / ".github" / "workflows" / "fusion-real-nightly.yml").read_text(
        encoding="utf-8"
    )

    assert "FUSION_AGENT_BACKEND: autodesk_http" in workflow
    assert 'FUSION_AGENT_REQUIRE_REAL: "1"' in workflow
    assert (
        "python scripts/run-real-capability-packs.py --artifact-root nightly-artifacts"
        in workflow
    )
    assert "nightly-artifacts/capability-packs.json" in workflow
    assert "if: always()" in workflow
    for path in (
        "manifests/**",
        "logs/**",
        "benchmark_parametric_suite/cases/*/images/**",
    ):
        assert path in workflow


class _OfflineRuntime:
    def __init__(self, *, fail_execution: bool = False) -> None:
        self.fail_execution = fail_execution
        self.executed_specs = []
        self.oracle_calls = 0

    async def execute_cad_spec_v2(self, spec, *, mode: str, dry_run: bool = False):
        assert mode == "real"
        assert dry_run is False
        self.executed_specs.append(spec)
        if self.fail_execution:
            raise RuntimeError("offline injected executor failure")
        return CapabilityExecutionResult(
            success=True,
            provider="autodesk_http",
            dry_run=False,
            required_capabilities=sorted(spec.capabilities),
            available_capabilities=sorted(spec.capabilities),
            transactions=[
                {
                    "operation_id": operation.id,
                    "kind": operation.kind,
                    "status": "ok",
                    "provider": "autodesk_http",
                    "requirement_ids": list(operation.requirement_ids),
                    "native_result": {"offline": True},
                }
                for operation in spec.operations
            ],
        )

    async def _call_trusted_native_real(
        self,
        name,
        arguments,
        *,
        semantics,
        operation_id,
    ):
        assert name == "fusion_mcp_execute"
        assert arguments["featureType"] == "script"
        assert semantics == "read_only"
        assert operation_id.endswith(":independent-oracle")
        self.oracle_calls += 1
        return ToolResult.success(
            message=json.dumps(
                {
                    "ok": True,
                    "schema_version": ORACLE_SCHEMA_VERSION,
                    "passed": True,
                    "checks": [{"id": "offline", "passed": True}],
                    "requirement_ids": ["independent_pack_contract"],
                    "evidence_source": "independent_programmatic_readback",
                }
            )
        )


class _OfflineLifecycle:
    def __init__(self) -> None:
        self.active_id = "data:user-document"
        self.open_ids = [self.active_id]
        self.events: list[str] = []

    async def read_active_document_id(self):
        return self.active_id

    async def list_open_document_ids(self):
        return list(self.open_ids)

    async def prepare_fixture(self, context):
        self.events.append("prepare")
        fixture_id = f"session:{context.trial_id}"
        self.active_id = fixture_id
        self.open_ids.append(fixture_id)
        return FixtureSession(
            original_document_id="data:user-document",
            fixture_document_id=fixture_id,
            fixture_marker=context.fixture_marker,
            fixture_fingerprint="a" * 64,
            unsaved=True,
        )

    async def read_fixture_identity(self, context, session):
        self.events.append("identity")
        return FixtureIdentity(
            document_id=session.fixture_document_id,
            fixture_marker=context.fixture_marker,
            fixture_fingerprint=session.fixture_fingerprint,
            unsaved=True,
        )

    async def close_fixture_without_save(self, context, session):
        del context
        self.events.append("close_without_save")
        self.open_ids.remove(session.fixture_document_id)
        self.active_id = session.original_document_id
        return True

    async def restore_original_document(self, context, session):
        del context
        self.events.append("restore")
        self.active_id = session.original_document_id
        return True


@pytest.mark.asyncio
async def test_offline_suite_uses_fixture_execute_oracle_cleanup_order(
    tmp_path: Path,
) -> None:
    runtime = _OfflineRuntime()
    lifecycle = _OfflineLifecycle()
    case = build_capability_pack_cases(tmp_path)[0]

    result = await run_capability_pack_suite(
        runtime=runtime,
        lifecycle=lifecycle,
        cases=(case,),
        artifact_root=tmp_path,
        environment={"backend": "autodesk_http"},
    )

    assert result["status"] == "passed"
    assert result["fixture_policy"] == "disposable_unsaved_only"
    assert result["save_user_documents"] is False
    assert result["cases"][0]["oracle"]["passed"] is True
    assert result["cases"][0]["cleanup"]["passed"] is True
    assert result["restoration"]["passed"] is True
    assert lifecycle.events == ["prepare", "identity", "close_without_save", "restore"]
    assert runtime.oracle_calls == 1
    persisted = json.loads(
        (tmp_path / "capability-packs.json").read_text(encoding="utf-8")
    )
    assert persisted["status"] == "passed"


@pytest.mark.asyncio
async def test_executor_failure_still_closes_without_save_and_restores(
    tmp_path: Path,
) -> None:
    runtime = _OfflineRuntime(fail_execution=True)
    lifecycle = _OfflineLifecycle()
    case = build_capability_pack_cases(tmp_path)[0]

    result = await run_capability_pack_suite(
        runtime=runtime,
        lifecycle=lifecycle,
        cases=(case,),
        artifact_root=tmp_path,
    )

    assert result["status"] == "failed"
    assert result["cases"][0]["status"] == "failed"
    assert result["cases"][0]["mutation_outcome"] == "unknown"
    assert result["cases"][0]["automatic_replay_suppressed"] is True
    assert result["cases"][0]["post_failure_readback_attempted"] is True
    assert result["cases"][0]["oracle"]["passed"] is True
    assert result["cases"][0]["cleanup"]["passed"] is True
    assert result["cases"][0]["disposable_fixture_destroyed"] is True
    assert result["restoration"]["passed"] is True
    assert lifecycle.events[-2:] == ["close_without_save", "restore"]
    assert runtime.oracle_calls == 1
