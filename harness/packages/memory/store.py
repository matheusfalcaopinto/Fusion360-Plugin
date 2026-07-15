"""Markdown memory store."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from memory.schemas import MemoryRecord, MemoryScope, MemorySource, MemoryType, TrustLevel
from memory.taint import inspect_memory_content, validate_memory_content


_PROJECT_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


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

        if not _PROJECT_NAME.fullmatch(project):
            raise ValueError("project must match [A-Za-z0-9_.-]{1,80}")
        root = (self.projects_root / project).resolve()
        projects_root = self.projects_root.resolve()
        if projects_root not in root.parents:
            raise ValueError("project path escapes memory workspace")
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
        """Persist one v2 record plus a metadata sidecar."""

        validate_memory_content(record.content)
        path = self._checked_record_path(record.content_path)
        if record.scope == MemoryScope.PROJECT:
            if not record.project:
                raise ValueError("project memory requires a project name")
            project_root = self.project_root(record.project)
            if project_root not in path.parents:
                raise ValueError("project memory path escapes project root")
        elif self.global_root.resolve() not in path.parents:
            raise ValueError("global memory path escapes global root")
        if record.source == MemorySource.WEB and not record.citations:
            raise ValueError("web memory requires at least one citation")
        if record.source == MemorySource.WEB:
            if any(not _valid_https_url(citation) for citation in record.citations):
                raise ValueError("web memory citations must use valid https URLs")
            record.source_url = record.source_url or record.citations[0]
            if not _valid_https_url(record.source_url):
                raise ValueError("web memory source_url must use a valid https URL")
            record.source_retrieved_at = record.source_retrieved_at or record.created_at
            record.source_content_sha256 = _sha256(record.content)
        record.content_sha256 = _sha256(record.content)
        record.taint_flags = sorted(set(record.taint_flags) | set(inspect_memory_content(record.content)))
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, record.content)
        record.content_path = path
        _atomic_write(_metadata_path(path), _metadata_json(record))
        return path

    def write_project_markdown(self, project: str, relative_path: str, content: str) -> Path:
        """Write a Markdown file under a project memory directory."""

        validate_memory_content(content)
        root = self.project_root(project)
        relative = Path(relative_path)
        if relative.is_absolute():
            raise ValueError("memory path must be relative")
        path = (root / relative).resolve()
        if root not in path.parents:
            raise ValueError("memory path escapes project root")
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, content)
        return path

    def _checked_record_path(self, path: Path) -> Path:
        resolved = path.resolve()
        workspace = self.workspace_root.resolve()
        if workspace not in resolved.parents:
            raise ValueError("memory record path escapes workspace root")
        if resolved.suffix.lower() != ".md":
            raise ValueError("memory records must use a .md path")
        return resolved


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
    base = {
        "id": f"{scope}:{path.stem}",
        "scope": scope,
        "type": record_type,
        "summary": title,
        "project": project,
        "tags": tags,
    }
    metadata_path = _metadata_path(path)
    if metadata_path.is_file():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            for key, value in base.items():
                metadata.setdefault(key, value)
            metadata["scope"] = scope
            metadata["project"] = project
            metadata.update({"content": content, "content_path": path})
            record = MemoryRecord.model_validate(metadata)
        except (OSError, ValueError, json.JSONDecodeError):
            record = MemoryRecord(
                **base,
                content=content,
                content_path=path,
                source=MemorySource.LEGACY,
                trust_level=TrustLevel.LEGACY_UNVERIFIED,
                taint_flags=["invalid_metadata"],
            )
    else:
        record = MemoryRecord(
            **base,
            content=content,
            content_path=path,
            source=MemorySource.LEGACY,
            trust_level=TrustLevel.LEGACY_UNVERIFIED,
            taint_flags=["legacy_record"],
        )
    actual_hash = _sha256(content)
    if record.content_sha256 and record.content_sha256 != actual_hash:
        record.taint_flags = sorted(set(record.taint_flags) | {"content_hash_mismatch"})
    record.content_sha256 = actual_hash
    return record


def _metadata_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".memory.json")


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _valid_https_url(value: str) -> bool:
    parsed = urlsplit(value)
    return (
        parsed.scheme.lower() == "https"
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
    )


def _metadata_json(record: MemoryRecord) -> str:
    payload = record.model_dump(
        mode="json",
        exclude={"content", "content_path", "relevance_score", "safety_status", "contradiction_status"},
    )
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _atomic_write(path: Path, content: str) -> None:
    descriptor, temp_name = tempfile.mkstemp(dir=path.parent, prefix=".memory-", suffix=".tmp")
    temp = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


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
