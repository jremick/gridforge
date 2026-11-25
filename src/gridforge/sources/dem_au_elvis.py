"""Cloud-native Australia-wide elevation source (GA National 5 m LiDAR WCS + DEA SRTM 1s COG)."""

from __future__ import annotations

from array import array
import hashlib
import logging
import math
import os
import shutil
import subprocess
from pathlib import Path
from typing import Final, Literal

import httpx

from gridforge.config import BBox, SheetLayout
from gridforge.sources.dem_copernicus import GDAL_VSICURL_ENV

logger = logging.getLogger(__name__)

GA_5M_LIDAR_WCS_URL: Final[str] = (
    "https://services.ga.gov.au/gis/services/DEM_LiDAR_5m_2025/MapServer/WCSServer"
)
DEA_SRTM_1S_SMOOTH_COG_URL: Final[str] = (
    "https://dea-public-data.s3.ap-southeast-2.amazonaws.com/"
    "projects/elevation/ga_srtm_dem1sv1_0/dems1sv1_0.tif"
)
DEA_SRTM_1S_HYDRO_COG_URL: Final[str] = (
    "https://dea-public-data.s3.ap-southeast-2.amazonaws.com/"
    "projects/elevation/ga_srtm_dem1sv1_0/demh1sv1_0.tif"
)
GA_SRTM_1S_WCS_URL: Final[str] = (
    "https://services.ga.gov.au/gis/services/"
    "DEM_SRTM_1Second_Hydro_Enforced_2024/MapServer/WCSServer"
)
USER_AGENT: Final[str] = "gridforge/0.2 (+https://github.com/gridforge/gridforge)"

# Mainland Australia + Tasmania + near islands across UTM Zones 49S–56S (EPSG:32749–32756)
AU_COVERAGE_BBOX: Final[tuple[float, float, float, float]] = (112.5, -44.5, 154.5, -9.5)


