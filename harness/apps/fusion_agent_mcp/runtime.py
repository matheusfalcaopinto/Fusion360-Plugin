"""Process-scoped runtime shared by every public Fusion Agent tool."""

from __future__ import annotations

import ast
import asyncio
import base64
import hashlib
import json
import math
import os
import re
import time
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_core.authority import (
    AuthorityBroker,
    AuthorityDeniedError,
    AuthorityPolicy,
    CapabilityLedger,
)
from agent_core.capability_executor import CapabilityExecutionResult, CapabilityExecutor
from agent_core.fast_path import FastPathService
from agent_core.request_context import current_request_context
from agent_core.session_controller import SessionController
from cad_spec.v2 import CadSpecV2, OperationSpec
from fusion_agent_mcp import __version__
from fusion_mcp_adapter.adapter import FusionMcpAdapter
from fusion_mcp_adapter.backend import create_fusion_client, selected_backend
from fusion_mcp_adapter.manifest_store import ManifestStore
from fusion_mcp_adapter.policy import ToolPolicy
from fusion_mcp_adapter.semantics import CallSemantics, ConnectionState, McpCallOptions
from fusion_mcp_adapter.tool_result import ToolResult
from fusion_tool_facade.autodesk_typed_backend import AutodeskTypedBackend
from fusion_tool_facade.typed_backend import FaustTypedBackend
from telemetry.trace import JsonlTraceLogger


_READINESS_TTL_SECONDS = 60.0
MOCK_IMPLEMENTED_CAPABILITIES = frozenset(
    {
        "parameters",
        "components",
        "sketch_create",
        "sketch_rectangle",
        "sketch_circle",
        "extrude",
        "sketch_constraints",
        "sketch_dimensions",
        "revolve",
        "sweep",
        "loft",
        "pattern_rectangular",
        "pattern_circular",
        "pattern_path",
        "mirror",
        "boolean",
        "split_body",
        "joint",
        "joint_with_limits",
        "as_built_joint",
        "rigid_groups",
        "physical_properties",
        "interference",
        "import_step",
        "import_stp",
        "import_iges",
        "import_igs",
        "import_sat",
        "import_f3d",
        "export_step",
        "export_stp",
        "export_stl",
        "export_iges",
        "export_igs",
        "export_f3d",
        "sheet_metal_create_flange",
        "sheet_metal_create_bend",
        "sheet_metal_flat_pattern",
        "sheet_metal_unfold",
        "cam_setup",
        "cam_operation",
        "cam_generate_toolpath",
        "cam_post_process",
    }
)
_PNG_1X1 = base64.b64encode(
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDAT\x08\xd7c\xf8"
    b"\xcf\xc0\xf0\x1f\x00\x05\x00\x01\xff\x89\x99=\x1d\x00\x00\x00\x00IEND\xaeB`\x82"
).decode("ascii")


