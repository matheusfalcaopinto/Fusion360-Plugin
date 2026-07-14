from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from agent_core.fast_path import lint_fusion_script, validate_fast_execute_request
from fusion_agent_mcp.runtime import FusionAgentRuntime
from fusion_agent_mcp.server import execute_tool


ROOT = Path(__file__).resolve().parent
SCRIPT_PATH = ROOT / "codex_build_script.py"

FEATURE_NAMES = [
    "EX01_Base_Plate",
    "EX02_Rear_Flange",
    "FL01_Flange_Top_Corners",
    "EX03_Base_Adjustment_Slots",
    "EX04_NEMA17_Through_Holes",
    "EX05_Left_Gusset",
    "MR01_Right_Gusset",
]

SKETCH_NAMES = [
    "SK01_Base_Profile",
    "SK02_Flange_Profile",
    "SK03_Adjustment_Slots",
    "SK04_NEMA17_Hole_Pattern",
    "SK05_Left_Gusset_Profile",
]

API_REFERENCES = [
    "adsk.fusion.Sketch.addCenterPointSlot",
    "adsk.fusion.ExtrudeFeatureInput.setSymmetricExtent",
    "adsk.fusion.MirrorFeatures.createInput",
    "adsk.fusion.FilletFeatureInput.edgeSetInputs",
    "adsk.fusion.FilletEdgeSetInputs.addConstantRadiusEdgeSet",
    "adsk.fusion.SketchDimensions.addDistanceDimension",
    "adsk.fusion.ConstructionPlaneInput.setByOffset",
]


def build_request(script: str) -> dict:
    queries = [
        {
            "id": "future_body",
            "entity_type": "body",
            "selector": {
                "component_path": "root",
                "name": "NEMA17_Adjustable_Bracket",
            },
            "fields": ["exists", "valid", "bounding_box_mm"],
        }
    ]
    assertions = [
        {
            "id": "body_exists",
            "query_id": "future_body",
            "field": "exists",
            "operator": "eq",
            "expected": True,
        },
        {
            "id": "body_valid",
            "query_id": "future_body",
            "field": "valid",
            "operator": "eq",
            "expected": True,
        },
    ]
    for axis, expected in enumerate((90.0, 70.0, 66.0)):
        assertions.append(
            {
                "id": f"body_size_{axis}",
                "query_id": "future_body",
                "field": f"bounding_box_mm.size_mm.{axis}",
                "operator": "approx",
                "expected": expected,
                "tolerance": 0.1,
            }
        )

    target_query_ids = ["future_body"]
    for index, name in enumerate(FEATURE_NAMES, start=1):
        query_id = f"future_feature_{index}"
        target_query_ids.append(query_id)
        queries.append(
            {
                "id": query_id,
                "entity_type": "feature",
                "selector": {"component_path": "root", "name": name},
                "fields": ["exists", "valid", "health"],
            }
        )
        assertions.extend(
            [
                {
                    "id": f"{query_id}_exists",
                    "query_id": query_id,
                    "field": "exists",
                    "operator": "eq",
                    "expected": True,
                },
                {
                    "id": f"{query_id}_valid",
                    "query_id": query_id,
                    "field": "valid",
                    "operator": "eq",
                    "expected": True,
                },
            ]
        )

    for index, name in enumerate(SKETCH_NAMES, start=1):
        query_id = f"future_sketch_{index}"
        target_query_ids.append(query_id)
        queries.append(
            {
                "id": query_id,
                "entity_type": "sketch",
                "selector": {"component_path": "root", "name": name},
                "fields": ["exists", "valid"],
            }
        )
        assertions.extend(
            [
                {
                    "id": f"{query_id}_exists",
                    "query_id": query_id,
                    "field": "exists",
                    "operator": "eq",
                    "expected": True,
                },
                {
                    "id": f"{query_id}_valid",
                    "query_id": query_id,
                    "field": "valid",
                    "operator": "eq",
                    "expected": True,
                },
            ]
        )

    return {
        "mode": "real",
        "intent": (
            "Create the complete single-body parametric NEMA17 adjustable motor "
            "bracket specified by benchmark_prompt.txt in the marked disposable document"
        ),
        "change_class": "additive",
        "script": script,
        "api_references": API_REFERENCES,
        "target_query_ids": target_query_ids,
        "verification": {
            "queries": queries,
            "assertions": assertions,
            "limit_per_query": 20,
            "include_screenshot": False,
        },
    }


async def main() -> None:
    script = SCRIPT_PATH.read_text(encoding="utf-8")
    request = validate_fast_execute_request(build_request(script))
    decision = lint_fusion_script(
        script,
        "additive",
        allowed_target_ids=set(request["target_query_ids"]),
        allowed_component_paths=set(request["target_component_paths"]),
    )
    if not decision.allowed:
        raise RuntimeError(json.dumps(decision.as_dict(), sort_keys=True))

    runtime = FusionAgentRuntime(manifest_root="manifests", outputs_root="outputs")
    started = time.perf_counter()
    try:
        result = await execute_tool(
            "fusion_agent_fast_execute",
            request,
            runtime=runtime,
        )
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        print(
            json.dumps(
                {
                    "elapsed_ms": elapsed_ms,
                    "linter": decision.as_dict(),
                    "result": result,
                },
                indent=2,
                sort_keys=True,
            )
        )
    finally:
        await runtime.close()


if __name__ == "__main__":
    asyncio.run(main())
