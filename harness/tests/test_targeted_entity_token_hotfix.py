from __future__ import annotations

import contextlib
import io
import json
import sys
import time
import types
from collections.abc import Callable
from typing import Any

import pytest

from agent_core.targeted_inspection import build_targeted_inspection_script


class _Collection:
    """Minimal stand-in for an Autodesk collection with count/item semantics."""

    def __init__(self, items: list[object] | tuple[object, ...] = ()) -> None:
        self._items = list(items)

    @property
    def count(self) -> int:
        return len(self._items)

    def item(self, index: int) -> object:
        return self._items[index]

    def itemByName(self, name: str) -> object | None:  # noqa: N802 - Autodesk API spelling
        return next((item for item in self._items if getattr(item, "name", None) == name), None)


class _Attributes:
    @staticmethod
    def itemByName(_group: str, _name: str) -> None:  # noqa: N802 - Autodesk API spelling
        return None


class _RootComponent:
    name = "Root"
    objectType = "adsk::fusion::Component"  # noqa: N815 - Autodesk API spelling
    entityToken = "component-root-token"  # noqa: N815 - Autodesk API spelling
    attributes = _Attributes()

    def __init__(self) -> None:
        self.occurrences = _Collection()
        self.bRepBodies = _Collection()  # noqa: N815 - Autodesk API spelling
        self.sketches = _Collection()
        self.features = _Collection()
        self.parentDesign: _Design | None = None  # noqa: N815 - Autodesk API spelling


class _Body:
    name = "Body1"
    objectType = "adsk::fusion::BRepBody"  # noqa: N815 - Autodesk API spelling
    entityToken = "body-token-1"  # noqa: N815 - Autodesk API spelling
    assemblyContext = None  # noqa: N815 - Autodesk API spelling
    isVisible = True  # noqa: N815 - Autodesk API spelling
    isValid = True  # noqa: N815 - Autodesk API spelling

    def __init__(self, parent: _RootComponent) -> None:
        self.parentComponent = parent  # noqa: N815 - Autodesk API spelling


TokenResult = object | Callable[[_Body], object]


class _Design:
    def __init__(self, root: _RootComponent, body: _Body, token_result: TokenResult) -> None:
        self.rootComponent = root  # noqa: N815 - Autodesk API spelling
        self.allComponents = _Collection([root])  # noqa: N815 - Autodesk API spelling
        self.allParameters = _Collection()  # noqa: N815 - Autodesk API spelling
        self._body = body
        self._token_result = token_result
        self.find_calls: list[str] = []

    def findEntityByToken(self, token: str) -> object:  # noqa: N802 - Autodesk API spelling
        self.find_calls.append(token)
        result = self._token_result(self._body) if callable(self._token_result) else self._token_result
        if isinstance(result, BaseException):
            raise result
        return result


class _Products:
    def __init__(self, design: _Design) -> None:
        self._design = design

    def itemByProductType(self, _product_type: str) -> _Design:  # noqa: N802 - Autodesk API spelling
        return self._design


class _Document:
    name = "TokenRoundTrip"
    dataFile = None  # noqa: N815 - Autodesk API spelling
    isModified = False  # noqa: N815 - Autodesk API spelling
    product = types.SimpleNamespace(productType="DesignProductType")

    def __init__(self, design: _Design) -> None:
        self.products = _Products(design)


class _FusionFixture:
    def __init__(self, token_result: TokenResult) -> None:
        self.root = _RootComponent()
        self.body = _Body(self.root)
        self.root.bRepBodies = _Collection([self.body])
        self.design = _Design(self.root, self.body, token_result)
        self.root.parentDesign = self.design
        self.document = _Document(self.design)


class _FailingItemCollection:
    count = 1

    @staticmethod
    def item(_index: int) -> object:
        raise RuntimeError("collection-item-sensitive-detail")


class _PrematureStopCollection:
    count = 1

    @staticmethod
    def item(_index: int) -> object:
        raise StopIteration


class _IterableResult:
    """Stand-in for Fusion's iterable Base[] wrapper."""

    def __init__(self, items: list[object] | tuple[object, ...]) -> None:
        self._items = items

    def __iter__(self):
        return iter(self._items)


class _FailingIterable:
    def __iter__(self):
        yield from ()
        raise RuntimeError("iterator-sensitive-detail")


