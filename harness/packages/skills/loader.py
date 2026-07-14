"""Load filesystem-backed SKILL.md files."""

from __future__ import annotations

from pathlib import Path

from skills.registry import Skill, SkillRegistry


class SkillLoader:
    """Read skills from a directory of skill folders or Markdown files."""

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root is not None else _default_skill_root()

    def load(self) -> SkillRegistry:
        """Load all SKILL.md files under the root."""

        skills: list[Skill] = []
        for path in sorted(self.root.rglob("SKILL.md")):
            content = path.read_text(encoding="utf-8")
            name = _extract_name(content) or path.parent.name
            status = _extract_status(content)
            if status.lower().startswith("future"):
                continue
            skills.append(
                Skill(
                    name=name,
                    path=path,
                    content=content,
                    purpose=_extract_section(content, "Purpose"),
                    status=status,
                    failure_modes=_extract_bullets(content, "Common failure modes") or _extract_bullets(content, "Failure modes"),
                    facade_operations=_extract_bullets(content, "Tool facade operations"),
                )
            )
        return SkillRegistry(skills)


def _default_skill_root() -> Path:
    local = Path("skills")
    if _looks_like_harness_skill_root(local):
        return local
    try:
        from fusion_agent_assets import asset_root

        bundled = asset_root("skills")
        if _looks_like_harness_skill_root(bundled):
            return bundled
    except Exception:
        pass
    return local


def _looks_like_harness_skill_root(path: Path) -> bool:
    return (
        (path / "fusion_mechanical_pro" / "SKILL.md").is_file()
        or (path / "create_parametric_plate" / "SKILL.md").is_file()
    )


def _extract_name(content: str) -> str | None:
    for line in content.splitlines():
        if line.startswith("# Skill:"):
            return line.split(":", 1)[1].strip()
    return None


def _extract_status(content: str) -> str:
    in_status = False
    for line in content.splitlines():
        stripped = line.strip()
        lowered = stripped.lower()
        if lowered.startswith("## status"):
            in_status = True
            continue
        if in_status:
            if stripped.startswith("## "):
                break
            if stripped:
                return stripped
        if lowered.startswith("v"):
            return stripped
    return "unknown"


def _extract_section(content: str, heading: str) -> str:
    lines = content.splitlines()
    capture = False
    collected: list[str] = []
    for line in lines:
        if line.startswith("## "):
            if capture:
                break
            capture = line[3:].strip().lower() == heading.lower()
            continue
        if capture:
            collected.append(line)
    return "\n".join(collected).strip()


def _extract_bullets(content: str, heading: str) -> list[str]:
    section = _extract_section(content, heading)
    return [line.lstrip("- ").strip() for line in section.splitlines() if line.strip().startswith("-")]
