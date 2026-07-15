"""Session memory writer."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from memory.schemas import MemoryRecord, MemoryScope, MemorySource, MemoryType, TrustLevel
from memory.store import MemoryStore
from memory.taint import inspect_memory_content
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
        verification_json = verification.model_dump_json(indent=2)
        if inspect_memory_content(verification_json):
            verification_json = json.dumps(
                {
                    "redacted": True,
                    "reason": "tainted_verification_payload",
                    "sha256": hashlib.sha256(verification_json.encode("utf-8")).hexdigest(),
                    "passed": verification.passed,
                },
                indent=2,
            )
        lines = [
            f"# Session {session_id}",
            "",
            f"Status: {status}",
            f"Prompt SHA-256: {hashlib.sha256(prompt.encode('utf-8')).hexdigest()}",
            "",
            "## Verification",
            verification_json,
        ]
        path = self.store.project_root(project) / f"sessions/{session_id}/memory_summary.md"
        record = MemoryRecord(
            id=f"project:{project}:session:{session_id}",
            scope=MemoryScope.PROJECT,
            project=project,
            type=MemoryType.SESSION_SUMMARY,
            summary=f"Session {session_id}",
            content="\n".join(lines),
            content_path=path,
            tags=["session", status],
            source=MemorySource.TOOL,
            provenance=[f"session:{session_id}", "verifier:result"],
            trust_level=TrustLevel.TRUSTED,
        )
        path = self.store.write_record(record)
        updates = [str(path)]
        if not verification.passed and verification.issues:
            issue = verification.issues[0]
            failure_path = self.store.project_root(project) / "KNOWN_FAILURES.md"
            issue_message = _safe_tool_text(issue.message)
            issue_details = _safe_tool_text(json.dumps(issue.details, sort_keys=True, default=str))
            entry = (
                f"\n\n## Failure: {issue.code}\n\n"
                f"Last seen: {datetime.now(timezone.utc).date().isoformat()}\n\n"
                f"Prompt SHA-256: {hashlib.sha256(prompt.encode('utf-8')).hexdigest()}\n\n"
                f"Symptom: {issue_message}\n\n"
                f"Details: `{issue_details}`\n"
            )
            existing = failure_path.read_text(encoding="utf-8") if failure_path.exists() else "# Known Failures\n"
            failure_record = MemoryRecord(
                id=f"project:{project}:known-failures",
                scope=MemoryScope.PROJECT,
                project=project,
                type=MemoryType.FAILURE_PATTERN,
                summary="Known Failures",
                content=existing + entry,
                content_path=failure_path,
                tags=["failure", issue.code.lower()],
                source=MemorySource.TOOL,
                provenance=[f"session:{session_id}", "verifier:issue"],
                trust_level=TrustLevel.TRUSTED,
            )
            self.store.write_record(failure_record)
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
            source=MemorySource.TOOL,
            provenance=["memory_writer:explicit_failure"],
            trust_level=TrustLevel.TRUSTED,
        )
        return self.store.write_record(record)


def _safe_tool_text(value: str) -> str:
    flags = inspect_memory_content(value)
    if not flags:
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"[redacted tainted tool content; flags={','.join(flags)}; sha256={digest}]"
