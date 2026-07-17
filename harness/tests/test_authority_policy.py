from __future__ import annotations

import json
import os
import subprocess
import types
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from agent_core import authority as authority_module
from agent_core.authority import (
    AuthorityBroker,
    AuthorityDeniedError,
    AuthorityPolicy,
    CadTargetBinding,
    CapabilityLedger,
    HostOutputDisabledError,
    OperationCapability,
)
from agent_core.capability_executor import CapabilityExecutor
from cad_spec.v2 import CadSpecV2
from fusion_mcp_adapter.tool_result import ToolDefinition, ToolManifest, ToolResult
from fusion_tool_facade import vendor_facade as vendor_facade_module
from fusion_tool_facade.autodesk_typed_backend import AutodeskTypedBackend
from fusion_agent_mcp import runtime as runtime_module
from fusion_agent_mcp.runtime import FusionAgentRuntime, RuntimeConfiguration


def _policy_file(tmp_path: Path, *, allow_overwrite: bool = False) -> Path:
    import_root = tmp_path / "imports"
    export_root = tmp_path / "exports"
    import_root.mkdir()
    export_root.mkdir()
    path = tmp_path / "authority.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "fusion_agent.authority_policy.v1",
                "import_roots": [
                    {
                        "id": "approved-imports",
                        "path": str(import_root),
                        "formats": ["step", "stp"],
                        "default": True,
                    }
                ],
                "export_roots": [
                    {
                        "id": "approved-exports",
                        "path": str(export_root),
                        "formats": ["step", "stp", "stl"],
                        "default": True,
                    }
                ],
                "allow_overwrite": allow_overwrite,
                "capability_ttl_seconds": 1800,
            }
        ),
        encoding="utf-8",
    )
    return path


def _export_spec(
    path: str | None = None, *, file_ref: dict | None = None, overwrite: bool = False
) -> CadSpecV2:
    operation = {
        "id": "export_part",
        "kind": "io.export",
        "target_ref": "part_body",
        "format": "step",
        "overwrite": overwrite,
        "requirement_ids": ["exported"],
    }
    if path is not None:
        operation["path"] = path
    if file_ref is not None:
        operation["file_ref"] = file_ref
    return CadSpecV2.model_validate(
        {
            "cad_spec_version": "2.0",
            "intent": "Export one approved artifact",
            "requirements": [
                {
                    "id": "exported",
                    "description": "artifact exists",
                    "assertion_ids": ["export_exists"],
                }
            ],
            "operations": [operation],
            "assertions": [
                {
                    "id": "export_exists",
                    "kind": "export_exists",
                    "target_ref": "part_body",
                }
            ],
        }
    )


def _binding(reference: str = "part_body") -> CadTargetBinding:
    return CadTargetBinding(
        reference_kind="export_target",
        requested_ref=reference,
        document_identity="d" * 64,
        entity_identity="e" * 64,
        fingerprint="a" * 64,
    )


def _prepare_graph(broker: AuthorityBroker, spec: CadSpecV2, **kwargs):
    targets = {}
    for operation in spec.operations:
        if operation.kind == "io.export":
            targets[operation.id] = (_binding(str(operation.target_ref)),)
        elif not operation.kind.startswith("analysis."):
            targets[operation.id] = (
                CadTargetBinding(
                    reference_kind="active_document",
                    requested_ref="active_document",
                    document_identity="d" * 64,
                    entity_identity="e" * 64,
                    fingerprint="f" * 64,
                ),
            )
    return broker.prepare_graph(spec, target_bindings_by_operation=targets, **kwargs)


def _import_spec(path: str) -> CadSpecV2:
    return CadSpecV2.model_validate(
        {
            "cad_spec_version": "2.0",
            "intent": "Import one approved artifact",
            "requirements": [
                {
                    "id": "imported",
                    "description": "component exists",
                    "assertion_ids": ["component_exists"],
                }
            ],
            "operations": [
                {
                    "id": "import_part",
                    "kind": "io.import",
                    "path": path,
                    "format": "step",
                    "component_name": "ImportedPart",
                    "requirement_ids": ["imported"],
                }
            ],
            "assertions": [
                {
                    "id": "component_exists",
                    "kind": "entity_exists",
                    "target_ref": "ImportedPart",
                }
            ],
        }
    )


