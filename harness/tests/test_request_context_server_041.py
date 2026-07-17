from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from agent_core.request_context import (
    RequestContext,
    bind_request_context,
    current_request_context,
)
from fusion_agent_mcp import profiles, server
from fusion_agent_mcp.runtime import (
    FusionAgentRuntime,
    RuntimeConfiguration,
    _host_io_platform_status,
)


def _parent_context(
    name: str,
    *,
    capabilities: tuple[str, ...] = (
        "fast_path:enabled",
        "execution_path:native_fast",
    ),
) -> RequestContext:
    return RequestContext(
        request_id=f"parent-{name}",
        session_id=f"session-{name}",
        trial_id=f"trial-{name}",
        profile="all",
        mode="mock",
        backend="test-backend",
        document_identity=f"document-{name}",
        spec_digest="f" * 64,
        timeouts={"operation": 12.5},
        capabilities=capabilities,
    )


@pytest.mark.parametrize("mode", ("REAL", "production"))
def test_request_context_rejects_noncanonical_mode(mode: str) -> None:
    with pytest.raises(ValueError, match="mode must be 'mock' or 'real'"):
        RequestContext(
            request_id="request-invalid-mode",
            profile="normal",
            mode=mode,  # type: ignore[arg-type]
            backend="autodesk_http",
        )


def _tool_spec(name: str, handler: server.Handler) -> server.ToolSpec:
    return server.ToolSpec(
        name=name,
        description="RequestContext boundary test tool.",
        input_schema={"type": "object"},
        output_schema=server._open_output_schema(),
        handler=handler,
        profiles=("all",),
    )


@pytest.mark.asyncio
async def test_request_execution_never_rereads_environment_after_startup_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    configuration = RuntimeConfiguration.from_environment()
    runtime = FusionAgentRuntime(
        manifest_root=tmp_path / "manifests",
        outputs_root=tmp_path / "outputs",
        configuration=configuration,
    )

    async def handler(_: server.JsonDict) -> server.JsonDict:
        active = current_request_context()
        assert active is not None
        return {"profile": active.profile, "mode": active.mode}

    spec = server.ToolSpec(
        name="fusion_agent_test_snapshot",
        description="Startup snapshot regression tool.",
        input_schema={"type": "object"},
        output_schema=server._open_output_schema(),
        handler=handler,
        profiles=("normal",),
    )
    monkeypatch.setattr(server, "_tool_spec_map", lambda: {spec.name: spec})

    def forbidden_getenv(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("request execution must not reread process environment")

    monkeypatch.setattr(profiles.os, "getenv", forbidden_getenv)
    try:
        response = await server.execute_tool_response(
            spec.name,
            {"mode": "mock"},
            runtime=runtime,
        )
    finally:
        await runtime.close()

    assert response.payload == {"profile": "normal", "mode": "mock"}


@pytest.mark.asyncio
async def test_nested_tool_calls_bind_unique_context_and_restore_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, RequestContext] = {}

    async def inner_handler(_: server.JsonDict) -> server.JsonDict:
        active = current_request_context()
        assert active is not None
        observed["inner"] = active
        return {"scope": "inner"}

    async def outer_handler(_: server.JsonDict) -> server.JsonDict:
        active = current_request_context()
        assert active is not None
        observed["outer"] = active
        await server.execute_tool_response(
            "fusion_agent_test_inner",
            {"nested": True},
            profile="all",
        )
        assert current_request_context() is active
        return {"scope": "outer"}

    specs = {
        "fusion_agent_test_outer": _tool_spec("fusion_agent_test_outer", outer_handler),
        "fusion_agent_test_inner": _tool_spec("fusion_agent_test_inner", inner_handler),
    }
    monkeypatch.setattr(server, "_tool_spec_map", lambda: specs)
    parent = _parent_context("nested")

    with bind_request_context(parent):
        response = await server.execute_tool_response(
            "fusion_agent_test_outer",
            {"mode": "mock", "outer": True},
            profile="all",
        )
        assert current_request_context() is parent

    assert response.payload == {"scope": "outer"}
    assert current_request_context() is None
    outer = observed["outer"]
    inner = observed["inner"]
    assert outer.request_id != inner.request_id
    assert outer.request_id.startswith("mcp_")
    assert inner.request_id.startswith("mcp_")
    assert outer.session_id == inner.session_id == parent.session_id
    assert outer.trial_id == inner.trial_id == parent.trial_id
    assert outer.document_identity == inner.document_identity == "document-nested"
    assert outer.timeouts == inner.timeouts
    assert outer.timeouts["operation"] == 12.5
    assert outer.timeouts["trusted_read"] == 10.0
    assert outer.limits == inner.limits
    assert outer.limits["protected_script_bytes"] == 28 * 1024
    assert len(outer.spec_digest or "") == 64
    assert len(inner.spec_digest or "") == 64
    assert "tool:fusion_agent_test_outer" in outer.capabilities
    assert "tool:fusion_agent_test_inner" in inner.capabilities
    assert "tool:fusion_agent_test_outer" not in inner.capabilities


@pytest.mark.asyncio
async def test_tool_context_restores_after_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failing_handler(_: server.JsonDict) -> server.JsonDict:
        assert current_request_context() is not None
        raise RuntimeError("private downstream failure")

    monkeypatch.setattr(
        server,
        "_tool_spec_map",
        lambda: {
            "fusion_agent_test_failure": _tool_spec(
                "fusion_agent_test_failure", failing_handler
            )
        },
    )
    parent = _parent_context("exception")

    with bind_request_context(parent):
        with pytest.raises(RuntimeError, match="private downstream failure"):
            await server.execute_tool_response(
                "fusion_agent_test_failure", profile="all"
            )
        assert current_request_context() is parent

    assert current_request_context() is None


