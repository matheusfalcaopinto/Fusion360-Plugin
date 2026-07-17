from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_core.authority import (
    AuthorityBroker,
    AuthorityPolicy,
    CapabilityLedger,
    HostOutputDisabledError,
)
from agent_core.executor import ExecutionContext, Executor, _safe_output_path
from agent_core.session_controller import SessionController, SessionOptions
from cad_spec.models import CadSpec


class RecordingFacade:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.host_io_preflights: list[tuple[str, bool]] = []
        self.exports: list[tuple[str, str, str]] = []
        self.captures: list[str] = []
        self.export_authority: list[dict[str, object]] = []
        self.capture_authority: list[dict[str, object]] = []

    def require_secure_host_io_platform(
        self, direction: str, *, overwrite: bool = False
    ) -> None:
        self.host_io_preflights.append((direction, overwrite))

    async def inspect_design(self):
        self.calls.append("inspect_design")
        return {}

    async def create_component(self, name):
        self.calls.append("create_component")
        return {"name": name}

    async def activate_component(self, name):
        self.calls.append("activate_component")
        return {"name": name}

    async def resolve_document_binding(self):
        return {
            "binding": {
                "reference_kind": "active_document",
                "requested_ref": "active_document",
                "document_identity": "d" * 64,
                "entity_identity": "e" * 64,
                "fingerprint": "f" * 64,
            }
        }

    async def resolve_export_target_binding(self, target, format_name):
        del format_name
        return {
            "binding": {
                "reference_kind": "export_target",
                "requested_ref": target,
                "document_identity": "d" * 64,
                "entity_identity": "a" * 64,
                "fingerprint": "b" * 64,
            }
        }

    async def export_step(self, target, path, **authority):
        self.calls.append("export_step")
        self.exports.append(("step", target, str(path)))
        self.export_authority.append(authority)

    async def export_stl(self, target, path, **authority):
        self.calls.append("export_stl")
        self.exports.append(("stl", target, str(path)))
        self.export_authority.append(authority)

    async def capture_viewport(self, *, path, **authority):
        self.calls.append("capture_viewport")
        self.captures.append(str(path))
        self.capture_authority.append(authority)
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


def _legacy_capture(path: str, *, overwrite: bool = False) -> CadSpec:
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
                            "inputs": {
                                "path": path,
                                "view": "isometric",
                                "overwrite": overwrite,
                            },
                        }
                    ],
                }
            ],
            "acceptance_tests": [{"type": "body_exists", "target": "part_body"}],
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ("session", "capture"))
async def test_controller_denies_real_output_before_facade_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
) -> None:
    controller = SessionController(real_client=object())
    facade_builds = 0

    async def fail_if_built(*_args, **_kwargs):
        nonlocal facade_builds
        facade_builds += 1
        raise AssertionError("real facade must not be constructed for deny_io")

    monkeypatch.setattr(controller, "_build_facade", fail_if_built)
    options = SessionOptions(
        mode="real",
        workspace_root=tmp_path / "workspace",
        output_dir=tmp_path / "outputs",
    )
    with pytest.raises(HostOutputDisabledError, match="disabled by deny_io"):
        if route == "session":
            await controller.run_spec(
                _legacy_export("part.step"), mode="real", options=options
            )
        else:
            await controller.capture_viewport(
                mode="real",
                options=options,
                output_dir=options.output_dir,
            )

    assert facade_builds == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ("REAL", "production"))
async def test_noncanonical_mode_is_zero_dispatch_before_output_or_discovery(
    tmp_path: Path,
    mode: str,
) -> None:
    class NeverClient:
        def __init__(self) -> None:
            self.calls = 0

        async def list_tools(self):
            self.calls += 1
            raise AssertionError("invalid mode must not reach provider discovery")

    client = NeverClient()
    controller = SessionController(real_client=client)
    output_dir = tmp_path / "must-not-exist"

    with pytest.raises(ValueError, match="mode must be 'mock' or 'real'"):
        await controller.capture_viewport(mode=mode, output_dir=output_dir)
    with pytest.raises(ValueError, match="mode must be 'mock' or 'real'"):
        await controller.discover_tools(mode=mode)
    with pytest.raises(ValueError):
        ExecutionContext(mode=mode)

    assert client.calls == 0
    assert not output_dir.exists()


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
async def test_legacy_relative_export_is_deny_io_before_binding_or_dispatch(
    tmp_path: Path,
) -> None:
    broker, output_root = _broker(tmp_path)
    (output_root / "nested").mkdir()
    facade = RecordingFacade()
    with pytest.raises(HostOutputDisabledError, match="disabled by deny_io") as error:
        await Executor(facade, authority_broker=broker).execute(
            _legacy_export("nested/part.step"),
            ExecutionContext(
                mode="real", output_dir=output_root, session_id="legacy-denied"
            ),
        )
    assert error.value.error_code == "HOST_OUTPUT_DISABLED"
    assert facade.calls == []
    assert facade.exports == []
    assert facade.host_io_preflights == []