class BoundBackend:
    # Broker/capability unit tests use the mock compatibility path. All real
    # providers are covered separately by deny_io zero-dispatch regressions.
    provider = "mock"
    capabilities = {"export_step"}

    def __init__(self) -> None:
        self.preflighted = []
        self.calls = []
        self.resolve_calls = []

    async def resolve_cad_target_binding(self, operation):
        self.resolve_calls.append(operation.id)
        return _binding(str(operation.target_ref))

    def preflight_bound_operations(self, operations):
        self.preflighted = list(operations)

    async def execute_bound_operation(self, operation):
        self.calls.append(operation)
        return {"success": True}


class UnknownOutcomeError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("provider disconnected after dispatch")
        self.transport = {
            "dispatched": True,
            "may_have_applied": True,
            "mutation_outcome": "unknown",
        }


class UnknownOutcomeBackend(BoundBackend):
    async def execute_bound_operation(self, operation):
        self.calls.append(operation)
        raise UnknownOutcomeError


class MissingBindingBackend:
    provider = "mock"
    capabilities = {"export_step"}

    def __init__(self) -> None:
        self.preflighted = []
        self.calls = []

    def preflight_bound_operations(self, operations):
        self.preflighted = list(operations)

    async def execute_bound_operation(self, operation):
        self.calls.append(operation)
        return {"success": True}


class MismatchedBindingBackend(BoundBackend):
    async def resolve_cad_target_binding(self, operation):
        self.resolve_calls.append(operation.id)
        return replace(
            _binding(str(operation.target_ref)), requested_ref="other-target"
        )


def _create_windows_junction_or_skip(junction: Path, target: Path) -> None:
    command = [
        os.environ.get("ComSpec", "cmd.exe"),
        "/d",
        "/c",
        "mklink",
        "/J",
        str(junction),
        str(target),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, PermissionError) as exc:
        pytest.skip(f"Windows junction tool unavailable: {exc}")
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"Windows junction creation timed out: {exc}")

    diagnostic = f"{completed.stdout}\n{completed.stderr}".strip()
    if completed.returncode != 0:
        unavailable_markers = (
            "access is denied",
            "acesso negado",
            "privilege",
            "privilégio",
            "privilegio",
            "not supported",
            "não há suporte",
            "nao ha suporte",
            "not recognized",
            "não é reconhecido",
            "nao e reconhecido",
        )
        if any(marker in diagnostic.lower() for marker in unavailable_markers):
            pytest.skip(f"Windows junction creation unavailable: {diagnostic}")
        pytest.fail(
            "Windows junction creation failed unexpectedly "
            f"(exit {completed.returncode}): {diagnostic}"
        )
    if not junction.is_dir():
        pytest.fail("mklink /J reported success without creating a directory junction")


class AutodeskClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name, arguments, *, options=None):
        self.calls.append((name, arguments))
        if len(self.calls) == 1:
            return ToolResult.success(
                message=json.dumps({"success": True, "binding": asdict(_binding())})
            )
        return ToolResult.success(
            message=json.dumps(
                {
                    "success": True,
                    "export": {
                        "format": "step",
                        "path": r"C:\private\provider\artifact.step",
                        "bytes": 123,
                    },
                }
            )
        )


def test_policy_is_immutable_and_missing_environment_is_deny_all(monkeypatch) -> None:
    monkeypatch.delenv("FUSION_AGENT_AUTHORITY_POLICY_PATH", raising=False)
    policy = AuthorityPolicy.from_environment()
    assert policy.io_enabled is False
    with pytest.raises(AttributeError):
        policy.allow_overwrite = True  # type: ignore[misc]


def test_host_file_ref_is_confined_and_legacy_absolute_path_requires_approved_root(
    tmp_path: Path,
) -> None:
    policy = AuthorityPolicy.load(_policy_file(tmp_path))
    broker = AuthorityBroker(policy, ledger=CapabilityLedger(tmp_path / "ledger"))

    graph = _prepare_graph(
        broker,
        _export_spec(
            file_ref={"root_id": "approved-exports", "relative_path": "part.step"}
        ),
        session_id="session-1",
        provider="bound-test",
    )
    assert graph.operations[0].host_path is not None
    assert graph.operations[0].host_path.canonical_path == str(
        (tmp_path / "exports" / "part.step").resolve()
    )

    legacy = _prepare_graph(
        broker,
        _export_spec(str(tmp_path / "exports" / "legacy.step")),
        session_id="session-legacy",
        provider="bound-test",
    )
    assert legacy.operations[0].host_path is not None

    with pytest.raises(AuthorityDeniedError, match="approved.*root"):
        _prepare_graph(
            broker,
            _export_spec(str(tmp_path / "outside.step")),
            session_id="session-2",
            provider="bound-test",
        )
    with pytest.raises(AuthorityDeniedError, match="relative path"):
        _prepare_graph(
            broker,
            _export_spec("../escape.step"),
            session_id="session-3",
            provider="bound-test",
        )