@pytest.mark.asyncio
async def test_tool_context_restores_after_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    never = asyncio.Event()
    restored: list[RequestContext | None] = []

    async def waiting_handler(_: server.JsonDict) -> server.JsonDict:
        assert current_request_context() is not None
        started.set()
        await never.wait()
        return {}

    monkeypatch.setattr(
        server,
        "_tool_spec_map",
        lambda: {
            "fusion_agent_test_cancel": _tool_spec(
                "fusion_agent_test_cancel", waiting_handler
            )
        },
    )
    parent = _parent_context("cancel")

    async def invoke() -> None:
        try:
            await server.execute_tool_response(
                "fusion_agent_test_cancel", profile="all"
            )
        finally:
            restored.append(current_request_context())

    with bind_request_context(parent):
        task = asyncio.create_task(invoke())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert restored == [parent]
        assert current_request_context() is parent

    assert current_request_context() is None


@pytest.mark.asyncio
async def test_concurrent_tool_calls_do_not_cross_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_entered = asyncio.Event()
    second_entered = asyncio.Event()
    observed: dict[str, tuple[RequestContext, RequestContext]] = {}

    async def handler(args: server.JsonDict) -> server.JsonDict:
        key = str(args["key"])
        before = current_request_context()
        assert before is not None
        if key == "first":
            first_entered.set()
            await second_entered.wait()
        else:
            second_entered.set()
            await first_entered.wait()
        await asyncio.sleep(0)
        after = current_request_context()
        assert after is not None
        observed[key] = (before, after)
        return {"key": key}

    monkeypatch.setattr(
        server,
        "_tool_spec_map",
        lambda: {
            "fusion_agent_test_concurrent": _tool_spec(
                "fusion_agent_test_concurrent", handler
            )
        },
    )
    first = _parent_context(
        "first",
        capabilities=("fast_path:enabled", "execution_path:native_fast"),
    )
    second = _parent_context(
        "second",
        capabilities=("fast_path:read_only", "execution_path:safe_harness"),
    )

    responses = await asyncio.gather(
        server.execute_tool_response(
            "fusion_agent_test_concurrent",
            {"key": "first"},
            profile="all",
            request_context=first,
        ),
        server.execute_tool_response(
            "fusion_agent_test_concurrent",
            {"key": "second"},
            profile="all",
            request_context=second,
        ),
    )

    assert [response.payload["key"] for response in responses] == ["first", "second"]
    assert current_request_context() is None
    for key, parent in (("first", first), ("second", second)):
        before, after = observed[key]
        assert before is after
        assert before.session_id == parent.session_id
        assert before.trial_id == parent.trial_id
        assert before.document_identity == parent.document_identity
        assert set(before.capabilities).issuperset(parent.capabilities)


def test_fast_path_authorization_uses_only_request_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FUSION_AGENT_FAST_PATH_MODE", "enabled")
    monkeypatch.setenv("FUSION_AGENT_EXECUTION_PATH", "safe_harness")
    monkeypatch.setenv("FUSION_AGENT_BENCHMARK_ROUTE_LOCK", "native_fast")
    monkeypatch.setenv("FUSION_AGENT_BENCHMARK_TRIAL_ID", "forged-trial")
    context = _parent_context(
        "request-local",
        capabilities=("execution_path:native_fast",),
    )

    with bind_request_context(context):
        assert server._fast_path_mode() == "read_only"
        assert server._execution_path() == "native_fast"

    conflict = _parent_context(
        "conflict",
        capabilities=("fast_path:enabled", "fast_path:read_only"),
    )
    with (
        bind_request_context(conflict),
        pytest.raises(ValueError, match="conflicting fast_path"),
    ):
        server._fast_path_mode()


def test_public_runtime_diagnostics_exposes_only_safe_authority_summary() -> None:
    public = server._public_runtime_diagnostics(
        {
            "state": "ready",
            "authority_policy": {
                "digest": "a" * 64,
                "io_enabled": True,
                "import_enabled": True,
                "output_enabled": False,
                "output_policy": "deny_io",
                "overwrite_supported": False,
                "root_ids": {
                    "import": ["imports"],
                    "export": ["exports"],
                    "private": ["must-not-escape"],
                },
                "import_roots": [r"C:\private\imports"],
                "export_roots": [r"C:\private\exports"],
                "secret": "must-not-escape",
            },
            "host_io_platform": {
                "import_staging": "sealed_windows_handle",
                "output_staging": "deny_io",
                "private": r"C:\private\stage",
            },
        }
    )

    assert public == {
        "state": "ready",
        "authority_policy": {
            "digest": "a" * 64,
            "io_enabled": True,
            "import_enabled": True,
            "output_enabled": False,
            "output_policy": "deny_io",
            "overwrite_supported": False,
            "root_ids": {"import": ["imports"]},
        },
        "host_io_platform": {
            "import_staging": "sealed_windows_handle",
            "output_staging": "deny_io",
        },
    }
    serialized = str(public)
    assert "C:\\private" not in serialized
    assert "must-not-escape" not in serialized


def test_host_io_platform_status_is_explicit_and_fail_closed() -> None:
    status = _host_io_platform_status()

    if os.name == "nt":
        assert status == {
            "import_staging": "sealed_windows_handle",
            "output_staging": "deny_io",
        }
    elif os.name == "posix" and sys.platform.startswith("linux"):
        assert status["import_staging"] in {
            "sealed_memfd",
            "fail_closed_unavailable",
        }
        assert status["output_staging"] == "deny_io"
    else:
        assert status == {
            "import_staging": "fail_closed_unavailable",
            "output_staging": "deny_io",
        }
