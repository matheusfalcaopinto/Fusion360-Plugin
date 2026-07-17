from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from cad_spec.unit_policy import expression_to_mm, validate_dimension_expression
from cad_spec.v2 import CadSpecV2, ParameterOperation
from fusion_mcp_adapter.tool_result import ToolDefinition, ToolManifest
from fusion_tool_facade.typed_backend import FaustTypedBackend


_OVERFLOWING_LITERAL = f"{'9' * 400} mm"


class _NoDispatchClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def list_tools(self) -> None:
        raise AssertionError("manifest is already supplied")

    async def call_tool(
        self, name: str, arguments: dict[str, object], *, options: object = None
    ) -> None:
        del options
        self.calls.append((name, arguments))
        raise AssertionError("invalid numeric input reached dispatch")


def _dimension_contract(
    *, expected: object = "10 mm", tolerance: object = "0.1 mm"
) -> dict[str, object]:
    return {
        "cad_spec_version": "2.0",
        "intent": "Set and verify one finite dimension",
        "requirements": [
            {
                "id": "shaft_dimension",
                "description": "shaft dimension is finite",
                "assertion_ids": ["shaft_dimension_matches"],
            }
        ],
        "operations": [
            {
                "id": "set_shaft_dimension",
                "kind": "parameter.set",
                "name": "shaft_dimension",
                "expression": "10 mm",
                "requirement_ids": ["shaft_dimension"],
            }
        ],
        "assertions": [
            {
                "id": "shaft_dimension_matches",
                "kind": "dimension_equals",
                "target_ref": "shaft_dimension",
                "expected": expected,
                "tolerance": tolerance,
            }
        ],
    }


def test_v2_rejects_negative_literal_tolerance() -> None:
    with pytest.raises(ValidationError, match="tolerance.*non-negative"):
        CadSpecV2.model_validate(_dimension_contract(tolerance="-1 mm"))


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), float("-inf")])
def test_v2_rejects_boolean_or_non_finite_expected_number(value: object) -> None:
    with pytest.raises(ValidationError, match="typed expected|finite"):
        CadSpecV2.model_validate(_dimension_contract(expected=value))


def test_v2_rejects_decimal_that_overflows_float() -> None:
    with pytest.raises(ValidationError, match="finite"):
        CadSpecV2.model_validate(_dimension_contract(expected=Decimal("1e10000")))


def test_v2_rejects_unit_literal_that_overflows_float() -> None:
    payload = _dimension_contract()
    operations = payload["operations"]
    assert isinstance(operations, list)
    operations[0]["expression"] = _OVERFLOWING_LITERAL

    with pytest.raises(ValidationError, match="finite"):
        CadSpecV2.model_validate(payload)


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), float("-inf")])
def test_unit_conversion_rejects_boolean_or_non_finite_input(value: object) -> None:
    with pytest.raises(ValueError, match="string|finite"):
        expression_to_mm(value)  # type: ignore[arg-type]


def test_unit_policy_rejects_decimal_to_float_overflow() -> None:
    with pytest.raises(ValueError, match="finite"):
        validate_dimension_expression(_OVERFLOWING_LITERAL, "parameter expression")
    with pytest.raises(ValueError, match="finite"):
        expression_to_mm(_OVERFLOWING_LITERAL)


def test_faust_preflight_rejects_unbound_operation_without_dispatch() -> None:
    client = _NoDispatchClient()
    backend = FaustTypedBackend.from_client(
        client,
        ToolManifest(
            source="numeric-security-test",
            tools=[ToolDefinition(name="create_parameter")],
        ),
    )
    operation = ParameterOperation.model_construct(
        id="set_unbounded_parameter",
        kind="parameter.set",
        name="unbounded_parameter",
        expression=_OVERFLOWING_LITERAL,
        depends_on=[],
        requirement_ids=[],
    )

    with pytest.raises(ValueError, match="lossless document and target authority"):
        backend.preflight_operations([operation])

    assert backend.capabilities == set()
    assert client.calls == []


def test_finite_numeric_controls_remain_supported() -> None:
    spec = CadSpecV2.model_validate(
        _dimension_contract(expected=Decimal("10.25"), tolerance="0.05 mm")
    )
    assert spec.assertions[0].expected == Decimal("10.25")
    assert expression_to_mm("1.25 in") == pytest.approx(31.75)

    client = _NoDispatchClient()
    backend = FaustTypedBackend.from_client(
        client,
        ToolManifest(
            source="numeric-security-test",
            tools=[ToolDefinition(name="create_parameter")],
        ),
    )
    assert backend.capabilities == set()
    with pytest.raises(ValueError, match="lossless document and target authority"):
        backend.preflight_operations(list(spec.operations))
    assert client.calls == []
