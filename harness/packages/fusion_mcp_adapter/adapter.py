"""Safe wrapper around an MCP client."""

from __future__ import annotations

import time
from typing import Any

from jsonschema import ValidationError, validate as json_validate

from fusion_mcp_adapter.client import McpClient
from fusion_mcp_adapter.errors import ErrorCode
from fusion_mcp_adapter.execute_guard import (
    EXECUTE_TOOL_NAME,
    execute_script_telemetry,
    prepare_execute_arguments,
)
from fusion_mcp_adapter.manifest_store import ManifestStore
from fusion_mcp_adapter.policy import ToolPolicy
from fusion_mcp_adapter.semantics import McpCallOptions
from fusion_mcp_adapter.tool_result import ToolDefinition, ToolManifest, ToolResult
from telemetry.trace import JsonlTraceLogger


class FusionMcpAdapter:
    """Adapter that enforces policy, logging, and manifest persistence."""

    def __init__(
        self,
        client: McpClient,
        manifest: ToolManifest | None = None,
        manifest_store: ManifestStore | None = None,
        policy: ToolPolicy | None = None,
        trace_logger: JsonlTraceLogger | None = None,
        session_id: str | None = None,
    ) -> None:
        self.client = client
        self.manifest = manifest
        self.manifest_store = manifest_store or ManifestStore()
        self.policy = policy or ToolPolicy()
        self.trace_logger = trace_logger
        self.session_id = session_id or "standalone"

    async def discover(self, save: bool = True) -> ToolManifest:
        """Discover native MCP tools and optionally persist the manifest."""

        manifest = await self.client.list_tools()
        self.manifest = manifest
        if save:
            self.manifest_store.save(manifest)
        return manifest

    async def call(
        self,
        native_tool_name: str,
        args: dict[str, Any],
        options: McpCallOptions | None = None,
    ) -> ToolResult:
        """Call an allowlisted native tool and emit a JSONL trace event."""

        self.policy.ensure_allowed(native_tool_name)
        payload = dict(args)
        facade_tool = payload.pop("_facade_tool", native_tool_name)
        executor_telemetry: dict[str, Any] = {}
        if native_tool_name == EXECUTE_TOOL_NAME:
            original_script = _execute_script(payload)
            try:
                payload = prepare_execute_arguments(payload)
            except ValueError as exc:
                result = ToolResult.failure(ErrorCode.TOOL_SCHEMA_VALIDATION_ERROR, str(exc))
                self._log_tool_call(
                    facade_tool,
                    native_tool_name,
                    args,
                    result,
                    duration_ms=0,
                    executor_telemetry=executor_telemetry,
                )
                return result
            transmitted_script = _execute_script(payload)
            if original_script is not None and transmitted_script is not None:
                executor_telemetry = execute_script_telemetry(original_script, transmitted_script)

        tool_def = self._tool_definition(native_tool_name)
        validation_error = self._validate_input(tool_def, payload)
        if validation_error:
            result = ToolResult.failure(
                ErrorCode.TOOL_SCHEMA_VALIDATION_ERROR,
                validation_error,
            )
            self._log_tool_call(
                facade_tool,
                native_tool_name,
                payload,
                result,
                duration_ms=0,
                executor_telemetry=executor_telemetry,
            )
            return result

        started = time.perf_counter()
        if options is None:
            result = await self.client.call_tool(native_tool_name, payload)
        else:
            result = await self.client.call_tool(native_tool_name, payload, options=options)
        duration_ms = int((time.perf_counter() - started) * 1000)

        if result.ok:
            output_error = self._validate_output(tool_def, result.data)
            if output_error:
                result = ToolResult.failure(ErrorCode.TOOL_SCHEMA_VALIDATION_ERROR, output_error)

        self._log_tool_call(
            facade_tool,
            native_tool_name,
            payload,
            result,
            duration_ms,
            executor_telemetry=executor_telemetry,
        )
        return result

    def _validate_input(self, tool_def: ToolDefinition | None, payload: dict[str, Any]) -> str | None:
        if tool_def is None or not tool_def.input_schema:
            return None
        return _validate_json_schema(payload, tool_def.input_schema)

    def _validate_output(self, tool_def: ToolDefinition | None, payload: Any) -> str | None:
        if tool_def is None or not tool_def.output_schema:
            return None
        return _validate_json_schema(payload, tool_def.output_schema)

    def _tool_definition(self, native_tool_name: str) -> ToolDefinition | None:
        if not self.manifest:
            return None
        for tool in self.manifest.tools:
            if tool.name == native_tool_name:
                return tool
        return None

    def _log_tool_call(
        self,
        facade_tool: str,
        native_tool: str,
        arguments: dict[str, Any],
        result: ToolResult,
        duration_ms: int,
        *,
        executor_telemetry: dict[str, Any] | None = None,
    ) -> None:
        if not self.trace_logger:
            return
        self.trace_logger.log_tool_call(
            session_id=self.session_id,
            facade_tool=facade_tool,
            native_tool=native_tool,
            arguments=arguments,
            result_status="ok" if result.ok else "error",
            duration_ms=duration_ms,
            error_code=result.error_code,
            **(executor_telemetry or {}),
        )


def _execute_script(payload: dict[str, Any]) -> str | None:
    if str(payload.get("featureType") or "") != "script":
        return None
    raw_object = payload.get("object")
    if not isinstance(raw_object, dict):
        return None
    script = raw_object.get("script")
    return script if isinstance(script, str) else None


def _validate_json_schema(payload: dict[str, Any], schema: dict[str, Any]) -> str | None:
    try:
        json_validate(payload, schema)
        return None
    except ValidationError as exc:  # pragma: no cover - exercised by unit tests
        return str(exc)
    except Exception as exc:  # pragma: no cover - defensive fallback
        return str(exc)