@pytest.mark.asyncio
async def test_capability_executor_denies_host_io_before_backend_preflight_or_dispatch(
    tmp_path: Path,
) -> None:
    backend = BoundBackend()
    broker = AuthorityBroker(
        AuthorityPolicy.deny_all(), ledger=CapabilityLedger(tmp_path / "ledger")
    )
    with pytest.raises(AuthorityDeniedError, match="disabled"):
        await CapabilityExecutor(backend, authority_broker=broker).execute(
            _export_spec(str(tmp_path / "arbitrary.step")), session_id="denied-session"
        )
    assert backend.preflighted == []
    assert backend.calls == []
    assert backend.resolve_calls == []


@pytest.mark.skipif(os.name != "nt", reason="Windows junction coverage")
@pytest.mark.asyncio
async def test_windows_junction_escape_is_denied_before_any_provider_call(
    tmp_path: Path,
) -> None:
    policy_path = _policy_file(tmp_path)
    export_root = tmp_path / "exports"
    outside = tmp_path / "outside"
    outside.mkdir()
    junction = export_root / "escape-junction"
    _create_windows_junction_or_skip(junction, outside)
    backend = BoundBackend()
    broker = AuthorityBroker(
        AuthorityPolicy.load(policy_path),
        ledger=CapabilityLedger(tmp_path / "ledger"),
    )

    try:
        with pytest.raises(AuthorityDeniedError, match="outside"):
            await CapabilityExecutor(backend, authority_broker=broker).execute(
                _export_spec(
                    file_ref={
                        "root_id": "approved-exports",
                        "relative_path": "escape-junction/part.step",
                    }
                ),
                session_id="junction-escape",
            )
    finally:
        if os.path.lexists(junction):
            os.rmdir(junction)

    assert backend.resolve_calls == []
    assert backend.preflighted == []
    assert backend.calls == []


@pytest.mark.asyncio
async def test_export_without_lossless_target_resolver_is_zero_dispatch(
    tmp_path: Path,
) -> None:
    backend = MissingBindingBackend()
    broker = AuthorityBroker(
        AuthorityPolicy.load(_policy_file(tmp_path)),
        ledger=CapabilityLedger(tmp_path / "ledger"),
    )

    with pytest.raises(AuthorityDeniedError, match="resolve lossless CAD target"):
        await CapabilityExecutor(backend, authority_broker=broker).execute(
            _export_spec(
                file_ref={
                    "root_id": "approved-exports",
                    "relative_path": "missing-binding.step",
                }
            ),
            session_id="missing-binding",
        )

    assert backend.preflighted == []
    assert backend.calls == []


@pytest.mark.asyncio
async def test_mismatched_target_binding_never_reaches_mutation_dispatch(
    tmp_path: Path,
) -> None:
    backend = MismatchedBindingBackend()
    broker = AuthorityBroker(
        AuthorityPolicy.load(_policy_file(tmp_path)),
        ledger=CapabilityLedger(tmp_path / "ledger"),
    )

    with pytest.raises(AuthorityDeniedError, match="does not match export reference"):
        await CapabilityExecutor(backend, authority_broker=broker).execute(
            _export_spec(
                file_ref={
                    "root_id": "approved-exports",
                    "relative_path": "mismatched-binding.step",
                }
            ),
            session_id="mismatched-binding",
        )

    assert backend.resolve_calls == ["export_part"]
    assert backend.preflighted == []
    assert backend.calls == []


