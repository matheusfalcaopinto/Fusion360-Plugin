from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_core.authority import (
    AuthorityBroker,
    AuthorityDeniedError,
    AuthorityPolicy,
    CapabilityLedger,
)
from agent_core.executor import ExecutionContext, Executor, _safe_output_path
from cad_spec.models import CadSpec


class RecordingFacade:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.exports: list[tuple[str, str, str]] = []
        self.captures: list[str] = []

    async def inspect_design(self):
        self.calls.append("inspect_design")
        return {}

    async def create_component(self, name):
        self.calls.append("create_component")
        return {"name": name}

    async def activate_component(self, name):
        self.calls.append("activate_component")
        return {"name": name}

    async def export_step(self, target, path):
        self.calls.append("export_step")
        self.exports.append(("step", target, str(path)))

    async def export_stl(self, target, path):
        self.calls.append("export_stl")
        self.exports.append(("stl", target, str(path)))

    async def capture_viewport(self, *, path, **_kwargs):
        self.calls.append("capture_viewport")
        self.captures.append(str(path))
        return {"screenshot": {"path": str(path)}}


class CapturingAuthorityBroker(AuthorityBroker):
    def __init__(self, policy: AuthorityPolicy, *, ledger: CapabilityLedger) -> None:
        super().__init__(policy, ledger=ledger)
        self.last_graph = None

    def prepare_legacy_output_graph(self, *args, **kwargs):  # noqa: ANN002, ANN003
        self.last_graph = super().prepare_legacy_output_graph(*args, **kwargs)
        return self.last_graph


def _broker(
    tmp_path: Path, *, allow_overwrite: bool = False
) -> tuple[AuthorityBroker, Path]:
    output_root = tmp_path / "outputs"
    output_root.mkdir(exist_ok=True)
    policy_path = tmp_path / "authority.json"
    policy_path.write_text(
        json.dumps(
            {
                "schema_version": "fusion_agent.authority_policy.v1",
                "import_roots": [],
                "export_roots": [
                    {
                        "id": "legacy-outputs",
                        "path": str(output_root),
                        "formats": ["step", "stl", "png"],
                        "default": True,
                    }
                ],
                "allow_overwrite": allow_overwrite,
                "capability_ttl_seconds": 1800,
            }
        ),
        encoding="utf-8",
    )
    return (
        AuthorityBroker(
            AuthorityPolicy.load(policy_path),
            ledger=CapabilityLedger(tmp_path / "ledger"),
        ),
        output_root,
    )


def _legacy_export(path: str, *, overwrite: bool = False) -> CadSpec:
    return CadSpec.model_validate(
        {
            "intent": "export one body",
            "parameters": [],
            "components": [
                {
                    "name": "part",
                    "features": [
                        {
                            "name": "part_export",
                            "type": "export",
                            "inputs": {
                                "target": "part_body",
                                "format": "step",
                                "path": path,
                                "overwrite": overwrite,
                            },
                        }
                    ],
                }
            ],
            "acceptance_tests": [{"type": "body_exists", "target": "part_body"}],
        }
    )


