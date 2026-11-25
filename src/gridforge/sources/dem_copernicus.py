"""Cloud-native Copernicus GLO-30 DEM source via AWS S3 COGs and GDAL /vsicurl/."""

from __future__ import annotations

import hashlib
import logging
import math
import os
import shutil
import struct
import subprocess
from pathlib import Path
from typing import Final

import httpx

from gridforge.config import BBox, SheetLayout

logger = logging.getLogger(__name__)

BASE_URL: Final[str] = "https://copernicus-dem-30m.s3.eu-central-1.amazonaws.com"
TILE_LIST_URL: Final[str] = f"{BASE_URL}/tileList.txt"
USER_AGENT: Final[str] = "gridforge/0.1 (+https://github.com/gridforge/gridforge)"

GDAL_VSICURL_ENV: Final[dict[str, str]] = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.vrt",
    "VSI_CACHE": "TRUE",
    "VSI_CACHE_SIZE": "67108864",
    "GDAL_HTTP_MAX_RETRY": "3",
    "GDAL_HTTP_RETRY_DELAY": "2",
    "GDAL_HTTP_USERAGENT": USER_AGENT,
}


def format_tile_name(lat_deg: int, lon_deg: int) -> str:
    """Format the canonical Copernicus GLO-30 1°×1° COG tile name for SW corner (lat_deg, lon_deg).

    Examples:
        (-34, 150) -> 'Copernicus_DSM_COG_10_S34_00_E150_00_DEM'
        (39, -106) -> 'Copernicus_DSM_COG_10_N39_00_W106_00_DEM'
    """
    lat_hemi = "N" if lat_deg >= 0 else "S"
    lon_hemi = "E" if lon_deg >= 0 else "W"
    return (
        f"Copernicus_DSM_COG_10_{lat_hemi}{abs(lat_deg):02d}_00_"
        f"{lon_hemi}{abs(lon_deg):03d}_00_DEM"
    )


def tile_url(tile_name: str) -> str:
    """Return the HTTPS URL for a Copernicus GLO-30 COG tile."""
    return f"{BASE_URL}/{tile_name}/{tile_name}.tif"


def _normalize_lon_tile(lon_deg: int) -> int:
    """Wrap integer tile longitude into [-180, 179] for anti-meridian crossings."""
    return ((lon_deg + 180) % 360) - 180


def tiles_for_bbox(bbox: BBox) -> list[str]:
    """Compute all 1°×1° Copernicus GLO-30 tile names intersecting a WGS84 bounding box.

    Uses ``math.floor`` so Southern and Western hemisphere coordinates map to the
    correct SW-corner tile (e.g. ``lat=-33.732 -> S34``, ``lon=-105.5 -> W106``).
    Handles exact integer upper boundaries and anti-meridian wraparound cleanly.
    """
    min_lon, min_lat, max_lon, max_lat = bbox.as_tuple()

    lat_start = max(-90, min(89, math.floor(min_lat)))
    if max_lat > min_lat:
        lat_end = max(lat_start, min(89, math.floor(max_lat - 1e-9)))
    else:
        lat_end = lat_start

    lon_start = math.floor(min_lon)
    if max_lon > min_lon:
        lon_end = max(lon_start, math.floor(max_lon - 1e-9))
    else:
        lon_end = lon_start

    seen: set[str] = set()
    tiles: list[str] = []
    for lat_deg in range(lat_start, lat_end + 1):
        for raw_lon_deg in range(lon_start, lon_end + 1):
            lon_deg = _normalize_lon_tile(raw_lon_deg)
            name = format_tile_name(lat_deg, lon_deg)
            if name not in seen:
                seen.add(name)
                tiles.append(name)
    return tiles


def parse_tile_list(raw_text: str) -> set[str]:
    """Parse Copernicus ``tileList.txt`` lines into canonical tile names."""
    tiles: set[str] = set()
    for line in raw_text.splitlines():
        token = line.strip().rstrip("/")
        if not token:
            continue
        # Tolerate either bare tile names or relative paths ending in .tif
        if "/" in token:
            token = token.rsplit("/", 1)[-1]
        if token.endswith(".tif"):
            token = token[:-4]
        if token:
            tiles.add(token)
    return tiles