@pytest.mark.asyncio
async def test_synthetic_volume_change_is_rejected_before_mutation_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    volume_changed = {"active": False}
    observed_devices: list[tuple[int, int]] = []
    original_fingerprint = authority_module._resource_fingerprint

    def fingerprint_with_synthetic_volume_change(
        path: Path,
        *,
        direction: str,
        existed: bool,
    ) -> str:
        if direction == "export" and not existed and volume_changed["active"]:
            stat = path.parent.stat()
            original_device = int(stat.st_dev)
            synthetic_device = original_device + 1
            observed_devices.append((original_device, synthetic_device))
            return authority_module._json_digest(
                {
                    "parent_device": synthetic_device,
                    "parent_inode": int(stat.st_ino),
                    "destination_absent": True,
                }
            )
        return original_fingerprint(path, direction=direction, existed=existed)

    monkeypatch.setattr(
        authority_module,
        "_resource_fingerprint",
        fingerprint_with_synthetic_volume_change,
    )

    class SyntheticVolumeShiftBackend(BoundBackend):
        def preflight_bound_operations(self, operations):
            super().preflight_bound_operations(operations)
            volume_changed["active"] = True

    backend = SyntheticVolumeShiftBackend()
    broker = AuthorityBroker(
        AuthorityPolicy.load(_policy_file(tmp_path)),
        ledger=CapabilityLedger(tmp_path / "ledger"),
    )
    result = await CapabilityExecutor(backend, authority_broker=broker).execute(
        _export_spec(
            file_ref={
                "root_id": "approved-exports",
                "relative_path": "volume-shift.step",
            }
        ),
        session_id="synthetic-volume-change",
    )

    assert result.success is False
    assert result.dispatched is False
    assert result.may_have_applied is False
    assert result.mutation_outcome == "known"
    assert result.transport_evidence_complete is True
    assert result.error_code == "AUTHORITY_DENIED"
    assert backend.resolve_calls == ["export_part"]
    assert len(backend.preflighted) == 1
    assert backend.calls == []
    assert observed_devices and all(
        before != after for before, after in observed_devices
    )
    capability = backend.preflighted[0].capability
    assert capability is not None
    assert broker.ledger.state(capability.capability_id) == "revoked"


