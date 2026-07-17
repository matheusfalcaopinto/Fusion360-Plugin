"""Fail-closed CadSpec v2 adapter for the optional Faust MCP surface.

Faust 0.1.0 cannot carry exact document and target authority into its mutation
sink, so 0.4.1 advertises no Faust operations.  In particular,
``execute_code`` and ``delete_all`` are never placed in the adapter policy.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from cad_spec.unit_policy import parse_finite_unit_expression
from cad_spec.v2 import OperationSpec, ParameterOperation
from fusion_mcp_adapter.adapter import FusionMcpAdapter
from fusion_mcp_adapter.client import McpClient
from fusion_mcp_adapter.policy import ToolPolicy
from fusion_mcp_adapter.semantics import McpCallOptions
from fusion_mcp_adapter.tool_result import ToolManifest


_TOOL_CAPABILITIES: dict[str, set[str]] = {}


@dataclass(frozen=True, slots=True)
class FaustCapabilityProof:
    operation_kind: str
    preserved_fields: tuple[str, ...]
    restrictions: tuple[str, ...]


FAUST_CAPABILITY_PROOFS: dict[str, FaustCapabilityProof] = {}

if set(FAUST_CAPABILITY_PROOFS) != set(_TOOL_CAPABILITIES):
    raise RuntimeError(
        "Faust capability advertisement lacks an exhaustive proof registry"
    )


_READ_TOOLS: set[str] = set()
_BLOCKED_TOOLS = {"execute_code", "delete_all"}
FAUST_IMPLEMENTED_CAPABILITIES = frozenset(_TOOL_CAPABILITIES)

FaustCall = tuple[str, dict[str, Any]]
FaustPlanMap = Mapping[str, tuple[FaustCall, ...]]
_EMPTY_FAUST_PLANS: FaustPlanMap = MappingProxyType({})


class FaustOperationDispatchError(RuntimeError):
    """Faust failure carrying authoritative stdio dispatch evidence."""

    def __init__(
        self, message: str, *, error_code: str | None, transport: dict[str, Any]
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.transport = transport


class FaustTypedBackend:
    """Retain Faust selection compatibility without advertising mutations."""

    provider = "faust_stdio"

    def __init__(self, adapter: FusionMcpAdapter, manifest: ToolManifest) -> None:
        self.adapter = adapter
        self.manifest = manifest
        self._tool_names = manifest.names()
        # One backend instance is shared by concurrent MCP requests.  A plan
        # therefore belongs to the current task context, not to the service.
        self._plans: ContextVar[FaustPlanMap] = ContextVar(
            f"faust_operation_plans_{id(self)}", default=_EMPTY_FAUST_PLANS
        )

    @classmethod
    def from_client(
        cls, client: McpClient, manifest: ToolManifest
    ) -> "FaustTypedBackend":
        # Faust currently exposes no operation that can carry both a document
        # identity and an exact target binding into the mutation sink.  Keep
        # its mutation tools outside policy even for direct backend callers.
        allowed = manifest.names() & _READ_TOOLS
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

        self._plans.set(_EMPTY_FAUST_PLANS)
        if operations:
            kinds = ", ".join(sorted({str(operation.kind) for operation in operations}))
            raise ValueError(
                "Faust cannot produce lossless document and target authority for: "
                + kinds
            )

    async def execute_operation(self, operation: OperationSpec) -> dict[str, Any]:
        calls = self._plans.get().get(operation.id)
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
    """Validate a future literal mapping without advertising or dispatching it."""

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
    try:
        return parse_finite_unit_expression(expression, "Faust parameter expression")
    except ValueError as exc:
        raise ValueError(
            f"Faust requires a finite literal numeric unit expression: {expression!r}"
        ) from exc


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
