from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_core.request_context import RequestContext, current_request_context
from benchmark_parametric_suite import run_reference_suite as reference_suite
from fusion_agent_mcp import server
from fusion_agent_mcp.profiles import ToolProfileError
from fusion_agent_mcp.runtime import FusionAgentRuntime, RuntimeConfiguration


class _AllowedLint:
    allowed = True

    def as_dict(self) -> dict[str, Any]:
        return {"allowed": True, "script_sha256": "a" * 64}


class _Lifecycle:
    def __init__(self) -> None:
        self.context: Any | None = None

    async def list_open_document_ids(self) -> list[str]:
        return []

    async def prepare_fixture(self, context: Any) -> Any:
        self.context = context
        return SimpleNamespace(
            original_document_id="document:original",
            fixture_document_id=f"marker:{context.fixture_marker}",
            fixture_marker=context.fixture_marker,
            fixture_fingerprint=hashlib.sha256(
                context.fixture_marker.encode("utf-8")
            ).hexdigest(),
            unsaved=True,
        )

    async def read_fixture_identity(self, _context: Any, session: Any) -> Any:
        return SimpleNamespace(
            document_id=session.fixture_document_id,
            fixture_marker=session.fixture_marker,
            fixture_fingerprint=session.fixture_fingerprint,
            unsaved=True,
        )


