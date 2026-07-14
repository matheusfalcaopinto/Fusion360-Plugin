"""Safe CLI: schema validation or deterministic mock execution only."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Sequence

from .loader import load_causal_suite, suite_fingerprint
from .models import CausalRunConfig, ExecutionObservation, OracleObservation, TrialContext
from .runner import CausalBenchmarkRunner, LAYERS, ROUTE_LOCK_ENV


class DeterministicMockExecutor:
    """Contract simulator. It never imports an artifact or calls an external process."""

    async def execute(self, context: TrialContext) -> ExecutionObservation:
        material = (
            f"{context.seed}|{context.case_id}|{context.layer}|{context.arm_id}|"
            f"{context.repetition}|{int(context.warmup)}"
        ).encode("utf-8")
        bucket = int.from_bytes(hashlib.sha256(material).digest()[:2], "big")
        duration = 25.0 + float(bucket % 100) / 10.0
        return ExecutionObservation(
            status="mock_succeeded",
            execution_success=True,
            duration_ms=duration,
            planning_ms=duration * 0.35 if context.layer != "transport_replay" else 0.0,
            execution_ms=duration * 0.55,
            verification_ms=duration * 0.10,
            call_count=2 if context.layer == "native_e2e" else 1,
            script_count=1,
            bytes_transferred=sum(len(path) + len(digest) for path, digest in context.artifacts.items()),
            mutation_dispatch_count=1 if context.risk != "read_only" else 0,
            observed_runner_id=context.runner_id,
            observed_route_lock=os.environ.get(ROUTE_LOCK_ENV),
            consumed_artifacts=dict(context.artifacts),
            trace={"source": "deterministic_mock", "external_processes": 0},
        )


class PassingMockOracle:
    async def observe(self, context: TrialContext) -> OracleObservation:
        return OracleObservation(
            passed=True,
            checks={"fixture_id_present": bool(context.fixture_id)},
            metrics={"mock": True},
            message="mock observer; no CAD correctness claim",
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate or mock a Fusion causal benchmark suite")
    parser.add_argument("--suite", required=True, type=Path)
    parser.add_argument("--mode", choices=("validate", "mock"), default="validate")
    parser.add_argument("--output", type=Path, default=Path("causal_outputs"))
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--run-id")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    suite = load_causal_suite(args.suite)
    if args.mode == "validate":
        print(
            json.dumps(
                {
                    "valid": True,
                    "suite_id": suite.suite_id,
                    "fingerprint": suite_fingerprint(suite),
                    "case_count": len(suite.cases),
                    "layers": list(LAYERS),
                    "external_processes_started": 0,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    mock = DeterministicMockExecutor()
    oracle = PassingMockOracle()
    runner = CausalBenchmarkRunner(
        output_dir=args.output,
        executors={layer: mock for layer in LAYERS},
        oracles={case.oracle_id: oracle for case in suite.cases},
        environment={"mode": "mock", "external_processes_started": 0},
    )
    result = asyncio.run(
        runner.run_suite(
            args.suite,
            config=CausalRunConfig(
                repetitions=args.repetitions,
                warmups=args.warmups,
                seed=args.seed,
            ),
            run_id=args.run_id,
        )
    )
    print(
        json.dumps(
            {
                "run_id": result.report.run_id,
                "status": result.report.status,
                "report": str(result.report_path.resolve()),
                "external_processes_started": 0,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
