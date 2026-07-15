from __future__ import annotations

import pytest

from agent_core.capability_executor import CapabilityExecutor
from cad_spec.v2 import CadSpecV2


def _spec() -> CadSpecV2:
    return CadSpecV2.model_validate(
        {
            "cad_spec_version": "2.0",
            "intent": "Measure a component",
            "requirements": [
                {
                    "id": "mass_recorded",
                    "description": "Mass is recorded",
                    "assertion_ids": ["mass_range"],
                }
            ],
            "operations": [
                {
                    "id": "measure_mass",
                    "kind": "analysis.physical_properties",
                    "target_refs": ["part"],
                    "output_ref": "mass_report",
                    "requirement_ids": ["mass_recorded"],
                }
            ],
            "assertions": [
                {
                    "id": "mass_range",
                    "kind": "physical_property_range",
                    "target_ref": "mass_report",
                    "expected": {"min_kg": 0.0, "max_kg": 10.0},
                }
            ],
        }
    )


class Backend:
    provider = "fake"

    def __init__(self, capabilities: set[str]) -> None:
        self.capabilities = capabilities
        self.calls: list[str] = []

    async def execute_operation(self, operation):
        self.calls.append(operation.id)
        return {"mass_kg": 1.0}


@pytest.mark.asyncio
async def test_capability_preflight_happens_before_first_dispatch() -> None:
    backend = Backend(set())
    with pytest.raises(ValueError, match="physical_properties"):
        await CapabilityExecutor(backend).execute(_spec())
    assert backend.calls == []


@pytest.mark.asyncio
async def test_capability_executor_records_provider_and_evidence() -> None:
    backend = Backend({"physical_properties"})
    result = await CapabilityExecutor(backend).execute(_spec())
    assert result.success is True
    assert result.provider == "fake"
    assert result.evidence["measure_mass"] == {"mass_kg": 1.0}
    assert backend.calls == ["measure_mass"]


@pytest.mark.asyncio
async def test_dry_run_needs_no_backend() -> None:
    result = await CapabilityExecutor().execute(_spec(), dry_run=True)
    assert result.dry_run is True
    assert result.transactions[0]["status"] == "simulated"
