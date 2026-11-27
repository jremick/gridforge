"""Cloud-native Australia-wide elevation source (QLD 2 m LiDAR -> GA 5 m LiDAR WCS -> DEA SRTM 1s COG)."""

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
from typing import Final, Literal

import httpx
import numpy as np

from gridforge.config import BBox, SheetLayout
from gridforge.sources.dem_copernicus import GDAL_VSICURL_ENV
from gridforge.subprocess_env import build_safe_subprocess_env

logger = logging.getLogger(__name__)

MAX_AU_DEM_DOWNLOAD_BYTES: Final[int] = 64 * 1024 * 1024  # 64 MiB hard disk-stream cap
MAX_AU_JSON_DOWNLOAD_BYTES: Final[int] = 4 * 1024 * 1024  # 4 MiB JSON response cap

# Tier 0: Queensland Spatial Imagery 2 m LiDAR DEM (CC BY 4.0, AHD), gated by completed LiDAR extents
QLD_LIDAR_EXTENTS_URL: Final[str] = (
    "https://spatial-gis.information.qld.gov.au/arcgis/rest/services/"
    "Elevation/ElevationProjectExtents/MapServer/200/query"
)
QLD_DEM_IMAGESERVER_URL: Final[str] = (
    "https://spatial-img.information.qld.gov.au/arcgis/rest/services/"
    "Elevation/QldDem/ImageServer/exportImage"
)
QLD_LIDAR_COVERAGE_BBOX: Final[tuple[float, float, float, float]] = (138.0, -29.3, 154.0, -10.5)
QLD_LIDAR_ATTRIBUTION: Final[str] = (
    "Elevation data: © State of Queensland (Department of Resources) QldDem LiDAR (CC BY 4.0)"
)
QLD_MAX_EXPORT_PX: Final[int] = 2048
QLD_MAX_EXPORT_WIDTH_PX: Final[int] = 4096
QLD_MAX_EXPORT_HEIGHT_PX: Final[int] = 2048

# Tier 1 & 2: Geoscience Australia National 5 m LiDAR WCS -> DEA SRTM 1s COG (CC BY 4.0, AHD)
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


def is_in_qld_lidar_coverage(bbox: BBox) -> bool:
    """Return ``True`` if a WGS84 bounding box overlaps Queensland state LiDAR bounds."""
    return _bboxes_intersect(bbox.as_tuple(), QLD_LIDAR_COVERAGE_BBOX)


def qld_export_size_for_bbox(
    bbox: BBox,
    *,
    target_res_m: float = 2.0,
    max_px: int = QLD_MAX_EXPORT_PX,
    max_width_px: int | None = None,
    max_height_px: int | None = None,
) -> tuple[int, int]:
    """Compute ``(width_px, height_px)`` for QLD ``QldDem`` export within server pixel limits."""
    mid_lat = 0.5 * (bbox.min_y + bbox.max_y)
    cos_lat = max(0.2, math.cos(math.radians(mid_lat)))
    width_m = max(1.0, bbox.width * 111320.0 * cos_lat)
    height_m = max(1.0, bbox.height * 111320.0)
    px_w = max(64, int(round(width_m / target_res_m)))
    px_h = max(64, int(round(height_m / target_res_m)))
    lim_w = max_width_px if max_width_px is not None else max_px
    lim_h = max_height_px if max_height_px is not None else max_px
    scale = min(1.0, lim_w / float(px_w), lim_h / float(px_h))
    if scale < 1.0:
        px_w = max(64, int(round(px_w * scale)))
        px_h = max(64, int(round(px_h * scale)))
    return (px_w, px_h)


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

        arr = np.frombuffer(raw, dtype=np.float32).reshape((buf_y, buf_x))
        total = int(arr.size)
        if total == 0:
            return (0.0, 1.0, 1.0, 0.0, 0.0)

        valid_mask = np.isfinite(arr) & (arr >= -500.0) & (arr <= 5000.0)
        if nodata is not None:
            valid_mask &= np.abs(arr - float(nodata)) >= 1e-3

        valid = int(np.count_nonzero(valid_mask))
        if valid == 0:
            return (0.0, 1.0, 1.0, 0.0, 0.0)

        valid_vals = arr[valid_mask]
        min_v = float(valid_vals.min())
        max_v = float(valid_vals.max())
        zero_mask = valid_mask & (arr == 0.0)
        exact_zero = int(np.count_nonzero(zero_mask))

        # Measure fraction of 0.0 -> non-zero transitions that jump above cliff_threshold_m
        zero_cliff_ratio = 0.0
        if 0 < exact_zero < valid:
            nz_valid = np.isfinite(arr) & (arr != 0.0) & (arr >= -500.0) & (arr <= 5000.0)
            b_edges = 0
            c_edges = 0
            for z_sl, nz_sl, v_sl in (
                (zero_mask[1:, :], nz_valid[:-1, :], arr[:-1, :]),
                (zero_mask[:-1, :], nz_valid[1:, :], arr[1:, :]),
                (zero_mask[:, 1:], nz_valid[:, :-1], arr[:, :-1]),
                (zero_mask[:, :-1], nz_valid[:, 1:], arr[:, 1:]),
            ):
                adj = z_sl & nz_sl
                b_edges += int(np.count_nonzero(adj))
                c_edges += int(np.count_nonzero(adj & (v_sl > cliff_threshold_m)))
            if b_edges > 0:
                zero_cliff_ratio = c_edges / b_edges
        elif exact_zero == valid:
            zero_cliff_ratio = 1.0

        return (valid / total, exact_zero / total, zero_cliff_ratio, min_v, max_v)
    finally:
        ds = None


