"""Long-path-safe filesystem boundaries for benchmark artifacts."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from pathlib import Path


def io_path(path: Path | str) -> str:
    """Return an absolute path suitable for a concrete OS call.

    Windows extended-length prefixes are deliberately introduced only at this
    boundary.  Reports and public APIs continue to carry normal logical paths.
    """

    absolute = os.path.abspath(os.fspath(path))
    if os.name != "nt" or absolute.startswith("\\\\?\\"):
        return absolute
    if absolute.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute[2:]
    return "\\\\?\\" + absolute


def mkdir(path: Path | str) -> None:
    """Create a directory tree through the long-path boundary."""

    os.makedirs(io_path(path), exist_ok=True)


def path_exists(path: Path | str) -> bool:
    return os.path.exists(io_path(path))


def path_is_dir(path: Path | str) -> bool:
    return os.path.isdir(io_path(path))


def path_is_file(path: Path | str) -> bool:
    return os.path.isfile(io_path(path))


def list_files(path: Path | str, *, suffix: str | None = None) -> list[Path]:
    """List physical leaves while returning prefix-free logical paths."""

    logical = Path(path)
    names = sorted(
        entry.name
        for entry in os.scandir(io_path(logical))
        if entry.is_file() and (suffix is None or entry.name.endswith(suffix))
    )
    return [logical / name for name in names]


def replace(source: Path | str, destination: Path | str) -> None:
    """Atomically replace within one volume using canonical OS paths."""

    os.replace(io_path(source), io_path(destination))


def rmtree(path: Path | str) -> None:
    shutil.rmtree(io_path(path), ignore_errors=True)


def unlink(path: Path | str, *, missing_ok: bool = False) -> None:
    try:
        os.unlink(io_path(path))
    except FileNotFoundError:
        if not missing_ok:
            raise


def read_text(path: Path | str) -> str:
    with open(io_path(path), encoding="utf-8") as handle:
        return handle.read()


def read_bytes(path: Path | str) -> bytes:
    with open(io_path(path), "rb") as handle:
        return handle.read()


def atomic_write_text(path: Path | str, text: str) -> None:
    """Durably write text beside its destination and atomically publish it."""

    _atomic_publish_text(path, text, exclusive=False)


def atomic_create_text(path: Path | str, text: str) -> None:
    """Atomically publish a new file without replacing an existing destination."""

    _atomic_publish_text(path, text, exclusive=True)


def _atomic_publish_text(path: Path | str, text: str, *, exclusive: bool) -> None:
    """Write beside the destination, revalidate its parent, then publish."""

    logical = Path(path)
    mkdir(logical.parent)
    parent_path = io_path(logical.parent)
    parent_identity = _directory_identity(parent_path)
    descriptor, temp_name = tempfile.mkstemp(
        dir=parent_path,
        prefix=".tmp-",
        suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if _directory_identity(parent_path) != parent_identity:
            raise OSError("benchmark artifact parent changed before atomic replace")
        temporary_stat = os.stat(io_path(temp_name), follow_symlinks=False)
        if temporary_stat.st_dev != parent_identity[0]:
            raise OSError("benchmark temporary file changed volume before replace")
        if exclusive:
            # A same-volume hard link is an atomic no-replace publish on both
            # Windows and POSIX: it raises FileExistsError if another writer
            # won the destination race. The private temporary name is removed
            # only after the complete file is visible at its final name.
            os.link(io_path(temp_name), io_path(logical))
            os.unlink(io_path(temp_name))
        else:
            replace(temp_name, logical)
    except BaseException:
        try:
            os.unlink(io_path(temp_name))
        except FileNotFoundError:
            pass
        raise


def _directory_identity(path: str) -> tuple[int, int, str]:
    stat = os.stat(path, follow_symlinks=True)
    if not os.path.isdir(path):
        raise NotADirectoryError(path)
    canonical = os.path.normcase(os.path.realpath(path))
    return int(stat.st_dev), int(stat.st_ino), canonical


def physical_artifact_name(logical_id: str, *, suffix: str = ".json") -> str:
    """Map a logical identifier to a short, stable physical filename."""

    if not suffix.startswith(".") or not re.fullmatch(r"\.[A-Za-z0-9]{1,10}", suffix):
        raise ValueError("artifact suffix must be a simple extension")
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "-", logical_id).strip(".-")
    prefix = (normalized[:20] or "artifact").rstrip(".-")
    digest = hashlib.sha256(logical_id.encode("utf-8")).hexdigest()[:32]
    return f"{prefix}-{digest}{suffix}"