def filter_available_tiles(candidate_tiles: list[str], available_tiles: set[str]) -> list[str]:
    """Filter candidate tile names against the Copernicus manifest to avoid 404 storms."""
    return [t for t in candidate_tiles if t in available_tiles]


def _round_enclosing_bbox(bbox: BBox, step_deg: float = 0.001) -> BBox:
    """Round a WGS84 bbox outward to ``step_deg`` so cached clips always cover the request."""
    inv = 1.0 / step_deg
    return BBox(
        min_x=math.floor(bbox.min_x * inv) / inv,
        min_y=math.floor(bbox.min_y * inv) / inv,
        max_x=math.ceil(bbox.max_x * inv) / inv,
        max_y=math.ceil(bbox.max_y * inv) / inv,
    )


def _clip_cache_key(bbox: BBox) -> str:
    payload = f"{bbox.min_x:.3f},{bbox.min_y:.3f},{bbox.max_x:.3f},{bbox.max_y:.3f}"
    return hashlib.sha256(payload.encode("ascii")).hexdigest()[:24]


def _write_zero_fallback_geotiff(bbox: BBox, out_path: Path, size: int = 32) -> Path:
    """Write a minimal flat 0.0 m Float32 GeoTIFF in EPSG:4326 covering ``bbox``."""
    from osgeo import gdal, osr  # type: ignore[import-untyped]

    gdal.UseExceptions()
    driver = gdal.GetDriverByName("GTiff")
    ds = driver.Create(
        str(out_path),
        size,
        size,
        1,
        gdal.GDT_Float32,
        options=["COMPRESS=DEFLATE", "TILED=YES"],
    )
    if ds is None:
        raise RuntimeError(f"Failed to create zero-elevation fallback GeoTIFF at {out_path}")
    pixel_w = bbox.width / size
    pixel_h = bbox.height / size
    ds.SetGeoTransform((bbox.min_x, pixel_w, 0.0, bbox.max_y, 0.0, -pixel_h))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    band = ds.GetRasterBand(1)
    band.SetNoDataValue(-9999.0)
    band.WriteRaster(0, 0, size, size, struct.pack(f"={size * size}f", *([0.0] * (size * size))))
    band.FlushCache()
    ds.FlushCache()
    ds = None
    return out_path


