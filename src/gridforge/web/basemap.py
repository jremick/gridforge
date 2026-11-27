"""Thread-safe read-only MBTiles basemap server for air-gapped GridForge Studio."""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from pydantic import BaseModel

from gridforge.packs.builder import load_pack_manifest, resolve_pack_directory

logger = logging.getLogger(__name__)

DEFAULT_CONTAINER_BASEMAP_PATH = Path("/data/basemap.mbtiles")
_PACK_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_HTML_TAG_RE = re.compile(r"<[^>]*>")
_PATH_CACHE_TTL_S: float = 5.0


def _strip_html_tags(raw: str | None) -> str:
    if not raw:
        return ""
    return _HTML_TAG_RE.sub("", raw).strip()

_FORMAT_TO_MEDIA_TYPE: dict[str, str] = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
}


class BasemapInfoResponse(BaseModel):
    """Metadata describing the optional local air-gapped MBTiles basemap."""

    available: bool = False
    name: str | None = None
    format: Literal["png", "jpg"] | None = None
    minzoom: int = 0
    maxzoom: int = 0
    bounds: list[float] | None = None
    attribution: str | None = None
    tile_url: str | None = None
    pack_id: str | None = None


def _normalize_format(raw: str | None) -> Literal["png", "jpg"]:
    val = (raw or "png").strip().lower()
    if val in ("jpg", "jpeg"):
        return "jpg"
    return "png"


def _detect_media_type(tile_bytes: bytes, fallback_ext: str | None = None) -> str:
    if tile_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if tile_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(tile_bytes) >= 12 and tile_bytes[:4] == b"RIFF" and tile_bytes[8:12] == b"WEBP":
        return "image/webp"
    if fallback_ext:
        return _FORMAT_TO_MEDIA_TYPE.get(fallback_ext.lower().lstrip("."), "image/png")
    return "image/png"


def _open_readonly_sqlite(mbtiles_path: Path) -> sqlite3.Connection:
    """Open SQLite in strict read-only immutable URI mode safe on ``:ro`` Docker mounts."""
    encoded = quote(str(mbtiles_path.resolve()), safe="/:")
    try:
        return sqlite3.connect(
            f"file:{encoded}?mode=ro&immutable=1",
            uri=True,
            timeout=5.0,
            check_same_thread=False,
        )
    except sqlite3.OperationalError:
        return sqlite3.connect(
            f"file:{encoded}?mode=ro",
            uri=True,
            timeout=5.0,
            check_same_thread=False,
        )


def _parse_bounds(raw: str | None) -> list[float] | None:
    if not raw:
        return None
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) != 4:
        return None
    try:
        vals = [float(p) for p in parts]
    except ValueError:
        return None
    if vals[0] >= vals[2] or vals[1] >= vals[3]:
        return None
    return [round(v, 6) for v in vals]


