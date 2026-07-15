"""Lexical memory retrieval."""

from __future__ import annotations

import re

from memory.schemas import MemoryRecord, TrustLevel
from memory.store import MemoryStore


TOKEN_RE = re.compile(r"[a-z0-9_]+")


class MemoryRetriever:
    """Simple lexical/tag memory retriever."""

    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def retrieve(self, query: str, project: str | None = None, limit: int = 5) -> list[MemoryRecord]:
        """Return ranked memory records for a query."""

        query_tokens = set(_tokens(query))
        records = []
        for record in self.store.iter_records(project=project):
            haystack = set(_tokens(f"{record.summary}\n{record.content}\n{' '.join(record.tags)}"))
            lexical_score = len(query_tokens & haystack) / max(1, len(query_tokens))
            trust_weight = {
                TrustLevel.VERIFIED: 1.0,
                TrustLevel.TRUSTED: 0.9,
                TrustLevel.UNTRUSTED: 0.75,
                TrustLevel.LEGACY_UNVERIFIED: 0.6,
            }[record.trust_level]
            score = lexical_score * trust_weight
            if score > 0:
                record.relevance_score = score
                records.append(record)
        return sorted(records, key=lambda item: item.relevance_score, reverse=True)[:limit]


def _tokens(text: str) -> list[str]:
    return TOKEN_RE.findall(text.lower())