@dataclass(frozen=True, slots=True)
class RuntimeConfiguration:
    """Immutable process-start snapshot for transport and execution policy."""

    backend: str
    endpoint: str | None
    command: str | None
    transport_mode: str
    faust_command: str | None
    faust_cwd: str | None
    remote_policy: str
    remote_allowlist: str
    bearer_token: str | None
    connect_timeout_seconds: float
    read_timeout_seconds: float
    mutation_timeout_seconds: float
    sse_timeout_seconds: float
    auto_canary_timeout_seconds: float
    post_dispatch_cooldown_seconds: float
    telemetry_enabled: bool
    experimental_manufacturing: bool
    launcher_python: str = ""
    default_mode: str = "real"
    default_mode_explicit: bool = False
    require_real: bool = False
    allow_dry_run: bool = True
    trusted_read_timeout_seconds: float = 10.0
    inspection_max_entities: int = 1000
    inspection_deadline_ms: int = 1500
    inspection_max_response_bytes: int = 1_048_576
    resource_max_bytes: int = 1_048_576
    plugin_version: str = "0.4.1"
    tool_profile: str = "normal"
    fast_path_mode: str = "read_only"
    execution_path: str = "auto"
    protected_script_limit_bytes: int = 28 * 1024
    git_commit: str | None = None
    fusion_version: str | None = None
    wheel_version: str | None = None
    mcp_manifest_fingerprint: str | None = None
    expected_git_commit: str | None = None
    expected_source_manifest_sha256: str | None = None
    authority_policy_path: str | None = None

    @classmethod
    def from_environment(cls) -> "RuntimeConfiguration":
        return cls(
            backend=selected_backend(),
            endpoint=_optional_env("FUSION_MCP_ENDPOINT"),
            command=_optional_env("FUSION_MCP_COMMAND"),
            transport_mode=os.getenv("FUSION_MCP_TRANSPORT_MODE", "legacy")
            .strip()
            .lower(),
            faust_command=_optional_env("FUSION_FAUST_COMMAND"),
            faust_cwd=_optional_env("FUSION_FAUST_CWD"),
            remote_policy=os.getenv("FUSION_AGENT_REMOTE_POLICY", "loopback_only")
            .strip()
            .lower(),
            remote_allowlist=os.getenv("FUSION_AGENT_REMOTE_ALLOWLIST", "").strip(),
            bearer_token=_optional_env("FUSION_MCP_BEARER_TOKEN"),
            connect_timeout_seconds=_float_env(
                "FUSION_MCP_CONNECT_TIMEOUT_SECONDS", 5.0
            ),
            read_timeout_seconds=_float_env("FUSION_MCP_READ_TIMEOUT_SECONDS", 120.0),
            mutation_timeout_seconds=_float_env(
                "FUSION_MCP_MUTATION_TIMEOUT_SECONDS", 240.0
            ),
            sse_timeout_seconds=_float_env("FUSION_MCP_SSE_TIMEOUT_SECONDS", 300.0),
            auto_canary_timeout_seconds=_float_env(
                "FUSION_MCP_AUTO_CANARY_TIMEOUT_SECONDS", 2.0
            ),
            post_dispatch_cooldown_seconds=_float_env(
                "FUSION_MCP_POST_DISPATCH_COOLDOWN_SECONDS", 5.0
            ),
            telemetry_enabled=_env_bool("FUSION_AGENT_TELEMETRY", False),
            experimental_manufacturing=_env_bool(
                "FUSION_AGENT_EXPERIMENTAL_MANUFACTURING", False
            ),
            launcher_python=os.getenv("FUSION_AGENT_PYTHON", "").strip(),
            default_mode=_choice_env(
                "FUSION_AGENT_DEFAULT_MODE", "real", {"mock", "real"}
            ),
            default_mode_explicit=os.getenv("FUSION_AGENT_DEFAULT_MODE") is not None,
            require_real=_env_bool("FUSION_AGENT_REQUIRE_REAL", False),
            allow_dry_run=_env_bool("FUSION_AGENT_ALLOW_DRY_RUN", True),
            trusted_read_timeout_seconds=_float_env(
                "FUSION_MCP_TRUSTED_READ_TIMEOUT_SECONDS", 10.0
            ),
            inspection_max_entities=_int_env(
                "FUSION_AGENT_INSPECTION_MAX_ENTITIES",
                1000,
                minimum=1,
                maximum=5000,
            ),
            inspection_deadline_ms=_int_env(
                "FUSION_AGENT_INSPECTION_DEADLINE_MS",
                1500,
                minimum=50,
                maximum=5000,
            ),
            inspection_max_response_bytes=_int_env(
                "FUSION_AGENT_INSPECTION_MAX_RESPONSE_BYTES",
                1_048_576,
                minimum=4096,
                maximum=1_048_576,
            ),
            resource_max_bytes=_int_env(
                "FUSION_AGENT_RESOURCE_MAX_BYTES",
                1_048_576,
                minimum=4096,
                maximum=64 * 1024 * 1024,
            ),
            plugin_version=(
                os.getenv("FUSION_AGENT_PLUGIN_VERSION", __version__).strip()
                or __version__
            ),
            tool_profile=_choice_env(
                "FUSION_AGENT_TOOL_PROFILE",
                "normal",
                {"normal", "advanced", "diagnostic", "benchmark", "all"},
            ),
            fast_path_mode=_choice_env(
                "FUSION_AGENT_FAST_PATH_MODE",
                "read_only",
                {"off", "read_only", "enabled"},
            ),
            execution_path=_choice_env(
                "FUSION_AGENT_EXECUTION_PATH",
                "auto",
                {"auto", "native_fast", "safe_harness"},
            ),
            protected_script_limit_bytes=_int_env(
                "FUSION_AGENT_MAX_PROTECTED_SCRIPT_BYTES",
                28 * 1024,
                minimum=0,
                maximum=16 * 1024 * 1024,
            ),
            git_commit=_optional_env("GIT_COMMIT"),
            fusion_version=_optional_env("FUSION_VERSION"),
            wheel_version=_optional_env("FUSION_AGENT_WHEEL_VERSION"),
            mcp_manifest_fingerprint=_optional_env("FUSION_MCP_MANIFEST_FINGERPRINT"),
            expected_git_commit=_optional_env("FUSION_AGENT_EXPECTED_GIT_COMMIT"),
            expected_source_manifest_sha256=_optional_env(
                "FUSION_AGENT_EXPECTED_SOURCE_MANIFEST_SHA256"
            ),
            authority_policy_path=_optional_env("FUSION_AGENT_AUTHORITY_POLICY_PATH"),
        )