@pytest.mark.asyncio
async def test_capability_executor_default_is_deny_all_even_when_policy_env_is_set(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(
        "FUSION_AGENT_AUTHORITY_POLICY_PATH", str(_policy_file(tmp_path))
    )
    backend = BoundBackend()
    with pytest.raises(AuthorityDeniedError, match="disabled"):
        await CapabilityExecutor(backend).execute(
            _export_spec(
                file_ref={
                    "root_id": "approved-exports",
                    "relative_path": "default-must-deny.step",
                }
            ),
            session_id="default-deny",
        )
    assert backend.preflighted == []
    assert backend.calls == []


@pytest.mark.asyncio
async def test_runtime_loads_one_startup_policy_and_injects_one_broker(
    monkeypatch,
    tmp_path: Path,
) -> None:
    policy_path = _policy_file(tmp_path)
    monkeypatch.setenv("FUSION_AGENT_AUTHORITY_POLICY_PATH", str(policy_path))
    outputs_root = tmp_path / "runtime-outputs"
    runtime = FusionAgentRuntime(
        manifest_root=tmp_path / "manifests",
        outputs_root=outputs_root,
    )
    startup_broker = runtime.authority_broker
    backend = BoundBackend()
    backend.provider = "mock"
    monkeypatch.setattr(runtime_module, "_MockCapabilityBackend", lambda: backend)

    # An environment mutation after runtime startup must not change authority.
    monkeypatch.setenv(
        "FUSION_AGENT_AUTHORITY_POLICY_PATH",
        str(tmp_path / "replacement-policy-that-does-not-exist.json"),
    )
    result = await runtime.execute_cad_spec_v2(
        _export_spec(
            file_ref={
                "root_id": "approved-exports",
                "relative_path": "runtime-bound.step",
            }
        ),
        mode="mock",
    )

    diagnostics = runtime.diagnostics()
    authority = diagnostics["authority_policy"]
    assert result.success is True
    assert runtime.authority_broker is startup_broker
    assert startup_broker.ledger.root == outputs_root / ".authority" / "capabilities"
    assert authority == {
        "digest": startup_broker.policy.digest,
        "io_enabled": True,
        "import_enabled": True,
        "output_enabled": False,
        "output_policy": "deny_io",
        "overwrite_supported": False,
        "root_ids": {"import": ["approved-imports"]},
    }
    assert str(tmp_path) not in json.dumps(authority)
    assert len(backend.calls) == 1
    await runtime.close()


@pytest.mark.asyncio
async def test_runtime_startup_reconciles_orphaned_durable_claim_to_unknown(
    monkeypatch,
    tmp_path: Path,
) -> None:
    policy_path = _policy_file(tmp_path)
    monkeypatch.setenv("FUSION_AGENT_AUTHORITY_POLICY_PATH", str(policy_path))
    outputs_root = tmp_path / "runtime-outputs"
    ledger_root = outputs_root / ".authority" / "capabilities"
    capability = OperationCapability(
        capability_id="orphaned-runtime-claim",
        direction="export",
        root_id="approved-exports",
        canonical_path=str((tmp_path / "exports" / "part.step").resolve()),
        spec_digest="a" * 64,
        operation_digest="b" * 64,
        session_id="interrupted-session",
        provider="mock",
        overwrite=False,
        issued_at=1.0,
        expires_at=1801.0,
        binding_digest="c" * 64,
    )
    previous_process = CapabilityLedger(ledger_root)
    previous_process.issue(capability)
    previous_process.claim(capability, now=2.0)
    assert previous_process.state(capability.capability_id) == "claimed"

    runtime = FusionAgentRuntime(
        manifest_root=tmp_path / "manifests",
        outputs_root=outputs_root,
    )

    assert runtime.authority_broker.ledger.state(capability.capability_id) == "unknown"
    with pytest.raises(AuthorityDeniedError, match="replay"):
        runtime.authority_broker.ledger.claim(capability, now=3.0)
    await runtime.close()


@pytest.mark.asyncio
async def test_runtime_invalid_startup_policy_denies_io_without_blocking_runtime(
    monkeypatch,
    tmp_path: Path,
) -> None:
    invalid_policy = tmp_path / "invalid-policy.json"
    invalid_policy.write_text('{"schema_version":"wrong"}', encoding="utf-8")
    monkeypatch.setenv("FUSION_AGENT_AUTHORITY_POLICY_PATH", str(invalid_policy))

    runtime = FusionAgentRuntime(
        manifest_root=tmp_path / "manifests",
        outputs_root=tmp_path / "outputs",
    )

    assert runtime.authority_policy.io_enabled is False
    assert runtime.authority_policy.safe_summary() == {
        "digest": runtime.authority_policy.digest,
        "io_enabled": False,
        "import_enabled": False,
        "output_enabled": False,
        "output_policy": "deny_io",
        "overwrite_supported": False,
        "root_ids": {"import": []},
    }
    with pytest.raises(AuthorityDeniedError, match="disabled"):
        _prepare_graph(
            runtime.authority_broker,
            _export_spec(str(tmp_path / "blocked.step")),
            session_id="invalid-policy",
            provider="bound-test",
        )
    await runtime.close()


@pytest.mark.asyncio
async def test_runtime_uses_authority_path_from_immutable_startup_configuration(
    monkeypatch,
    tmp_path: Path,
) -> None:
    policy_path = _policy_file(tmp_path)
    monkeypatch.setenv("FUSION_AGENT_AUTHORITY_POLICY_PATH", str(policy_path))
    configuration = RuntimeConfiguration.from_environment()
    monkeypatch.setenv(
        "FUSION_AGENT_AUTHORITY_POLICY_PATH",
        str(tmp_path / "changed-after-startup.json"),
    )

    runtime = FusionAgentRuntime(
        manifest_root=tmp_path / "manifests",
        outputs_root=tmp_path / "outputs",
        configuration=configuration,
    )

    assert runtime.authority_policy.source_path == str(policy_path.resolve())
    assert runtime.authority_policy.io_enabled is True
    await runtime.close()


@pytest.mark.asyncio
async def test_real_v2_export_is_denied_before_runtime_readiness_or_binding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = FusionAgentRuntime(
        manifest_root=tmp_path / "manifests",
        outputs_root=tmp_path / "outputs",
    )
    readiness_calls = 0

    async def fail_if_ready() -> None:
        nonlocal readiness_calls
        readiness_calls += 1
        raise AssertionError("deny_io must precede runtime readiness")

    monkeypatch.setattr(runtime, "ensure_ready", fail_if_ready)
    with pytest.raises(HostOutputDisabledError, match="disabled by deny_io") as error:
        await runtime.execute_cad_spec_v2(
            _export_spec(str(tmp_path / "blocked.step")), mode="real"
        )

    assert error.value.error_code == "HOST_OUTPUT_DISABLED"
    assert readiness_calls == 0
    await runtime.close()


@pytest.mark.asyncio
async def test_runtime_transport_configuration_is_startup_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FUSION_AGENT_BACKEND", "autodesk_http")
    monkeypatch.setenv("FUSION_MCP_READ_TIMEOUT_SECONDS", "7")
    monkeypatch.setenv("FUSION_MCP_ENDPOINT", "http://127.0.0.1:27182/mcp")
    runtime = FusionAgentRuntime(
        manifest_root=tmp_path / "manifests",
        outputs_root=tmp_path / "outputs",
    )

    monkeypatch.setenv("FUSION_AGENT_BACKEND", "faust_stdio")
    monkeypatch.setenv("FUSION_MCP_READ_TIMEOUT_SECONDS", "999")
    monkeypatch.setenv("FUSION_MCP_ENDPOINT", "http://127.0.0.1:9999/mcp")
    await runtime._replace_real_client()

    assert runtime.configuration.backend == "autodesk_http"
    assert runtime.configuration.read_timeout_seconds == 7.0
    assert runtime.real_client.endpoint == "http://127.0.0.1:27182/mcp"
    assert runtime.real_client.read_timeout_seconds == 7.0
    await runtime.close()


@pytest.mark.asyncio
async def test_capability_is_single_use_and_bound_to_graph_session_and_path(
    tmp_path: Path,
) -> None:
    backend = BoundBackend()
    broker = AuthorityBroker(
        AuthorityPolicy.load(_policy_file(tmp_path)),
        ledger=CapabilityLedger(tmp_path / "ledger"),
    )
    result = await CapabilityExecutor(backend, authority_broker=broker).execute(
        _export_spec(
            file_ref={"root_id": "approved-exports", "relative_path": "part.step"}
        ),
        session_id="single-use",
    )
    assert result.success is True
    assert len(backend.calls) == 1
    bound = backend.calls[0]
    with pytest.raises(AuthorityDeniedError, match="consumed|replay"):
        broker.claim(bound)


def test_existing_export_requires_both_policy_and_operation_overwrite_opt_in(
    tmp_path: Path,
) -> None:
    policy_path = _policy_file(tmp_path)
    destination = tmp_path / "exports" / "part.step"
    destination.write_text("existing", encoding="utf-8")
    broker = AuthorityBroker(
        AuthorityPolicy.load(policy_path), ledger=CapabilityLedger(tmp_path / "ledger")
    )
    with pytest.raises(AuthorityDeniedError, match="overwrite"):
        _prepare_graph(
            broker,
            _export_spec(
                file_ref={"root_id": "approved-exports", "relative_path": "part.step"},
                overwrite=True,
            ),
            session_id="overwrite-denied",
            provider="bound-test",
        )


def test_import_binding_detects_resource_change_before_claim(tmp_path: Path) -> None:
    policy = AuthorityPolicy.load(_policy_file(tmp_path))
    source = tmp_path / "imports" / "part.step"
    source.write_text("first", encoding="utf-8")
    broker = AuthorityBroker(policy, ledger=CapabilityLedger(tmp_path / "ledger"))
    graph = _prepare_graph(
        broker,
        _import_spec(str(source)),
        session_id="import-toctou",
        provider="bound-test",
    )
    source.write_text("changed after authorization", encoding="utf-8")
    with pytest.raises(AuthorityDeniedError, match="changed"):
        broker.claim(graph.operations[0])
    capability = graph.operations[0].capability
    assert capability is not None
    assert broker.ledger.state(capability.capability_id) == "revoked"


@pytest.mark.asyncio
async def test_unknown_dispatch_outcome_is_terminal_and_cannot_be_replayed(
    tmp_path: Path,
) -> None:
    backend = UnknownOutcomeBackend()
    broker = AuthorityBroker(
        AuthorityPolicy.load(_policy_file(tmp_path)),
        ledger=CapabilityLedger(tmp_path / "ledger"),
    )
    result = await CapabilityExecutor(backend, authority_broker=broker).execute(
        _export_spec(
            file_ref={"root_id": "approved-exports", "relative_path": "part.step"}
        ),
        session_id="unknown-outcome",
    )
    bound = backend.calls[0]
    capability = bound.capability
    assert capability is not None
    assert result.success is False
    assert result.mutation_outcome == "unknown"
    assert broker.ledger.state(capability.capability_id) == "unknown"
    with pytest.raises(AuthorityDeniedError, match="unknown|replay"):
        broker.claim(bound)


@pytest.mark.asyncio
async def test_autodesk_sink_receives_only_canonical_bound_export_path(
    tmp_path: Path,
) -> None:
    client = AutodeskClient()
    manifest = ToolManifest(
        source="authority-test",
        tools=[
            ToolDefinition(name="fusion_mcp_read"),
            ToolDefinition(name="fusion_mcp_execute"),
        ],
    )
    backend = AutodeskTypedBackend.from_client(client, manifest)
    broker = AuthorityBroker(
        AuthorityPolicy.load(_policy_file(tmp_path)),
        ledger=CapabilityLedger(tmp_path / "ledger"),
    )
    spec = _export_spec(
        file_ref={
            "root_id": "approved-exports",
            "relative_path": "autodesk.step",
        }
    )
    with pytest.raises(HostOutputDisabledError, match="disabled by deny_io"):
        await CapabilityExecutor(backend, authority_broker=broker).execute(
            spec, session_id="autodesk-bound-export"
        )

    assert client.calls == []
    assert backend._prepared == {}
    assert not (tmp_path / "exports" / "autodesk.step").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("policy_allows_overwrite", "operation_allows_overwrite"),
    [(False, True), (True, False)],
)
async def test_existing_export_without_double_opt_in_has_zero_binding_or_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    policy_allows_overwrite: bool,
    operation_allows_overwrite: bool,
) -> None:
    destination = tmp_path / "exports" / "existing.step"
    policy_path = _policy_file(tmp_path, allow_overwrite=policy_allows_overwrite)
    destination.write_text("existing", encoding="utf-8")
    client = AutodeskClient()
    backend = AutodeskTypedBackend.from_client(
        client,
        ToolManifest(
            source="authority-test",
            tools=[
                ToolDefinition(name="fusion_mcp_read"),
                ToolDefinition(name="fusion_mcp_execute"),
            ],
        ),
    )
    binding_calls: list[str] = []
    resolve_cad_target_binding = backend.resolve_cad_target_binding

    async def track_cad_target_binding(operation):
        binding_calls.append(operation.id)
        return await resolve_cad_target_binding(operation)

    monkeypatch.setattr(backend, "resolve_cad_target_binding", track_cad_target_binding)
    broker = AuthorityBroker(
        AuthorityPolicy.load(policy_path),
        ledger=CapabilityLedger(tmp_path / "ledger"),
    )

    with pytest.raises(HostOutputDisabledError, match="disabled by deny_io"):
        await CapabilityExecutor(backend, authority_broker=broker).execute(
            _export_spec(
                file_ref={
                    "root_id": "approved-exports",
                    "relative_path": "existing.step",
                },
                overwrite=operation_allows_overwrite,
            ),
            session_id="overwrite-missing-opt-in",
        )

    assert binding_calls == []
    assert client.calls == []
    assert backend._prepared == {}


