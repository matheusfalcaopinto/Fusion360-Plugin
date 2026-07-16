"""Typed CadSpec v2 adapter for the optional Faust MCP surface.

Only curated tools are mapped.  In particular, ``execute_code`` and
``delete_all`` are never placed in the adapter policy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from cad_spec.v2 import OperationSpec, ParameterOperation
from fusion_mcp_adapter.adapter import FusionMcpAdapter
from fusion_mcp_adapter.client import McpClient
from fusion_mcp_adapter.policy import ToolPolicy
from fusion_mcp_adapter.semantics import McpCallOptions
from fusion_mcp_adapter.tool_result import ToolManifest


_TOOL_CAPABILITIES: dict[str, set[str]] = {
    "parameters": {"create_parameter"},
}


@dataclass(frozen=True, slots=True)
class FaustCapabilityProof:
    operation_kind: str
    preserved_fields: tuple[str, ...]
    restrictions: tuple[str, ...]


FAUST_CAPABILITY_PROOFS = {
    "parameters": FaustCapabilityProof(
        operation_kind="parameter.set",
        preserved_fields=("name", "expression.value", "expression.unit", "comment"),
        restrictions=(
            "expression must be one literal numeric value with an explicit unit",
        ),
    )
}

if set(FAUST_CAPABILITY_PROOFS) != set(_TOOL_CAPABILITIES):
    raise RuntimeError(
        "Faust capability advertisement lacks an exhaustive proof registry"
    )


_READ_TOOLS: set[str] = set()
_BLOCKED_TOOLS = {"execute_code", "delete_all"}
FAUST_IMPLEMENTED_CAPABILITIES = frozenset(_TOOL_CAPABILITIES)


class FaustOperationDispatchError(RuntimeError):
    """Faust failure carrying authoritative stdio dispatch evidence."""

    def __init__(
        self, message: str, *, error_code: str | None, transport: dict[str, Any]
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.transport = transport


class FaustTypedBackend:
    """Map strict operations to Faust 0.1.0's explicit tool schemas."""

    provider = "faust_stdio"

    def __init__(self, adapter: FusionMcpAdapter, manifest: ToolManifest) -> None:
        self.adapter = adapter
        self.manifest = manifest
        self._tool_names = manifest.names()
        self._plans: dict[str, list[tuple[str, dict[str, Any]]]] = {}

    @classmethod
    def from_client(
        cls, client: McpClient, manifest: ToolManifest
    ) -> "FaustTypedBackend":
        allowed = manifest.names() - _BLOCKED_TOOLS
        adapter = FusionMcpAdapter(
            client=client,
            manifest=manifest,
            policy=ToolPolicy.from_manifest(allowed),
        )
        return cls(adapter, manifest)

    @property
    def capabilities(self) -> set[str]:
        return {
            capability
            for capability, required_tools in _TOOL_CAPABILITIES.items()
            if required_tools <= self._tool_names
        }

    def preflight_operations(self, operations: list[OperationSpec]) -> None:
        """Compile every native call before the first one can be dispatched."""

        self._plans = {}
        plans: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        for operation in operations:
            plans[operation.id] = _faust_calls(operation)
        self._plans = plans

    async def execute_operation(self, operation: OperationSpec) -> dict[str, Any]:
        calls = self._plans.get(operation.id)
        if calls is None:
            raise RuntimeError("Faust operation was not preflighted")
        results: list[dict[str, Any]] = []
        for tool, arguments in calls:
            options = (
                McpCallOptions.for_read()
                if tool in _READ_TOOLS
                else McpCallOptions.for_mutation()
            )
            result = await self.adapter.call(tool, arguments, options=options)
            if not result.ok:
                raise FaustOperationDispatchError(
                    f"Faust typed operation {operation.id} failed: "
                    f"{result.error_code}: {result.error_message}",
                    error_code=result.error_code,
                    transport=_transport_evidence(result),
                )
            results.append(
                {
                    "native_tool": tool,
                    "data": result.data,
                    "transport": _transport_evidence(result),
                    "manifest_fingerprint": self.manifest.fingerprint,
                }
            )
        return {"provider": self.provider, "calls": results}


def _faust_calls(
    operation: OperationSpec,
    parameters: dict[str, str] | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    """Compile the sole Faust operation with an exhaustive lossless proof."""

    del parameters
    if not isinstance(operation, ParameterOperation):
        raise ValueError(
            f"Faust cannot produce a lossless capability proof for {operation.kind}"
        )
    value, unit = _numeric_unit(operation.expression)
    return [
        (
            "create_parameter",
            {
                "name": operation.name,
                "value": value,
                "unit": unit,
                "comment": operation.comment or "",
            },
        )
    ]


def _numeric_unit(expression: str) -> tuple[float, str]:
    match = re.fullmatch(
        r"\s*(-?\d+(?:\.\d+)?)\s*(mm|cm|in|deg|rad)\s*",
        expression,
        re.IGNORECASE,
    )
    if not match:
        raise ValueError(
            f"Faust requires a literal numeric unit expression: {expression!r}"
        )
    return float(match.group(1)), match.group(2).lower()


def _transport_evidence(result: Any) -> dict[str, Any]:
    transport = getattr(result, "meta", {}).get("fusion_agent_transport")
    if isinstance(transport, dict):
        return dict(transport)
    data = getattr(result, "data", {})
    if isinstance(data, dict):
        return {
            key: data[key]
            for key in (
                "dispatched",
                "may_have_applied",
                "post_dispatch_replay_suppressed",
                "mutation_outcome",
            )
            if key in data
        }
    return {}
