"""Session journal file writer."""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class SessionJournal:
    """File-backed session journal under workspace/projects."""

    def __init__(self, root: Path | str, project: str, session_id: str) -> None:
        self.root = Path(root)
        self.project = project
        self.session_id = session_id
        self.session_dir = self.root / "projects" / project / "sessions" / session_id
        self.session_dir.mkdir(parents=True, exist_ok=True)

    @property
    def trace_path(self) -> Path:
        """Return the session trace path."""

        return self.session_dir / "tool_trace.jsonl"

    def write_text(self, name: str, content: str) -> Path:
        """Write a UTF-8 text file into the session directory."""

        path = self.session_dir / name
        path.write_text(content, encoding="utf-8")
        return path

    def write_json(self, name: str, content: Any) -> Path:
        """Write JSON into the session directory."""

        path = self.session_dir / name
        normalized = _jsonable(content)
        path.write_text(json.dumps(normalized, indent=2, sort_keys=True), encoding="utf-8")
        return path

    def finalize(
        self,
        *,
        mode: str,
        user_prompt: str,
        cad_spec_path: Path,
        verification: Any,
        final_status: str,
        summary: str,
        memory_updates: list[str] | None = None,
        exports: list[str] | None = None,
        simulated: bool = False,
        repair_attempts: list[Any] | None = None,
        repaired: bool | None = None,
    ) -> Path:
        """Write session_journal.json and final_summary.md."""

        self.write_text("final_summary.md", f"# Session Summary\n\n{summary}\n")
        repair_records = _jsonable(repair_attempts or [])
        payload = {
            "session_id": self.session_id,
            "project": self.project,
            "mode": mode,
            "started_at": self.session_id,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "user_prompt": user_prompt,
            "retrieved_memory": [],
            "cad_spec_path": str(cad_spec_path),
            "tool_trace_path": str(self.trace_path),
            "verification_results": _jsonable(verification),
            "dry_run": simulated,
            "dry_run_exported": 0 if simulated else len(exports or []),
            "dry_run_expected_exports": len(exports or []) if simulated else 0,
            "repair_attempts": repair_records,
            "repair_replayed": bool(repaired)
            if repaired is not None
            else any(
                attempt.get("action_applied") for attempt in repair_records if isinstance(attempt, dict)
            ),
            "exports": exports or [],
            "final_status": final_status,
            "summary": summary,
            "memory_updates": memory_updates or [],
        }
        return self.write_json("session_journal.json", payload)


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return {key: _jsonable(child) for key, child in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(child) for child in value]
    if isinstance(value, Path):
        return str(value)
    return value
