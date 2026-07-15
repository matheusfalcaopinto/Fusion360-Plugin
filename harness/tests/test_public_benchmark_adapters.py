from __future__ import annotations

from pathlib import Path

import pytest

import cli.main as cli_main
from benchmark.adapters import (
    PUBLIC_ADAPTER_TYPES,
    AdapterPrerequisites,
    FaustAdapter,
    build_public_adapter_registry,
)
from benchmark.public import (
    AdapterExecution,
    AdapterPreflight,
    NormalizedPublicMetrics,
    PublicBenchmarkConfig,
    PublicBenchmarkRunner,
    load_public_manifest,
)
from fusion_agent_assets import asset_root


MANIFEST = asset_root("benchmarks") / "public_competitors_v1.json"


class RecordingDriver:
    def __init__(self, revision: str) -> None:
        self.revision = revision
        self.preflight_calls = 0
        self.execute_calls = 0
        self.contexts = []

    async def preflight(self, context, config) -> AdapterPreflight:  # noqa: ANN001
        self.preflight_calls += 1
        self.contexts.append(context)
        return AdapterPreflight(
            ready=True,
            observed_revision=self.revision,
            environment={"driver": "explicit_test_injection"},
        )

    async def execute(self, context, task, config) -> AdapterExecution:  # noqa: ANN001
        self.execute_calls += 1
        self.contexts.append(context)
        return AdapterExecution(
            state="completed",
            metrics=NormalizedPublicMetrics(
                task_success=True,
                oracle_passed=True,
                contract_coverage=1.0,
                mutation_dispatch_count=1,
                replay_count=0,
                install_status="pinned_isolated",
            ),
            evidence={"driver": "trusted_injection"},
        )


def _faust_subject():
    manifest, _ = load_public_manifest(MANIFEST)
    return next(subject for subject in manifest.subjects if subject.adapter_id == "faust_fusion360_mcp")


def _approved(*, license_accepted: bool = True) -> AdapterPrerequisites:
    return AdapterPrerequisites(
        subject_id="faust_fusion360_mcp",
        license_id="MIT",
        license_accepted=license_accepted,
        entitlement_confirmed=True,
        isolated_installation_confirmed=True,
        normal_profile_equivalent_confirmed=True,
    )


def test_fixed_registry_contains_all_five_pinned_adapters() -> None:
    registry = build_public_adapter_registry()
    assert set(registry) == {
        "fusion_agent_codex",
        "autodesk_fusion_official",
        "faust_fusion360_mcp",
        "frank_autodesk_fusion_mcp",
        "ndoo_fusion360_bridge",
    }
    assert set(registry) == set(PUBLIC_ADAPTER_TYPES)
    assert all(adapter.definition.pin_value for adapter in registry.values())


@pytest.mark.asyncio
async def test_missing_prerequisites_never_touch_injected_driver() -> None:
    driver = RecordingDriver(FaustAdapter.definition.pin_value)
    runner = PublicBenchmarkRunner(
        build_public_adapter_registry(drivers={"faust_fusion360_mcp": driver})
    )
    report = await runner.run(
        MANIFEST,
        config=PublicBenchmarkConfig(
            mode="mock",
            include_faults=False,
            subject_ids=["faust_fusion360_mcp"],
        ),
    )
    assert driver.preflight_calls == 0
    assert driver.execute_calls == 0
    assert {result.reason for result in report.results} == {"prerequisites_not_injected"}
    assert report.summary["scoreable"] is False


@pytest.mark.asyncio
async def test_unaccepted_license_never_touches_driver() -> None:
    driver = RecordingDriver(FaustAdapter.definition.pin_value)
    adapter = FaustAdapter(driver=driver, prerequisites=_approved(license_accepted=False))
    preflight = await adapter.preflight(_faust_subject(), PublicBenchmarkConfig(mode="mock"))
    assert preflight.ready is False
    assert preflight.reason == "license_not_accepted"
    assert driver.preflight_calls == 0


@pytest.mark.asyncio
async def test_direct_real_preflight_requires_both_safety_confirmations() -> None:
    driver = RecordingDriver(FaustAdapter.definition.pin_value)
    adapter = FaustAdapter(driver=driver, prerequisites=_approved())
    preflight = await adapter.preflight(_faust_subject(), PublicBenchmarkConfig(mode="real"))
    assert preflight.ready is False
    assert preflight.reason == "real_execution_not_confirmed"
    assert driver.preflight_calls == 0


