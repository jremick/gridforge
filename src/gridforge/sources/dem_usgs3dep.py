"""Cloud-native USGS 3DEP (1 m LiDAR + 10 m seamless NAVD88) and Auto DEM sources."""

from __future__ import annotations

from array import array
import hashlib
import json
import logging
import math
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Final

import httpx

from gridforge.config import BBox, SheetLayout
from gridforge.sources.dem_copernicus import (
    CopernicusGLO30Source,
    GDAL_VSICURL_ENV,
)

logger = logging.getLogger(__name__)

TNM_ACCESS_API_URL: Final[str] = "https://tnmaccess.nationalmap.gov/api/v1/products"
USGS_1M_DATASET_NAME: Final[str] = "Digital Elevation Model (DEM) 1 meter"
USGS_10M_SEAMLESS_VRT_URL: Final[str] = (
    "https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/13/TIFF/USGS_Seamless_DEM_13.vrt"
)
USER_AGENT: Final[str] = "gridforge/0.2 (+https://github.com/gridforge/gridforge)"

# Coarse bounding boxes (min_lon, min_lat, max_lon, max_lat) for US 3DEP coverage regions:
# CONUS, Alaska (+ eastern Aleutian anti-meridian tail), Hawaii, PR/USVI, Guam/CNMI, American Samoa.
US_3DEP_COVERAGE_BOXES: Final[tuple[tuple[float, float, float, float], ...]] = (
    (-125.5, 24.0, -66.5, 49.6),     # CONUS
    (-179.9, 51.0, -129.5, 71.6),    # Alaska main + western Aleutians
    (172.0, 51.0, 180.0, 55.5),      # Alaska eastern Aleutians
    (-161.0, 18.5, -154.5, 22.5),    # Hawaii
    (-68.0, 17.5, -64.4, 18.7),      # Puerto Rico & USVI
    (144.5, 13.1, 146.1, 20.6),      # Guam & CNMI
    (-171.2, -14.6, -169.3, -14.0),  # American Samoa
)


def _bboxes_intersect(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    return not (a[2] <= b[0] or a[0] >= b[2] or a[3] <= b[1] or a[1] >= b[3])


def is_in_us_3dep_coverage(bbox: BBox) -> bool:
    """Return ``True`` if a WGS84 bounding box overlaps US 3DEP coverage bounds."""
    target = bbox.as_tuple()
    return any(_bboxes_intersect(target, box) for box in US_3DEP_COVERAGE_BOXES)


def _round_enclosing_bbox(bbox: BBox, step_deg: float = 0.001) -> BBox:
    inv = 1.0 / step_deg
    return BBox(
        min_x=math.floor(bbox.min_x * inv) / inv,
        min_y=math.floor(bbox.min_y * inv) / inv,
        max_x=math.ceil(bbox.max_x * inv) / inv,
        max_y=math.ceil(bbox.max_y * inv) / inv,
    )


def _bbox_hash(bbox: BBox) -> str:
    payload = f"{bbox.min_x:.3f},{bbox.min_y:.3f},{bbox.max_x:.3f},{bbox.max_y:.3f}"
    return hashlib.sha256(payload.encode("ascii")).hexdigest()[:24]


def select_1m_tile_urls(
    tnm_payload: dict[str, Any],
    bbox: BBox,
    max_tiles: int = 24,
) -> list[str]:
    """Extract, deduplicate, and date-order USGS 3DEP 1 m COG URLs intersecting ``bbox``.

    Returns URLs ordered oldest -> newest so ``gdalwarp`` overlays newer LiDAR projects
    on top of older overlapping surveys while preserving complementary partial projects
    within the same 10 km cell (e.g., adjacent National Forest and municipal LiDAR blocks).
    """
    items = tnm_payload.get("items") or []
    target = bbox.as_tuple()
    seen_urls: dict[str, str] = {}

    for item in items:
        if not isinstance(item, dict):
            continue
        url = str(item.get("downloadURL") or "").strip()
        if not url or not url.lower().startswith(("https://", "http://")):
            continue
        if not url.lower().endswith((".tif", ".tiff")):
            continue

        item_bbox_raw = item.get("boundingBox")
        if isinstance(item_bbox_raw, dict):
            try:
                ib = (
                    float(item_bbox_raw["minX"]),
                    float(item_bbox_raw["minY"]),
                    float(item_bbox_raw["maxX"]),
                    float(item_bbox_raw["maxY"]),
                )
            except (KeyError, TypeError, ValueError):
                ib = None
            if ib is not None and not _bboxes_intersect(target, ib):
                continue

        sort_date = str(item.get("publicationDate") or item.get("lastUpdated") or "")
        if url not in seen_urls or sort_date > seen_urls[url]:
            seen_urls[url] = sort_date

    # Sort ascending by publication date so newer surveys overwrite older ones in gdalwarp
    ordered = sorted(seen_urls.items(), key=lambda row: (row[1], row[0]))
    if len(ordered) > max_tiles:
        ordered = ordered[-max_tiles:]
    return [url for url, _ in ordered]


def _raster_valid_pixel_fraction(tif_path: Path) -> float:
    """Compute the fraction of finite non-nodata elevation pixels in ``tif_path``."""
    from osgeo import gdal  # type: ignore[import-untyped]

    gdal.UseExceptions()
    ds = gdal.Open(str(tif_path), gdal.GA_ReadOnly)
    if ds is None:
        return 0.0

    try:
        cols, rows = ds.RasterXSize, ds.RasterYSize
        if cols <= 0 or rows <= 0:
            return 0.0
        # Subsample large rasters to <= 512x512 for fast coverage check
        buf_x = min(cols, 512)
        buf_y = min(rows, 512)
        band = ds.GetRasterBand(1)
        nodata = band.GetNoDataValue()
        raw = band.ReadRaster(0, 0, cols, rows, buf_x, buf_y, gdal.GDT_Float32)
        if not raw:
            return 0.0

        vals = array("f")
        vals.frombytes(raw)
        total = len(vals)
        if total == 0:
            return 0.0

        valid = 0
        for v in vals:
            if not math.isfinite(v):
                continue
            if nodata is not None and abs(v - nodata) < 1e-3:
                continue
            if v < -500.0 or v > 9000.0:
                continue
            valid += 1
        return valid / total
    finally:
        ds = None


def _run_subprocess(cmd: list[str], env: dict[str, str], context: str) -> None:
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"{context} failed (exit {proc.returncode}):\n"
            f"cmd: {' '.join(cmd)}\n"
            f"stderr: {proc.stderr.strip()}"
        )


