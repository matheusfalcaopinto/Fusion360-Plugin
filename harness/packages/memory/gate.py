"""Memory safety and relevance gate."""

from __future__ import annotations

from memory.schemas import MemoryRecord


UNSAFE_TERMS = {"delete", "destructive", "raw mcp", "ignore instructions", "run arbitrary code"}


class MemoryGate:
    """Filter retrieved memory before it can influence planning."""

    def __init__(self, min_relevance: float = 0.1, max_chars: int = 4000) -> None:
        self.min_relevance = min_relevance
        self.max_chars = max_chars

    def filter(self, records: list[MemoryRecord], current_request: str) -> list[MemoryRecord]:
        """Return only allowed memory records."""

        allowed: list[MemoryRecord] = []
        request_lower = current_request.lower()
        for record in records:
            content_lower = record.content.lower()
            if record.relevance_score < self.min_relevance:
                record.safety_status = "blocked"
                continue
            if len(record.content) > self.max_chars:
                record.safety_status = "blocked"
                continue
            if any(term in content_lower for term in UNSAFE_TERMS):
                record.safety_status = "blocked"
                continue
            if "millimeter" in request_lower or " mm" in request_lower:
                if "prefer inches" in content_lower or "use inches" in content_lower:
                    record.contradiction_status = "likely"
                    record.safety_status = "blocked"
                    continue
            record.safety_status = "allowed"
            allowed.append(record)
        return allowed
