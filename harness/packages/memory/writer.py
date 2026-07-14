"""Session memory writer."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from memory.schemas import MemoryRecord, MemoryScope, MemoryType
from memory.store import MemoryStore
from verifier.result_models import VerificationResult


class MemoryWriter:
    """Write session summaries and reusable failure memories."""

    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def write_session_memory(
        self,
        *,
        project: str,
        session_id: str,
        prompt: str,
        verification: VerificationResult,
    ) -> list[str]:
        """Persist a concise memory summary for a session."""

        status = "success" if verification.passed else "failed"
        lines = [
            f"# Session {session_id}",
            "",
            f"Status: {status}",
            f"Prompt: {prompt}",
            "",
            "## Verification",
            verification.model_dump_json(indent=2),
        ]
        path = self.store.write_project_markdown(project, f"sessions/{session_id}/memory_summary.md", "\n".join(lines))
        updates = [str(path)]
        if not verification.passed and verification.issues:
            issue = verification.issues[0]
            failure_path = self.store.project_root(project) / "KNOWN_FAILURES.md"
            entry = (
                f"\n\n## Failure: {issue.code}\n\n"
                f"Last seen: {datetime.now(timezone.utc).date().isoformat()}\n\n"
                f"Prompt: {prompt}\n\n"
                f"Symptom: {issue.message}\n\n"
                f"Details: `{issue.details}`\n"
            )
            existing = failure_path.read_text(encoding="utf-8") if failure_path.exists() else "# Known Failures\n"
            failure_path.write_text(existing + entry, encoding="utf-8")
            updates.append(str(failure_path))
        return updates

    def write_failure_record(self, project: str, code: str, content: str) -> Path:
        """Write one explicit failure memory record."""

        path = self.store.project_root(project) / "KNOWN_FAILURES.md"
        record = MemoryRecord(
            id=f"project:{project}:{code.lower()}",
            scope=MemoryScope.PROJECT,
            project=project,
            type=MemoryType.FAILURE_PATTERN,
            summary=f"Failure {code}",
            content=content,
            content_path=path,
            tags=[code.lower()],
        )
        return self.store.write_record(record)
