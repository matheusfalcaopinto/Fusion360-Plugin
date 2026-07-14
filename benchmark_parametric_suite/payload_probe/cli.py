"""Offline-only CLI for validating payload-probe calibration."""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path
from typing import Callable

from .calibration import PayloadScriptCalibrator
from .loader import load_probe_matrix
from .models import CanaryContract


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MATRIX = ROOT / "benchmark_parametric_suite" / "payload_probe_matrix.json"


def _production_protector() -> Callable[[str], str]:
    packages = ROOT / "harness" / "packages"
    if packages.exists():
        sys.path.insert(0, str(packages))
        loaded = sys.modules.get("fusion_mcp_adapter")
        loaded_file = Path(str(getattr(loaded, "__file__", ""))).resolve() if loaded else None
        if loaded_file is not None and packages not in loaded_file.parents:
            for name in tuple(sys.modules):
                if name == "fusion_mcp_adapter" or name.startswith("fusion_mcp_adapter."):
                    sys.modules.pop(name, None)
        importlib.invalidate_caches()
    try:
        from fusion_mcp_adapter.execute_guard import normalize_execute_script
    except ModuleNotFoundError as exc:
        raise RuntimeError("canonical fusion_mcp_adapter.execute_guard is unavailable") from exc
    return normalize_execute_script


def validate(matrix_path: str | Path) -> dict[str, object]:
    """Load and exactly calibrate every matrix point without dispatching."""

    matrix = load_probe_matrix(matrix_path)
    calibrator = PayloadScriptCalibrator(_production_protector())
    canaries = CanaryContract.for_trial(
        run_id="offline_validation_0000000000000000",
        trial_id="pp_offline_0000000000000000",
    )
    scripts = [
        calibrator.calibrate(
            target_protected_bytes=target.target_protected_bytes,
            canaries=canaries,
        )
        for target in matrix.targets
    ]
    topologies = {script.ast_topology_sha256 for script in scripts}
    if len(topologies) != 1:
        raise RuntimeError("calibrated matrix did not retain one AST topology")
    return {
        "ok": True,
        "mode": "offline_validate_only",
        "dispatch_count": 0,
        "experiment_id": matrix.experiment_id,
        "target_count": len(scripts),
        "target_protected_bytes": [script.protected_payload_bytes for script in scripts],
        "ast_topology_sha256": next(iter(topologies)),
        "maximum_target_bytes": matrix.maximum_target_bytes,
        "historical_observations_used_as_expectations": False,
        "historical_observations_used_as_oracles": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate the Fusion executor payload matrix offline; this CLI has no real mode."
    )
    parser.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX)
    args = parser.parse_args(argv)
    payload = validate(args.matrix)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