class MBTilesBasemapStore:
    """Discovers and serves tiles from a local raster ``.mbtiles`` file without Qt/QGIS."""

    def __init__(
        self,
        *,
        packs_dir: Path,
        explicit_path: Path | str | None = None,
        allow_default_data_path: bool = False,
    ) -> None:
        self._packs_dir = Path(packs_dir).resolve()
        self._explicit_path = Path(explicit_path).resolve() if explicit_path is not None else None
        self._allow_default_data_path = allow_default_data_path
        self._cache_lock = threading.Lock()
        self._path_cache: dict[str | None, tuple[float, tuple[Path, str | None] | None]] = {}
        self._local = threading.local()

    def invalidate_cache(self) -> None:
        """Clear cached pack resolutions and close thread-local read-only SQLite handles."""
        with self._cache_lock:
            self._path_cache.clear()
        self._close_thread_conns()

    def _close_thread_conns(self) -> None:
        conns: dict[str, tuple[tuple[int, int], sqlite3.Connection]] | None = getattr(
            self._local, "conns", None
        )
        if not conns:
            return
        for _, conn in conns.values():
            try:
                conn.close()
            except sqlite3.Error:
                pass
        conns.clear()

    def _get_cached_connection(self, path: Path) -> sqlite3.Connection:
        resolved = path.resolve()
        st = resolved.stat()
        sig = (st.st_mtime_ns, st.st_ino)
        key = str(resolved)
        conns: dict[str, tuple[tuple[int, int], sqlite3.Connection]] = getattr(
            self._local, "conns", None
        ) or {}
        self._local.conns = conns
        existing = conns.get(key)
        if existing is not None:
            cached_sig, conn = existing
            if cached_sig == sig:
                return conn
            try:
                conn.close()
            except sqlite3.Error:
                pass
            conns.pop(key, None)
        conn = _open_readonly_sqlite(resolved)
        if len(conns) >= 8:
            old_key = next(iter(conns))
            _, old_conn = conns.pop(old_key)
            try:
                old_conn.close()
            except sqlite3.Error:
                pass
        conns[key] = (sig, conn)
        return conn

    def _resolve_pack_basemap(self, pack_id: str) -> Path | None:
        """Resolve ``<pack_dir>/basemap.mbtiles`` (or manifest ``basemap_path``) safely under ``packs_dir``."""
        if not pack_id or not _PACK_ID_RE.match(pack_id) or not self._packs_dir.is_dir():
            return None
        try:
            pack_dir = resolve_pack_directory(self._packs_dir, pack_id)
        except FileNotFoundError:
            return None
        if not pack_dir.is_relative_to(self._packs_dir):
            return None

        try:
            manifest = load_pack_manifest(pack_dir)
            if manifest.basemap_path:
                cand = (pack_dir / manifest.basemap_path).resolve()
                if cand.is_relative_to(pack_dir) and cand.is_file():
                    return cand
        except Exception:
            pass

        default_bm = (pack_dir / "basemap.mbtiles").resolve()
        if default_bm.is_relative_to(pack_dir) and default_bm.is_file():
            return default_bm
        return None

    def _discover_first_pack_basemap(self) -> tuple[Path, str] | None:
        """Return the first installed pack's bundled ``basemap.mbtiles``, if any."""
        if not self._packs_dir.is_dir():
            return None
        for child in sorted(self._packs_dir.iterdir()):
            if not child.is_dir() or not (child / "pack.json").is_file():
                continue
            resolved = child.resolve()
            if not resolved.is_relative_to(self._packs_dir):
                continue
            try:
                manifest = load_pack_manifest(resolved)
            except Exception:
                continue
            if manifest.basemap_path:
                cand = (resolved / manifest.basemap_path).resolve()
                if cand.is_relative_to(resolved) and cand.is_file():
                    return (cand, manifest.pack_id)
            fallback = (resolved / "basemap.mbtiles").resolve()
            if fallback.is_relative_to(resolved) and fallback.is_file():
                return (fallback, manifest.pack_id)
        return None

    def resolve_mbtiles_path(self, pack_id: str | None = None) -> Path | None:
        """Resolve the active local ``.mbtiles`` file (optionally preferring ``pack_id``)."""
        resolved = self._resolve_with_source(pack_id=pack_id)
        return resolved[0] if resolved is not None else None

    def _resolve_with_source(self, pack_id: str | None = None) -> tuple[Path, str | None] | None:
        now = time.monotonic()
        with self._cache_lock:
            entry = self._path_cache.get(pack_id)
            if entry is not None:
                ts, cached = entry
                if now - ts <= _PATH_CACHE_TTL_S:
                    if cached is None or cached[0].is_file():
                        return cached
                self._path_cache.pop(pack_id, None)

        result = self._resolve_with_source_uncached(pack_id=pack_id)
        with self._cache_lock:
            self._path_cache[pack_id] = (now, result)
        return result

    def _resolve_with_source_uncached(self, pack_id: str | None = None) -> tuple[Path, str | None] | None:
        if pack_id:
            pack_bm = self._resolve_pack_basemap(pack_id)
            if pack_bm is not None:
                return (pack_bm, pack_id)

        if self._explicit_path is not None:
            return (self._explicit_path, None) if self._explicit_path.is_file() else None

        env_raw = os.environ.get("GRIDFORGE_BASEMAP_MBTILES", "").strip()
        if env_raw:
            env_path = Path(env_raw).expanduser().resolve()
            if env_path.is_file():
                return (env_path, None)

        pack_candidate = self._packs_dir / "basemap.mbtiles"
        if pack_candidate.is_file():
            return (pack_candidate, None)

        if self._allow_default_data_path and DEFAULT_CONTAINER_BASEMAP_PATH.is_file():
            return (DEFAULT_CONTAINER_BASEMAP_PATH, None)

        discovered = self._discover_first_pack_basemap()
        if discovered is not None:
            return discovered

        return None

    def get_info(self, *, pack_id: str | None = None) -> BasemapInfoResponse:
        """Read MBTiles ``metadata`` and zoom extents; returns ``available=False`` when absent."""
        resolved = self._resolve_with_source(pack_id=pack_id)
        if resolved is None:
            return BasemapInfoResponse(available=False)
        path, resolved_pack_id = resolved

        conn: sqlite3.Connection | None = None
        try:
            conn = _open_readonly_sqlite(path)
            meta: dict[str, str] = {}
            try:
                rows = conn.execute("SELECT name, value FROM metadata").fetchall()
                for k, v in rows:
                    if k is not None and v is not None:
                        meta[str(k).strip().lower()] = str(v).strip()
            except sqlite3.OperationalError:
                pass

            minzoom: int | None = None
            maxzoom: int | None = None
            if "minzoom" in meta:
                try:
                    minzoom = int(float(meta["minzoom"]))
                except ValueError:
                    minzoom = None
            if "maxzoom" in meta:
                try:
                    maxzoom = int(float(meta["maxzoom"]))
                except ValueError:
                    maxzoom = None

            if minzoom is None or maxzoom is None:
                try:
                    row = conn.execute(
                        "SELECT MIN(zoom_level), MAX(zoom_level) FROM tiles"
                    ).fetchone()
                    if row is not None:
                        if minzoom is None and row[0] is not None:
                            minzoom = int(row[0])
                        if maxzoom is None and row[1] is not None:
                            maxzoom = int(row[1])
                except sqlite3.OperationalError:
                    pass

            fmt = _normalize_format(meta.get("format"))
            name = _strip_html_tags(meta.get("name")) or path.stem
            attribution = _strip_html_tags(meta.get("attribution")) or "Offline MBTiles Basemap"
            bounds = _parse_bounds(meta.get("bounds"))
            qs = f"?pack_id={quote(resolved_pack_id)}" if resolved_pack_id else ""

            return BasemapInfoResponse(
                available=True,
                name=name,
                format=fmt,
                minzoom=max(0, minzoom if minzoom is not None else 0),
                maxzoom=max(0, maxzoom if maxzoom is not None else 0),
                bounds=bounds,
                attribution=attribution,
                tile_url=f"/api/basemap/{{z}}/{{x}}/{{y}}.{fmt}{qs}",
                pack_id=resolved_pack_id,
            )
        except Exception as exc:
            logger.warning("Failed reading MBTiles basemap '%s': %s", path, exc)
            return BasemapInfoResponse(available=False)
        finally:
            if conn is not None:
                conn.close()

    def get_tile(
        self,
        z: int,
        x: int,
        y: int,
        *,
        requested_ext: str | None = None,
        pack_id: str | None = None,
    ) -> tuple[bytes, str] | None:
        """Fetch one XYZ tile, converting XYZ ``y`` to MBTiles TMS ``tms_y = (2**z - 1) - y``."""
        if z < 0 or z > 22:
            return None
        max_coord = 1 << z
        if x < 0 or x >= max_coord or y < 0 or y >= max_coord:
            return None

        path = self.resolve_mbtiles_path(pack_id=pack_id)
        if path is None:
            return None

        tms_y = (max_coord - 1) - y
        try:
            conn = self._get_cached_connection(path)
            row = conn.execute(
                "SELECT tile_data FROM tiles "
                "WHERE zoom_level = ? AND tile_column = ? AND tile_row = ? LIMIT 1",
                (z, x, tms_y),
            ).fetchone()
            if row is None or not row[0]:
                return None
            raw = bytes(row[0])
            media_type = _detect_media_type(raw, fallback_ext=requested_ext)
            return (raw, media_type)
        except Exception as exc:
            self._close_thread_conns()
            logger.debug("MBTiles tile lookup failed at %d/%d/%d (%s)", z, x, y, exc)
            return None
