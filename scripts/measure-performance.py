"""Measure the reproducible no-I/O 0.4.1 release workload."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import math
import os
import re
import sys
import time
from pathlib import Path
from typing import Any


SCHEMA = "fusion_agent.performance_evidence.v1"
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
WORKLOAD_ID = "fusion_agent.mcp_surface_no_io.v1"


class PerformanceMeasurementError(RuntimeError):
    """The requested source or measurement is invalid."""


def _p95(samples: list[float]) -> float:
    if not samples or any(
        isinstance(value, bool) or not math.isfinite(value) or value < 0
        for value in samples
    ):
        raise PerformanceMeasurementError(
            "latency samples must be finite and non-negative"
        )
    ordered = sorted(samples)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def _workload_digest(*, iterations: int, warmup: int) -> str:
    contract = {
        "id": WORKLOAD_ID,
        "iterations": iterations,
        "operations": [
            "list_tool_definitions:normal",
            "consume:name,inputSchema,outputSchema",
        ],
        "warmup": warmup,
    }
    encoded = json.dumps(contract, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _peak_rss_bytes() -> int:
    if os.name == "nt":
        return _windows_peak_rss_bytes()
    try:
        resource_module: Any = importlib.import_module("resource")
        peak = int(resource_module.getrusage(resource_module.RUSAGE_SELF).ru_maxrss)
    except (ImportError, OSError, ValueError) as exc:
        raise PerformanceMeasurementError("peak RSS is unavailable") from exc
    # Linux and the BSDs report KiB; macOS reports bytes.
    return peak if sys.platform == "darwin" else peak * 1024


def _windows_peak_rss_bytes() -> int:
    import ctypes
    from ctypes import wintypes

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    get_process_memory_info = psapi.GetProcessMemoryInfo
    get_process_memory_info.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ProcessMemoryCounters),
        wintypes.DWORD,
    ]
    get_process_memory_info.restype = wintypes.BOOL

    counters = ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    process = kernel32.GetCurrentProcess()
    if not get_process_memory_info(process, ctypes.byref(counters), counters.cb):
        error = ctypes.get_last_error()
        raise PerformanceMeasurementError(
            f"GetProcessMemoryInfo failed with Windows error {error}"
        )
    return int(counters.PeakWorkingSetSize)


def measure(
    source_root: Path,
    *,
    git_commit: str,
    iterations: int,
    warmup: int,
) -> dict[str, Any]:
    root = source_root.resolve()
    if not COMMIT_PATTERN.fullmatch(git_commit):
        raise PerformanceMeasurementError("git commit must be a lowercase 40-byte SHA")
    if iterations < 100 or warmup < 1:
        raise PerformanceMeasurementError(
            "measurement requires at least 100 samples and one warmup"
        )
    packages = root / "harness" / "packages"
    apps = root / "harness" / "apps"
    if not packages.is_dir() or not apps.is_dir():
        raise PerformanceMeasurementError("source root does not contain the harness")
    sys.path[:0] = [str(packages), str(apps)]

    from fusion_agent_mcp import server

    def workload() -> int:
        definitions = server.list_tool_definitions("normal")
        if len(definitions) != 12:
            raise PerformanceMeasurementError(
                f"normal profile must expose exactly 12 tools, found {len(definitions)}"
            )
        return sum(
            len(item.name)
            + len(item.inputSchema)
            + (len(item.outputSchema) if item.outputSchema is not None else 0)
            for item in definitions
        )

    checksum = 0
    for _ in range(warmup):
        checksum ^= workload()
    samples: list[float] = []
    for _ in range(iterations):
        started = time.perf_counter_ns()
        checksum ^= workload()
        samples.append((time.perf_counter_ns() - started) / 1_000_000.0)
    p95 = _p95(samples)
    peak_rss = _peak_rss_bytes()
    if p95 <= 0 or peak_rss <= 0:
        raise PerformanceMeasurementError("measurement produced non-positive metrics")
    return {
        "schema_version": SCHEMA,
        "git_commit": git_commit,
        "workload_digest": _workload_digest(iterations=iterations, warmup=warmup),
        "workload_id": WORKLOAD_ID,
        "sample_count": iterations,
        "warmup_count": warmup,
        "no_io_p95_ms": p95,
        "peak_rss_bytes": peak_rss,
        "checksum": checksum,
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "platform": sys.platform,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--git-commit", required=True)
    parser.add_argument("--iterations", type=int, default=2000)
    parser.add_argument("--warmup", type=int, default=200)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        evidence = measure(
            args.source_root,
            git_commit=args.git_commit,
            iterations=args.iterations,
            warmup=args.warmup,
        )
    except PerformanceMeasurementError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 2
    rendered = json.dumps(evidence, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8", newline="\n")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