class CopernicusGLO30Source:
    """On-demand Copernicus GLO-30 DEM source streaming COGs via GDAL ``/vsicurl/``."""

    name: str = "copernicus-glo30"
    attribution: str = (
        "Produced using Copernicus WorldDEM-30 © DLR e.V. 2010-2014 and "
        "© Airbus Defence and Space GmbH 2014-2018 provided under COPERNICUS "
        "by the European Union and ESA; all rights reserved."
    )

    def __init__(
        self,
        cache_dir: Path | str | None = None,
        allow_ocean_fallback: bool = False,
        timeout_s: float = 60.0,
        use_clip_cache: bool = True,
    ) -> None:
        base_cache = Path(
            cache_dir
            if cache_dir is not None
            else os.environ.get("GRIDFORGE_CACHE_DIR", "/tmp/gridforge-cache")
        )
        self.cache_dir: Path = base_cache / "copernicus"
        self.allow_ocean_fallback = allow_ocean_fallback
        self.timeout_s = timeout_s
        self.use_clip_cache = use_clip_cache

    @property
    def tile_list_cache_path(self) -> Path:
        return self.cache_dir / "tileList.txt"

    def load_tile_list(self) -> set[str]:
        """Load the Copernicus GLO-30 tile manifest from disk cache or download it."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = self.tile_list_cache_path
        if cache_path.is_file() and cache_path.stat().st_size > 0:
            return parse_tile_list(cache_path.read_text(encoding="utf-8"))

        logger.info("Downloading Copernicus GLO-30 tileList.txt to %s", cache_path)
        with httpx.Client(
            timeout=self.timeout_s,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            resp = client.get(TILE_LIST_URL)
            resp.raise_for_status()
            text = resp.text

        tmp_path = cache_path.with_suffix(".txt.tmp")
        tmp_path.write_text(text, encoding="utf-8")
        tmp_path.replace(cache_path)
        return parse_tile_list(text)

    def _gdal_env(self) -> dict[str, str]:
        env = os.environ.copy()
        for key, value in GDAL_VSICURL_ENV.items():
            env.setdefault(key, value)
        return env

    def fetch_dem(self, layout: SheetLayout, work_dir: Path) -> Path:
        """Stream and clip Copernicus GLO-30 elevation data for ``layout.wgs84_fetch_bbox``."""
        work_dir.mkdir(parents=True, exist_ok=True)
        clip_bbox = _round_enclosing_bbox(layout.wgs84_fetch_bbox, step_deg=0.001)
        out_tif = work_dir / "dem_raw.tif"

        clip_cache_path = self.cache_dir / "clips" / f"glo30_{_clip_cache_key(clip_bbox)}.tif"
        if self.use_clip_cache and clip_cache_path.is_file() and clip_cache_path.stat().st_size > 0:
            logger.info("Using cached Copernicus GLO-30 clip: %s", clip_cache_path)
            shutil.copy2(clip_cache_path, out_tif)
            return out_tif

        candidates = tiles_for_bbox(clip_bbox)
        available = self.load_tile_list()
        matching = filter_available_tiles(candidates, available)

        if not matching:
            if self.allow_ocean_fallback:
                logger.warning(
                    "No Copernicus GLO-30 land tiles intersect %s; writing 0 m ocean fallback",
                    clip_bbox.as_tuple(),
                )
                return _write_zero_fallback_geotiff(clip_bbox, out_tif)
            raise RuntimeError(
                f"No Copernicus GLO-30 land tiles intersect requested footprint "
                f"{clip_bbox.as_tuple()} (checked {len(candidates)} candidate tile(s))"
            )

        vsicurl_paths = [f"/vsicurl/{tile_url(tile)}" for tile in matching]
        vrt_path = work_dir / "copernicus_mosaic.vrt"
        env = self._gdal_env()

        logger.info("Building /vsicurl/ VRT for %d Copernicus GLO-30 tile(s): %s", len(matching), matching)
        if vrt_path.exists():
            vrt_path.unlink()
        vrt_cmd = ["gdalbuildvrt", str(vrt_path), *vsicurl_paths]
        _run_subprocess(vrt_cmd, env=env, context="gdalbuildvrt (Copernicus GLO-30)")

        # GDAL -projwin expects: ulx uly lrx lry = min_lon max_lat max_lon min_lat
        translate_cmd = [
            "gdal_translate",
            "-of",
            "GTiff",
            "-ot",
            "Float32",
            "-co",
            "COMPRESS=DEFLATE",
            "-co",
            "TILED=YES",
            "-projwin",
            f"{clip_bbox.min_x:.6f}",
            f"{clip_bbox.max_y:.6f}",
            f"{clip_bbox.max_x:.6f}",
            f"{clip_bbox.min_y:.6f}",
            "-projwin_srs",
            "EPSG:4326",
            str(vrt_path),
            str(out_tif),
        ]
        _run_subprocess(translate_cmd, env=env, context="gdal_translate (Copernicus GLO-30 clip)")

        if self.use_clip_cache and out_tif.is_file() and out_tif.stat().st_size > 0:
            clip_cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_cache = clip_cache_path.with_suffix(".tif.tmp")
            shutil.copy2(out_tif, tmp_cache)
            tmp_cache.replace(clip_cache_path)

        return out_tif


def _run_subprocess(cmd: list[str], env: dict[str, str], context: str) -> None:
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"{context} failed (exit {proc.returncode}):\n"
            f"cmd: {' '.join(cmd)}\n"
            f"stderr: {proc.stderr.strip()}"
        )
