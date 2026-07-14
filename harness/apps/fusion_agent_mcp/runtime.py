"""Process-scoped runtime shared by every public Fusion Agent tool."""

from __future__ import annotations

import ast
import asyncio
import base64
import hashlib
import json
import os
import re
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

from agent_core.fast_path import FastPathService
from agent_core.session_controller import SessionController
from fusion_mcp_adapter.adapter import FusionMcpAdapter
from fusion_mcp_adapter.manifest_store import ManifestStore
from fusion_mcp_adapter.policy import ToolPolicy
from fusion_mcp_adapter.real_client import RealMcpClient
from fusion_mcp_adapter.semantics import CallSemantics, ConnectionState, McpCallOptions
from fusion_mcp_adapter.tool_result import ToolResult
from telemetry.trace import JsonlTraceLogger


_READINESS_TTL_SECONDS = 60.0
_PNG_1X1 = base64.b64encode(
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDAT\x08\xd7c\xf8"
    b"\xcf\xc0\xf0\x1f\x00\x05\x00\x01\xff\x89\x99=\x1d\x00\x00\x00\x00IEND\xaeB`\x82"
).decode("ascii")


class FusionAgentRuntime:
    """Own the persistent client, manifest, controller, and fast-path state."""

    def __init__(
        self,
        *,
        manifest_root: Path | str = "manifests",
        outputs_root: Path | str = "outputs",
        real_benchmark_backend: Any | None = None,
    ) -> None:
        self.manifest_store = ManifestStore(manifest_root)
        self.outputs_root = Path(outputs_root)
        # A caller may inject a fully reviewed real benchmark backend.  When it
        # does not, the stock lifecycle-only backend is installed below; it
        # deliberately advertises no canonical route actions or oracles.
        self.real_benchmark_backend = real_benchmark_backend
        self._readiness_lock = asyncio.Lock()
        self._closing = False
        self._ready_at = 0.0
        self._ready_generation = -1
        self._ready_fingerprint = ""
        self._configuration = self._configuration_signature()
        self._adapter: FusionMcpAdapter | None = None
        self.real_client = self._new_real_client()
        self.controller = SessionController(
            real_client=self.real_client,
            manifest_store=self.manifest_store,
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
            from fusion_agent_mcp.benchmark_bridge import FusionRuntimeLifecycleBackend

            self.real_benchmark_backend = FusionRuntimeLifecycleBackend(self)

    def _new_real_client(self) -> RealMcpClient:
        logger = None
        if _env_bool("FUSION_AGENT_TELEMETRY", False):
            logger = JsonlTraceLogger(Path("logs") / "fusion_agent_runtime.jsonl")
        return RealMcpClient(
            connect_timeout_seconds=_float_env("FUSION_MCP_CONNECT_TIMEOUT_SECONDS", 5.0),
            read_timeout_seconds=_float_env("FUSION_MCP_READ_TIMEOUT_SECONDS", 120.0),
            mutation_timeout_seconds=_float_env("FUSION_MCP_MUTATION_TIMEOUT_SECONDS", 240.0),
            sse_read_timeout_seconds=_float_env("FUSION_MCP_SSE_TIMEOUT_SECONDS", 300.0),
            manifest_store=self.manifest_store,
            trace_logger=logger,
        )

    async def ensure_ready(self) -> dict[str, Any]:
        """Connect once, cache readiness, and rebuild policy after reconnect."""

        self._ensure_open()
        signature = self._configuration_signature()
        if signature != self._configuration:
            async with self._readiness_lock:
                if signature != self._configuration:
                    await self._replace_real_client(signature)

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
        return str(self.real_client.diagnostics.get("fingerprint") or self._ready_fingerprint)

    def fast_path(self, mode: str) -> FastPathService:
        if mode == "real":
            return self._real_fast_path
        if mode == "mock":
            return self._mock_fast_path
        raise ValueError("mode must be 'mock' or 'real'")

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
            return ToolResult.failure("CONNECTION_UNAVAILABLE", "Fusion MCP adapter is not ready")
        semantic = CallSemantics(semantics)
        if trusted_internal_read:
            options = McpCallOptions.for_trusted_internal_read(
                timeout_seconds=_float_env("FUSION_MCP_READ_TIMEOUT_SECONDS", 120.0),
                operation_id=operation_id,
            )
        elif semantic == CallSemantics.READ_ONLY:
            options = McpCallOptions.for_read(
                timeout_seconds=_float_env("FUSION_MCP_READ_TIMEOUT_SECONDS", 120.0),
                operation_id=operation_id,
            )
        else:
            options = McpCallOptions.for_mutation(
                timeout_seconds=_float_env("FUSION_MCP_MUTATION_TIMEOUT_SECONDS", 240.0),
                operation_id=operation_id,
            )
        result = await adapter.call(name, arguments, options=options)
        current_generation = self.real_client.connection_generation
        if (
            current_generation != self._ready_generation
            or result.error_code
            in {
                "MANIFEST_DRIFT",
                "TIMEOUT",
                "READ_TIMEOUT_MAY_STILL_BE_RUNNING",
                "CALL_CANCELLED",
                "CONNECTION_LOST",
                "MUTATION_OUTCOME_UNKNOWN",
            }
        ):
            self.invalidate_readiness()
        return result

    def diagnostics(self) -> dict[str, Any]:
        return {
            **dict(self.real_client.diagnostics),
            "readiness_cached": bool(self._adapter and self._ready_at),
            "readiness_ttl_seconds": _READINESS_TTL_SECONDS,
            "manifest_status": self.manifest_store.latest_status(),
            "real_benchmark_backend": self.real_benchmark_backend is not None,
            "closing": self._closing,
        }

    async def close(self, timeout_seconds: float = 2.0) -> None:
        if self._closing:
            return
        self._closing = True
        self.invalidate_readiness()
        await self.real_client.close(timeout_seconds=timeout_seconds)

    async def _replace_real_client(self, signature: tuple[str, ...]) -> None:
        await self.real_client.close(timeout_seconds=2.0)
        self.real_client = self._new_real_client()
        self.controller = SessionController(
            real_client=self.real_client,
            manifest_store=self.manifest_store,
        )
        self._configuration = signature
        self._ready_generation = -1
        self._ready_fingerprint = ""
        self.invalidate_readiness()

    def _configuration_signature(self) -> tuple[str, ...]:
        return tuple(
            os.getenv(name, "")
            for name in (
                "FUSION_MCP_ENDPOINT",
                "FUSION_MCP_COMMAND",
                "FUSION_MCP_TRANSPORT_MODE",
                "FUSION_MCP_CONNECT_TIMEOUT_SECONDS",
                "FUSION_MCP_READ_TIMEOUT_SECONDS",
                "FUSION_MCP_MUTATION_TIMEOUT_SECONDS",
                "FUSION_MCP_SSE_TIMEOUT_SECONDS",
                "FUSION_MCP_AUTO_CANARY_TIMEOUT_SECONDS",
                "FUSION_MCP_TRUSTED_READ_TIMEOUT_SECONDS",
                "FUSION_MCP_POST_DISPATCH_COOLDOWN_SECONDS",
            )
        )

    def _ensure_open(self) -> None:
        if self._closing:
            raise RuntimeError("Fusion Agent runtime is closed")


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
            return ToolResult.success(message=json.dumps(self._snapshot(request), sort_keys=True))
        if name == "fusion_mcp_execute":
            self._previous_entities = deepcopy(self.entities)
            for query in self._last_queries:
                if str(query.get("id") or "").startswith("__fusion_agent_component_"):
                    continue
                selector = query.get("selector") or {}
                entity_name = str(selector.get("name") or query.get("id") or "FastPathEntity")
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
                self.entities, self._previous_entities = self._previous_entities, deepcopy(self.entities)
            elif action == "redo":
                self.entities, self._previous_entities = self._previous_entities, deepcopy(self.entities)
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
            return ToolResult.success(operation=arguments.get("operation"), documents=[self.document])
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
        return ToolResult.failure("MOCK_OPERATION_FAILED", f"unsupported read query: {query}")

    def _snapshot(self, request: dict[str, Any]) -> dict[str, Any]:
        results = []
        limit = int(request.get("limit_per_query", 20))
        for query in request.get("queries") or []:
            entity_type = str(query.get("entity_type") or "")
            selector = query.get("selector") or {}
            if entity_type == "document":
                matches = [{"entity_type": "document", "exists": True, **self.document}]
                match_count = 1
            elif entity_type == "component" and selector.get("path") in {"root", "Root"}:
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
        counts = {name: 0 for name in ("components", "occurrences", "bodies", "sketches", "features", "parameters")}
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
    return loaded if isinstance(loaded, dict) else {"queries": [], "limit_per_query": 20}


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    parsed = float(value)
    if parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}