@pytest.mark.asyncio
async def test_exact_git_pin_is_enforced_before_task_execution() -> None:
    driver = RecordingDriver("wrong-revision")
    runner = PublicBenchmarkRunner(
        build_public_adapter_registry(
            drivers={"faust_fusion360_mcp": driver},
            prerequisites={"faust_fusion360_mcp": _approved()},
        )
    )
    report = await runner.run(
        MANIFEST,
        config=PublicBenchmarkConfig(
            mode="mock",
            include_faults=False,
            subject_ids=["faust_fusion360_mcp"],
        ),
    )
    assert driver.preflight_calls == 1
    assert driver.execute_calls == 0
    assert all(result.state == "not_run" for result in report.results)
    assert all(result.reason and result.reason.startswith("revision_mismatch:") for result in report.results)


@pytest.mark.asyncio
async def test_manifest_identity_cannot_substitute_adapter_source() -> None:
    driver = RecordingDriver(FaustAdapter.definition.pin_value)
    subject = _faust_subject().model_copy(
        update={"source_url": "https://example.invalid/substituted-source"}
    )
    adapter = FaustAdapter(driver=driver, prerequisites=_approved())
    preflight = await adapter.preflight(subject, PublicBenchmarkConfig(mode="mock"))
    assert preflight.ready is False
    assert preflight.reason == "subject_policy_mismatch:source_url"
    assert driver.preflight_calls == 0


@pytest.mark.asyncio
async def test_approved_driver_receives_non_executable_fairness_context() -> None:
    driver = RecordingDriver(FaustAdapter.definition.pin_value)
    runner = PublicBenchmarkRunner(
        build_public_adapter_registry(
            drivers={"faust_fusion360_mcp": driver},
            prerequisites={"faust_fusion360_mcp": _approved()},
        )
    )
    report = await runner.run(
        MANIFEST,
        config=PublicBenchmarkConfig(
            mode="mock",
            include_faults=False,
            subject_ids=["faust_fusion360_mcp"],
        ),
    )
    assert report.summary["states"] == {"completed": 6, "failed": 0, "not_run": 0}
    assert driver.preflight_calls == 1
    assert driver.execute_calls == 6
    assert all(context.arbitrary_code_allowed is False for context in driver.contexts)
    assert all(context.execution_profile == "normal_equivalent" for context in driver.contexts)


@pytest.mark.asyncio
async def test_execute_cannot_bypass_preflight() -> None:
    driver = RecordingDriver(FaustAdapter.definition.pin_value)
    adapter = FaustAdapter(driver=driver, prerequisites=_approved())
    manifest, _ = load_public_manifest(MANIFEST)
    task_report = await PublicBenchmarkRunner().run(
        MANIFEST,
        config=PublicBenchmarkConfig(
            mode="mock",
            include_faults=False,
            subject_ids=["faust_fusion360_mcp"],
        ),
    )
    execution = await adapter.execute(
        _faust_subject(),
        task_report.results[0].task,
        PublicBenchmarkConfig(mode="mock"),
    )
    assert manifest.schema_version == "public_benchmark.v1"
    assert execution.state == "not_run"
    assert execution.reason == "adapter_preflight_not_authorized"
    assert driver.execute_calls == 0


def test_unknown_driver_injection_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown public benchmark adapter injection"):
        build_public_adapter_registry(drivers={"manifest_supplied_plugin": object()})  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_cli_registers_fixed_policy_adapters(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    registry = build_public_adapter_registry()
    captured = {}

    class StopAfterConstruction(Exception):
        pass

    class CapturingRunner:
        def __init__(self, adapters) -> None:  # noqa: ANN001
            captured["adapters"] = adapters

        async def run(self, manifest, *, config) -> None:  # noqa: ANN001
            raise StopAfterConstruction

    def capture_registry(**kwargs):  # noqa: ANN003
        captured["drivers"] = kwargs["drivers"]
        captured["prerequisites"] = kwargs["prerequisites"]
        return registry

    monkeypatch.setattr(cli_main, "build_public_adapter_registry", capture_registry)
    monkeypatch.setattr(cli_main, "PublicBenchmarkRunner", CapturingRunner)
    with pytest.raises(StopAfterConstruction):
        await cli_main._benchmark_public(
            str(MANIFEST),
            str(tmp_path),
            "mock",
            False,
            False,
            False,
        )
    assert captured["adapters"] is registry
    assert set(captured["drivers"]) == {"fusion_agent_codex"}
    assert set(captured["prerequisites"]) == {"fusion_agent_codex"}