def _run_subprocess(cmd: list[str], env: dict[str, str], context: str, *, timeout: int = 300) -> None:
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"{context} failed (exit {proc.returncode}):\n"
            f"cmd: {' '.join(cmd)}\n"
            f"stderr: {proc.stderr.strip()}"
        )


class AUElvisDEMSource:
    """On-demand Australia-wide elevation source (QLD 2 m LiDAR -> GA 5 m LiDAR WCS -> DEA SRTM 1s COG).

    Never mixes resolution tiers on the same sheet; every tier uses Australian Height Datum (``AHD``).
    Tier 0 (Queensland ``QldDem/ImageServer`` 2 m LiDAR, gated by completed LiDAR extents) activates
    for tactical scales (``<= 1:25,000``) inside Queensland and falls through cleanly to GA National
    5 m LiDAR WCS and DEA SRTM 1s COG whenever state LiDAR coverage is absent or incomplete.
    """

    name: str = "au-elvis"
    vertical_datum: str = "AHD"
    attribution: str = "Elevation data: © Commonwealth of Australia (Geoscience Australia) (CC BY 4.0)"

    def __init__(
        self,
        cache_dir: Path | str | None = None,
        *,
        prefer_state_lidar: bool = True,
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
        self.prefer_state_lidar = prefer_state_lidar
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
        return build_safe_subprocess_env(extra=GDAL_VSICURL_ENV)

    def _srtm_cog_url(self) -> str:
        if self.srtm_product == "hydro":
            return DEA_SRTM_1S_HYDRO_COG_URL
        return DEA_SRTM_1S_SMOOTH_COG_URL

    def _http_get_bytes(
        self,
        url: str,
        params: dict[str, str],
        *,
        max_bytes: int = MAX_AU_JSON_DOWNLOAD_BYTES,
    ) -> tuple[int, str, bytes]:
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
            with client.stream("GET", url, params=params, headers={"User-Agent": USER_AGENT}) as resp:
                chunks: list[bytes] = []
                total = 0
                for chunk in resp.iter_bytes(chunk_size=65_536):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        raise RuntimeError(
                            f"AU DEM metadata response exceeded {max_bytes // (1024 * 1024)} MiB limit"
                        )
                    chunks.append(chunk)
                return (resp.status_code, resp.headers.get("content-type", ""), b"".join(chunks))
        finally:
            if owns_client:
                client.close()

    def _http_stream_to_file(
        self,
        url: str,
        params: dict[str, str],
        dest_path: Path,
        *,
        max_bytes: int = MAX_AU_DEM_DOWNLOAD_BYTES,
    ) -> tuple[int, str, int]:
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
        dest_path.unlink(missing_ok=True)
        try:
            with client.stream("GET", url, params=params, headers={"User-Agent": USER_AGENT}) as resp:
                ct = resp.headers.get("content-type", "")
                ct_low = ct.lower()
                if resp.status_code != 200 or "xml" in ct_low or "html" in ct_low or "json" in ct_low:
                    return (resp.status_code, ct, 0)
                written = 0
                with dest_path.open("wb") as fh:
                    for chunk in resp.iter_bytes(chunk_size=65_536):
                        if not chunk:
                            continue
                        written += len(chunk)
                        if written > max_bytes:
                            raise RuntimeError(
                                f"AU DEM raster response exceeded {max_bytes // (1024 * 1024)} MiB limit"
                            )
                        fh.write(chunk)
                return (resp.status_code, ct, written)
        except Exception:
            dest_path.unlink(missing_ok=True)
            raise
        finally:
            if owns_client:
                client.close()

    def _has_qld_completed_lidar(self, clip_bbox: BBox) -> bool:
        """Return ``True`` if Queensland Spatial completed LiDAR project extents overlap ``clip_bbox``."""
        key = _bbox_hash(clip_bbox)
        extents_cache = self.cache_dir / "qld_extents" / f"{key}.json"
        if self.use_clip_cache and extents_cache.is_file() and extents_cache.stat().st_size > 0:
            try:
                cached = json.loads(extents_cache.read_text(encoding="utf-8"))
                return bool(cached.get("has_lidar"))
            except (json.JSONDecodeError, OSError):
                logger.warning("Ignoring corrupt cached QLD LiDAR extents file: %s", extents_cache)

        params = {
            "geometry": f"{clip_bbox.min_x:.6f},{clip_bbox.min_y:.6f},{clip_bbox.max_x:.6f},{clip_bbox.max_y:.6f}",
            "geometryType": "esriGeometryEnvelope",
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "where": "completed='Yes'",
            "outFields": "project_name,completed,year",
            "returnGeometry": "false",
            "f": "json",
        }
        try:
            status, _content_type, body = self._http_get_bytes(QLD_LIDAR_EXTENTS_URL, params)
        except Exception as exc:
            logger.warning("QLD LiDAR extent query failed (%s); falling back to GA 5m LiDAR", exc)
            return False

        if status != 200 or not body:
            return False

        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return False

        if not isinstance(payload, dict) or payload.get("error"):
            return False

        features = payload.get("features") or []
        has_lidar = len(features) > 0
        if self.use_clip_cache:
            extents_cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = extents_cache.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({"has_lidar": has_lidar, "count": len(features)}), encoding="utf-8")
            tmp.replace(extents_cache)
        return has_lidar

    def _fetch_qld_lidar_dem(
        self,
        layout: SheetLayout,
        clip_bbox: BBox,
        work_dir: Path,
        out_tif: Path,
    ) -> bool:
        """Attempt Tier-0 Queensland 1 m / 2 m LiDAR DEM via ``QldDem/ImageServer`` (AHD, CC BY 4.0)."""
        if layout.spec.scale > 25000 or not is_in_qld_lidar_coverage(clip_bbox):
            return False

        fine_scale = layout.spec.scale <= 15000
        target_res_m = 1.0 if fine_scale else 2.0
        tier_label = "qld-1m-lidar" if fine_scale else "qld-2m-lidar"
        cache_tag = "au_qld1m" if fine_scale else "au_qld2m"
        px_w, px_h = qld_export_size_for_bbox(
            clip_bbox,
            target_res_m=target_res_m,
            max_width_px=QLD_MAX_EXPORT_WIDTH_PX if fine_scale else QLD_MAX_EXPORT_PX,
            max_height_px=QLD_MAX_EXPORT_HEIGHT_PX,
        )
        key = _bbox_hash(clip_bbox)
        cache_path = self.cache_dir / "clips" / f"{cache_tag}_{px_w}x{px_h}_{key}.tif"
        if self.use_clip_cache and cache_path.is_file() and cache_path.stat().st_size > 0:
            logger.info("Using cached QLD %sm LiDAR clip: %s", int(target_res_m), cache_path)
            shutil.copy2(cache_path, out_tif)
            self.resolved_tier = tier_label
            self.resolved_attribution = QLD_LIDAR_ATTRIBUTION
            self.resolved_vertical_datum = self.vertical_datum
            return True

        if not self._has_qld_completed_lidar(clip_bbox):
            logger.info("No completed QLD LiDAR project overlaps %s; trying GA 5m LiDAR", clip_bbox.as_tuple())
            return False

        params = {
            "bbox": f"{clip_bbox.min_x:.6f},{clip_bbox.min_y:.6f},{clip_bbox.max_x:.6f},{clip_bbox.max_y:.6f}",
            "bboxSR": "4326",
            "imageSR": "4326",
            "size": f"{px_w},{px_h}",
            "format": "tiff",
            "pixelType": "F32",
            "noData": "-9999",
            "interpolation": "RSP_BilinearInterpolation",
            "f": "image",
        }
        tmp_qld = work_dir / "dem_au_qld2m_tmp.tif"
        try:
            status, content_type, byte_len = self._http_stream_to_file(
                QLD_DEM_IMAGESERVER_URL, params, tmp_qld
            )
        except Exception as exc:
            logger.warning("QLD 2m QldDem ImageServer failed (%s); falling back to GA 5m LiDAR", exc)
            return False

        ct_low = content_type.lower()
        if status != 200 or "xml" in ct_low or "html" in ct_low or "json" in ct_low or byte_len < 2048:
            tmp_qld.unlink(missing_ok=True)
            logger.info(
                "QLD QldDem returned non-DEM payload (status=%s, ct=%s, bytes=%d); falling back to GA 5m LiDAR",
                status,
                content_type,
                byte_len,
            )
            return False

        try:
            valid_frac, zero_frac, cliff_ratio, min_v, max_v = _inspect_raster_coverage(tmp_qld)
        except Exception as exc:
            logger.warning("QLD 2m LiDAR GeoTIFF unreadable (%s); falling back to GA 5m LiDAR", exc)
            tmp_qld.unlink(missing_ok=True)
            return False

        coastal_water_ok = zero_frac <= 0.45 and cliff_ratio <= 0.15
        if (
            valid_frac < self.min_valid_ratio
            or max_v <= min_v + 0.1
            or (zero_frac > self.max_exact_zero_ratio and not coastal_water_ok)
        ):
            logger.info(
                "QLD 2m LiDAR incomplete/absent (valid=%.1f%%, exact_zero=%.1f%%, cliff_ratio=%.1f%%, "
                "range=[%.1f, %.1f]); falling back to GA 5m LiDAR",
                valid_frac * 100.0,
                zero_frac * 100.0,
                cliff_ratio * 100.0,
                min_v,
                max_v,
            )
            tmp_qld.unlink(missing_ok=True)
            return False

        tmp_qld.replace(out_tif)
        self.resolved_tier = tier_label
        self.resolved_attribution = QLD_LIDAR_ATTRIBUTION
        self.resolved_vertical_datum = self.vertical_datum
        if self.use_clip_cache and out_tif.is_file() and out_tif.stat().st_size > 0:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_cache = cache_path.with_suffix(".tif.tmp")
            shutil.copy2(out_tif, tmp_cache)
            tmp_cache.replace(cache_path)
        return True

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
            self.resolved_attribution = self.attribution
            self.resolved_vertical_datum = self.vertical_datum
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
        tmp_5m = work_dir / "dem_au_5m_tmp.tif"
        try:
            status, content_type, byte_len = self._http_stream_to_file(
                GA_5M_LIDAR_WCS_URL, params, tmp_5m
            )
        except Exception as exc:
            logger.warning("GA 5m LiDAR WCS request failed (%s); falling back to SRTM 1s", exc)
            return False

        if status != 200 or "xml" in content_type.lower() or "html" in content_type.lower() or byte_len < 2048:
            tmp_5m.unlink(missing_ok=True)
            logger.info(
                "GA 5m LiDAR WCS returned non-DEM payload (status=%s, ct=%s, bytes=%d); falling back to SRTM 1s",
                status,
                content_type,
                byte_len,
            )
            return False

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
        self.resolved_attribution = self.attribution
        self.resolved_vertical_datum = self.vertical_datum
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
            status, content_type, byte_len = self._http_stream_to_file(
                GA_SRTM_1S_WCS_URL, params, out_tif
            )
        except Exception as exc:
            logger.warning("GA SRTM 1s backup WCS failed: %s", exc)
            return False

        if status != 200 or "xml" in content_type.lower() or "html" in content_type.lower() or byte_len < 1024:
            out_tif.unlink(missing_ok=True)
            return False

        return True

    def _fetch_srtm_1s(self, clip_bbox: BBox, out_tif: Path) -> Path:
        key = _bbox_hash(clip_bbox)
        cache_path = self.cache_dir / "clips" / f"au_srtm1s_{self.srtm_product}_{key}.tif"
        if self.use_clip_cache and cache_path.is_file() and cache_path.stat().st_size > 0:
            logger.info("Using cached GA SRTM 1s clip: %s", cache_path)
            shutil.copy2(cache_path, out_tif)
            self.resolved_tier = "srtm-1s"
            self.resolved_attribution = self.attribution
            self.resolved_vertical_datum = self.vertical_datum
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
        self.resolved_attribution = self.attribution
        self.resolved_vertical_datum = self.vertical_datum
        if self.use_clip_cache and out_tif.is_file() and out_tif.stat().st_size > 0:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_cache = cache_path.with_suffix(".tif.tmp")
            shutil.copy2(out_tif, tmp_cache)
            tmp_cache.replace(cache_path)
        return out_tif

    def fetch_dem(self, layout: SheetLayout, work_dir: Path) -> Path:
        """Fetch QLD 2 m LiDAR (where completed), GA 5 m LiDAR WCS, or DEA GA SRTM 1s DEM."""
        work_dir.mkdir(parents=True, exist_ok=True)
        clip_bbox = _round_enclosing_bbox(layout.wgs84_fetch_bbox, step_deg=0.001)
        out_tif = work_dir / "dem_raw.tif"

        if not is_in_australia_coverage(clip_bbox):
            raise RuntimeError(
                f"Outside Australian DEM coverage for requested footprint {clip_bbox.as_tuple()}"
            )

        if self.prefer_state_lidar and self._fetch_qld_lidar_dem(layout, clip_bbox, work_dir, out_tif):
            return out_tif

        if self.prefer_5m and self._fetch_5m_lidar_wcs(layout, clip_bbox, work_dir, out_tif):
            return out_tif

        return self._fetch_srtm_1s(clip_bbox, out_tif)
