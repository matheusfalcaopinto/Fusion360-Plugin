"""Memory safety and relevance gate."""

from __future__ import annotations

from datetime import datetime, timezone

from memory.schemas import MemoryRecord, MemorySource, TrustLevel
from memory.taint import inspect_memory_content


UNSAFE_TERMS = {"raw mcp", "run arbitrary code"}


class MemoryGate:
    """Filter retrieved memory before it can influence planning."""

    def __init__(self, min_relevance: float = 0.1, max_chars: int = 4000) -> None:
        self.min_relevance = min_relevance
        self.max_chars = max_chars

    def filter(
        self, records: list[MemoryRecord], current_request: str
    ) -> list[MemoryRecord]:
        """Return only allowed memory records."""

        allowed: list[MemoryRecord] = []
        request_lower = current_request.lower()
        for record in records:
            content_lower = record.content.lower()
            taint_flags = set(record.taint_flags) | set(
                inspect_memory_content(record.content)
            )
            record.taint_flags = sorted(taint_flags)
            if record.relevance_score < self.min_relevance:
                record.safety_status = "blocked_low_relevance"
                continue
            if len(record.content) > self.max_chars:
                record.safety_status = "blocked_oversized"
                continue
            if record.expires_at and record.expires_at <= datetime.now(timezone.utc):
                record.safety_status = "blocked_expired"
                continue
            if taint_flags & {
                "instruction_injection",
                "tool_directive",
                "possible_secret",
                "binary_content",
                "content_hash_mismatch",
                "invalid_metadata",
            }:
                record.safety_status = "blocked_tainted"
                continue
            if any(term in content_lower for term in UNSAFE_TERMS):
                record.safety_status = "blocked_unsafe_content"
                continue
            if "millimeter" in request_lower or " mm" in request_lower:
                if "prefer inches" in content_lower or "use inches" in content_lower:
                    record.contradiction_status = "likely"
                    record.safety_status = "blocked_contradiction"
                    continue
            if record.trust_level in {
                TrustLevel.UNTRUSTED,
                TrustLevel.LEGACY_UNVERIFIED,
            }:
                record.safety_status = "allowed_untrusted_data"
            else:
                record.safety_status = "allowed"
            if record.source == MemorySource.WEB:
                metadata_only = record.model_copy(deep=True)
                metadata_only.content = (
                    "Remote documentation body withheld from execution context. "
                    f"summary={record.summary!r}; source_url={record.source_url!r}; "
                    f"retrieved_at={record.source_retrieved_at}; "
                    f"content_sha256={record.source_content_sha256 or record.content_sha256}"
                )
                metadata_only.safety_status = "allowed_untrusted_metadata_only"
                allowed.append(metadata_only)
            else:
                allowed.append(record)
        return allowed