class FusionAgentRuntime:
    """Own the persistent client, manifest, controller, and fast-path state."""

    def __init__(
        self,
        *,
        manifest_root: Path | str = "manifests",
        outputs_root: Path | str = "outputs",
        real_benchmark_backend: Any | None = None,
        configuration: RuntimeConfiguration | None = None,
    ) -> None:
        self.configuration = configuration or RuntimeConfiguration.from_environment()
        self.manifest_store = ManifestStore(manifest_root)
        self.outputs_root = Path(outputs_root)
        # Authority is a process/runtime startup snapshot.  Never reconstruct
        # it from environment inside an operation or after a reconnect.
        try:
            self.authority_policy = AuthorityPolicy.from_environment(
                {
                    "FUSION_AGENT_AUTHORITY_POLICY_PATH": (
                        self.configuration.authority_policy_path or ""
                    )
                }
            )
        except AuthorityDeniedError:
            # A malformed or unavailable startup policy must fail closed for
            # host I/O without preventing sessions that never touch the host
            # filesystem.  The exception text remains private.
            self.authority_policy = AuthorityPolicy.deny_all()
        self.authority_broker = AuthorityBroker(
            self.authority_policy,
            ledger=CapabilityLedger(self.outputs_root / ".authority" / "capabilities"),
        )
        # A caller may inject a fully reviewed real benchmark backend.  When it
        # does not, the stock lifecycle-only backend is installed below; it
        # deliberately advertises no canonical route actions or oracles.
        self.real_benchmark_backend = real_benchmark_backend
        self._readiness_lock = asyncio.Lock()
        self._closing = False
        self._ready_at = 0.0
        self._ready_generation = -1
        self._ready_fingerprint = ""
        self._adapter: FusionMcpAdapter | None = None
        self.real_client = self._new_real_client()
        self.controller = SessionController(
            real_client=self.real_client,
            manifest_store=self.manifest_store,
            environment_snapshot=self._session_environment_snapshot(),
            authority_broker=self.authority_broker,
            authority_provider=self.configuration.backend,
        )
        self._mock_backend = _MockFastBackend()
        self._real_fast_path = FastPathService(
            self._call_native_real,
            manifest_fingerprint=self.manifest_fingerprint,
            trusted_read_native=self._call_trusted_native_real,
        )
        self._mock_fast_path = FastPathService(
            self._mock_backend.call,
            manifest_fingerprint=lambda: self._mock_backend.fingerprint,
        )
        if self.real_benchmark_backend is None:
            # Local import avoids coupling the persistent runtime core to the
            # optional benchmark package at module import time.
            from fusion_agent_mcp.benchmark_bridge import (
                FusionRuntimeLifecycleBackend,
            )

            self.real_benchmark_backend = FusionRuntimeLifecycleBackend(self)

    def _new_real_client(self):
        logger = None
        config = self.configuration
        if config.telemetry_enabled:
            logger = JsonlTraceLogger(Path("logs") / "fusion_agent_runtime.jsonl")
        return create_fusion_client(
            backend=config.backend,
            endpoint=config.endpoint,
            command=config.command,
            transport_mode=config.transport_mode,
            faust_command=config.faust_command,
            faust_cwd=config.faust_cwd,
            remote_policy=config.remote_policy,
            remote_allowlist=config.remote_allowlist,
            bearer_token=config.bearer_token,
            connect_timeout_seconds=config.connect_timeout_seconds,
            read_timeout_seconds=config.read_timeout_seconds,
            mutation_timeout_seconds=config.mutation_timeout_seconds,
            sse_read_timeout_seconds=config.sse_timeout_seconds,
            auto_canary_timeout_seconds=config.auto_canary_timeout_seconds,
            post_dispatch_cooldown_seconds=config.post_dispatch_cooldown_seconds,
            manifest_store=self.manifest_store,
            trace_logger=logger,
        )

    async def ensure_ready(self) -> dict[str, Any]:
        """Connect once, cache readiness, and rebuild policy after reconnect."""

        self._ensure_open()
        now = time.monotonic()
        diagnostics = self.real_client.diagnostics
        cache_valid = (
            self._adapter is not None
            and self.real_client.state == ConnectionState.READY
            and self._ready_generation == diagnostics["connection_generation"]
            and now - self._ready_at < _READINESS_TTL_SECONDS
        )
        if cache_valid:
            return self.diagnostics()

        async with self._readiness_lock:
            self._ensure_open()
            diagnostics = self.real_client.diagnostics
            cache_valid = (
                self._adapter is not None
                and self.real_client.state == ConnectionState.READY
                and self._ready_generation == diagnostics["connection_generation"]
                and time.monotonic() - self._ready_at < _READINESS_TTL_SECONDS
            )
            if not cache_valid:
                await self.real_client.ensure_ready()
                manifest = await self.real_client.list_tools()
                self._adapter = FusionMcpAdapter(
                    client=self.real_client,
                    manifest=manifest,
                    manifest_store=self.manifest_store,
                    policy=ToolPolicy.from_manifest(manifest.names()),
                    session_id="fusion-agent-runtime",
                )
                self._ready_generation = self.real_client.connection_generation
                self._ready_fingerprint = manifest.fingerprint
                self._ready_at = time.monotonic()
        return self.diagnostics()

    def invalidate_readiness(self) -> None:
        """Invalidate readiness after timeout, reconnect, drift, or config change."""

        self._ready_at = 0.0
        self._adapter = None

    def manifest_fingerprint(self) -> str:
        return str(
            self.real_client.diagnostics.get("fingerprint") or self._ready_fingerprint
        )

    def fast_path(self, mode: str) -> FastPathService:
        if mode == "real":
            return self._real_fast_path
        if mode == "mock":
            return self._mock_fast_path
        raise ValueError("mode must be 'mock' or 'real'")

    async def execute_cad_spec_v2(
        self,
        spec: CadSpecV2,
        *,
        mode: str,
        dry_run: bool = False,
    ) -> CapabilityExecutionResult:
        """Execute one strict v2 graph through the explicitly selected backend.

        Capability and operation compilation preflight is performed by
        ``CapabilityExecutor`` before the first provider call.  Provider
        selection never falls back from Autodesk to Faust (or vice versa).
        """

        if dry_run:
            return await CapabilityExecutor(
                authority_broker=self.authority_broker,
                experimental_enabled=self.configuration.experimental_manufacturing,
            ).execute(spec, dry_run=True, session_id=_request_session_id())
        if mode == "mock":
            return await CapabilityExecutor(
                _MockCapabilityBackend(),
                authority_broker=self.authority_broker,
                experimental_enabled=self.configuration.experimental_manufacturing,
            ).execute(spec, session_id=_request_session_id())
        if mode != "real":
            raise ValueError("mode must be 'mock' or 'real'")

        await self.ensure_ready()
        manifest = self.real_client.current_manifest
        if manifest is None:
            raise RuntimeError("typed capability backend requires a live tool manifest")
        backend_name = self.configuration.backend
        if backend_name == "faust_stdio":
            backend = FaustTypedBackend.from_client(self.real_client, manifest)
        elif backend_name == "autodesk_http":
            backend = AutodeskTypedBackend.from_client(self.real_client, manifest)
        else:  # selected_backend is fail-closed, retain a local safety check.
            raise ValueError(f"unsupported Fusion backend: {backend_name}")
        return await CapabilityExecutor(
            backend,
            authority_broker=self.authority_broker,
            experimental_enabled=self.configuration.experimental_manufacturing,
        ).execute(spec, session_id=_request_session_id())

    async def _call_native_real(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        semantics: str,
        operation_id: str,
    ) -> ToolResult:
        return await self._call_native(
            name,
            arguments,
            semantics=semantics,
            operation_id=operation_id,
            trusted_internal_read=False,
        )

    async def _call_trusted_native_real(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        semantics: str,
        operation_id: str,
    ) -> ToolResult:
        return await self._call_native(
            name,
            arguments,
            semantics=semantics,
            operation_id=operation_id,
            trusted_internal_read=True,
        )

    async def _call_native(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        semantics: str,
        operation_id: str,
        trusted_internal_read: bool,
    ) -> ToolResult:
        await self.ensure_ready()
        adapter = self._adapter
        if adapter is None:
            return ToolResult.failure(
                "CONNECTION_UNAVAILABLE", "Fusion MCP adapter is not ready"
            )
        semantic = CallSemantics(semantics)
        if trusted_internal_read:
            options = McpCallOptions.for_trusted_internal_read(
                timeout_seconds=self.configuration.read_timeout_seconds,
                operation_id=operation_id,
            )
        elif semantic == CallSemantics.READ_ONLY:
            options = McpCallOptions.for_read(
                timeout_seconds=self.configuration.read_timeout_seconds,
                operation_id=operation_id,
            )
        else:
            options = McpCallOptions.for_mutation(
                timeout_seconds=self.configuration.mutation_timeout_seconds,
                operation_id=operation_id,
            )
        result = await adapter.call(name, arguments, options=options)
        current_generation = self.real_client.connection_generation
        if current_generation != self._ready_generation or result.error_code in {
            "MANIFEST_DRIFT",
            "TIMEOUT",
            "READ_TIMEOUT_MAY_STILL_BE_RUNNING",
            "CALL_CANCELLED",
            "CONNECTION_LOST",
            "MUTATION_OUTCOME_UNKNOWN",
        }:
            self.invalidate_readiness()
        return result

    def diagnostics(self) -> dict[str, Any]:
        return {
            **dict(self.real_client.diagnostics),
            "backend": self.configuration.backend,
            "frontend_transport": "stdio",
            "readiness_cached": bool(self._adapter and self._ready_at),
            "readiness_ttl_seconds": _READINESS_TTL_SECONDS,
            "manifest_status": self.manifest_store.latest_status(),
            "authority_policy": self.authority_policy.safe_summary(),
            "real_benchmark_backend": self.real_benchmark_backend is not None,
            "closing": self._closing,
        }

    async def close(self, timeout_seconds: float = 2.0) -> None:
        if self._closing:
            return
        self._closing = True
        self.invalidate_readiness()
        await self.real_client.close(timeout_seconds=timeout_seconds)

    async def _replace_real_client(self) -> None:
        await self.real_client.close(timeout_seconds=2.0)
        self.real_client = self._new_real_client()
        self.controller = SessionController(
            real_client=self.real_client,
            manifest_store=self.manifest_store,
            environment_snapshot=self._session_environment_snapshot(),
            authority_broker=self.authority_broker,
            authority_provider=self.configuration.backend,
        )
        self._ready_generation = -1
        self._ready_fingerprint = ""
        self.invalidate_readiness()

    def _ensure_open(self) -> None:
        if self._closing:
            raise RuntimeError("Fusion Agent runtime is closed")

    def _session_environment_snapshot(self) -> dict[str, str]:
        return {
            "launcher_python": self.configuration.launcher_python,
            "fusion_mcp_endpoint": self.configuration.endpoint or "",
            "default_mode": self.configuration.default_mode,
            "require_real": str(self.configuration.require_real).lower(),
            "allow_dry_run": str(self.configuration.allow_dry_run).lower(),
        }


