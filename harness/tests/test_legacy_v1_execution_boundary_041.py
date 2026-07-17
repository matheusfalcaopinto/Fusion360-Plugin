from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_core.authority import AuthorityDeniedError
from agent_core.executor import ExecutionContext, Executor
from agent_core.repair_loop import RepairLoop
from agent_core.session_controller import SessionController, SessionOptions
from cad_spec.models import CadSpec
from verifier.result_models import (
    DecisionReasonCode,
    DecisionStatus,
    EvidenceEnvelope,
    FailureCode,
    VerificationIssue,
    VerificationResult,
)


def _v1_extrude_spec(operation: str = "new_body") -> CadSpec:
    return CadSpec.model_validate(
        {
            "intent": "exercise the deprecated CadSpec v1 execution boundary",
            "parameters": [],
            "components": [
                {
                    "name": "fixture",
                    "features": [
                        {
                            "name": "fixture_extrude",
                            "type": "extrude_rectangle",
                            "operation": operation,
                            "inputs": {
                                "sketch_name": "fixture_sketch",
                                "plane": "XY",
                                "center": ["0 mm", "0 mm"],
                                "width": "10 mm",
                                "height": "8 mm",
                                "distance": "4 mm",
                                "body_name": "fixture_body",
                            },
                        }
                    ],
                }
            ],
            "acceptance_tests": [{"type": "body_count", "target": 1}],
        }
    )


class _RecordingFacade:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def inspect_design(self) -> dict[str, Any]:
        self.calls.append(("inspect_design", {}))
        return {}

    async def create_component(self, name: str) -> dict[str, Any]:
        self.calls.append(("create_component", {"name": name}))
        return {}

    async def activate_component(self, name: str) -> dict[str, Any]:
        self.calls.append(("activate_component", {"name": name}))
        return {}

    async def create_sketch_on_plane(
        self, component: str, plane: str, name: str
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "create_sketch_on_plane",
                {"component": component, "plane": plane, "name": name},
            )
        )
        return {}

    async def draw_constrained_rectangle(
        self, sketch: str, center: list[str], width: str, height: str
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "draw_constrained_rectangle",
                {
                    "sketch": sketch,
                    "center": center,
                    "width": width,
                    "height": height,
                },
            )
        )
        return {"profile_ref": "profile:fixture_sketch:0"}

    async def extrude_profile(self, **payload: Any) -> dict[str, Any]:
        self.calls.append(("extrude_profile", payload))
        return {}


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["new_body", "cut", "future_modifier"])
async def test_real_v1_execute_rejects_entire_graph_before_any_provider_call(
    operation: str,
) -> None:
    spec = _v1_extrude_spec(operation)
    facade = _RecordingFacade()

    # CadSpec v1 parsing remains compatible, including the legacy free-form
    # operation field. The deprecated graph is denied only at real execution.
    assert spec.components[0].features[0].operation == operation
    with pytest.raises(AuthorityDeniedError, match="CadSpec v1.*CadSpec v2"):
        await Executor(facade).execute(  # type: ignore[arg-type]
            spec,
            ExecutionContext(mode="real"),
        )

    assert facade.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("method_name", ["replay_features", "replay_exports"])
async def test_real_v1_replay_rejects_before_activation_or_dispatch(
    method_name: str,
) -> None:
    facade = _RecordingFacade()
    executor = Executor(facade)  # type: ignore[arg-type]

    with pytest.raises(AuthorityDeniedError, match="CadSpec v1.*CadSpec v2"):
        await getattr(executor, method_name)(
            _v1_extrude_spec("cut"),
            ExecutionContext(mode="real"),
        )

    assert facade.calls == []


@pytest.mark.asyncio
async def test_mock_and_dry_run_v1_controls_remain_compatible() -> None:
    mock_facade = _RecordingFacade()
    mock_result = await Executor(mock_facade).execute(  # type: ignore[arg-type]
        _v1_extrude_spec("cut"),
        ExecutionContext(mode="mock"),
    )

    assert mock_result.success is True
    assert [name for name, _ in mock_facade.calls][-1] == "extrude_profile"
    assert mock_facade.calls[-1][1]["operation"] == "cut"

    dry_run = await Executor().execute(
        _v1_extrude_spec("future_modifier"),
        ExecutionContext(mode="real", dry_run=True),
    )
    assert dry_run.success is True
    assert dry_run.transactions[-1]["status"] == "simulated"


class _ProviderProbe:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def list_tools(self) -> Any:
        self.calls.append("list_tools")
        raise AssertionError("real provider discovery must not run for CadSpec v1")


@pytest.mark.asyncio
async def test_session_controller_rejects_real_v1_before_provider_discovery(
    tmp_path: Path,
) -> None:
    provider = _ProviderProbe()
    controller = SessionController(real_client=provider)
    options = SessionOptions(
        mode="real",
        workspace_root=tmp_path / "workspace",
        output_dir=tmp_path / "outputs",
        manifest_dir=tmp_path / "manifests",
    )

    with pytest.raises(AuthorityDeniedError, match="CadSpec v1.*CadSpec v2"):
        await controller.run_spec(
            _v1_extrude_spec(),
            mode="real",
            options=options,
        )

    assert provider.calls == []


def _failed_open_profile() -> VerificationResult:
    evidence = EvidenceEnvelope(
        producer="legacy-v1-boundary-test",
        provenance={"source": "fixture"},
        document_identity="document:fixture",
        complete=True,
        counts_exact=True,
        truncated=False,
        stop_reason="complete",
        assertion_ids=["body_count:0"],
        assertion_count=1,
        evaluated_count=1,
    )
    return VerificationResult(
        passed=False,
        status=DecisionStatus.FAILED,
        reason_codes=[DecisionReasonCode.ASSERTION_FAILED],
        issues=[
            VerificationIssue(
                code=FailureCode.OPEN_PROFILE,
                message="fixture profile is open",
            )
        ],
        evidence=evidence,
    )


@pytest.mark.asyncio
async def test_repair_cannot_turn_real_v1_replay_into_provider_dispatch() -> None:
    class _Verifier:
        async def verify(self, _spec: CadSpec) -> VerificationResult:
            return _failed_open_profile()

    facade = _RecordingFacade()
    legacy_executor = Executor(facade)  # type: ignore[arg-type]

    class _BoundReplayAdapter:
        async def replay_features_bound(
            self, spec: CadSpec, context: ExecutionContext
        ) -> bool:
            return await legacy_executor.replay_features(spec, context)

    loop = RepairLoop(
        _Verifier(),  # type: ignore[arg-type]
        executor=_BoundReplayAdapter(),
        max_total_attempts=1,
    )
    result = await loop.run(
        _v1_extrude_spec("cut"),
        context=ExecutionContext(mode="real"),
    )

    assert result.status is DecisionStatus.FAILED
    assert loop.attempts[0].action == "replay_features"
    assert loop.attempts[0].action_applied is False
    assert facade.calls == []