class _SlowEmptyIterable:
    def __iter__(self):
        time.sleep(0.06)
        return iter(())


class _SlowEmptyCollection:
    @property
    def count(self) -> int:
        time.sleep(0.06)
        return 0

    @staticmethod
    def item(_index: int) -> object:
        raise AssertionError("empty collection must not access an item")


def _install_fake_adsk(monkeypatch: pytest.MonkeyPatch, fixture: _FusionFixture) -> None:
    application = types.SimpleNamespace(activeDocument=fixture.document, activeProduct=fixture.design)
    adsk = types.ModuleType("adsk")
    adsk.__path__ = []  # type: ignore[attr-defined]
    core = types.ModuleType("adsk.core")
    fusion = types.ModuleType("adsk.fusion")
    core.Application = type("Application", (), {"get": staticmethod(lambda: application)})
    fusion.Design = type("FusionDesign", (), {"cast": staticmethod(lambda value: value)})
    adsk.core = core  # type: ignore[attr-defined]
    adsk.fusion = fusion  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "adsk", adsk)
    monkeypatch.setitem(sys.modules, "adsk.core", core)
    monkeypatch.setitem(sys.modules, "adsk.fusion", fusion)


def _execute(payload: dict[str, Any]) -> dict[str, Any]:
    script = build_targeted_inspection_script(payload)
    namespace: dict[str, object] = {}
    exec(compile(script, "<targeted-entity-token-hotfix>", "exec"), namespace)
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        namespace["run"]("")  # type: ignore[index,operator]
    return json.loads(output.getvalue().strip())


def _token_query(token: str = "body-token-1") -> dict[str, Any]:
    return {
        "queries": [
            {
                "id": "body-by-token",
                "entity_type": "body",
                "selector": {"entity_token": token},
                "fields": ["exists", "valid"],
            }
        ]
    }


def _assert_token_failure_is_sanitized(
    payload: dict[str, Any],
    *,
    secrets: tuple[str, ...] = (),
) -> dict[str, Any]:
    result = payload["results"][0]
    assert payload["complete"] is False
    assert payload["counts_exact"] is False
    assert payload["stop_reason"]
    assert result["match_count_exact"] is False
    assert result["truncated"] is True
    assert payload["warnings"]
    assert all(str(warning.get("code", "")).startswith("ENTITY_TOKEN_") for warning in payload["warnings"])
    encoded_warnings = json.dumps(payload["warnings"], ensure_ascii=False)
    for secret in secrets:
        assert secret not in encoded_warnings
    return result


@pytest.mark.parametrize(
    "container_factory",
    [pytest.param(list, id="python-list"), pytest.param(tuple, id="python-tuple")],
)
def test_entity_token_normalizes_python_sequence_results(
    monkeypatch: pytest.MonkeyPatch,
    container_factory: Callable[[list[_Body]], list[_Body] | tuple[_Body, ...]],
) -> None:
    fixture = _FusionFixture(lambda body: container_factory([body]))
    _install_fake_adsk(monkeypatch, fixture)

    payload = _execute(_token_query())
    result = payload["results"][0]

    assert payload["complete"] is True
    assert result["match_count"] == 1
    assert result["match_count_exact"] is True
    assert result["ambiguous"] is False
    assert result["matches"][0]["entity_token"] == "body-token-1"


def test_entity_token_normalizes_autodesk_count_item_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _FusionFixture(lambda body: _Collection([body]))
    _install_fake_adsk(monkeypatch, fixture)

    payload = _execute(_token_query())
    result = payload["results"][0]

    assert payload["complete"] is True
    assert result["match_count"] == 1
    assert result["match_count_exact"] is True
    assert result["matches"][0]["name"] == "Body1"