class _MockCapabilityBackend:
    """Deterministic typed backend for v2 protocol and benchmark tests."""

    provider = "mock"
    capabilities = set(MOCK_IMPLEMENTED_CAPABILITIES)

    def preflight_operations(self, operations: list[OperationSpec]) -> None:
        del operations

    async def execute_operation(self, operation: OperationSpec) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "mock": True,
            "operation": operation.model_dump(mode="json"),
        }
        if operation.kind == "analysis.physical_properties":
            payload["physical_properties"] = {
                target: {"mass_kg": 1.0, "volume_mm3": 1000.0}
                for target in operation.target_refs
            }
        elif operation.kind == "analysis.interference":
            payload["interference"] = {"count": 0, "pairs": []}
        return payload


class _MockFastBackend:
    """Deterministic native-shaped backend for tests and benchmark mock mode."""

    fingerprint = "mock-fast-path-v2"

    def __init__(self) -> None:
        self.document = {
            "name": "FusionAgentMockTrial",
            "runtime_id": "mock-document-runtime",
            "id": "mock-document",
            "version_id": "mock-v1",
            "is_modified": False,
            "product_type": "DesignProductType",
        }
        self.entities: dict[tuple[str, str], dict[str, Any]] = {}
        self._previous_entities: dict[tuple[str, str], dict[str, Any]] = {}
        self._last_queries: list[dict[str, Any]] = []

    async def call(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        semantics: str,
        operation_id: str,
    ) -> ToolResult:
        del operation_id
        if name == "fusion_mcp_read":
            return self._read(arguments)
        if name == "fusion_mcp_execute" and semantics == "read_only":
            script = str(((arguments.get("object") or {}).get("script")) or "")
            if "fusion_agent_active_command" in script:
                return ToolResult.success(
                    message=json.dumps(
                        {
                            "success": True,
                            "probe": "fusion_agent_active_command",
                            "activeCommand": None,
                        }
                    )
                )
            request = _inspection_request(arguments)
            self._last_queries = list(request.get("queries") or [])
            return ToolResult.success(
                message=json.dumps(self._snapshot(request), sort_keys=True)
            )
        if name == "fusion_mcp_execute":
            self._previous_entities = deepcopy(self.entities)
            for query in self._last_queries:
                if str(query.get("id") or "").startswith("__fusion_agent_component_"):
                    continue
                selector = query.get("selector") or {}
                entity_name = str(
                    selector.get("name") or query.get("id") or "FastPathEntity"
                )
                entity_type = str(query.get("entity_type") or "feature")
                self.entities[(entity_type, entity_name)] = {
                    "entity_type": entity_type,
                    "name": entity_name,
                    "path": f"root/{entity_name}",
                    "component_path": "root",
                    "entity_token": f"mock:{entity_type}:{entity_name}",
                    "exists": True,
                    "valid": True,
                    "visible": True,
                    "health": "0",
                    "bounding_box_mm": {
                        "min_mm": [0.0, 0.0, 0.0],
                        "max_mm": [10.0, 10.0, 10.0],
                        "size_mm": [10.0, 10.0, 10.0],
                    },
                }
            self.document["is_modified"] = True
            return ToolResult.success(message="mock mutation applied")
        if name == "fusion_mcp_update":
            action = str(arguments.get("featureType") or "")
            if action == "undo":
                self.entities, self._previous_entities = (
                    self._previous_entities,
                    deepcopy(self.entities),
                )
            elif action == "redo":
                self.entities, self._previous_entities = (
                    self._previous_entities,
                    deepcopy(self.entities),
                )
            return ToolResult.success(success=True, action=action)
        return ToolResult.failure("UNKNOWN_TOOL", f"mock native tool not found: {name}")

    def _read(self, arguments: dict[str, Any]) -> ToolResult:
        query = str(arguments.get("queryType") or "")
        if query == "activeCommand":
            return ToolResult.success(activeCommand=None)
        if query == "apiDocumentation":
            return ToolResult.success(
                query=arguments.get("searchPattern"),
                references=[
                    {
                        "symbol": arguments.get("searchPattern"),
                        "description": "Deterministic mock Autodesk API reference.",
                    }
                ],
            )
        if query == "projects":
            return ToolResult.success(projects=[])
        if query == "document":
            return ToolResult.success(
                operation=arguments.get("operation"), documents=[self.document]
            )
        if query == "screenshot":
            return ToolResult.success(
                structured_content={
                    "screenshot": {
                        "mimeType": "image/png",
                        "base64Data": _PNG_1X1,
                        "width": int(arguments.get("width", 1)),
                        "height": int(arguments.get("height", 1)),
                    }
                }
            )
        return ToolResult.failure(
            "MOCK_OPERATION_FAILED", f"unsupported read query: {query}"
        )

    def _snapshot(self, request: dict[str, Any]) -> dict[str, Any]:
        results = []
        limit = int(request.get("limit_per_query", 20))
        for query in request.get("queries") or []:
            entity_type = str(query.get("entity_type") or "")
            selector = query.get("selector") or {}
            if entity_type == "document":
                matches = [{"entity_type": "document", "exists": True, **self.document}]
                match_count = 1
            elif entity_type == "component" and selector.get("path") in {
                "root",
                "Root",
            }:
                path = str(selector["path"])
                matches = [
                    {
                        "entity_type": "component",
                        "name": path,
                        "path": path,
                        "paths": [path],
                        "component_path": path,
                        "component_paths": [path],
                        "entity_token": "mock:component:root",
                        "exists": True,
                        "valid": True,
                        "visible": True,
                        "is_referenced_component": False,
                        "occurrence_count_for_component": 0,
                    }
                ]
                match_count = 1
            else:
                name = str(selector.get("name") or "")
                all_matches = [
                    deepcopy(value)
                    for (kind, entity_name), value in self.entities.items()
                    if kind == entity_type and (not name or entity_name == name)
                ]
                match_count = len(all_matches)
                matches = all_matches[:limit]
            results.append(
                {
                    "query_id": query.get("id"),
                    "matches": matches,
                    "ambiguous": match_count > 1,
                    "truncated": match_count > limit,
                    "match_count": match_count,
                    "match_count_exact": True,
                }
            )
        counts = {
            name: 0
            for name in (
                "components",
                "occurrences",
                "bodies",
                "sketches",
                "features",
                "parameters",
            )
        }
        singular_to_plural = {
            "component": "components",
            "occurrence": "occurrences",
            "body": "bodies",
            "sketch": "sketches",
            "feature": "features",
            "parameter": "parameters",
        }
        for entity_type, _ in self.entities:
            plural = singular_to_plural.get(entity_type)
            if plural:
                counts[plural] += 1
        counts["components"] += 1
        visible_bodies = [
            value
            for (entity_type, _), value in self.entities.items()
            if entity_type == "body" and value.get("visible", True)
        ]
        counts["visible_body_count"] = len(visible_bodies)
        counts["visible_body_bbox_mm"] = (
            {
                "min_mm": [0.0, 0.0, 0.0],
                "max_mm": [10.0, 10.0, 10.0],
                "size_mm": [10.0, 10.0, 10.0],
            }
            if visible_bodies
            else None
        )
        if request.get("include_state_fingerprint"):
            state_items = [
                [entity_type, entity_name, value]
                for (entity_type, entity_name), value in sorted(self.entities.items())
            ]
            state_limit = int(request.get("state_fingerprint_limit", 5000))
            truncated = len(state_items) + 1 > state_limit
            counts["state_fingerprint"] = (
                None
                if truncated
                else hashlib.sha256(
                    json.dumps(
                        {"document": self.document, "entities": state_items},
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
            )
            counts["state_fingerprint_truncated"] = truncated
            counts["state_fingerprint_items"] = min(len(state_items) + 1, state_limit)
        return {
            "success": True,
            "document": deepcopy(self.document),
            "summary": counts,
            "results": results,
            "warnings": [],
            "complete": True,
            "truncated": False,
            "visited_entities": len(self.entities),
            "elapsed_ms": 0,
            "response_bytes": 0,
            "counts_exact": True,
            "stop_reason": "complete",
        }


def _inspection_request(arguments: dict[str, Any]) -> dict[str, Any]:
    script = str(((arguments.get("object") or {}).get("script")) or "")
    match = re.search(r"^_REQUEST = json\.loads\((.+)\)$", script, flags=re.MULTILINE)
    if not match:
        return {"queries": [], "limit_per_query": 20}
    encoded = ast.literal_eval(match.group(1))
    loaded = json.loads(encoded)
    return (
        loaded if isinstance(loaded, dict) else {"queries": [], "limit_per_query": 20}
    )


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed


def _int_env(name: str, default: int, *, minimum: int, maximum: int) -> int:
    value = os.getenv(name)
    try:
        parsed = default if value is None else int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _choice_env(name: str, default: str, allowed: set[str]) -> str:
    normalized = os.getenv(name, default).strip().lower()
    if normalized not in allowed:
        raise ValueError(f"{name} must be one of: {', '.join(sorted(allowed))}")
    return normalized


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _optional_env(name: str) -> str | None:
    value = os.getenv(name)
    if value is None or not value.strip():
        return None
    return value.strip()


def _request_session_id() -> str | None:
    context = current_request_context()
    if context is None:
        return None
    return context.session_id or context.request_id