@pytest.mark.asyncio
async def test_existing_export_is_globally_denied_before_binding_or_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy_path = _policy_file(tmp_path, allow_overwrite=True)
    destination = tmp_path / "exports" / "existing.step"
    destination.write_text("existing", encoding="utf-8")
    client = AutodeskClient()
    backend = AutodeskTypedBackend.from_client(
        client,
        ToolManifest(
            source="authority-test",
            tools=[
                ToolDefinition(name="fusion_mcp_read"),
                ToolDefinition(name="fusion_mcp_execute"),
            ],
        ),
    )
    binding_calls: list[str] = []
    resolve_cad_target_binding = backend.resolve_cad_target_binding

    async def track_cad_target_binding(operation):
        binding_calls.append(operation.id)
        return await resolve_cad_target_binding(operation)

    monkeypatch.setattr(backend, "resolve_cad_target_binding", track_cad_target_binding)
    broker = AuthorityBroker(
        AuthorityPolicy.load(policy_path),
        ledger=CapabilityLedger(tmp_path / "ledger"),
    )

    with pytest.raises(HostOutputDisabledError, match="disabled by deny_io"):
        await CapabilityExecutor(backend, authority_broker=broker).execute(
            _export_spec(
                file_ref={
                    "root_id": "approved-exports",
                    "relative_path": "existing.step",
                },
                overwrite=True,
            ),
            session_id="linux-existing-overwrite",
        )

    assert binding_calls == []
    assert client.calls == []
    assert backend._prepared == {}
    assert destination.read_text(encoding="utf-8") == "existing"


