"""Memory record schemas."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


class MemoryScope(StrEnum):
    """Memory scope."""

    GLOBAL = "global"
    PROJECT = "project"


class MemoryType(StrEnum):
    """Supported memory record types."""

    FACT = "fact"
    RESULT = "result"
    USER_PREFERENCE = "user_preference"
    FAILURE_PATTERN = "failure_pattern"
    REPAIR_RECIPE = "repair_recipe"
    DESIGN_DECISION = "design_decision"
    SKILL_NOTE = "skill_note"
    BENCHMARK_RESULT = "benchmark_result"
    SESSION_SUMMARY = "session_summary"


class MemorySource(StrEnum):
    """Origin class for memory content."""

    USER = "user"
    WORKSPACE = "workspace"
    TOOL = "tool"
    WEB = "web"
    LEGACY = "legacy"


class TrustLevel(StrEnum):
    """How strongly a consumer may rely on a memory record."""

    VERIFIED = "verified"
    TRUSTED = "trusted"
    UNTRUSTED = "untrusted"
    LEGACY_UNVERIFIED = "legacy_unverified"


class MemoryRecord(BaseModel):
    """One retrieved or written memory item."""

    schema_version: Literal["memory_record.v2"] = "memory_record.v2"
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
    source: MemorySource = MemorySource.LEGACY
    provenance: list[str] = Field(default_factory=list)
    trust_level: TrustLevel = TrustLevel.LEGACY_UNVERIFIED
    expires_at: datetime | None = None
    content_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    citations: list[str] = Field(default_factory=list)
    source_url: str | None = None
    source_retrieved_at: datetime | None = None
    source_content_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    taint_flags: list[str] = Field(default_factory=list)
