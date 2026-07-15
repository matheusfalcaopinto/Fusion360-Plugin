"""Pinned, policy-gated adapters for the public Fusion MCP benchmark.

The comparison manifest is data only.  This module deliberately has no
dynamic imports, command fields, subprocess launchers, or environment-based
driver discovery.  An embedding application must inject an already-created
driver and explicit prerequisite attestations from trusted code.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from benchmark.public import (
    AdapterExecution,
    AdapterPreflight,
    PublicBenchmarkConfig,
    PublicBenchmarkSubject,
    PublicBenchmarkTask,
    RevisionPin,
)


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class AdapterPrerequisites(_StrictFrozenModel):
    """Explicit local attestations required before a driver can be touched."""

    subject_id: str
    license_id: str
    license_accepted: bool
    entitlement_confirmed: bool
    isolated_installation_confirmed: bool
    normal_profile_equivalent_confirmed: bool


@dataclass(frozen=True, slots=True)
class AdapterDefinition:
    """Identity and pin compiled into the trusted adapter implementation."""

    subject_id: str
    adapter_id: str
    display_name: str
    source_url: str
    license_id: str
    redistributable: bool
    pin_kind: Literal["git", "pypi", "runtime", "workspace"]
    pin_value: str

    def context(self) -> "TrustedAdapterContext":
        return TrustedAdapterContext(
            subject_id=self.subject_id,
            adapter_id=self.adapter_id,
            display_name=self.display_name,
            source_url=self.source_url,
            license_id=self.license_id,
            redistributable=self.redistributable,
            expected_pin=RevisionPin(kind=self.pin_kind, value=self.pin_value),
        )


class TrustedAdapterContext(_StrictFrozenModel):
    """Non-executable context passed to an explicitly injected driver."""

    subject_id: str
    adapter_id: str
    display_name: str
    source_url: str
    license_id: str
    redistributable: bool
    expected_pin: RevisionPin
    execution_profile: Literal["normal_equivalent"] = "normal_equivalent"
    arbitrary_code_allowed: Literal[False] = False


class TrustedPublicBenchmarkDriver(Protocol):
    """Driver injected by trusted application code, never by a manifest."""

    async def preflight(
        self,
        context: TrustedAdapterContext,
        config: PublicBenchmarkConfig,
    ) -> AdapterPreflight: ...

    async def execute(
        self,
        context: TrustedAdapterContext,
        task: PublicBenchmarkTask,
        config: PublicBenchmarkConfig,
    ) -> AdapterExecution: ...


class PinnedPublicBenchmarkAdapter:
    """Policy wrapper shared by all supported public comparison subjects."""

    definition: AdapterDefinition

    def __init__(
        self,
        *,
        driver: TrustedPublicBenchmarkDriver | None = None,
        prerequisites: AdapterPrerequisites | None = None,
    ) -> None:
        self._driver = driver
        self._prerequisites = prerequisites
        self._authorized: set[tuple[str, str]] = set()

    async def preflight(
        self,
        subject: PublicBenchmarkSubject,
        config: PublicBenchmarkConfig,
    ) -> AdapterPreflight:
        key = self._authorization_key(subject, config)
        self._authorized.discard(key)
        config_error = self._config_error(config)
        if config_error:
            return AdapterPreflight(ready=False, reason=config_error)
        policy_error = self._policy_error(subject)
        if policy_error:
            return AdapterPreflight(ready=False, reason=policy_error)
        if self._driver is None:
            return AdapterPreflight(ready=False, reason="trusted_driver_not_injected")

        observed = await self._driver.preflight(self.definition.context(), config)
        if not isinstance(observed, AdapterPreflight):
            raise TypeError("trusted benchmark driver preflight must return AdapterPreflight")
        if not observed.ready:
            return observed
        revision_error = self._observed_revision_error(observed.observed_revision)
        if revision_error:
            return AdapterPreflight(
                ready=False,
                observed_revision=observed.observed_revision,
                environment=observed.environment,
                reason=revision_error,
            )

        self._authorized.add(key)
        environment = {
            **observed.environment,
            "adapter_policy": "pinned_trusted_injection",
            "license_status": "explicitly_accepted",
            "entitlement_status": "confirmed",
            "installation_status": "isolated_confirmed",
            "fairness_profile": "normal_equivalent_no_arbitrary_code",
        }
        return AdapterPreflight(
            ready=True,
            observed_revision=observed.observed_revision,
            environment=environment,
        )

    async def execute(
        self,
        subject: PublicBenchmarkSubject,
        task: PublicBenchmarkTask,
        config: PublicBenchmarkConfig,
    ) -> AdapterExecution:
        config_error = self._config_error(config)
        if config_error:
            return AdapterExecution(state="not_run", reason=config_error)
        policy_error = self._policy_error(subject)
        if policy_error:
            return AdapterExecution(state="not_run", reason=policy_error)
        if self._driver is None:
            return AdapterExecution(state="not_run", reason="trusted_driver_not_injected")
        if self._authorization_key(subject, config) not in self._authorized:
            return AdapterExecution(state="not_run", reason="adapter_preflight_not_authorized")
        execution = await self._driver.execute(self.definition.context(), task, config)
        if not isinstance(execution, AdapterExecution):
            raise TypeError("trusted benchmark driver execute must return AdapterExecution")
        return execution

    def _policy_error(self, subject: PublicBenchmarkSubject) -> str | None:
        definition = self.definition
        expected = {
            "subject_id": definition.subject_id,
            "adapter_id": definition.adapter_id,
            "display_name": definition.display_name,
            "source_url": definition.source_url,
            "license": definition.license_id,
            "redistributable": definition.redistributable,
            "pin_kind": definition.pin_kind,
            "pin_value": definition.pin_value,
        }
        observed = {
            "subject_id": subject.id,
            "adapter_id": subject.adapter_id,
            "display_name": subject.display_name,
            "source_url": subject.source_url,
            "license": subject.license,
            "redistributable": subject.redistributable,
            "pin_kind": subject.pin.kind,
            "pin_value": subject.pin.value,
        }
        for field, expected_value in expected.items():
            if observed[field] != expected_value:
                return f"subject_policy_mismatch:{field}"

        prerequisites = self._prerequisites
        if prerequisites is None:
            return "prerequisites_not_injected"
        if prerequisites.subject_id != definition.subject_id:
            return "prerequisite_subject_mismatch"
        if prerequisites.license_id != definition.license_id:
            return "license_id_mismatch"
        if not prerequisites.license_accepted:
            return "license_not_accepted"
        if not prerequisites.entitlement_confirmed:
            return "entitlement_not_confirmed"
        if not prerequisites.isolated_installation_confirmed:
            return "isolated_installation_not_confirmed"
        if not prerequisites.normal_profile_equivalent_confirmed:
            return "normal_profile_equivalence_not_confirmed"
        return None

    def _observed_revision_error(self, observed_revision: str | None) -> str | None:
        if not observed_revision:
            return "observed_revision_missing"
        definition = self.definition
        if definition.pin_kind in {"git", "pypi"} and observed_revision != definition.pin_value:
            return f"revision_mismatch:expected={definition.pin_value}:observed={observed_revision}"
        return None

    @staticmethod
    def _config_error(config: PublicBenchmarkConfig) -> str | None:
        if config.mode == "real" and not config.confirm_real_benchmark:
            return "real_execution_not_confirmed"
        if config.mode == "real" and not config.disposable_fixture_confirmed:
            return "disposable_fixture_not_confirmed"
        return None

    @staticmethod
    def _authorization_key(
        subject: PublicBenchmarkSubject,
        config: PublicBenchmarkConfig,
    ) -> tuple[str, str]:
        return (
            subject.model_dump_json(),
            config.model_dump_json(),
        )


class FusionAgentCodexAdapter(PinnedPublicBenchmarkAdapter):
    definition = AdapterDefinition(
        subject_id="fusion_agent_codex",
        adapter_id="fusion_agent_codex",
        display_name="Fusion Agent Codex",
        source_url="https://github.com/matheusfalcaopinto/Fusion360-Plugin",
        license_id="MIT",
        redistributable=True,
        pin_kind="workspace",
        pin_value="runtime-git-commit",
    )


class AutodeskOfficialAdapter(PinnedPublicBenchmarkAdapter):
    definition = AdapterDefinition(
        subject_id="autodesk_fusion_official",
        adapter_id="autodesk_fusion_official",
        display_name="Autodesk Fusion MCP Server",
        source_url="https://help.autodesk.com/view/fusion360/ENU/?guid=FMCP-OVERVIEW",
        license_id="Autodesk product terms",
        redistributable=False,
        pin_kind="runtime",
        pin_value="fusion-runtime-version",
    )


class FaustAdapter(PinnedPublicBenchmarkAdapter):
    definition = AdapterDefinition(
        subject_id="faust_fusion360_mcp",
        adapter_id="faust_fusion360_mcp",
        display_name="Faust Fusion 360 MCP Server",
        source_url="https://github.com/faust-machines/fusion360-mcp-server",
        license_id="MIT",
        redistributable=True,
        pin_kind="git",
        pin_value="b44b667e440da070081795cfcbfaf75de2a44251",
    )


class FrankSMcpAdapter(PinnedPublicBenchmarkAdapter):
    definition = AdapterDefinition(
        subject_id="frank_autodesk_fusion_mcp",
        adapter_id="frank_autodesk_fusion_mcp",
        display_name="Frank Hommers Autodesk Fusion MCP",
        source_url="https://github.com/frankhommers/autodesk-fusion-mcp",
        license_id="MIT",
        redistributable=True,
        pin_kind="git",
        pin_value="3859d7e82faff70dcf056bd15be7e47c5cf912a0",
    )


class NdooAdapter(PinnedPublicBenchmarkAdapter):
    definition = AdapterDefinition(
        subject_id="ndoo_fusion360_bridge",
        adapter_id="ndoo_fusion360_bridge",
        display_name="ndoo Fusion 360 MCP Bridge",
        source_url="https://github.com/ndoo/fusion360-mcp-bridge",
        license_id="MIT",
        redistributable=True,
        pin_kind="git",
        pin_value="6bd42f48d815e06825e1d3b1f95860cee98d755c",
    )


PUBLIC_ADAPTER_TYPES: Mapping[str, type[PinnedPublicBenchmarkAdapter]] = MappingProxyType(
    {
        adapter_type.definition.adapter_id: adapter_type
        for adapter_type in (
            FusionAgentCodexAdapter,
            AutodeskOfficialAdapter,
            FaustAdapter,
            FrankSMcpAdapter,
            NdooAdapter,
        )
    }
)


def build_public_adapter_registry(
    *,
    drivers: Mapping[str, TrustedPublicBenchmarkDriver] | None = None,
    prerequisites: Mapping[str, AdapterPrerequisites] | None = None,
) -> dict[str, PinnedPublicBenchmarkAdapter]:
    """Build the fixed registry without loading executable configuration.

    Missing drivers and prerequisite attestations intentionally remain missing;
    their adapters report ``not_run`` at preflight.  Unknown keys are rejected
    so a misspelled approval cannot silently authorize a different subject.
    """

    injected_drivers = dict(drivers or {})
    injected_prerequisites = dict(prerequisites or {})
    known = set(PUBLIC_ADAPTER_TYPES)
    unknown = (set(injected_drivers) | set(injected_prerequisites)) - known
    if unknown:
        raise ValueError(f"unknown public benchmark adapter injection: {sorted(unknown)}")
    return {
        adapter_id: adapter_type(
            driver=injected_drivers.get(adapter_id),
            prerequisites=injected_prerequisites.get(adapter_id),
        )
        for adapter_id, adapter_type in PUBLIC_ADAPTER_TYPES.items()
    }
