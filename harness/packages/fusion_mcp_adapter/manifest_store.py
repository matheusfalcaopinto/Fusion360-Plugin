"""Atomic persistence and migration for discovered MCP tool manifests."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fusion_mcp_adapter.tool_result import ToolManifest


class ManifestStore:
    """Read and write fingerprinted Fusion MCP manifest schema v2 files."""

    def __init__(self, root: Path | str = "manifests") -> None:
        self.root = Path(root)
        self.last_error: str | None = None
        self.last_persistence_error: str | None = None

    def save(self, manifest: ToolManifest) -> Path:
        """Persist a manifest and return its timestamped (or existing latest) path.

        This compatibility API delegates to :meth:`save_if_changed`; callers
        that need to know whether a new snapshot was created should use that
        method directly.
        """

        saved = self.save_if_changed(manifest)
        if saved is not None:
            return saved
        return self.root / f"fusion_mcp_tools_latest_{self._source_key(manifest.source)}.json"

    def save_if_changed(self, manifest: ToolManifest) -> Path | None:
        """Atomically persist ``manifest`` only when its tool fingerprint changed."""

        self.last_error = None
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            manifest.schema_version = 2
            manifest.refresh_fingerprint()
            source = self._source_key(manifest.source)
            latest = self.root / f"fusion_mcp_tools_latest_{source}.json"
            current = self._load_path(latest)
            if current is None and source == "real":
                current = self._load_path(self.root / "fusion_mcp_tools_latest.json")
            if current is not None and current.fingerprint == manifest.fingerprint:
                if not latest.exists():
                    self._atomic_write(latest, manifest.model_dump_json(indent=2, by_alias=True))
                self.last_persistence_error = None
                return None

            manifest.previous_fingerprint = current.fingerprint if current is not None else None

            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
            path = self.root / f"fusion_mcp_tools_{source}_{timestamp}.json"
            text = manifest.model_dump_json(indent=2, by_alias=True)
            self._atomic_write(path, text)
            self._atomic_write(latest, text)
            if source == "real":
                # Keep the v0.1 alias readable while making latest_real the
                # authoritative source-specific file.
                self._atomic_write(self.root / "fusion_mcp_tools_latest.json", text)
            self.last_persistence_error = None
            return path
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.last_persistence_error = self.last_error
            raise

    def load_latest(self, source: str | None = None) -> ToolManifest | None:
        """Load the latest manifest and migrate a real legacy alias in place."""

        self.last_error = None
        candidates: list[Path] = []
        source_key = self._source_key(source) if source else None
        if source_key:
            candidates.append(self.root / f"fusion_mcp_tools_latest_{source_key}.json")
            if source_key == "real":
                candidates.append(self.root / "fusion_mcp_tools_latest.json")
        else:
            candidates.extend(
                [
                    self.root / "fusion_mcp_tools_latest_real.json",
                    self.root / "fusion_mcp_tools_latest.json",
                    self.root / "fusion_mcp_tools_latest_mock.json",
                ]
            )

        for candidate in candidates:
            if not candidate.exists():
                continue
            try:
                manifest = self._load_path(candidate)
                if manifest is None:
                    continue
                authoritative = self.root / "fusion_mcp_tools_latest_real.json"
                if (
                    candidate.name == "fusion_mcp_tools_latest.json"
                    and self._source_key(manifest.source) == "real"
                    and not authoritative.exists()
                ):
                    # The migration is atomic and intentionally does not create
                    # a timestamped duplicate.
                    self.root.mkdir(parents=True, exist_ok=True)
                    self._atomic_write(authoritative, manifest.model_dump_json(indent=2, by_alias=True))
                return manifest
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                raise
        return None

    def latest_status(self) -> dict[str, dict[str, Any]]:
        """Return presence, schema, and fingerprint diagnostics."""

        status: dict[str, dict[str, Any]] = {}
        for source, filename in {
            "real": "fusion_mcp_tools_latest_real.json",
            "mock": "fusion_mcp_tools_latest_mock.json",
            "legacy": "fusion_mcp_tools_latest.json",
        }.items():
            path = self.root / filename
            entry: dict[str, Any] = {
                "path": str(path),
                "exists": path.exists(),
                "bytes": path.stat().st_size if path.exists() else 0,
            }
            if path.exists():
                try:
                    manifest = self._load_path(path)
                    if manifest is not None:
                        entry.update(
                            schema_version=manifest.schema_version,
                            fingerprint=manifest.fingerprint,
                            captured_at=manifest.captured_at,
                        )
                except Exception as exc:  # diagnostic calls must remain usable
                    entry["error"] = f"{type(exc).__name__}: {exc}"
            status[source] = entry
        if self.last_persistence_error:
            status["persistence"] = {"error": self.last_persistence_error}
        return status

    def _load_path(self, path: Path) -> ToolManifest | None:
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        manifest = ToolManifest.model_validate(payload)
        manifest.schema_version = 2
        manifest.refresh_fingerprint()
        return manifest

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise

    @staticmethod
    def _source_key(source: str | None) -> str:
        value = (source or "real").lower()
        if "mock" in value:
            return "mock"
        if "real" in value or "fusion" in value:
            return "real"
        return value.replace(" ", "_").replace("-", "_")
