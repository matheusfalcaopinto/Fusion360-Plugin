"""Skill registry models."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field


class Skill(BaseModel):
    """Loaded SKILL.md metadata."""

    name: str
    path: Path
    content: str
    purpose: str = ""
    status: str = "unknown"
    failure_modes: list[str] = Field(default_factory=list)
    facade_operations: list[str] = Field(default_factory=list)


class SkillRegistry:
    """In-memory skill registry."""

    def __init__(self, skills: list[Skill] | None = None) -> None:
        self.skills = {skill.name: skill for skill in skills or []}

    def get(self, name: str) -> Skill | None:
        """Return a skill by name."""

        return self.skills.get(name)

    def all(self) -> list[Skill]:
        """Return all loaded skills."""

        return list(self.skills.values())
