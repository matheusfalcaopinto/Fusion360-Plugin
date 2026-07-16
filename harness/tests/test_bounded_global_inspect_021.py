from __future__ import annotations

import io
import json
import sys
import types
from contextlib import redirect_stdout

import pytest

from fusion_mcp_adapter.semantics import ReplayPolicy
from fusion_mcp_adapter.tool_result import ToolResult
from fusion_tool_facade.vendor_facade import (
    VendorFusionFacade,
    _bounded_inspect_script,
    _normalize_inspection_options,
)


class _CountOnlyCollection:
    def __init__(self, count: int) -> None:
        self.count = count
        self.item_accesses = 0

    def item(self, _index: int):
        self.item_accesses += 1
        raise AssertionError(
            "bounded document/count inspection must not enumerate this collection"
        )


class _ItemCollection:
    def __init__(self, items) -> None:
        self._items = list(items)
        self.count = len(self._items)
        self.item_accesses = 0

    def item(self, index: int):
        self.item_accesses += 1
        return self._items[index]


def _install_fake_adsk(monkeypatch: pytest.MonkeyPatch):
    components = _CountOnlyCollection(50_000)
    occurrences = _CountOnlyCollection(50_000)
    bodies = _CountOnlyCollection(50_000)
    parameters = _CountOnlyCollection(50_000)
    root = types.SimpleNamespace(
        name="Root",
        allOccurrences=occurrences,
        bRepBodies=bodies,
    )
    design = types.SimpleNamespace(
        rootComponent=root,
        allComponents=components,
        userParameters=parameters,
        unitsManager=types.SimpleNamespace(defaultLengthUnits="mm"),
    )
    app = types.SimpleNamespace(
        activeDocument=types.SimpleNamespace(name="LargeFixture"),
        activeProduct=design,
    )

    adsk = types.ModuleType("adsk")
    adsk.__path__ = []  # type: ignore[attr-defined]
    core = types.ModuleType("adsk.core")
    fusion = types.ModuleType("adsk.fusion")
    core.Application = types.SimpleNamespace(get=lambda: app)
    fusion.Design = types.SimpleNamespace(cast=lambda value: value)
    adsk.core = core  # type: ignore[attr-defined]
    adsk.fusion = fusion  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "adsk", adsk)
    monkeypatch.setitem(sys.modules, "adsk.core", core)
    monkeypatch.setitem(sys.modules, "adsk.fusion", fusion)
    return components, occurrences, bodies, parameters


def _install_geometry_adsk(monkeypatch: pytest.MonkeyPatch):
    point_min = types.SimpleNamespace(x=0.0, y=0.0, z=0.0)
    point_max = types.SimpleNamespace(x=1.0, y=2.0, z=3.0)
    bbox = types.SimpleNamespace(minPoint=point_min, maxPoint=point_max)
    components = []
    for component_index in range(100):
        bodies = [
            types.SimpleNamespace(
                name=f"Body_{component_index}_{body_index}_" + ("x" * 180),
                boundingBox=bbox,
                isValid=True,
                isLightBulbOn=True,
            )
            for body_index in range(10)
        ]
        components.append(
            types.SimpleNamespace(
                name=f"Component_{component_index}_" + ("y" * 80),
                bRepBodies=_ItemCollection(bodies),
                sketches=_ItemCollection([]),
            )
        )
    component_collection = _ItemCollection(components)
    root = types.SimpleNamespace(
        name="Root",
        allOccurrences=_ItemCollection([]),
        bRepBodies=_ItemCollection([]),
    )
    design = types.SimpleNamespace(
        rootComponent=root,
        allComponents=component_collection,
        userParameters=_ItemCollection([]),
        unitsManager=types.SimpleNamespace(defaultLengthUnits="mm"),
    )
    app = types.SimpleNamespace(
        activeDocument=types.SimpleNamespace(name="GeometryFixture"),
        activeProduct=design,
    )
    adsk = types.ModuleType("adsk")
    adsk.__path__ = []  # type: ignore[attr-defined]
    core = types.ModuleType("adsk.core")
    fusion = types.ModuleType("adsk.fusion")
    core.Application = types.SimpleNamespace(get=lambda: app)
    fusion.Design = types.SimpleNamespace(cast=lambda value: value)
    adsk.core = core  # type: ignore[attr-defined]
    adsk.fusion = fusion  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "adsk", adsk)
    monkeypatch.setitem(sys.modules, "adsk.core", core)
    monkeypatch.setitem(sys.modules, "adsk.fusion", fusion)
    return component_collection


