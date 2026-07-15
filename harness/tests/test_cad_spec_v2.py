from __future__ import annotations

import pytest
from pydantic import ValidationError

from cad_spec.v2 import CadSpecV2, parse_cad_spec


def _payload() -> dict:
    return {
        "cad_spec_version": "2.0",
        "intent": "Create and validate a revolved shaft",
        "requirements": [
            {
                "id": "shaft_exists",
                "description": "The shaft body exists",
                "assertion_ids": ["shaft_body_exists"],
            }
        ],
        "operations": [
            {
                "id": "shaft_revolve",
                "kind": "feature.revolve",
                "component_ref": "root",
                "profile_ref": "shaft_profile",
                "axis_ref": "x_axis",
                "angle": "360 deg",
                "result_name": "shaft_body",
                "requirement_ids": ["shaft_exists"],
            }
        ],
        "assertions": [
            {
                "id": "shaft_body_exists",
                "kind": "entity_exists",
                "target_ref": "shaft_body",
            }
        ],
    }


def test_v2_is_strict_and_exposes_required_capabilities() -> None:
    spec = CadSpecV2.model_validate(_payload())
    assert spec.capabilities == {"revolve"}
    spec.ensure_supported({"revolve"})
    with pytest.raises(ValueError, match="lacks required capabilities"):
        spec.ensure_supported(set())

    invalid = _payload()
    invalid["operations"][0]["anglle"] = "90 deg"
    with pytest.raises(ValidationError, match="anglle"):
        CadSpecV2.model_validate(invalid)

    invalid_policy = _payload()
    invalid_policy["document_policy"] = {
        "modify_existing": False,
        "create_checkpoint": True,
        "silently_save": True,
    }
    with pytest.raises(ValidationError, match="silently_save"):
        CadSpecV2.model_validate(invalid_policy)


def test_v2_rejects_uncovered_required_requirement() -> None:
    payload = _payload()
    payload["requirements"][0]["assertion_ids"] = []
    with pytest.raises(ValidationError, match="has no assertions"):
        CadSpecV2.model_validate(payload)


def test_v2_rejects_forward_or_unknown_dependencies() -> None:
    payload = _payload()
    payload["operations"][0]["depends_on"] = ["future_operation"]
    with pytest.raises(ValidationError, match="unknown dependencies"):
        CadSpecV2.model_validate(payload)


def test_experimental_manufacturing_is_double_gated() -> None:
    payload = _payload()
    payload["operations"] = [
        {
            "id": "cam_setup",
            "kind": "experimental.cam",
            "operation": "setup",
            "target_ref": "shaft_body",
            "requirement_ids": ["shaft_exists"],
        }
    ]
    spec = CadSpecV2.model_validate(payload)
    with pytest.raises(ValueError, match="EXPERIMENTAL_MANUFACTURING"):
        spec.ensure_supported({"cam_setup"})
    spec.ensure_supported({"cam_setup"}, experimental_enabled=True)


def test_legacy_spec_is_accepted_but_not_contract_eligible() -> None:
    legacy = {
        "intent": "Create a block",
        "parameters": [],
        "components": [
            {
                "name": "block_component",
                "features": [
                    {
                        "name": "block_feature",
                        "type": "extrude_rectangle",
                        "inputs": {
                            "sketch_name": "block_sketch",
                            "body_name": "block_body",
                            "width": "10 mm",
                            "height": "10 mm",
                            "distance": "10 mm",
                        },
                    }
                ],
            }
        ],
        "acceptance_tests": [{"type": "body_exists", "target": "block_body"}],
    }
    normalized = parse_cad_spec(legacy)
    assert normalized.source_version == "1"
    assert normalized.legacy_spec is not None
    assert normalized.spec is None
    assert normalized.contract_eligible is False
    assert "deprecated" in normalized.warnings[0]


def test_parse_v2_is_contract_eligible() -> None:
    normalized = parse_cad_spec(_payload())
    assert normalized.source_version == "2.0"
    assert normalized.spec is not None
    assert normalized.contract_eligible is True
