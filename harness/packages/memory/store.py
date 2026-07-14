"""Markdown memory store."""

from __future__ import annotations

import shutil
from pathlib import Path

from memory.schemas import MemoryRecord, MemoryScope, MemoryType


class MemoryStore:
    """Read and write Markdown memory files under workspace/."""

    def __init__(self, workspace_root: Path | str = "workspace", template_root: Path | str | None = None) -> None:
        self.workspace_root = Path(workspace_root)
        self.template_root = Path(template_root) if template_root is not None else _default_template_root()
        self.global_root = self.workspace_root / "global"
        self.projects_root = self.workspace_root / "projects"

    def seed_global(self) -> None:
        """Copy initial global memory templates when missing."""

        self.global_root.mkdir(parents=True, exist_ok=True)
        template_global = self.template_root / "global"
        if not template_global.exists():
            return
        for source in template_global.glob("*.md"):
            target = self.global_root / source.name
            if not target.exists():
                shutil.copyfile(source, target)

    def project_root(self, project: str) -> Path:
        """Return and create a project memory directory."""

        root = self.projects_root / project
        root.mkdir(parents=True, exist_ok=True)
        return root

    def iter_records(self, project: str | None = None) -> list[MemoryRecord]:
        """Load Markdown memory records from global and optional project scope."""

        records: list[MemoryRecord] = []
        for path in sorted(self.global_root.glob("*.md")):
            records.append(_record_from_file(path, MemoryScope.GLOBAL, None))
        if project:
            root = self.project_root(project)
            for path in sorted(root.rglob("*.md")):
                if "sessions" in path.parts and path.name != "memory_summary.md":
                    continue
                records.append(_record_from_file(path, MemoryScope.PROJECT, project))
        return records

    def write_record(self, record: MemoryRecord) -> Path:
        """Persist one memory record."""

        record.content_path.parent.mkdir(parents=True, exist_ok=True)
        record.content_path.write_text(record.content, encoding="utf-8")
        return record.content_path

    def write_project_markdown(self, project: str, relative_path: str, content: str) -> Path:
        """Write a Markdown file under a project memory directory."""

        path = self.project_root(project) / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path


def _record_from_file(path: Path, scope: MemoryScope, project: str | None) -> MemoryRecord:
    content = path.read_text(encoding="utf-8")
    title = next((line.lstrip("# ").strip() for line in content.splitlines() if line.startswith("#")), path.stem)
    lowered = path.stem.lower()
    if "failure" in lowered:
        record_type = MemoryType.FAILURE_PATTERN
    elif "decision" in lowered:
        record_type = MemoryType.DESIGN_DECISION
    elif "preference" in lowered:
        record_type = MemoryType.USER_PREFERENCE
    else:
        record_type = MemoryType.SKILL_NOTE
    tags = [part.lower() for part in path.stem.replace("-", "_").split("_") if part]
    return MemoryRecord(
        id=f"{scope}:{path.stem}",
        scope=scope,
        type=record_type,
        summary=title,
        content=content,
        content_path=path,
        project=project,
        tags=tags,
    )


def _default_template_root() -> Path:
    local = Path("memory")
    if (local / "global" / "UNIT_POLICY.md").is_file():
        return local
    try:
        from fusion_agent_assets import asset_root

        bundled = asset_root("memory")
        if (bundled / "global" / "UNIT_POLICY.md").is_file():
            return bundled
    except Exception:
        pass
    return local
