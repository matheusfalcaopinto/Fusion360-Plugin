"""Isolated Codex E2E benchmark driver and executable discovery."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from benchmark.models import BenchmarkCase, ExecutionObservation, ExecutionPath


ROUTE_LOCK_ENV = "FUSION_AGENT_BENCHMARK_ROUTE_LOCK"
EXECUTION_PATH_ENV = "FUSION_AGENT_EXECUTION_PATH"


class CodexExecutableError(FileNotFoundError):
    """Codex executable was absent or unsafe."""


@dataclass(slots=True)
class CodexInvocation:
    observation: ExecutionObservation
    trace: dict[str, Any]


def discover_codex_executable(env: dict[str, str] | None = None) -> Path:
    """Resolve Codex through CODEX_BIN, a real PATH entry, or LocalAppData."""

    env = dict(os.environ if env is None else env)
    candidates: list[Path] = []
    explicit = env.get("CODEX_BIN")
    if explicit:
        candidates.append(Path(explicit).expanduser())

    from_path = shutil.which("codex", path=env.get("PATH"))
    if from_path:
        candidates.append(Path(from_path))

    local_app_data = env.get("LOCALAPPDATA")
    if local_app_data:
        root = Path(local_app_data) / "OpenAI" / "Codex" / "bin"
        candidates.append(root / "codex.exe")
        if root.is_dir():
            candidates.extend(
                sorted(
                    root.glob("*/codex.exe"),
                    key=lambda path: path.stat().st_mtime,
                    reverse=True,
                )
            )

    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except (FileNotFoundError, OSError):
            continue
        if not resolved.is_file():
            continue
        if resolved.name.lower() not in {"codex", "codex.exe"}:
            continue
        # WindowsApps aliases can exist but fail when launched from the desktop
        # sandbox. Prefer the concrete LocalAppData binary when one is present.
        if "windowsapps" in {part.lower() for part in resolved.parts}:
            continue
        return resolved
    raise CodexExecutableError("unable to locate a concrete Codex executable")


class CodexE2EDriver:
    """Launch each benchmark arm in a fresh ephemeral Codex task."""

    def __init__(
        self,
        codex_bin: Path | str | None = None,
        cwd: Path | str | None = None,
        *,
        base_environment: Mapping[str, str] | None = None,
    ) -> None:
        self.base_environment = dict(
            os.environ if base_environment is None else base_environment
        )
        self.codex_bin = (
            _validate_codex_executable(Path(codex_bin))
            if codex_bin
            else discover_codex_executable(self.base_environment)
        )
        self.cwd = Path(cwd or Path.cwd()).resolve()

    def build_command(
        self,
        *,
        case: BenchmarkCase,
        execution_path: ExecutionPath,
        mode: str,
        model: str,
        reasoning_effort: str,
        run_id: str,
        trial_id: str,
    ) -> tuple[list[str], dict[str, str]]:
        """Build a fixed command without using child environment as authorization."""

        if mode not in {"mock", "real"}:
            raise ValueError("benchmark mode must be mock or real")
        prompt = _benchmark_prompt(case, execution_path, mode, run_id, trial_id)
        command = [
            str(self.codex_bin),
            "exec",
            "--ephemeral",
            "--json",
            "--sandbox",
            "read-only",
            "-m",
            model,
            "-c",
            f'model_reasoning_effort="{reasoning_effort}"',
            "-C",
            str(self.cwd),
            prompt,
        ]
        return command, dict(self.base_environment)

    async def run(
        self,
        *,
        case: BenchmarkCase,
        execution_path: ExecutionPath,
        mode: str,
        model: str,
        reasoning_effort: str,
        run_id: str,
        trial_id: str,
        timeout_seconds: float,
    ) -> CodexInvocation:
        command, child_env = self.build_command(
            case=case,
            execution_path=execution_path,
            mode=mode,
            model=model,
            reasoning_effort=reasoning_effort,
            run_id=run_id,
            trial_id=trial_id,
        )
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        started = time.perf_counter()
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=self.cwd,
            env=child_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=creationflags,
        )
        timed_out = False
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout_seconds
            )
        except TimeoutError:
            timed_out = True
            process.kill()
            stdout, stderr = await process.communicate()
        duration_ms = (time.perf_counter() - started) * 1000
        stdout_text = stdout.decode("utf-8", errors="replace")
        events = _jsonl_events(stdout_text)
        observation_payload = _find_observation(events)
        measured_tokens = _token_count(events)
        if observation_payload is None:
            observation_payload = {
                "status": "timeout"
                if timed_out
                else ("completed" if process.returncode == 0 else "failed"),
                "execution_success": process.returncode == 0 and not timed_out,
                "duration_ms": duration_ms,
                "execution_ms": duration_ms,
                "observation": {},
            }
        else:
            observation_payload = {
                **observation_payload,
                "duration_ms": duration_ms,
                "execution_ms": duration_ms,
            }
        if measured_tokens is not None:
            observation_payload["token_count"] = measured_tokens
        observation = ExecutionObservation.model_validate(observation_payload)
        trace = {
            "prompt_sha256": hashlib.sha256(command[-1].encode("utf-8")).hexdigest(),
            "returncode": process.returncode,
            "timed_out": timed_out,
            "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
            "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
            "stdout_bytes": len(stdout),
            "stderr_bytes": len(stderr),
            "event_count": len(events),
            "token_count": measured_tokens,
        }
        return CodexInvocation(observation=observation, trace=trace)


def _benchmark_prompt(
    case: BenchmarkCase, path: ExecutionPath, mode: str, run_id: str, trial_id: str
) -> str:
    return (
        "Run exactly one Fusion Agent benchmark case. "
        f"The request-local benchmark contract requires execution_path={path} and mode={mode}. "
        "Do not save, sync, export, or change routes. Use only the benchmark fixture selected by the server. "
        f"run_id={run_id}; trial_id={trial_id}; case_id={case.id}. "
        f"Task: {case.prompt} "
        "Finish with a single JSON object matching the benchmark execution observation contract."
    )


def _validate_codex_executable(path: Path) -> Path:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise CodexExecutableError("Codex executable does not exist") from exc
    if not resolved.is_file() or resolved.name.lower() not in {"codex", "codex.exe"}:
        raise CodexExecutableError("invalid Codex executable")
    if "windowsapps" in {part.lower() for part in resolved.parts}:
        raise CodexExecutableError("WindowsApps Codex alias is not allowed")
    return resolved


def _jsonl_events(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def _find_observation(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    required = {"status", "execution_success"}
    for event in reversed(events):
        candidates: list[Any] = [
            event.get("benchmark_result"),
            event.get("result"),
            event,
        ]
        item = event.get("item")
        if isinstance(item, dict):
            candidates.extend(
                [item.get("benchmark_result"), item.get("result"), item.get("text")]
            )
        for candidate in candidates:
            if isinstance(candidate, str):
                try:
                    candidate = json.loads(candidate)
                except json.JSONDecodeError:
                    continue
            if isinstance(candidate, dict) and required.issubset(candidate):
                return candidate
    return None


def _token_count(events: list[dict[str, Any]]) -> int | None:
    """Extract the largest concrete total-token counter from Codex JSON events."""

    values: list[int] = []

    def visit(value: Any, key: str = "") -> None:
        normalized = key.lower().replace("-", "_")
        if isinstance(value, dict):
            for child_key, child_value in value.items():
                visit(child_value, str(child_key))
        elif isinstance(value, list):
            for child in value:
                visit(child, key)
        elif isinstance(value, int) and not isinstance(value, bool):
            if (
                normalized in {"total_tokens", "total_token_count", "tokens_used"}
                and value >= 0
            ):
                values.append(value)

    for event in events:
        visit(event)
    return max(values) if values else None