class USGS3DEPSource:
    """On-demand USGS 3DEP elevation source (1 m LiDAR COGs with seamless 10 m NAVD88 fallback).

    Never mixes NAVD88 (USGS 3DEP) and EGM2008 (Copernicus GLO-30) on the same sheet.
    """

    name: str = "usgs-3dep"
    vertical_datum: str = "NAVD88"
    attribution: str = "Elevation data: U.S. Geological Survey 3DEP (Public Domain)"

    def __init__(
        self,
        cache_dir: Path | str | None = None,
        *,
        prefer_1m: bool = True,
        lidar_warp_res_m: float = 2.0,
        min_1m_coverage_ratio: float = 0.95,
        timeout_s: float = 30.0,
        use_clip_cache: bool = True,
        client: httpx.Client | None = None,
    ) -> None:
        base_cache = Path(
            cache_dir
            if cache_dir is not None
            else os.environ.get("GRIDFORGE_CACHE_DIR", "/tmp/gridforge-cache")
        )
        self.cache_dir: Path = base_cache / "usgs3dep"
        self.prefer_1m = prefer_1m
        self.lidar_warp_res_m = lidar_warp_res_m
        self.min_1m_coverage_ratio = min_1m_coverage_ratio
        self.timeout_s = timeout_s
        self.use_clip_cache = use_clip_cache
        self._injected_client = client

        self.resolved_tier: str = "10m"
        self.resolved_source_name: str = self.name
        self.resolved_vertical_datum: str = self.vertical_datum
        self.resolved_attribution: str = self.attribution

    def _gdal_env(self) -> dict[str, str]:
        env = os.environ.copy()
        for key, value in GDAL_VSICURL_ENV.items():
            env.setdefault(key, value)
        return env

    def _query_tnm_1m_products(self, clip_bbox: BBox) -> dict[str, Any]:
        key = _bbox_hash(clip_bbox)
        query_cache = self.cache_dir / "tnm_queries" / f"1m_{key}.json"
        if query_cache.is_file() and query_cache.stat().st_size > 0:
            try:
                return json.loads(query_cache.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                logger.warning("Ignoring corrupt cached TNM JSON: %s", query_cache)

        params = {
            "datasets": USGS_1M_DATASET_NAME,
            "bbox": f"{clip_bbox.min_x:.6f},{clip_bbox.min_y:.6f},{clip_bbox.max_x:.6f},{clip_bbox.max_y:.6f}",
            "prodFormats": "GeoTIFF",
            "outputFormat": "JSON",
            "max": "50",
        }
        owns_client = self._injected_client is None
        client = (
            self._injected_client
            if self._injected_client is not None
            else httpx.Client(
                timeout=self.timeout_s,
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT},
            )
        )
        try:
            resp = client.get(TNM_ACCESS_API_URL, params=params, headers={"User-Agent": USER_AGENT})
            resp.raise_for_status()
            payload = resp.json()
        finally:
            if owns_client:
                client.close()

        query_cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = query_cache.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(query_cache)
        return payload

    def _fetch_1m_lidar(self, layout: SheetLayout, clip_bbox: BBox, work_dir: Path, out_tif: Path) -> bool:
        key = _bbox_hash(clip_bbox)
        cache_path = self.cache_dir / "clips" / f"3dep_1m_{layout.utm_epsg}_{key}.tif"
        if self.use_clip_cache and cache_path.is_file() and cache_path.stat().st_size > 0:
            logger.info("Using cached USGS 3DEP 1m clip: %s", cache_path)
            shutil.copy2(cache_path, out_tif)
            self.resolved_tier = "1m"
            return True

        try:
            payload = self._query_tnm_1m_products(clip_bbox)
        except Exception as exc:
            logger.warning("USGS TNM 1m discovery failed (%s); falling back to 10m seamless VRT", exc)
            return False

        tile_urls = select_1m_tile_urls(payload, clip_bbox)
        if not tile_urls:
            logger.info("No USGS 3DEP 1m COGs cover %s; falling back to 10m seamless VRT", clip_bbox.as_tuple())
            return False

        vsicurl_inputs = [f"/vsicurl/{u}" for u in tile_urls]
        tmp_1m = work_dir / "dem_3dep_1m_tmp.tif"
        b = layout.utm_fetch_bbox
        cmd = [
            "gdalwarp",
            "-overwrite",
            "-t_srs",
            f"EPSG:{layout.utm_epsg}",
            "-te",
            f"{b.min_x}",
            f"{b.min_y}",
            f"{b.max_x}",
            f"{b.max_y}",
            "-tr",
            f"{self.lidar_warp_res_m}",
            f"{self.lidar_warp_res_m}",
            "-r",
            "bilinear",
            "-dstnodata",
            "-9999",
            "-ot",
            "Float32",
            "-co",
            "COMPRESS=DEFLATE",
            "-co",
            "TILED=YES",
            *vsicurl_inputs,
            str(tmp_1m),
        ]
        logger.info(
            "Streaming %d USGS 3DEP 1m LiDAR COG(s) at %.1f m resolution -> %s",
            len(tile_urls),
            self.lidar_warp_res_m,
            out_tif,
        )
        try:
            _run_subprocess(cmd, env=self._gdal_env(), context="gdalwarp (USGS 3DEP 1m)")
        except RuntimeError as exc:
            logger.warning("USGS 3DEP 1m stream failed (%s); falling back to 10m seamless VRT", exc)
            if tmp_1m.exists():
                tmp_1m.unlink()
            return False

        coverage = _raster_valid_pixel_fraction(tmp_1m)
        if coverage < self.min_1m_coverage_ratio:
            logger.warning(
                "USGS 3DEP 1m LiDAR coverage incomplete (%.1f%% < %.1f%%); falling back to 10m NAVD88 VRT",
                coverage * 100.0,
                self.min_1m_coverage_ratio * 100.0,
            )
            tmp_1m.unlink(missing_ok=True)
            return False

        tmp_1m.replace(out_tif)
        self.resolved_tier = "1m"
        if self.use_clip_cache and out_tif.is_file() and out_tif.stat().st_size > 0:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_cache = cache_path.with_suffix(".tif.tmp")
            shutil.copy2(out_tif, tmp_cache)
            tmp_cache.replace(cache_path)
        return True

    def _fetch_10m_seamless(self, clip_bbox: BBox, out_tif: Path) -> Path:
        key = _bbox_hash(clip_bbox)
        cache_path = self.cache_dir / "clips" / f"3dep_10m_{key}.tif"
        if self.use_clip_cache and cache_path.is_file() and cache_path.stat().st_size > 0:
            logger.info("Using cached USGS 3DEP 10m clip: %s", cache_path)
            shutil.copy2(cache_path, out_tif)
            self.resolved_tier = "10m"
            return out_tif

        vsicurl_vrt = f"/vsicurl/{USGS_10M_SEAMLESS_VRT_URL}"
        cmd = [
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
            vsicurl_vrt,
            str(out_tif),
        ]
        logger.info("Streaming USGS 3DEP 10m seamless NAVD88 VRT for %s", clip_bbox.as_tuple())
        _run_subprocess(cmd, env=self._gdal_env(), context="gdal_translate (USGS 3DEP 10m seamless)")

        coverage = _raster_valid_pixel_fraction(out_tif)
        if coverage < 0.05:
            out_tif.unlink(missing_ok=True)
            raise RuntimeError(
                f"Outside USGS 3DEP coverage (valid elevation pixels: {coverage * 100.0:.1f}% "
                f"for {clip_bbox.as_tuple()})"
            )

        self.resolved_tier = "10m"
        if self.use_clip_cache and out_tif.is_file() and out_tif.stat().st_size > 0:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_cache = cache_path.with_suffix(".tif.tmp")
            shutil.copy2(out_tif, tmp_cache)
            tmp_cache.replace(cache_path)
        return out_tif

    def fetch_dem(self, layout: SheetLayout, work_dir: Path) -> Path:
        """Fetch USGS 3DEP 1 m LiDAR (preferred) or 10 m seamless NAVD88 DEM for ``layout``."""
        work_dir.mkdir(parents=True, exist_ok=True)
        clip_bbox = _round_enclosing_bbox(layout.wgs84_fetch_bbox, step_deg=0.001)
        out_tif = work_dir / "dem_raw.tif"

        if not is_in_us_3dep_coverage(clip_bbox):
            raise RuntimeError(
                f"Outside USGS 3DEP coverage for requested footprint {clip_bbox.as_tuple()}"
            )

        if self.prefer_1m and self._fetch_1m_lidar(layout, clip_bbox, work_dir, out_tif):
            return out_tif

        return self._fetch_10m_seamless(clip_bbox, out_tif)


