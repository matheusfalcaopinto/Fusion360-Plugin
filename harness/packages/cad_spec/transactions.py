"""CadSpec transaction normalization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cad_spec.models import CadSpec


@dataclass(frozen=True)
class TransactionStep:
    """A small executable phase derived from a CadSpec."""

    name: str
    operation: str
    payload: dict[str, Any]


def normalize_transactions(spec: CadSpec) -> list[TransactionStep]:
    """Convert a CAD Spec into ordered transaction steps."""

    steps: list[TransactionStep] = [
        TransactionStep("inspect_design", "inspect_design", {}),
    ]
    if spec.document_policy.create_checkpoint:
        steps.append(
            TransactionStep(
                "checkpoint_policy",
                "checkpoint_policy",
                spec.document_policy.model_dump(),
            )
        )
    for parameter in spec.parameters:
        steps.append(
            TransactionStep(
                f"parameter_{parameter.name}",
                "create_named_parameter",
                parameter.model_dump(),
            )
        )
    for component in spec.components:
        steps.append(
            TransactionStep(
                f"component_{component.name}",
                "create_component",
                {"name": component.name},
            )
        )
        for feature in component.features:
            steps.append(
                TransactionStep(
                    f"feature_{feature.name}",
                    feature.type,
                    {"component": component.name, "feature": feature.model_dump()},
                )
            )
    steps.append(TransactionStep("verify_final", "verify", {}))
    return steps
