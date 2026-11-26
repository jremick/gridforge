"""Thread-safe read-only MBTiles basemap server for air-gapped GridForge Studio."""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from pydantic import BaseModel

logger = logging.getLogger(__name__)

DEFAULT_CONTAINER_BASEMAP_PATH = Path("/data/basemap.mbtiles")

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

    def resolve_mbtiles_path(self) -> Path | None:
        """Resolve the active local ``.mbtiles`` file, or ``None`` if unavailable."""
        if self._explicit_path is not None:
            return self._explicit_path if self._explicit_path.is_file() else None

        env_raw = os.environ.get("GRIDFORGE_BASEMAP_MBTILES", "").strip()
        if env_raw:
            env_path = Path(env_raw).expanduser().resolve()
            if env_path.is_file():
                return env_path

        pack_candidate = self._packs_dir / "basemap.mbtiles"
        if pack_candidate.is_file():
            return pack_candidate

        if self._allow_default_data_path and DEFAULT_CONTAINER_BASEMAP_PATH.is_file():
            return DEFAULT_CONTAINER_BASEMAP_PATH

        return None

    def get_info(self) -> BasemapInfoResponse:
        """Read MBTiles ``metadata`` and zoom extents; returns ``available=False`` when absent."""
        path = self.resolve_mbtiles_path()
        if path is None:
            return BasemapInfoResponse(available=False)

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
            name = meta.get("name") or path.stem
            attribution = meta.get("attribution") or "Offline MBTiles Basemap"
            bounds = _parse_bounds(meta.get("bounds"))

            return BasemapInfoResponse(
                available=True,
                name=name,
                format=fmt,
                minzoom=max(0, minzoom if minzoom is not None else 0),
                maxzoom=max(0, maxzoom if maxzoom is not None else 0),
                bounds=bounds,
                attribution=attribution,
                tile_url=f"/api/basemap/{{z}}/{{x}}/{{y}}.{fmt}",
            )
        except Exception as exc:
            logger.warning("Failed reading MBTiles basemap '%s': %s", path, exc)
            return BasemapInfoResponse(available=False)
        finally:
            if conn is not None:
                conn.close()

    def get_tile(self, z: int, x: int, y: int, *, requested_ext: str | None = None) -> tuple[bytes, str] | None:
        """Fetch one XYZ tile, converting XYZ ``y`` to MBTiles TMS ``tms_y = (2**z - 1) - y``."""
        if z < 0 or z > 22:
            return None
        max_coord = 1 << z
        if x < 0 or x >= max_coord or y < 0 or y >= max_coord:
            return None

        path = self.resolve_mbtiles_path()
        if path is None:
            return None

        tms_y = (max_coord - 1) - y
        conn: sqlite3.Connection | None = None
        try:
            conn = _open_readonly_sqlite(path)
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
            logger.debug("MBTiles tile lookup failed at %d/%d/%d (%s)", z, x, y, exc)
            return None
        finally:
            if conn is not None:
                conn.close()