def test_entity_token_normalizes_fusion_iterable_result(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _FusionFixture(lambda body: _IterableResult([body]))
    _install_fake_adsk(monkeypatch, fixture)

    payload = _execute(_token_query())
    result = payload["results"][0]

    assert payload["complete"] is True
    assert result["match_count"] == 1
    assert result["match_count_exact"] is True
    assert result["matches"][0]["entity_token"] == "body-token-1"


def test_entity_token_iterable_failure_is_inexact_and_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _FusionFixture(_FailingIterable())
    _install_fake_adsk(monkeypatch, fixture)

    payload = _execute(_token_query())
    result = _assert_token_failure_is_sanitized(payload, secrets=("iterator-sensitive-detail",))

    assert result["matches"] == []
    assert payload["warnings"] == [
        {
            "code": "ENTITY_TOKEN_LOOKUP_FAILED",
            "exception_type": "RuntimeError",
            "reason": "item_access_failed",
        }
    ]


def test_entity_token_collection_stop_iteration_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _FusionFixture(_PrematureStopCollection())
    _install_fake_adsk(monkeypatch, fixture)

    payload = _execute(_token_query())
    result = _assert_token_failure_is_sanitized(payload)

    assert result["matches"] == []
    assert payload["warnings"] == [
        {
            "code": "ENTITY_TOKEN_LOOKUP_FAILED",
            "exception_type": "StopIteration",
            "reason": "item_access_failed",
        }
    ]


@pytest.mark.parametrize(
    "token_result",
    [
        pytest.param(_SlowEmptyIterable(), id="iterable-shape-normalization"),
        pytest.param(_SlowEmptyCollection(), id="collection-count"),
    ],
)
def test_entity_token_empty_slow_result_respects_deadline(
    monkeypatch: pytest.MonkeyPatch,
    token_result: object,
) -> None:
    fixture = _FusionFixture(token_result)
    _install_fake_adsk(monkeypatch, fixture)

    payload = _execute({**_token_query(), "deadline_ms": 50})
    result = payload["results"][0]

    assert payload["complete"] is False
    assert payload["stop_reason"] == "deadline_ms"
    assert payload["counts_exact"] is False
    assert result["matches"] == []
    assert result["match_count_exact"] is False


@pytest.mark.parametrize(
    "invalid_item",
    [pytest.param("not-an-autodesk-base", id="string"), pytest.param({}, id="mapping")],
)
def test_entity_token_non_autodesk_item_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    invalid_item: object,
) -> None:
    fixture = _FusionFixture(_IterableResult([invalid_item]))
    _install_fake_adsk(monkeypatch, fixture)

    payload = _execute(_token_query())
    result = _assert_token_failure_is_sanitized(payload, secrets=("not-an-autodesk-base",))

    assert result["matches"] == []
    assert payload["warnings"] == [
        {
            "code": "ENTITY_TOKEN_LOOKUP_FAILED",
            "reason": "invalid_result_item",
        }
    ]