def _bboxes_intersect(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> bool:
    return not (a[2] <= b[0] or a[0] >= b[2] or a[3] <= b[1] or a[1] >= b[3])


def is_in_australia_coverage(bbox: BBox) -> bool:
    """Return ``True`` if a WGS84 bounding box overlaps Australian GA DEM coverage bounds."""
    return _bboxes_intersect(bbox.as_tuple(), AU_COVERAGE_BBOX)


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


def wcs_resolution_for_scale(scale: int) -> tuple[float, float, str]:
    """Choose ``(resx_deg, resy_deg, tag)`` for GA 5 m WCS keeping A3 1:50k transfers < 20 MB."""
    if scale <= 25000:
        return (0.000055, 0.000052, "5m")
    return (0.000110, 0.000104, "10m")


def _inspect_raster_coverage(
    tif_path: Path,
    *,
    cliff_threshold_m: float = 15.0,
) -> tuple[float, float, float, float, float]:
    """Return ``(valid_frac, exact_zero_frac, zero_cliff_ratio, min_elev, max_elev)`` for ``tif_path``.

    Distinguishes real coastal/estuary sea-level water (0.0 m adjacent to low shoreline,
    e.g. Swan River in Perth or Derwent in Hobart where ``zero_cliff_ratio`` is ~2–6%)
    from GA WCS missing-LiDAR masks (0.0 m directly abutting inland >15 m ridges where
    ``zero_cliff_ratio`` is ~95–100%).
    """
    from osgeo import gdal  # type: ignore[import-untyped]

    gdal.UseExceptions()
    ds = gdal.Open(str(tif_path), gdal.GA_ReadOnly)
    if ds is None:
        return (0.0, 1.0, 1.0, 0.0, 0.0)

    try:
        cols, rows = ds.RasterXSize, ds.RasterYSize
        if cols <= 0 or rows <= 0:
            return (0.0, 1.0, 1.0, 0.0, 0.0)
        buf_x = min(cols, 256)
        buf_y = min(rows, 256)
        band = ds.GetRasterBand(1)
        nodata = band.GetNoDataValue()
        raw = band.ReadRaster(0, 0, cols, rows, buf_x, buf_y, gdal.GDT_Float32)
        if not raw:
            return (0.0, 1.0, 1.0, 0.0, 0.0)

        vals = array("f")
        vals.frombytes(raw)
        total = len(vals)
        if total == 0:
            return (0.0, 1.0, 1.0, 0.0, 0.0)

        valid = 0
        exact_zero = 0
        min_v = math.inf
        max_v = -math.inf
        for v in vals:
            if not math.isfinite(v):
                continue
            if nodata is not None and abs(v - nodata) < 1e-3:
                continue
            if v < -500.0 or v > 5000.0:
                continue
            valid += 1
            if v == 0.0:
                exact_zero += 1
            if v < min_v:
                min_v = v
            if v > max_v:
                max_v = v

        if valid == 0:
            return (0.0, 1.0, 1.0, 0.0, 0.0)

        # Measure fraction of 0.0 -> non-zero transitions that jump above cliff_threshold_m
        zero_cliff_ratio = 0.0
        if exact_zero > 0 and exact_zero < valid:
            b_edges = 0
            c_edges = 0
            for r in range(buf_y):
                row_off = r * buf_x
                for c in range(buf_x):
                    if vals[row_off + c] != 0.0:
                        continue
                    for nr, nc in ((r - 1, c), (r + 1, c), (r, c - 1), (r, c + 1)):
                        if 0 <= nr < buf_y and 0 <= nc < buf_x:
                            nv = vals[nr * buf_x + nc]
                            if nv != 0.0 and math.isfinite(nv) and -500.0 <= nv <= 5000.0:
                                b_edges += 1
                                if nv > cliff_threshold_m:
                                    c_edges += 1
            if b_edges > 0:
                zero_cliff_ratio = c_edges / b_edges
        elif exact_zero == valid:
            zero_cliff_ratio = 1.0

        return (valid / total, exact_zero / total, zero_cliff_ratio, float(min_v), float(max_v))
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


class AUElvisDEMSource:
    """On-demand Australia-wide elevation source (GA 5 m LiDAR WCS -> DEA SRTM 1s COG).

    Never mixes Tier 1 (5 m LiDAR) and Tier 2 (SRTM 1s) pixels on the same sheet.
    Vertical datum is Australian Height Datum (``AHD``).
    """

    name: str = "au-elvis"
    vertical_datum: str = "AHD"
    attribution: str = "Elevation data: © Commonwealth of Australia (Geoscience Australia) (CC BY 4.0)"

    def __init__(
        self,
        cache_dir: Path | str | None = None,
        *,
        prefer_5m: bool = True,
        srtm_product: Literal["smoothed", "hydro"] = "smoothed",
        max_exact_zero_ratio: float = 0.05,
        min_valid_ratio: float = 0.95,
        timeout_s: float = 30.0,
        use_clip_cache: bool = True,
        client: httpx.Client | None = None,
    ) -> None:
        base_cache = Path(
            cache_dir
            if cache_dir is not None
            else os.environ.get("GRIDFORGE_CACHE_DIR", "/tmp/gridforge-cache")
        )
        self.cache_dir: Path = base_cache / "au_elvis"
        self.prefer_5m = prefer_5m
        self.srtm_product = srtm_product
        self.max_exact_zero_ratio = max_exact_zero_ratio
        self.min_valid_ratio = min_valid_ratio
        self.timeout_s = timeout_s
        self.use_clip_cache = use_clip_cache
        self._injected_client = client

        self.resolved_tier: str = "srtm-1s"
        self.resolved_source_name: str = self.name
        self.resolved_vertical_datum: str = self.vertical_datum
        self.resolved_attribution: str = self.attribution

    def _gdal_env(self) -> dict[str, str]:
        env = os.environ.copy()
        for key, value in GDAL_VSICURL_ENV.items():
            env.setdefault(key, value)
        return env

    def _srtm_cog_url(self) -> str:
        if self.srtm_product == "hydro":
            return DEA_SRTM_1S_HYDRO_COG_URL
        return DEA_SRTM_1S_SMOOTH_COG_URL

    def _http_get_bytes(self, url: str, params: dict[str, str]) -> tuple[int, str, bytes]:
        owns_client = self._injected_client is None
        client = (
            self._injected_client
            if self._injected_client is not None
            else httpx.Client(
                timeout=httpx.Timeout(self.timeout_s, connect=min(10.0, self.timeout_s)),
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT},
            )
        )
        try:
            resp = client.get(url, params=params, headers={"User-Agent": USER_AGENT})
            return (resp.status_code, resp.headers.get("content-type", ""), resp.content)
        finally:
            if owns_client:
                client.close()

    def _fetch_5m_lidar_wcs(
        self,
        layout: SheetLayout,
        clip_bbox: BBox,
        work_dir: Path,
        out_tif: Path,
    ) -> bool:
        resx, resy, res_tag = wcs_resolution_for_scale(layout.spec.scale)
        key = _bbox_hash(clip_bbox)
        cache_path = self.cache_dir / "clips" / f"au_5m_{res_tag}_{key}.tif"
        if self.use_clip_cache and cache_path.is_file() and cache_path.stat().st_size > 0:
            logger.info("Using cached GA 5m LiDAR clip: %s", cache_path)
            shutil.copy2(cache_path, out_tif)
            self.resolved_tier = "5m-lidar"
            return True

        params = {
            "SERVICE": "WCS",
            "VERSION": "1.0.0",
            "REQUEST": "GetCoverage",
            "COVERAGE": "1",
            "CRS": "EPSG:4283",
            "BBOX": f"{clip_bbox.min_x:.6f},{clip_bbox.min_y:.6f},{clip_bbox.max_x:.6f},{clip_bbox.max_y:.6f}",
            "RESX": f"{resx:.6f}",
            "RESY": f"{resy:.6f}",
            "FORMAT": "GeoTIFF",
        }
        try:
            status, content_type, body = self._http_get_bytes(GA_5M_LIDAR_WCS_URL, params)
        except Exception as exc:
            logger.warning("GA 5m LiDAR WCS request failed (%s); falling back to SRTM 1s", exc)
            return False

        if status != 200 or "xml" in content_type.lower() or "html" in content_type.lower() or len(body) < 2048:
            logger.info(
                "GA 5m LiDAR WCS returned non-DEM payload (status=%s, ct=%s, bytes=%d); falling back to SRTM 1s",
                status,
                content_type,
                len(body),
            )
            return False

        tmp_5m = work_dir / "dem_au_5m_tmp.tif"
        tmp_5m.write_bytes(body)

        try:
            valid_frac, zero_frac, cliff_ratio, min_v, max_v = _inspect_raster_coverage(tmp_5m)
        except Exception as exc:
            logger.warning("GA 5m LiDAR GeoTIFF unreadable (%s); falling back to SRTM 1s", exc)
            tmp_5m.unlink(missing_ok=True)
            return False

        # Accept complete 5m LiDAR (including coastal/estuary water at 0.0m with low shoreline
        # cliff ratio <= 15%), while rejecting empty all-0.0 clips and inland fringe masks where
        # 0.0m pixels abut steep inland terrain (cliff_ratio ~95-100%).
        coastal_water_ok = zero_frac <= 0.45 and cliff_ratio <= 0.15
        if (
            valid_frac < self.min_valid_ratio
            or max_v <= min_v + 0.1
            or (zero_frac > self.max_exact_zero_ratio and not coastal_water_ok)
        ):
            logger.info(
                "GA 5m LiDAR incomplete/absent (valid=%.1f%%, exact_zero=%.1f%%, cliff_ratio=%.1f%%, "
                "range=[%.1f, %.1f]); falling back cleanly to Australia SRTM 1s",
                valid_frac * 100.0,
                zero_frac * 100.0,
                cliff_ratio * 100.0,
                min_v,
                max_v,
            )
            tmp_5m.unlink(missing_ok=True)
            return False

        tmp_5m.replace(out_tif)
        self.resolved_tier = "5m-lidar"
        if self.use_clip_cache and out_tif.is_file() and out_tif.stat().st_size > 0:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_cache = cache_path.with_suffix(".tif.tmp")
            shutil.copy2(out_tif, tmp_cache)
            tmp_cache.replace(cache_path)
        return True

    def _fetch_srtm_1s_wcs_backup(self, clip_bbox: BBox, out_tif: Path) -> bool:
        params = {
            "SERVICE": "WCS",
            "VERSION": "1.0.0",
            "REQUEST": "GetCoverage",
            "COVERAGE": "1",
            "CRS": "EPSG:4326",
            "BBOX": f"{clip_bbox.min_x:.6f},{clip_bbox.min_y:.6f},{clip_bbox.max_x:.6f},{clip_bbox.max_y:.6f}",
            "RESX": "0.000277778",
            "RESY": "0.000277778",
            "FORMAT": "GeoTIFF",
        }
        try:
            status, content_type, body = self._http_get_bytes(GA_SRTM_1S_WCS_URL, params)
        except Exception as exc:
            logger.warning("GA SRTM 1s backup WCS failed: %s", exc)
            return False

        if status != 200 or "xml" in content_type.lower() or "html" in content_type.lower() or len(body) < 1024:
            return False

        out_tif.write_bytes(body)
        return True

    def _fetch_srtm_1s(self, clip_bbox: BBox, out_tif: Path) -> Path:
        key = _bbox_hash(clip_bbox)
        cache_path = self.cache_dir / "clips" / f"au_srtm1s_{self.srtm_product}_{key}.tif"
        if self.use_clip_cache and cache_path.is_file() and cache_path.stat().st_size > 0:
            logger.info("Using cached GA SRTM 1s clip: %s", cache_path)
            shutil.copy2(cache_path, out_tif)
            self.resolved_tier = "srtm-1s"
            return out_tif

        vsicurl_cog = f"/vsicurl/{self._srtm_cog_url()}"
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
            vsicurl_cog,
            str(out_tif),
        ]
        logger.info("Streaming DEA GA SRTM 1s COG (%s) for %s", self.srtm_product, clip_bbox.as_tuple())
        try:
            _run_subprocess(cmd, env=self._gdal_env(), context="gdal_translate (DEA GA SRTM 1s COG)")
        except RuntimeError as exc:
            logger.warning("DEA SRTM 1s COG stream failed (%s); trying GA SRTM 2024 WCS backup", exc)
            if not self._fetch_srtm_1s_wcs_backup(clip_bbox, out_tif):
                raise RuntimeError(
                    f"Both DEA SRTM 1s COG and GA SRTM 2024 WCS backup failed for {clip_bbox.as_tuple()}"
                ) from exc

        valid_frac, _zero_frac, _cliff_ratio, _min_v, _max_v = _inspect_raster_coverage(out_tif)
        if valid_frac < 0.05:
            out_tif.unlink(missing_ok=True)
            raise RuntimeError(
                f"Outside Australian DEM coverage (valid pixels: {valid_frac * 100.0:.1f}% "
                f"for {clip_bbox.as_tuple()})"
            )

        self.resolved_tier = "srtm-1s"
        if self.use_clip_cache and out_tif.is_file() and out_tif.stat().st_size > 0:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_cache = cache_path.with_suffix(".tif.tmp")
            shutil.copy2(out_tif, tmp_cache)
            tmp_cache.replace(cache_path)
        return out_tif

    def fetch_dem(self, layout: SheetLayout, work_dir: Path) -> Path:
        """Fetch GA 5 m LiDAR (where complete) or DEA GA SRTM 1s DEM for ``layout``."""
        work_dir.mkdir(parents=True, exist_ok=True)
        clip_bbox = _round_enclosing_bbox(layout.wgs84_fetch_bbox, step_deg=0.001)
        out_tif = work_dir / "dem_raw.tif"

        if not is_in_australia_coverage(clip_bbox):
            raise RuntimeError(
                f"Outside Australian DEM coverage for requested footprint {clip_bbox.as_tuple()}"
            )

        if self.prefer_5m and self._fetch_5m_lidar_wcs(layout, clip_bbox, work_dir, out_tif):
            return out_tif

        return self._fetch_srtm_1s(clip_bbox, out_tif)