@pytest.mark.asyncio
async def test_create_new_export_is_globally_denied_before_platform_preflight(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = AutodeskClient()
    backend = AutodeskTypedBackend.from_client(
        client,
        ToolManifest(
            source="authority-test",
            tools=[
                ToolDefinition(name="fusion_mcp_read"),
                ToolDefinition(name="fusion_mcp_execute"),
            ],
        ),
    )
    platform_preflight: list[tuple[str, bool]] = []

    def emulate_linux_host_io_preflight(
        direction: str, *, overwrite: bool = False
    ) -> None:
        platform_preflight.append((direction, overwrite))

    monkeypatch.setattr(
        vendor_facade_module,
        "_require_secure_host_io_platform",
        emulate_linux_host_io_preflight,
    )
    broker = AuthorityBroker(
        AuthorityPolicy.load(_policy_file(tmp_path)),
        ledger=CapabilityLedger(tmp_path / "ledger"),
    )

    with pytest.raises(HostOutputDisabledError, match="disabled by deny_io"):
        await CapabilityExecutor(backend, authority_broker=broker).execute(
            _export_spec(
                file_ref={
                    "root_id": "approved-exports",
                    "relative_path": "new.step",
                }
            ),
            session_id="linux-create-new-export",
        )

    assert platform_preflight == []
    assert client.calls == []
    assert backend._prepared == {}
    assert not (tmp_path / "exports" / "new.step").exists()


def test_platform_preflight_denies_all_linux_output_even_create_new(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        vendor_facade_module,
        "os",
        types.SimpleNamespace(
            name="posix",
            memfd_create=lambda: None,
            MFD_ALLOW_SEALING=1,
            O_TMPFILE=1,
        ),
    )
    monkeypatch.setattr(
        vendor_facade_module, "sys", types.SimpleNamespace(platform="linux")
    )

    with pytest.raises(AuthorityDeniedError, match="disabled by deny_io"):
        vendor_facade_module._require_secure_host_io_platform("export", overwrite=False)
    with pytest.raises(AuthorityDeniedError, match="disabled by deny_io"):
        vendor_facade_module._require_secure_host_io_platform("export", overwrite=True)


@pytest.mark.asyncio
async def test_autodesk_raw_host_io_bypass_is_denied_before_dispatch(
    tmp_path: Path,
) -> None:
    client = AutodeskClient()
    manifest = ToolManifest(
        source="authority-test",
        tools=[ToolDefinition(name="export_step")],
    )
    backend = AutodeskTypedBackend.from_client(client, manifest)
    operation = _export_spec(str(tmp_path / "unbound.step")).operations[0]
    with pytest.raises(AuthorityDeniedError, match="disabled by deny_io"):
        backend.preflight_host_io_operations([operation])
    assert client.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("method_name", ["export_step", "export_stl"])
async def test_vendor_direct_export_denies_before_profile_compile_or_transport(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    method_name: str,
) -> None:
    client = AutodeskClient()
    backend = AutodeskTypedBackend.from_client(
        client,
        ToolManifest(source="authority-test", tools=[]),
    )
    compiled: list[str] = []

    def track_compile(*_args, **_kwargs):
        compiled.append("called")
        raise AssertionError("deny_io must precede compilation")

    monkeypatch.setattr(backend.facade, "prepare_typed_operation", track_compile)

    with pytest.raises(HostOutputDisabledError, match="disabled by deny_io"):
        await getattr(backend.facade, method_name)(
            "part_body",
            tmp_path / f"part.{method_name.removeprefix('export_')}",
            host_path_binding={"overwrite": True},
        )

    assert compiled == []
    assert client.calls == []


@pytest.mark.asyncio
async def test_autodesk_export_binding_bypass_is_denied_before_read_transport() -> None:
    client = AutodeskClient()
    backend = AutodeskTypedBackend.from_client(
        client,
        ToolManifest(
            source="authority-test",
            tools=[
                ToolDefinition(name="fusion_mcp_read"),
                ToolDefinition(name="fusion_mcp_execute"),
            ],
        ),
    )
    operation = _export_spec(r"C:\exports\part.step").operations[0]

    with pytest.raises(HostOutputDisabledError, match="disabled by deny_io"):
        await backend.resolve_cad_target_binding(operation)

    assert client.calls == []