class AutoDEMSource:
    """Automatic DEM router: USGS 3DEP (NAVD88) inside US coverage, Copernicus GLO-30 (EGM2008) globally."""

    name: str = "auto-dem"
    attribution: str = CopernicusGLO30Source.attribution

    def __init__(
        self,
        cache_dir: Path | str | None = None,
        *,
        usgs_source: USGS3DEPSource | None = None,
        copernicus_source: CopernicusGLO30Source | None = None,
    ) -> None:
        self.usgs_source = usgs_source or USGS3DEPSource(cache_dir=cache_dir)
        self.copernicus_source = copernicus_source or CopernicusGLO30Source(cache_dir=cache_dir)
        self.resolved_source_name: str = self.copernicus_source.name
        self.resolved_vertical_datum: str = "EGM2008"
        self.resolved_attribution: str = self.copernicus_source.attribution
        self.attribution = self.resolved_attribution

    def fetch_dem(self, layout: SheetLayout, work_dir: Path) -> Path:
        """Select USGS 3DEP for US sheets (falling back cleanly to Copernicus GLO-30 globally)."""
        if is_in_us_3dep_coverage(layout.wgs84_fetch_bbox):
            try:
                path = self.usgs_source.fetch_dem(layout, work_dir)
            except Exception as exc:
                logger.warning(
                    "USGS 3DEP fetch unavailable for %s (%s); falling back to Copernicus GLO-30",
                    layout.wgs84_fetch_bbox.as_tuple(),
                    exc,
                )
            else:
                self.resolved_source_name = self.usgs_source.name
                self.resolved_vertical_datum = self.usgs_source.vertical_datum
                self.resolved_attribution = self.usgs_source.attribution
                self.attribution = self.resolved_attribution
                return path

        path = self.copernicus_source.fetch_dem(layout, work_dir)
        self.resolved_source_name = self.copernicus_source.name
        self.resolved_vertical_datum = "EGM2008"
        self.resolved_attribution = self.copernicus_source.attribution
        self.attribution = self.resolved_attribution
        return path
