"""Bounded LRU cache directory pruning for GridForge DEM and OSM caches."""

from __future__ import annotations

import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CachePruneResult:
    """Summary metrics returned by :func:`trim_cache_dir`."""

    total_bytes_before: int
    total_bytes_after: int
    deleted_files: int
    deleted_bytes: int
    removed_dirs: int
    skipped_files: int = 0


def _is_protected_file(name: str) -> bool:
    """Return True for active temporary, partial download, or lock filenames."""
    lower = name.lower()
    return (
        lower.endswith((".tmp", ".part", ".lock"))
        or ".tmp." in lower
        or ".part." in lower
    )


def trim_cache_dir(cache_dir: Path | str, max_bytes: int) -> CachePruneResult:
    """LRU-trim regular files in ``cache_dir`` until total size <= ``max_bytes``.

    Uses ``st_atime_ns`` (falling back to ``st_mtime_ns`` when ``atime_ns <= 0``)
    with deterministic tie-breaking on ``(lru_ns, mtime_ns, path_str)``.
    Symlinks and active ``.tmp`` / ``.part`` / ``.lock`` files are skipped and
    never deleted. Empty subdirectories are removed bottom-up while preserving
    ``cache_dir`` itself.
    """
    root = Path(cache_dir)
    if root.is_symlink() or not root.exists() or not root.is_dir():
        return CachePruneResult(
            total_bytes_before=0,
            total_bytes_after=0,
            deleted_files=0,
            deleted_bytes=0,
            removed_dirs=0,
            skipped_files=0,
        )

    effective_max = max(0, int(max_bytes))
    entries: list[tuple[int, int, str, Path, int]] = []
    total_bytes_before = 0
    skipped_files = 0

    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        dir_path = Path(dirpath)
        dirnames[:] = [d for d in dirnames if not (dir_path / d).is_symlink()]

        for fname in filenames:
            fpath = dir_path / fname
            if fpath.is_symlink() or _is_protected_file(fname):
                skipped_files += 1
                continue

            try:
                st = os.stat(fpath, follow_symlinks=False)
            except OSError:
                skipped_files += 1
                continue

            if not stat.S_ISREG(st.st_mode):
                skipped_files += 1
                continue

            size = max(0, int(st.st_size))
            atime_ns_raw = getattr(st, "st_atime_ns", None)
            atime_ns = (
                int(atime_ns_raw)
                if atime_ns_raw is not None
                else int(st.st_atime * 1_000_000_000)
            )
            mtime_ns_raw = getattr(st, "st_mtime_ns", None)
            mtime_ns = (
                int(mtime_ns_raw)
                if mtime_ns_raw is not None
                else int(st.st_mtime * 1_000_000_000)
            )
            lru_ns = atime_ns if atime_ns > 0 else mtime_ns
            entries.append((lru_ns, mtime_ns, str(fpath), fpath, size))
            total_bytes_before += size

    remaining = total_bytes_before
    deleted_files = 0
    deleted_bytes = 0

    if remaining > effective_max:
        entries.sort(key=lambda item: (item[0], item[1], item[2]))
        for _lru_ns, _mtime_ns, _path_str, fpath, size in entries:
            if remaining <= effective_max:
                break
            try:
                fpath.unlink()
            except FileNotFoundError:
                remaining = max(0, remaining - size)
                continue
            except OSError as exc:
                logger.warning("Failed to prune cache file %s: %s", fpath, exc)
                continue

            remaining = max(0, remaining - size)
            deleted_files += 1
            deleted_bytes += size

    removed_dirs = 0
    for dirpath, _dirnames, _filenames in os.walk(root, topdown=False, followlinks=False):
        dpath = Path(dirpath)
        if dpath == root or dpath.is_symlink():
            continue
        try:
            dpath.rmdir()
            removed_dirs += 1
        except OSError:
            pass

    return CachePruneResult(
        total_bytes_before=total_bytes_before,
        total_bytes_after=remaining,
        deleted_files=deleted_files,
        deleted_bytes=deleted_bytes,
        removed_dirs=removed_dirs,
        skipped_files=skipped_files,
    )
