from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

import pytest

from fusion_mcp_adapter.adapter import FusionMcpAdapter
from fusion_mcp_adapter.errors import ErrorCode
from fusion_mcp_adapter.policy import ToolPolicy
from fusion_mcp_adapter.semantics import ConnectionState, McpCallOptions
from fusion_mcp_adapter.stdio_client import StdioMcpClient
from fusion_mcp_adapter.tool_result import ToolDefinition, ToolManifest, ToolResult


SENTINEL = "FA041_PRIVATE_RESULT_CANARY"


def test_execute_guard_import_order_does_not_cycle_through_agent_core() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import fusion_mcp_adapter.execute_guard; "
                "from agent_core import Executor, RequestContext; "
                "assert Executor.__name__ == 'Executor'; "
                "assert RequestContext.__name__ == 'RequestContext'"
            ),
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def _serialized(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(by_alias=True, mode="json")
    return json.dumps(value, sort_keys=True, default=str)


@pytest.mark.parametrize(
    "structured",
    [
        {"success": True, "error": SENTINEL},
        {"ok": True, "error_message": SENTINEL},
        {"result": {"success": True, "error": SENTINEL}},
        {"data": {"ok": True, "error_message": SENTINEL}},
    ],
)
def test_positive_ack_with_error_is_public_failure_and_drops_raw_channels(
    structured: dict[str, Any],
) -> None:
    result = ToolResult.from_mcp(
        {
            "content": [{"type": "text", "text": SENTINEL}],
            "structuredContent": structured,
            "_meta": {"diagnostic": SENTINEL},
            "isError": False,
        }
    )

    assert result.ok is False
    assert result.error_code == ErrorCode.FUSION_OPERATION_FAILED
    assert result.error_message == "The downstream Fusion operation failed."
    assert result.public_error is not None
    assert result.content == []
    assert result.structured_content is None
    assert result.meta == {}
    assert SENTINEL not in _serialized(result)


@pytest.mark.parametrize(
    "structured",
    [
        {"success": True, "response": {"payload": {"error": SENTINEL}}},
        {"ok": True, "items": [{"details": {"exception": SENTINEL}}]},
        {
            "success": True,
            "wrapper": [{"value": [{"traceback": SENTINEL}]}],
        },
        {
            "success": True,
            "response": json.dumps({"nested": {"error_message": SENTINEL}}),
        },
    ],
)
def test_positive_ack_with_arbitrarily_nested_error_fails_closed(
    structured: dict[str, Any],
) -> None:
    result = ToolResult.from_mcp(
        {
            "content": [{"type": "text", "text": SENTINEL}],
            "structuredContent": structured,
            "_meta": {"diagnostic": SENTINEL},
            "isError": False,
        }
    )

    assert result.ok is False
    assert result.error_code == ErrorCode.FUSION_OPERATION_FAILED
    assert result.content == []
    assert result.structured_content is None
    assert result.meta == {}
    assert SENTINEL not in _serialized(result)


def test_legitimate_positive_ack_without_error_remains_successful() -> None:
    result = ToolResult.from_mcp({"structuredContent": {"success": True, "value": 1}})

    assert result.ok is True
    assert result.data == {"success": True, "value": 1}


def test_top_level_error_is_not_hidden_by_compatible_content_data() -> None:
    result = ToolResult.from_mcp(
        {
            "success": True,
            "error": SENTINEL,
            "content": [{"type": "text", "text": '{"value": 1}'}],
            "isError": False,
        }
    )

    assert result.ok is False
    assert result.public_error is not None
    assert SENTINEL not in _serialized(result)


class _CountingSchemaClient:
    def __init__(self, result: ToolResult | None = None) -> None:
        self.call_count = 0
        self.result = result or ToolResult.success(value=1)

    async def call_tool(
        self, _name: str, _args: dict[str, Any], **_kwargs: Any
    ) -> ToolResult:
        self.call_count += 1
        return self.result


def _schema_manifest() -> ToolManifest:
    return ToolManifest(
        source="test",
        tools=[
            ToolDefinition(
                name="fusion_mcp_read",
                input_schema={
                    "type": "object",
                    "properties": {"count": {"type": "integer"}},
                    "required": ["count"],
                    "additionalProperties": False,
                },
                output_schema={
                    "type": "object",
                    "properties": {"value": {"type": "integer"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
            )
        ],
    )


@pytest.mark.asyncio
async def test_adapter_input_schema_error_is_generic_and_zero_dispatch() -> None:
    client = _CountingSchemaClient()
    adapter = FusionMcpAdapter(
        client=client,
        manifest=_schema_manifest(),
        policy=ToolPolicy({"fusion_mcp_read"}),
    )

    result = await adapter.call("fusion_mcp_read", {"count": SENTINEL})

    assert result.error_code == ErrorCode.TOOL_SCHEMA_VALIDATION_ERROR
    assert result.public_error is not None
    assert result.error_message == result.public_error.generic_message
    assert client.call_count == 0
    assert SENTINEL not in _serialized(result)


@pytest.mark.asyncio
async def test_adapter_output_schema_error_is_generic_after_single_dispatch() -> None:
    client = _CountingSchemaClient(ToolResult.success(value=SENTINEL))
    adapter = FusionMcpAdapter(
        client=client,
        manifest=_schema_manifest(),
        policy=ToolPolicy({"fusion_mcp_read"}),
    )

    result = await adapter.call("fusion_mcp_read", {"count": 1})

    assert result.error_code == ErrorCode.TOOL_SCHEMA_VALIDATION_ERROR
    assert result.public_error is not None
    assert result.error_message == result.public_error.generic_message
    assert client.call_count == 1
    assert SENTINEL not in _serialized(result)


class _ExplodingSession:
    async def call_tool(self, *_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(f"provider diagnostic {SENTINEL}")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("options", "expected_code", "expected_outcome"),
    [
        (
            McpCallOptions.for_read(timeout_seconds=1),
            ErrorCode.CONNECTION_LOST,
            "known",
        ),
        (
            McpCallOptions.for_mutation(timeout_seconds=1),
            ErrorCode.MUTATION_OUTCOME_UNKNOWN,
            "unknown",
        ),
    ],
)
async def test_stdio_exception_is_generic_and_retains_only_typed_transport_state(
    options: McpCallOptions,
    expected_code: ErrorCode,
    expected_outcome: str,
) -> None:
    client = StdioMcpClient("unused-test-command")
    client._session = _ExplodingSession()
    client.state = ConnectionState.READY

    result = await client.call_tool("fusion_mcp_read", {}, options=options)

    assert result.ok is False
    assert result.error_code == expected_code
    assert result.public_error is not None
    assert result.error_message == result.public_error.generic_message
    assert result.meta["fusion_agent_transport"]["mutation_outcome"] == expected_outcome
    assert SENTINEL not in _serialized(result)
    assert client.diagnostics["last_error_present"] is True
