from __future__ import annotations

from pathlib import Path

import pytest

from agent_core.planner import PlanningRequest, RuleBasedPlanner
from fusion_agent_assets import asset_root


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prompt",
    [
        "Build a spacer assembly with two plates and spacers",
        "Build a simple hinge assembly with a revolute joint",
    ],
)
async def test_professional_planner_never_injects_real_host_output(
    prompt: str,
) -> None:
    legacy = await RuleBasedPlanner().plan(PlanningRequest(user_prompt=prompt))

    assert legacy.outputs == []
    assert all(
        feature.type not in {"export", "capture_viewport"}
        for component in legacy.components
        for feature in component.features
    )
    assert all(
        acceptance.type not in {"screenshots_exist", "export_exists"}
        for acceptance in legacy.acceptance_tests
    )


def test_bundled_instructions_and_suite_do_not_promise_real_output() -> None:
    package_root = asset_root("").resolve()
    repository_root = Path(__file__).resolve().parents[2]
    instruction_paths = [
        repository_root / "skills" / "fusion-cad-harness" / "SKILL.md",
        package_root / "prompts" / "planner_prompt.md",
        package_root / "prompts" / "executor_prompt.md",
        package_root / "prompts" / "verifier_prompt.md",
        package_root / "prompts" / "system_prompt_fusion_agent.md",
        package_root / "prompts" / "codex_bootstrap_prompt.md",
        package_root / "docs" / "policies" / "review_gates.md",
        package_root / "docs" / "policies" / "assembly_policy.md",
        package_root / "docs" / "policies" / "acceptance_metrics.md",
        package_root / "skills" / "fusion_mechanical_pro" / "SKILL.md",
        package_root / "skills" / "validate_export" / "SKILL.md",
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in instruction_paths)
    for stale_instruction in (
        "Capture requested screenshots",
        "Capture required viewport screenshots",
        "output contracts for viewport captures",
        "metadata, screenshots, physical",
        "screenshots_exist checks for professional",
    ):
        assert stale_instruction not in combined
    assert "HOST_OUTPUT_DISABLED" in combined
    assert "deny_io" in combined

    suite = (package_root / "benchmarks" / "benchmark_suite_v2.json").read_text(
        encoding="utf-8"
    )
    assert "screenshot_verified" not in suite
    assert "capture_screenshot" not in suite
