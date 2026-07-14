"""Strict Fusion Agent A/B benchmark package."""

from benchmark.artifacts import BenchmarkArtifactStore
from benchmark.codex_driver import (
    EXECUTION_PATH_ENV,
    ROUTE_LOCK_ENV,
    CodexE2EDriver,
    discover_codex_executable,
)
from benchmark.fixtures import FIXTURE_REGISTRY, SCRIPT_REGISTRY
from benchmark.loader import BenchmarkSuiteError, load_benchmark_cases, load_benchmark_suite
from benchmark.models import (
    BenchmarkCase,
    BenchmarkReport,
    BenchmarkResult,
    BenchmarkRun,
    BenchmarkRunConfig,
    BenchmarkSuite,
    BenchmarkTrial,
    ExecutionObservation,
)
from benchmark.registry import ORACLE_REGISTRY
from benchmark.runner import (
    BenchmarkExecutionError,
    BenchmarkRunner,
    IndependentOracleObserver,
    InternalRouteExecutor,
    TrialContext,
    enforce_route_lock,
)

__all__ = [
    "BenchmarkArtifactStore",
    "BenchmarkCase",
    "BenchmarkExecutionError",
    "BenchmarkReport",
    "BenchmarkResult",
    "BenchmarkRun",
    "BenchmarkRunConfig",
    "BenchmarkRunner",
    "BenchmarkSuite",
    "BenchmarkSuiteError",
    "BenchmarkTrial",
    "CodexE2EDriver",
    "EXECUTION_PATH_ENV",
    "ExecutionObservation",
    "FIXTURE_REGISTRY",
    "InternalRouteExecutor",
    "IndependentOracleObserver",
    "ORACLE_REGISTRY",
    "ROUTE_LOCK_ENV",
    "SCRIPT_REGISTRY",
    "TrialContext",
    "discover_codex_executable",
    "enforce_route_lock",
    "load_benchmark_cases",
    "load_benchmark_suite",
]
