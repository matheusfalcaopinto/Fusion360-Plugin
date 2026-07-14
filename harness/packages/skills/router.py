"""Skill router."""

from __future__ import annotations

from skills.registry import Skill, SkillRegistry


class SkillRouter:
    """Rank skills for a request or CadSpec intent."""

    def __init__(self, registry: SkillRegistry) -> None:
        self.registry = registry

    def rank(self, query: str, limit: int = 3) -> list[Skill]:
        """Return likely relevant skills."""

        normalized_query = query.lower().replace("-", "_")
        query_tokens = set(normalized_query.split())
        scored: list[tuple[int, Skill]] = []
        for skill in self.registry.all():
            haystack = f"{skill.name} {skill.purpose} {' '.join(skill.facade_operations)}".lower()
            score = sum(1 for token in query_tokens if token and (token in haystack or token.rstrip("s") in haystack))
            if skill.name == "fusion_mechanical_pro" and any(
                marker in normalized_query
                for marker in ("assembly", "assembl", "hinge", "revolute", "spacer", "standoff", "two plates")
            ):
                score += 5
            if score:
                scored.append((score, skill))
        return [skill for _, skill in sorted(scored, key=lambda item: item[0], reverse=True)[:limit]]
