"""Memory record schemas."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field


class MemoryScope(StrEnum):
    """Memory scope."""

    GLOBAL = "global"
    PROJECT = "project"


class MemoryType(StrEnum):
    """Supported memory record types."""

    USER_PREFERENCE = "user_preference"
    FAILURE_PATTERN = "failure_pattern"
    REPAIR_RECIPE = "repair_recipe"
    DESIGN_DECISION = "design_decision"
    SKILL_NOTE = "skill_note"
    BENCHMARK_RESULT = "benchmark_result"
    SESSION_SUMMARY = "session_summary"


class MemoryRecord(BaseModel):
    """One retrieved or written memory item."""

    id: str
    scope: MemoryScope
    type: MemoryType
    summary: str
    content: str
    content_path: Path
    project: str | None = None
    tags: list[str] = Field(default_factory=list)
    confidence: str = "medium"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    relevance_score: float = 0.0
    safety_status: str = "allowed"
    contradiction_status: str = "none"
