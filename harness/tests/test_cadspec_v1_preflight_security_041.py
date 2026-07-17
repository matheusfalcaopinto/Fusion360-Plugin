from __future__ import annotations

import pytest
from pydantic import ValidationError

from cad_spec.models import CadSpec


def _payload(feature: dict[str, object]) -> dict[str, object]:
    return {
        "intent": "Validate the complete legacy graph before mutation",
        "parameters": [],
        "components": [{"name": "fixture", "features": [feature]}],
        "acceptance_tests": [{"type": "body_count", "target": 1}],
    }


def test_cadspec_v1_rejects_unknown_feature_before_execution() -> None:
    with pytest.raises(ValidationError, match="type"):
        CadSpec.model_validate(
            _payload(
                {
                    "name": "late_unknown",
                    "type": "unregistered_mutation",
                    "inputs": {},
                }
            )
        )


@pytest.mark.parametrize("value", [True, False])
def test_cadspec_v1_rejects_boolean_dimensions_before_execution(value: bool) -> None:
    with pytest.raises(ValidationError, match="width"):
        CadSpec.model_validate(
            _payload(
                {
                    "name": "boolean_width",
                    "type": "extrude_rectangle",
                    "inputs": {
                        "width": value,
                        "height": "5 mm",
                        "distance": "2 mm",
                    },
                }
            )
        )


def test_cadspec_v1_keeps_registered_feature_and_explicit_units() -> None:
    spec = CadSpec.model_validate(
        _payload(
            {
                "name": "valid_profile",
                "type": "extrude_rectangle",
                "inputs": {
                    "width": "10 mm",
                    "height": "5 mm",
                    "distance": "2 mm",
                },
            }
        )
    )

    assert spec.components[0].features[0].type == "extrude_rectangle"


@pytest.mark.parametrize(
    "acceptance",
    [
        {"type": "body_exists"},
        {"type": "component_exists", "target": ""},
        {"type": "named_bodies", "target": []},
        {"type": "named_parameters", "target": None},
        {"type": "bounding_box"},
        {
            "type": "target_bounding_box",
            "target": "body",
            "target_mm": [1.0, 2.0],
        },
        {"type": "nema17_dimensions", "target": {}},
        {"type": "occurrence_contract", "target": {}},
        {"type": "export_exists", "target": []},
    ],
)
def test_cadspec_v1_rejects_vacuous_assertion_inputs(
    acceptance: dict[str, object],
) -> None:
    payload = _payload(
        {
            "name": "valid_profile",
            "type": "extrude_rectangle",
            "inputs": {
                "width": "10 mm",
                "height": "5 mm",
                "distance": "2 mm",
            },
        }
    )
    payload["acceptance_tests"] = [acceptance]

    with pytest.raises(ValidationError):
        CadSpec.model_validate(payload)


@pytest.mark.parametrize(
    "assertion_type",
    [
        "component_metadata",
        "joint_contract",
        "screenshots_exist",
        "physical_properties",
    ],
)
def test_cadspec_v1_rejects_assertions_without_their_spec_contract(
    assertion_type: str,
) -> None:
    payload = _payload(
        {
            "name": "valid_profile",
            "type": "extrude_rectangle",
            "inputs": {
                "width": "10 mm",
                "height": "5 mm",
                "distance": "2 mm",
            },
        }
    )
    payload["acceptance_tests"] = [{"type": assertion_type}]

    with pytest.raises(ValidationError):
        CadSpec.model_validate(payload)