def test_default_global_inspect_is_constant_time_on_large_design(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collections = _install_fake_adsk(monkeypatch)
    namespace: dict[str, object] = {}
    exec(_bounded_inspect_script(), namespace)
    output = io.StringIO()
    with redirect_stdout(output):
        namespace["run"]("")  # type: ignore[index,operator]

    encoded = output.getvalue().strip().encode("utf-8")
    payload = json.loads(encoded)
    assert payload["document_name"] == "LargeFixture"
    assert payload["inspection_meta"]["sections_requested"] == ["document", "counts"]
    assert payload["inspection_meta"]["visited_entities"] == 0
    assert payload["physical_properties"] == {}
    assert payload["inspection_meta"]["response_bytes"] == len(encoded)
    assert len(encoded) <= 1_048_576
    assert all(collection.item_accesses == 0 for collection in collections)


def test_inspection_budget_validation_is_hard_capped() -> None:
    assert _normalize_inspection_options(None) == {
        "sections": ["document", "counts"],
        "max_entities_visited": 1000,
        "deadline_ms": 1500,
        "max_response_bytes": 1_048_576,
    }
    with pytest.raises(ValueError, match="max_entities_visited"):
        _normalize_inspection_options({"max_entities_visited": 5001})
    with pytest.raises(ValueError, match="deadline_ms"):
        _normalize_inspection_options({"deadline_ms": 5001})
    with pytest.raises(ValueError, match="max_response_bytes"):
        _normalize_inspection_options({"max_response_bytes": 1_048_577})
    with pytest.raises(ValueError, match="sections"):
        _normalize_inspection_options({"sections": []})


def test_response_budget_stops_geometry_traversal_and_is_exact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    components = _install_geometry_adsk(monkeypatch)
    namespace: dict[str, object] = {}
    exec(
        _bounded_inspect_script(
            {
                "sections": ["document", "counts", "geometry"],
                "max_entities_visited": 5000,
                "deadline_ms": 5000,
                "max_response_bytes": 4096,
            }
        ),
        namespace,
    )
    output = io.StringIO()
    with redirect_stdout(output):
        namespace["run"]("")  # type: ignore[index,operator]

    encoded = output.getvalue().strip().encode("utf-8")
    payload = json.loads(encoded)
    meta = payload["inspection_meta"]
    assert meta["complete"] is False
    assert meta["truncated"] is True
    assert meta["stop_reason"] == "response_limit"
    assert meta["counts_exact"] is False
    assert meta["response_bytes"] == len(encoded)
    assert len(encoded) <= 4096
    assert components.item_accesses < components.count


@pytest.mark.asyncio
async def test_global_inspect_uses_non_replayable_trusted_read() -> None:
    class Adapter:
        def __init__(self) -> None:
            self.options = None

        async def call(self, _name, _arguments, *, options=None):
            self.options = options
            state = {
                "document_name": "Fixture",
                "inspection_meta": {
                    "complete": True,
                    "truncated": False,
                    "visited_entities": 0,
                    "elapsed_ms": 1,
                    "response_bytes": 128,
                    "counts_exact": True,
                    "stop_reason": "complete",
                },
            }
            return ToolResult.success(message=json.dumps(state))

    adapter = Adapter()
    facade = VendorFusionFacade(
        adapter, available_tools={"fusion_mcp_read", "fusion_mcp_execute"}
    )
    result = await facade.inspect_design()
    assert result["complete"] is True
    assert adapter.options.replay_policy == ReplayPolicy.BEFORE_DISPATCH_ONLY
    assert adapter.options.trusted_internal_read is True


@pytest.mark.asyncio
async def test_non_crud_inspection_fails_closed_without_vendor_calls() -> None:
    class Adapter:
        async def call(self, *_args, **_kwargs):
            raise AssertionError("unbounded legacy vendor calls must not run")

    facade = VendorFusionFacade(Adapter(), available_tools={"get_scene_info"})
    result = await facade.inspect_design()
    assert result["complete"] is False
    assert result["counts_exact"] is False
    assert result["stop_reason"] == "unsupported_unbounded_facade"