@pytest.mark.asyncio
async def test_reference_runner_passes_advanced_trial_bound_context_to_fast_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    request = {
        "mode": "real",
        "intent": "reviewed disposable fixture",
        "change_class": "additive",
        "target_component_paths": [],
    }
    eco_request = {
        "mode": "real",
        "intent": "reviewed fixture update",
        "change_class": "scoped_update",
        "target_query_ids": ["fixture_body"],
    }
    calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    monkeypatch.setattr(
        reference_suite,
        "_load_case",
        lambda case_id: (
            {
                "case_id": case_id,
                "title": "Reviewed fixture",
                "eco": {"id": "reviewed_eco"},
            },
            "reviewed_build_script",
            "reviewed_oracle_script",
            "reviewed_eco_script",
            "reviewed_eco_oracle_script",
        ),
    )
    monkeypatch.setattr(reference_suite, "_build_request", lambda *_: request)
    monkeypatch.setattr(reference_suite, "_eco_request", lambda *_: eco_request)
    monkeypatch.setattr(
        reference_suite, "lint_fusion_script", lambda *_a, **_k: _AllowedLint()
    )

    async def execute_probe(
        name: str, arguments: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        calls.append((name, arguments, kwargs))
        return {"status": "applied_verified"}

    async def oracle_probe(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"passed": True, "failed_checks": []}

    async def image_probe(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return []

    async def cleanup_probe(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "closed_without_save": True,
            "restored": True,
            "active_document_id": "document:original",
            "original_document_id": "document:original",
            "inventory_restored": True,
        }

    monkeypatch.setattr(reference_suite, "execute_tool", execute_probe)
    monkeypatch.setattr(reference_suite, "_run_oracle", oracle_probe)
    monkeypatch.setattr(reference_suite, "_capture_images", image_probe)
    monkeypatch.setattr(reference_suite, "_safe_cleanup", cleanup_probe)
    artifact_root = tmp_path / "reference"
    artifact_root.mkdir()
    runtime = SimpleNamespace(configuration=SimpleNamespace(backend="autodesk_http"))
    lifecycle = _Lifecycle()

    await reference_suite._run_case(  # type: ignore[arg-type]
        runtime,
        lifecycle,  # type: ignore[arg-type]
        "case_a",
        "run_a",
        artifact_root,
    )

    assert len(calls) == 2
    assert [arguments for _, arguments, _ in calls] == [request, eco_request]
    assert lifecycle.context is not None
    for phase, (name, arguments, kwargs) in zip(("initial", "eco"), calls, strict=True):
        assert name == "fusion_agent_fast_execute"
        assert kwargs["profile"] == "advanced"
        parent = kwargs["request_context"]
        assert isinstance(parent, RequestContext)
        assert parent.request_id.endswith(f":{phase}")
        assert parent.profile == "advanced"
        assert parent.mode == "real"
        assert parent.backend == "autodesk_http"
        assert parent.session_id == "run_a"
        assert parent.trial_id == lifecycle.context.trial_id
        assert parent.document_identity == (
            f"runtime:marker:{lifecycle.context.fixture_marker}"
        )
        assert (
            parent.spec_digest
            == hashlib.sha256(
                json.dumps(
                    arguments,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    default=str,
                ).encode("utf-8")
            ).hexdigest()
        )
        assert set(parent.capabilities) == {
            "fast_path:enabled",
            "execution_path:native_fast",
            "benchmark_fixture:"
            + hashlib.sha256(
                lifecycle.context.fixture_marker.encode("utf-8")
            ).hexdigest(),
        }


@pytest.mark.asyncio
async def test_derived_fast_path_context_is_trial_local_and_profile_gated(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("FUSION_AGENT_FAST_PATH_MODE", "read_only")
    configuration = RuntimeConfiguration.from_environment()
    assert configuration.fast_path_mode == "read_only"
    runtime = FusionAgentRuntime(
        manifest_root=tmp_path / "manifests",
        outputs_root=tmp_path / "outputs",
        configuration=configuration,
    )
    observed: dict[str, RequestContext] = {}
    entered = asyncio.Event()
    release = asyncio.Event()

    async def handler(arguments: server.JsonDict) -> server.JsonDict:
        active = current_request_context()
        assert active is not None
        observed[arguments["key"]] = active
        assert server._fast_path_mode() == "enabled"
        assert server._execution_path() == "native_fast"
        if arguments["key"] == "first":
            entered.set()
            await release.wait()
        else:
            await entered.wait()
            release.set()
        return {"key": arguments["key"]}

    spec = server.ToolSpec(
        name="fusion_agent_fast_execute",
        description="Reference-suite context regression handler.",
        input_schema={"type": "object"},
        output_schema=server._open_output_schema(),
        handler=handler,
        profiles=("advanced", "all"),
    )
    monkeypatch.setattr(server, "_tool_spec_map", lambda: {spec.name: spec})

    def parent(key: str) -> RequestContext:
        context = SimpleNamespace(
            run_id=f"run-{key}",
            trial_id=f"trial-{key}",
            mode="real",
            dry_run=False,
            execution_path="native_fast",
            fixture_marker=f"marker-{key}",
        )
        session = SimpleNamespace(
            fixture_document_id=f"marker:marker-{key}",
            fixture_marker=f"marker-{key}",
            fixture_fingerprint=hashlib.sha256(key.encode("utf-8")).hexdigest(),
            unsaved=True,
        )
        return reference_suite._fast_path_request_context(
            runtime,
            context,  # type: ignore[arg-type]
            session,
            {"key": key},
            phase="initial",
        )

    parents = {key: parent(key) for key in ("first", "second")}
    try:
        responses = await asyncio.gather(
            *(
                server.execute_tool_response(
                    spec.name,
                    {"key": key},
                    runtime=runtime,
                    profile="advanced",
                    request_context=parents[key],
                )
                for key in ("first", "second")
            )
        )
        with pytest.raises(ToolProfileError):
            await server.execute_tool_response(
                spec.name,
                {"key": "benchmark-public"},
                runtime=runtime,
                profile="benchmark",
                request_context=parents["first"],
            )
    finally:
        await runtime.close()

    assert [response.payload["key"] for response in responses] == ["first", "second"]
    assert current_request_context() is None
    for key in ("first", "second"):
        active = observed[key]
        expected = parents[key]
        assert active is not expected
        assert active.profile == "advanced"
        assert active.session_id == expected.session_id
        assert active.trial_id == expected.trial_id
        assert active.document_identity == expected.document_identity
        assert active.spec_digest == expected.spec_digest
        assert set(active.capabilities).issuperset(expected.capabilities)
        own_fingerprint = hashlib.sha256(key.encode("utf-8")).hexdigest()
        other_key = "second" if key == "first" else "first"
        other_fingerprint = hashlib.sha256(other_key.encode("utf-8")).hexdigest()
        assert f"benchmark_fixture:{own_fingerprint}" in active.capabilities
        assert f"benchmark_fixture:{other_fingerprint}" not in active.capabilities


def test_reference_context_accepts_only_normalizable_lifecycle_document_ids() -> None:
    runtime = SimpleNamespace(configuration=SimpleNamespace(backend="autodesk_http"))
    context = SimpleNamespace(
        run_id="run-normalize",
        trial_id="trial-normalize",
        mode="real",
        dry_run=False,
        execution_path="native_fast",
        fixture_marker="fixture-marker",
    )
    fingerprint = hashlib.sha256(b"fixture-marker").hexdigest()

    def session(document_id: str) -> Any:
        return SimpleNamespace(
            fixture_document_id=document_id,
            fixture_marker="fixture-marker",
            fixture_fingerprint=fingerprint,
            unsaved=True,
        )

    marker = reference_suite._fast_path_request_context(
        runtime,  # type: ignore[arg-type]
        context,  # type: ignore[arg-type]
        session("marker:fixture-marker"),
        {"phase": "marker"},
        phase="initial",
    )
    data = reference_suite._fast_path_request_context(
        runtime,  # type: ignore[arg-type]
        context,  # type: ignore[arg-type]
        session("data:urn:adsk.wipprod:dm.lineage:test"),
        {"phase": "data"},
        phase="initial",
    )

    assert marker.document_identity == "runtime:marker:fixture-marker"
    assert data.document_identity == "data:urn:adsk.wipprod:dm.lineage:test"
    for invalid_document_id in (
        "document:unbound",
        "runtime:marker:fixture-marker",
        "marker:other-marker",
        "data:",
        " data:leading-space",
    ):
        with pytest.raises(RuntimeError, match="fixture binding is incomplete"):
            reference_suite._fast_path_request_context(
                runtime,  # type: ignore[arg-type]
                context,  # type: ignore[arg-type]
                session(invalid_document_id),
                {"phase": "invalid"},
                phase="initial",
            )


def test_public_profiles_and_nightly_workflow_do_not_globally_authorize_fast_path() -> (
    None
):
    normal_names = {tool.name for tool in server.list_tool_definitions("normal")}
    benchmark_names = {tool.name for tool in server.list_tool_definitions("benchmark")}
    advanced_names = {tool.name for tool in server.list_tool_definitions("advanced")}
    assert "fusion_agent_fast_execute" not in normal_names
    assert "fusion_agent_fast_execute" not in benchmark_names
    assert "fusion_agent_fast_execute" in advanced_names

    repository_root = Path(__file__).resolve().parents[2]
    workflow = (
        repository_root / ".github" / "workflows" / "fusion-real-nightly.yml"
    ).read_text(encoding="utf-8")
    assert "FUSION_AGENT_FAST_PATH_MODE" not in workflow