@pytest.mark.asyncio
async def test_legacy_real_output_without_policy_fails_before_any_provider_call(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "outputs"
    output_root.mkdir()
    facade = RecordingFacade()

    with pytest.raises(HostOutputDisabledError, match="disabled by deny_io"):
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
async def test_legacy_capture_is_deny_io_before_capability_or_sink(
    tmp_path: Path,
) -> None:
    broker, output_root = _broker(tmp_path)
    (output_root / "captures").mkdir()
    facade = RecordingFacade()

    with pytest.raises(HostOutputDisabledError, match="disabled by deny_io"):
        await Executor(facade, authority_broker=broker).execute(
            _legacy_capture("captures/part.png"),
            ExecutionContext(
                mode="real", output_dir=output_root, session_id="capture-denied"
            ),
        )

    assert facade.calls == []
    assert facade.captures == []
    assert facade.capture_authority == []
    assert facade.host_io_preflights == []


@pytest.mark.asyncio
async def test_direct_real_capture_is_deny_io_without_capability_issue(
    tmp_path: Path,
) -> None:
    base_broker, output_root = _broker(tmp_path)
    broker = CapturingAuthorityBroker(
        base_broker.policy, ledger=CapabilityLedger(tmp_path / "capturing-ledger")
    )
    facade = RecordingFacade()

    with pytest.raises(HostOutputDisabledError, match="disabled by deny_io"):
        await Executor(facade, authority_broker=broker).capture_viewport(
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

    assert broker.last_graph is None
    assert facade.calls == []
    assert facade.captures == []


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
    assert facade.host_io_preflights == []
    assert simulated.exports == [str((output_root / "dry.step").resolve())]


@pytest.mark.asyncio
async def test_legacy_output_never_reaches_unknown_provider_outcome(
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
    with pytest.raises(HostOutputDisabledError, match="disabled by deny_io"):
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

    assert broker.last_graph is None
    assert facade.calls == []
    assert facade.captures == []


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
async def test_legacy_overwrite_field_is_compatible_but_real_output_is_deny_io(
    tmp_path: Path,
) -> None:
    broker, output_root = _broker(tmp_path)
    destination = output_root / "part.step"
    destination.write_text("existing", encoding="utf-8")
    facade = RecordingFacade()

    with pytest.raises(HostOutputDisabledError, match="disabled by deny_io"):
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
    with pytest.raises(HostOutputDisabledError, match="disabled by deny_io"):
        await Executor(approved_facade, authority_broker=approved_broker).execute(
            _legacy_export("part.step", overwrite=True),
            ExecutionContext(mode="real", output_dir=approved_output),
        )
    assert approved_destination.read_text(encoding="utf-8") == "existing"
    assert approved_facade.calls == []
    assert approved_facade.exports == []


@pytest.mark.asyncio
async def test_all_platform_legacy_capture_overwrite_is_deny_io_before_binding(
    tmp_path: Path,
) -> None:
    broker, output_root = _broker(tmp_path, allow_overwrite=True)
    destination = output_root / "existing.png"
    destination.write_bytes(b"existing")

    class LinuxOverwriteFacade(RecordingFacade):
        def __init__(self) -> None:
            super().__init__()
            self.binding_calls = 0
            self.provider_calls = 0

        def require_secure_host_io_platform(
            self, direction: str, *, overwrite: bool = False
        ) -> None:
            super().require_secure_host_io_platform(direction, overwrite=overwrite)
            if direction == "export" and overwrite:
                raise RuntimeError("secure POSIX overwrite is unavailable")

        async def resolve_document_binding(self):
            self.binding_calls += 1
            return await super().resolve_document_binding()

        async def capture_viewport(self, *, path, **authority):
            self.provider_calls += 1
            host_path_binding = authority.get("host_path_binding") or {}
            self.require_secure_host_io_platform(
                "export", overwrite=host_path_binding.get("overwrite") is True
            )
            return await super().capture_viewport(path=path, **authority)

    facade = LinuxOverwriteFacade()

    with pytest.raises(HostOutputDisabledError, match="disabled by deny_io"):
        await Executor(facade, authority_broker=broker).execute(
            _legacy_capture("existing.png", overwrite=True),
            ExecutionContext(
                mode="real",
                output_dir=output_root,
                session_id="legacy-linux-overwrite",
            ),
        )

    assert facade.host_io_preflights == []
    assert facade.binding_calls == 0
    assert facade.provider_calls == 0
    assert facade.calls == []
    assert facade.captures == []


@pytest.mark.asyncio
async def test_real_legacy_output_without_host_io_preflight_fails_closed(
    tmp_path: Path,
) -> None:
    broker, output_root = _broker(tmp_path)
    facade = RecordingFacade()
    facade.require_secure_host_io_platform = None  # type: ignore[method-assign]

    with pytest.raises(HostOutputDisabledError, match="disabled by deny_io"):
        await Executor(facade, authority_broker=broker).execute(
            _legacy_capture("capture.png"),
            ExecutionContext(mode="real", output_dir=output_root),
        )

    assert facade.calls == []
    assert facade.captures == []


@pytest.mark.asyncio
async def test_deny_io_precedes_legacy_export_sink_revalidation(
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
    with pytest.raises(HostOutputDisabledError, match="disabled by deny_io"):
        await Executor(facade, authority_broker=broker).execute(
            _legacy_export("stable/part.step"),
            ExecutionContext(mode="real", output_dir=output_root),
        )

    assert facade.exports == []
    assert "export_step" not in facade.calls
    assert stable_parent.is_dir()