def _legacy_capture(path: str) -> CadSpec:
    return CadSpec.model_validate(
        {
            "intent": "capture one viewport",
            "parameters": [],
            "components": [
                {
                    "name": "part",
                    "features": [
                        {
                            "name": "part_capture",
                            "type": "capture_viewport",
                            "inputs": {"path": path, "view": "isometric"},
                        }
                    ],
                }
            ],
            "acceptance_tests": [{"type": "body_exists", "target": "part_body"}],
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["../escape.step", "nested/../../escape.step"])
async def test_legacy_export_rejects_parent_escape_before_facade_call(
    tmp_path: Path, path: str
) -> None:
    broker, output_root = _broker(tmp_path)
    facade = RecordingFacade()
    with pytest.raises(ValueError, match="under output_dir"):
        await Executor(facade, authority_broker=broker).execute(
            _legacy_export(path),
            ExecutionContext(mode="real", output_dir=output_root),
        )
    assert facade.calls == []
    assert facade.exports == []


def test_legacy_capture_rejects_absolute_path_even_when_it_exists_under_output_root(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "outputs"
    output_root.mkdir()
    with pytest.raises(ValueError, match="relative"):
        _safe_output_path(output_root, output_root / "capture.png")


@pytest.mark.asyncio
async def test_legacy_relative_export_is_canonicalized_under_output_root(
    tmp_path: Path,
) -> None:
    broker, output_root = _broker(tmp_path)
    (output_root / "nested").mkdir()
    facade = RecordingFacade()
    await Executor(facade, authority_broker=broker).execute(
        _legacy_export("nested/part.step"),
        ExecutionContext(
            mode="real", output_dir=output_root, session_id="legacy-positive"
        ),
    )
    assert facade.exports == [
        ("step", "part_body", str((output_root / "nested" / "part.step").resolve()))
    ]


@pytest.mark.asyncio
async def test_legacy_real_output_without_policy_fails_before_any_provider_call(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "outputs"
    output_root.mkdir()
    facade = RecordingFacade()

    with pytest.raises(AuthorityDeniedError, match="disabled by authority policy"):
        await Executor(facade).execute(
            _legacy_export("part.step"),
            ExecutionContext(mode="real", output_dir=output_root),
        )

    assert facade.calls == []
    assert facade.exports == []


@pytest.mark.asyncio
async def test_entire_legacy_output_graph_is_rejected_before_first_dispatch(
    tmp_path: Path,
) -> None:
    broker, output_root = _broker(tmp_path)
    payload = _legacy_export("first.step").model_dump(mode="json")
    payload["components"][0]["features"].append(
        {
            "name": "second_export",
            "type": "export",
            "inputs": {
                "target": "part_body",
                "format": "step",
                "path": "../escape.step",
            },
        }
    )
    facade = RecordingFacade()

    with pytest.raises(ValueError, match="under output_dir"):
        await Executor(facade, authority_broker=broker).execute(
            CadSpec.model_validate(payload),
            ExecutionContext(mode="real", output_dir=output_root),
        )

    assert facade.calls == []
    assert facade.exports == []


@pytest.mark.asyncio
async def test_legacy_capture_uses_png_capability_and_canonical_sink_path(
    tmp_path: Path,
) -> None:
    broker, output_root = _broker(tmp_path)
    (output_root / "captures").mkdir()
    facade = RecordingFacade()

    await Executor(facade, authority_broker=broker).execute(
        _legacy_capture("captures/part.png"),
        ExecutionContext(
            mode="real", output_dir=output_root, session_id="capture-positive"
        ),
    )

    assert facade.captures == [str((output_root / "captures" / "part.png").resolve())]


@pytest.mark.asyncio
async def test_direct_capture_uses_the_same_single_use_authority_boundary(
    tmp_path: Path,
) -> None:
    base_broker, output_root = _broker(tmp_path)
    broker = CapturingAuthorityBroker(
        base_broker.policy, ledger=CapabilityLedger(tmp_path / "capturing-ledger")
    )
    facade = RecordingFacade()

    payload = await Executor(facade, authority_broker=broker).capture_viewport(
        context=ExecutionContext(
            mode="real", output_dir=output_root, session_id="direct-capture"
        ),
        name="active_design",
        path="active_design.png",
        view="isometric",
        isolate_prefix=None,
        width=800,
        height=600,
    )

    expected = str((output_root / "active_design.png").resolve())
    assert facade.captures == [expected]
    assert payload["screenshot"]["path"] == expected
    assert broker.last_graph is not None
    bound = broker.last_graph.operations[0]
    assert bound.capability is not None
    assert broker.ledger.state(bound.capability.capability_id) == "consumed"
    with pytest.raises(AuthorityDeniedError, match="replay denied"):
        broker.claim(bound)
    assert facade.captures == [expected]


@pytest.mark.asyncio
async def test_mock_and_dry_run_preserve_confined_legacy_compatibility(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "outputs"
    output_root.mkdir()
    facade = RecordingFacade()

    await Executor(facade).execute(
        _legacy_capture("mock.png"),
        ExecutionContext(mode="mock", output_dir=output_root),
    )
    simulated = await Executor().execute(
        _legacy_export("dry.step"),
        ExecutionContext(mode="real", output_dir=output_root, dry_run=True),
    )

    assert facade.captures == [str((output_root / "mock.png").resolve())]
    assert simulated.exports == [str((output_root / "dry.step").resolve())]


@pytest.mark.asyncio
async def test_legacy_output_failure_becomes_unknown_and_cannot_replay(
    tmp_path: Path,
) -> None:
    base_broker, output_root = _broker(tmp_path)
    broker = CapturingAuthorityBroker(
        base_broker.policy, ledger=CapabilityLedger(tmp_path / "unknown-ledger")
    )

    class FailingFacade(RecordingFacade):
        async def capture_viewport(self, *, path, **_kwargs):
            self.calls.append("capture_viewport")
            self.captures.append(str(path))
            raise RuntimeError("provider outcome unavailable")

    facade = FailingFacade()
    with pytest.raises(RuntimeError, match="outcome unavailable"):
        await Executor(facade, authority_broker=broker).capture_viewport(
            context=ExecutionContext(
                mode="real", output_dir=output_root, session_id="unknown-capture"
            ),
            name="active_design",
            path="active_design.png",
            view="isometric",
            isolate_prefix=None,
            width=800,
            height=600,
        )

    assert broker.last_graph is not None
    bound = broker.last_graph.operations[0]
    assert bound.capability is not None
    assert broker.ledger.state(bound.capability.capability_id) == "unknown"
    with pytest.raises(AuthorityDeniedError, match="replay denied"):
        broker.claim(bound)
    assert facade.calls == ["capture_viewport"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path", ["../escape.png", "nested/../../escape.png", "C:/escape.png"]
)
async def test_legacy_capture_bypasses_are_zero_dispatch(
    tmp_path: Path, path: str
) -> None:
    broker, output_root = _broker(tmp_path)
    facade = RecordingFacade()

    with pytest.raises(ValueError):
        await Executor(facade, authority_broker=broker).execute(
            _legacy_capture(path),
            ExecutionContext(mode="real", output_dir=output_root),
        )

    assert facade.calls == []
    assert facade.captures == []


@pytest.mark.asyncio
async def test_legacy_overwrite_requires_policy_and_operation_opt_in(
    tmp_path: Path,
) -> None:
    broker, output_root = _broker(tmp_path)
    destination = output_root / "part.step"
    destination.write_text("existing", encoding="utf-8")
    facade = RecordingFacade()

    with pytest.raises(AuthorityDeniedError, match="both policy and operation"):
        await Executor(facade, authority_broker=broker).execute(
            _legacy_export("part.step", overwrite=True),
            ExecutionContext(mode="real", output_dir=output_root),
        )
    assert facade.calls == []

    approved_root = tmp_path / "approved"
    approved_root.mkdir()
    approved_broker, approved_output = _broker(approved_root, allow_overwrite=True)
    approved_destination = approved_output / "part.step"
    approved_destination.write_text("existing", encoding="utf-8")
    approved_facade = RecordingFacade()
    await Executor(approved_facade, authority_broker=approved_broker).execute(
        _legacy_export("part.step", overwrite=True),
        ExecutionContext(mode="real", output_dir=approved_output),
    )
    assert approved_facade.exports == [
        ("step", "part_body", str(approved_destination.resolve()))
    ]


@pytest.mark.asyncio
async def test_sink_revalidation_blocks_parent_identity_swap_before_export(
    tmp_path: Path,
) -> None:
    broker, output_root = _broker(tmp_path)
    stable_parent = output_root / "stable"
    stable_parent.mkdir()

    class SwappingFacade(RecordingFacade):
        async def inspect_design(self):
            await super().inspect_design()
            stable_parent.rename(output_root / "stable-original")
            stable_parent.mkdir()
            return {}

    facade = SwappingFacade()
    with pytest.raises(AuthorityDeniedError, match="changed before dispatch"):
        await Executor(facade, authority_broker=broker).execute(
            _legacy_export("stable/part.step"),
            ExecutionContext(mode="real", output_dir=output_root),
        )

    assert facade.exports == []
    assert "export_step" not in facade.calls