def test_entity_token_empty_result_is_an_exact_zero_match(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _FusionFixture([])
    _install_fake_adsk(monkeypatch, fixture)

    payload = _execute(_token_query("missing-token"))
    result = payload["results"][0]

    assert payload["complete"] is True
    assert payload["counts_exact"] is True
    assert result["matches"] == []
    assert result["match_count"] == 0
    assert result["match_count_exact"] is True
    assert result["ambiguous"] is False
    assert result["truncated"] is False


def test_entity_token_lookup_exception_is_inexact_and_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _FusionFixture(RuntimeError("token database unavailable"))
    _install_fake_adsk(monkeypatch, fixture)

    payload = _execute(_token_query())
    result = payload["results"][0]

    assert payload["complete"] is False
    assert payload["counts_exact"] is False
    assert payload["stop_reason"] == "entity_token_lookup_failed"
    assert result["matches"] == []
    assert result["match_count_exact"] is False
    assert result["truncated"] is True
    assert payload["warnings"] == [
        {
            "code": "ENTITY_TOKEN_LOOKUP_FAILED",
            "exception_type": "RuntimeError",
        }
    ]
    assert "token database unavailable" not in json.dumps(payload)


def test_entity_token_none_result_is_invalid_and_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    selector_token = "none-selector-sensitive-token"
    fixture = _FusionFixture(None)
    _install_fake_adsk(monkeypatch, fixture)

    payload = _execute(_token_query(selector_token))
    result = _assert_token_failure_is_sanitized(payload, secrets=(selector_token,))

    assert result["matches"] == []


def test_entity_token_scalar_result_is_an_invalid_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    selector_token = "scalar-selector-sensitive-token"
    fixture = _FusionFixture(lambda body: body)
    _install_fake_adsk(monkeypatch, fixture)

    payload = _execute(_token_query(selector_token))
    result = _assert_token_failure_is_sanitized(payload, secrets=(selector_token,))

    assert result["matches"] == []


def test_entity_token_collection_item_failure_is_inexact(monkeypatch: pytest.MonkeyPatch) -> None:
    selector_token = "collection-selector-sensitive-token"
    fixture = _FusionFixture(_FailingItemCollection())
    _install_fake_adsk(monkeypatch, fixture)

    payload = _execute(_token_query(selector_token))
    result = _assert_token_failure_is_sanitized(
        payload,
        secrets=(selector_token, "collection-item-sensitive-detail"),
    )

    assert result["matches"] == []


def test_entity_token_false_status_tuple_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    selector_token = "false-status-sensitive-token"
    fixture = _FusionFixture(lambda body: ([body], False))
    _install_fake_adsk(monkeypatch, fixture)

    payload = _execute(_token_query(selector_token))
    result = _assert_token_failure_is_sanitized(payload, secrets=(selector_token,))

    assert result["matches"] == []


def test_entity_token_results_above_limit_are_capped_and_inexact(monkeypatch: pytest.MonkeyPatch) -> None:
    selector_token = "limit-selector-sensitive-token"
    fixture = _FusionFixture(lambda body: [body] * 8)
    _install_fake_adsk(monkeypatch, fixture)
    request = _token_query(selector_token)
    request["limit_per_query"] = 2

    payload = _execute(request)
    result = payload["results"][0]

    assert payload["complete"] is True
    assert payload["counts_exact"] is True
    assert payload["stop_reason"] is None
    assert payload["warnings"] == []
    assert len(result["matches"]) == 2
    assert len(result["matches"]) <= request["limit_per_query"]
    assert result["match_count_exact"] is False
    assert result["truncated"] is True
    assert result["ambiguous"] is True
    assert selector_token not in json.dumps(payload)


def test_entity_token_iteration_respects_entity_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    selector_token = "budget-selector-sensitive-token"
    fixture = _FusionFixture(lambda body: [body] * 20)
    _install_fake_adsk(monkeypatch, fixture)
    request = _token_query(selector_token)
    request.update({"max_entities_visited": 2, "limit_per_query": 20})

    payload = _execute(request)
    result = payload["results"][0]

    assert payload["complete"] is False
    assert payload["counts_exact"] is False
    assert payload["stop_reason"] == "max_entities_visited"
    assert payload["visited_entities"] <= request["max_entities_visited"]
    assert payload["warnings"] == []
    assert result["match_count_exact"] is False
    assert result["truncated"] is True
    assert len(result["matches"]) <= request["max_entities_visited"]
    assert selector_token not in json.dumps(payload)


def test_entity_token_lookup_rechecks_deadline_after_api_call(monkeypatch: pytest.MonkeyPatch) -> None:
    selector_token = "deadline-selector-sensitive-token"

    def delayed_lookup(body: _Body) -> list[_Body]:
        time.sleep(0.06)
        return [body]

    fixture = _FusionFixture(delayed_lookup)
    _install_fake_adsk(monkeypatch, fixture)
    request = _token_query(selector_token)
    request["deadline_ms"] = 50

    payload = _execute(request)
    result = payload["results"][0]

    assert payload["complete"] is False
    assert payload["counts_exact"] is False
    assert payload["stop_reason"] == "deadline_ms"
    assert payload["warnings"] == []
    assert result["matches"] == []
    assert result["match_count_exact"] is False
    assert result["truncated"] is True
    assert selector_token not in json.dumps(payload)


def test_path_to_token_to_token_round_trip_uses_the_returned_token(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _FusionFixture(lambda body: [body])
    _install_fake_adsk(monkeypatch, fixture)

    path_payload = _execute(
        {
            "queries": [
                {
                    "id": "body-by-path",
                    "entity_type": "body",
                    "selector": {"component_path": "root", "name": "Body1"},
                    "fields": ["exists"],
                }
            ]
        }
    )
    returned_token = path_payload["results"][0]["matches"][0]["entity_token"]
    token_payload = _execute(_token_query(returned_token))
    token_result = token_payload["results"][0]

    assert returned_token == "body-token-1"
    assert fixture.design.find_calls == [returned_token]
    assert token_result["match_count"] == 1
    assert token_result["match_count_exact"] is True
    assert token_result["matches"][0]["path"] == "root/Body1"
