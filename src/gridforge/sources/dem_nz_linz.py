"""Cloud-native New Zealand LINZ elevation source (1 m LiDAR COG -> 8 m NZ DEM COG -> GLO-30 fallback)."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlparse

import httpx

from gridforge.config import BBox, SheetLayout
from gridforge.sources.dem_copernicus import CopernicusGLO30Source, GDAL_VSICURL_ENV
from gridforge.subprocess_env import build_safe_subprocess_env

logger = logging.getLogger(__name__)

_SAFE_LINZ_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{2,64}$")
_SAFE_REL_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_ALLOWED_NZ_HOST_SUFFIXES: Final[tuple[str, ...]] = (
    "amazonaws.com",
    "linz.govt.nz",
)


def _is_safe_rel_href(rel_href: str) -> bool:
    if not rel_href or rel_href.startswith(("/", "http://", "https://")) or "\\" in rel_href:
        return False
    if any(ch in rel_href for ch in ("\r", "\n", "\x00", "?", "#", "@")):
        return False
    parts = [p for p in rel_href.split("/") if p]
    if not parts or any(p in (".", "..") or not _SAFE_REL_SEGMENT_RE.match(p) for p in parts):
        return False
    return True


def _is_allowed_linz_url(url: str, *, base_url: str | None = None) -> bool:
    if any(ch in url for ch in ("\r", "\n", "\x00")):
        return False
    parsed = urlparse(url)
    if parsed.scheme.lower() != "https" or parsed.username or parsed.password:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host or ".." in parsed.path.split("/"):
        return False
    if base_url:
        base_host = (urlparse(base_url).hostname or "").lower().rstrip(".")
        if base_host and host == base_host:
            return True
    return any(host == suffix or host.endswith(f".{suffix}") for suffix in _ALLOWED_NZ_HOST_SUFFIXES)

LINZ_S3_BASE_URL: Final[str] = "https://nz-elevation.s3.ap-southeast-2.amazonaws.com"
LINZ_STAC_CATALOG_URL: Final[str] = f"{LINZ_S3_BASE_URL}/catalog.json"
LINZ_8M_COLLECTION_URL: Final[str] = (
    f"{LINZ_S3_BASE_URL}/new-zealand/new-zealand-contour/dem_8m/2193/collection.json"
)
USER_AGENT: Final[str] = "gridforge/1.3 (+https://github.com/jremick/gridforge)"

# New Zealand mainland + Stewart Island across UTM Zones 58S–60S (EPSG:32758–32760)
NZ_COVERAGE_BBOX: Final[tuple[float, float, float, float]] = (166.0, -47.5, 179.0, -34.0)

# Official LINZ NZTM2000 (EPSG:2193) Topo50 grid constants (verified against live s3://nz-elevation)
_TOPO50_LETTERS: Final[str] = "ABCDEFGHJKLMNPQRSTUVWXYZ"  # 24 letters (I and O omitted)
_TOPO50_E_ORIGIN: Final[float] = 988_000.0
_TOPO50_N_TOP_AS: Final[float] = 6_234_000.0
_TOPO50_ROW_INDEX_AS: Final[int] = 16  # 'A' (0) * 24 + 'S' (16)
_TOPO50_WIDTH_M: Final[float] = 24_000.0
_TOPO50_HEIGHT_M: Final[float] = 36_000.0
_TILE_10K_WIDTH_M: Final[float] = 4_800.0
_TILE_10K_HEIGHT_M: Final[float] = 7_200.0

# Coarse WGS84 regional prefixes to avoid probing unrelated LINZ 1 m STAC collections
_NZ_REGION_BOXES: Final[tuple[tuple[str, tuple[float, float, float, float]], ...]] = (
    ("northland", (172.5, -36.5, 174.9, -34.2)),
    ("auckland", (174.1, -37.4, 175.6, -36.0)),
    ("waikato", (174.4, -39.4, 176.8, -36.3)),
    ("bay-of-plenty", (175.8, -38.9, 178.1, -37.2)),
    ("gisborne", (177.2, -39.0, 178.7, -37.5)),
    ("hawkes-bay", (176.0, -40.5, 178.1, -38.5)),
    ("taranaki", (173.6, -39.9, 175.2, -38.6)),
    ("manawatu-whanganui", (174.6, -40.8, 176.6, -38.3)),
    ("wellington", (174.5, -41.7, 176.4, -40.5)),
    ("tasman", (172.0, -42.3, 173.4, -40.4)),
    ("nelson", (173.0, -41.5, 173.7, -41.0)),
    ("marlborough", (172.7, -42.5, 174.5, -40.7)),
    ("west-coast", (168.0, -44.6, 172.8, -40.7)),
    ("canterbury", (169.4, -45.5, 174.1, -41.8)),
    ("otago", (168.0, -46.7, 171.3, -43.9)),
    ("southland", (166.3, -47.4, 169.5, -44.3)),
)


def _bboxes_intersect(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> bool:
    return not (a[2] <= b[0] or a[0] >= b[2] or a[3] <= b[1] or a[1] >= b[3])


def is_in_nz_coverage(bbox: BBox) -> bool:
    """Return ``True`` if a WGS84 bounding box overlaps New Zealand LINZ DEM coverage."""
    return _bboxes_intersect(bbox.as_tuple(), NZ_COVERAGE_BBOX)


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


def _topo50_row_code(row_idx: int) -> str | None:
    if row_idx < 0 or row_idx >= 24 * 24:
        return None
    return f"{_TOPO50_LETTERS[row_idx // 24]}{_TOPO50_LETTERS[row_idx % 24]}"


def wgs84_bbox_to_nztm2000(bbox: BBox) -> tuple[float, float, float, float]:
    """Transform a WGS84 bounding box into NZTM2000 (``EPSG:2193``) ``(min_e, min_n, max_e, max_n)``."""
    from osgeo import osr  # type: ignore[import-untyped]

    osr.UseExceptions()
    src = osr.SpatialReference()
    src.ImportFromEPSG(4326)
    src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    dst = osr.SpatialReference()
    dst.ImportFromEPSG(2193)
    dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    ct = osr.CoordinateTransformation(src, dst)

    corners = (
        (bbox.min_x, bbox.min_y),
        (bbox.max_x, bbox.min_y),
        (bbox.max_x, bbox.max_y),
        (bbox.min_x, bbox.max_y),
        ((bbox.min_x + bbox.max_x) / 2.0, bbox.min_y),
        ((bbox.min_x + bbox.max_x) / 2.0, bbox.max_y),
    )
    es: list[float] = []
    ns: list[float] = []
    for lon, lat in corners:
        e, n, _ = ct.TransformPoint(lon, lat)
        es.append(e)
        ns.append(n)
    return (min(es), min(ns), max(es), max(ns))


def topo50_sheets_for_nztm_bbox(
    min_e: float,
    min_n: float,
    max_e: float,
    max_n: float,
) -> list[str]:
    """Compute exact intersecting LINZ Topo50 sheet codes (e.g. ``['BH35', 'BH36']``)."""
    c_min = int(math.floor((min_e - _TOPO50_E_ORIGIN) / _TOPO50_WIDTH_M))
    c_max = int(math.floor((max_e - _TOPO50_E_ORIGIN - 1e-6) / _TOPO50_WIDTH_M))
    r_top = _TOPO50_ROW_INDEX_AS + int(math.floor((_TOPO50_N_TOP_AS - max_n) / _TOPO50_HEIGHT_M))
    r_bot = _TOPO50_ROW_INDEX_AS + int(math.floor((_TOPO50_N_TOP_AS - min_n - 1e-6) / _TOPO50_HEIGHT_M))

    sheets: list[str] = []
    for row_idx in range(r_top, r_bot + 1):
        rc = _topo50_row_code(row_idx)
        if rc is None:
            continue
        for col in range(max(1, c_min), c_max + 1):
            sheets.append(f"{rc}{col:02d}")
    return sheets


def linz_10k_tiles_for_nztm_bbox(
    min_e: float,
    min_n: float,
    max_e: float,
    max_n: float,
) -> list[str]:
    """Compute exact intersecting LINZ 1:10,000 sub-tile IDs (``{SHEET}_10000_{RR:02d}{CC:02d}``)."""
    c_min = int(math.floor((min_e - _TOPO50_E_ORIGIN) / _TOPO50_WIDTH_M))
    c_max = int(math.floor((max_e - _TOPO50_E_ORIGIN - 1e-6) / _TOPO50_WIDTH_M))
    r_top = _TOPO50_ROW_INDEX_AS + int(math.floor((_TOPO50_N_TOP_AS - max_n) / _TOPO50_HEIGHT_M))
    r_bot = _TOPO50_ROW_INDEX_AS + int(math.floor((_TOPO50_N_TOP_AS - min_n - 1e-6) / _TOPO50_HEIGHT_M))

    tiles: list[str] = []
    for row_idx in range(r_top, r_bot + 1):
        rc = _topo50_row_code(row_idx)
        if rc is None:
            continue
        sheet_n_max = _TOPO50_N_TOP_AS - (row_idx - _TOPO50_ROW_INDEX_AS) * _TOPO50_HEIGHT_M
        sheet_n_min = sheet_n_max - _TOPO50_HEIGHT_M
        for col in range(max(1, c_min), c_max + 1):
            sheet_e_min = _TOPO50_E_ORIGIN + col * _TOPO50_WIDTH_M
            sheet_e_max = sheet_e_min + _TOPO50_WIDTH_M

            ov_e_min = max(min_e, sheet_e_min)
            ov_e_max = min(max_e, sheet_e_max)
            ov_n_min = max(min_n, sheet_n_min)
            ov_n_max = min(max_n, sheet_n_max)
            if ov_e_max <= ov_e_min or ov_n_max <= ov_n_min:
                continue

            cc_min = max(1, min(5, int(math.floor((ov_e_min - sheet_e_min) / _TILE_10K_WIDTH_M)) + 1))
            cc_max = max(1, min(5, int(math.floor((ov_e_max - sheet_e_min - 1e-6) / _TILE_10K_WIDTH_M)) + 1))
            rr_min = max(1, min(5, int(math.floor((sheet_n_max - ov_n_max) / _TILE_10K_HEIGHT_M)) + 1))
            rr_max = max(1, min(5, int(math.floor((sheet_n_max - ov_n_min - 1e-6) / _TILE_10K_HEIGHT_M)) + 1))

            for rr in range(rr_min, rr_max + 1):
                for cc in range(cc_min, cc_max + 1):
                    tiles.append(f"{rc}{col:02d}_10000_{rr:02d}{cc:02d}")
    return tiles


def _raster_valid_pixel_fraction(tif_path: Path) -> float:
    from array import array
    from osgeo import gdal  # type: ignore[import-untyped]

    gdal.UseExceptions()
    ds = gdal.Open(str(tif_path), gdal.GA_ReadOnly)
    if ds is None:
        return 0.0
    try:
        cols, rows = ds.RasterXSize, ds.RasterYSize
        if cols <= 0 or rows <= 0:
            return 0.0
        buf_x, buf_y = min(cols, 512), min(rows, 512)
        band = ds.GetRasterBand(1)
        nodata = band.GetNoDataValue()
        raw = band.ReadRaster(0, 0, cols, rows, buf_x, buf_y, gdal.GDT_Float32)
        if not raw:
            return 0.0
        vals = array("f")
        vals.frombytes(raw)
        if not vals:
            return 0.0
        valid = 0
        for v in vals:
            if not math.isfinite(v):
                continue
            if nodata is not None and abs(v - nodata) < 1e-3:
                continue
            if -500.0 <= v <= 5000.0:
                valid += 1
        return valid / len(vals)
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


class NZLinzDEMSource:
    """Cloud-native New Zealand LINZ elevation source (1 m LiDAR COG -> 8 m NZ DEM COG -> GLO-30).

    Streams LERC-compressed COGs directly from public AWS Open Data ``s3://nz-elevation``
    (``ap-southeast-2``, zero-auth, CC BY 4.0) in vertical datum ``NZVD2016``.
    Falls back cleanly to Copernicus GLO-30 (``EGM2008``) if LINZ tiles are unavailable,
    updating ``resolved_vertical_datum`` and ``resolved_attribution`` accurately.
    """

    name: str = "nz-linz"
    vertical_datum: str = "NZVD2016"
    attribution: str = (
        "Elevation data: Toitū Te Whenua Land Information New Zealand (LINZ) (CC BY 4.0)"
    )

    def __init__(
        self,
        cache_dir: Path | str | None = None,
        *,
        s3_base_url: str | None = None,
        prefer_1m: bool = True,
        lidar_warp_res_m: float = 2.0,
        min_1m_coverage_ratio: float = 0.95,
        timeout_s: float = 30.0,
        use_clip_cache: bool = True,
        copernicus_fallback: CopernicusGLO30Source | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        base_cache = Path(
            cache_dir
            if cache_dir is not None
            else os.environ.get("GRIDFORGE_CACHE_DIR", "/tmp/gridforge-cache")
        )
        self.cache_dir: Path = base_cache / "nz_linz"
        self.s3_base_url = (s3_base_url or os.environ.get("GRIDFORGE_NZ_LINZ_S3_URL", LINZ_S3_BASE_URL)).rstrip("/")
        self.prefer_1m = prefer_1m
        self.lidar_warp_res_m = lidar_warp_res_m
        self.min_1m_coverage_ratio = min_1m_coverage_ratio
        self.timeout_s = timeout_s
        self.use_clip_cache = use_clip_cache
        self._copernicus_fallback = copernicus_fallback
        self._base_cache_dir = cache_dir
        self._injected_client = client

        self.resolved_tier: str = "8m-dem"
        self.resolved_source_name: str = self.name
        self.resolved_vertical_datum: str = self.vertical_datum
        self.resolved_attribution: str = self.attribution

    def _gdal_env(self) -> dict[str, str]:
        extra = dict(GDAL_VSICURL_ENV)
        # Allow .tiff extension used by LINZ COGs in addition to .tif/.vrt
        extra["CPL_VSIL_CURL_ALLOWED_EXTENSIONS"] = ".tif,.tiff,.vrt"
        return build_safe_subprocess_env(extra=extra)

    def _get_json_cached(self, url: str, cache_path: Path) -> dict[str, Any]:
        if cache_path.is_file() and cache_path.stat().st_size > 0:
            try:
                return json.loads(cache_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                logger.warning("Ignoring corrupt cached LINZ STAC JSON: %s", cache_path)

        owns_client = self._injected_client is None
        http = (
            self._injected_client
            if self._injected_client is not None
            else httpx.Client(
                timeout=httpx.Timeout(self.timeout_s, connect=min(10.0, self.timeout_s)),
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT},
            )
        )
        try:
            resp = http.get(url, headers={"User-Agent": USER_AGENT})
            resp.raise_for_status()
            payload = resp.json()
        finally:
            if owns_client:
                http.close()

        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(cache_path)
        return payload

    def _find_1m_cog_urls(self, clip_bbox: BBox, tile_codes: list[str]) -> list[str]:
        """Discover LINZ 1 m LiDAR COG URLs intersecting ``clip_bbox`` via STAC catalog + regional collections."""
        catalog = self._get_json_cached(
            f"{self.s3_base_url}/catalog.json",
            self.cache_dir / "stac" / "catalog.json",
        )
        target_tuple = clip_bbox.as_tuple()
        candidate_regions = {
            reg for reg, rbox in _NZ_REGION_BOXES if _bboxes_intersect(target_tuple, rbox)
        }

        dem1m_hrefs: list[str] = []
        for link in catalog.get("links", []):
            if link.get("rel") != "child":
                continue
            href = str(link.get("href") or "").strip()
            if "/dem_1m/2193/collection.json" not in href:
                continue
            norm = href[2:] if href.startswith("./") else href
            if not _is_safe_rel_href(norm):
                continue
            region_slug = norm.split("/", 1)[0].lower()
            if candidate_regions and region_slug not in candidate_regions:
                continue
            dem1m_hrefs.append(norm)

        if not dem1m_hrefs:
            return []

        wanted_set = set(tile_codes)
        matched_urls: list[tuple[str, str]] = []  # (collection_sort_key, cog_url)
        for rel_href in sorted(dem1m_hrefs):
            col_url = f"{self.s3_base_url}/{rel_href}"
            if not _is_allowed_linz_url(col_url, base_url=self.s3_base_url):
                continue
            col_hash = hashlib.sha256(rel_href.encode("ascii")).hexdigest()[:16]
            try:
                col = self._get_json_cached(col_url, self.cache_dir / "stac" / f"col_{col_hash}.json")
            except Exception as exc:
                logger.debug("Skipping LINZ collection %s: %s", rel_href, exc)
                continue

            bboxes = ((col.get("extent") or {}).get("spatial") or {}).get("bbox") or []
            if bboxes and len(bboxes[0]) >= 4:
                sb = (float(bboxes[0][0]), float(bboxes[0][1]), float(bboxes[0][2]), float(bboxes[0][3]))
                if not _bboxes_intersect(target_tuple, sb):
                    continue

            col_dir = col_url.rsplit("/", 1)[0]
            items_in_col = {
                code
                for lk in col.get("links", [])
                if lk.get("rel") == "item"
                for code in (str(lk.get("href") or "").rsplit("/", 1)[-1].removesuffix(".json"),)
                if _SAFE_LINZ_CODE_RE.match(code)
            }
            hits = sorted(wanted_set & items_in_col)
            if hits:
                # Order collections oldest -> newest by temporal interval or path so newer LiDAR overlays older
                temporal = ((col.get("extent") or {}).get("temporal") or {}).get("interval") or [[""]]
                sort_key = str((temporal[0][0] if temporal and temporal[0] else "") or rel_href)
                for code in hits:
                    cog_url = f"{col_dir}/{code}.tiff"
                    if _is_allowed_linz_url(cog_url, base_url=self.s3_base_url):
                        matched_urls.append((sort_key, cog_url))

        matched_urls.sort(key=lambda pair: (pair[0], pair[1]))
        # Deduplicate preserving newest ordering
        ordered: list[str] = []
        seen: set[str] = set()
        for _, u in matched_urls:
            if u not in seen:
                seen.add(u)
                ordered.append(u)
        return ordered[:24]

    def _fetch_1m_lidar(
        self,
        layout: SheetLayout,
        clip_bbox: BBox,
        nztm_bbox: tuple[float, float, float, float],
        work_dir: Path,
        out_tif: Path,
    ) -> bool:
        key = _bbox_hash(clip_bbox)
        cache_path = self.cache_dir / "clips" / f"nz_1m_{layout.utm_epsg}_{key}.tif"
        if self.use_clip_cache and cache_path.is_file() and cache_path.stat().st_size > 0:
            logger.info("Using cached LINZ 1 m LiDAR clip: %s", cache_path)
            shutil.copy2(cache_path, out_tif)
            self._set_linz_resolved("1m-lidar")
            return True

        tile_codes = linz_10k_tiles_for_nztm_bbox(*nztm_bbox)
        if not tile_codes:
            return False

        try:
            cog_urls = self._find_1m_cog_urls(clip_bbox, tile_codes)
        except Exception as exc:
            logger.warning("LINZ 1 m STAC lookup failed (%s); falling back to LINZ 8 m DEM", exc)
            return False

        if not cog_urls:
            logger.info("No LINZ 1 m LiDAR COGs cover %s; falling back to LINZ 8 m DEM", clip_bbox.as_tuple())
            return False

        tmp_1m = work_dir / "dem_nz_1m_tmp.tif"
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
            *[f"/vsicurl/{u}" for u in cog_urls],
            str(tmp_1m),
        ]
        logger.info("Streaming %d LINZ 1 m LiDAR COG(s) at %.1f m -> %s", len(cog_urls), self.lidar_warp_res_m, out_tif)
        try:
            _run_subprocess(cmd, env=self._gdal_env(), context="gdalwarp (LINZ 1m LiDAR)")
        except RuntimeError as exc:
            logger.warning("LINZ 1 m LiDAR stream failed (%s); falling back to LINZ 8 m DEM", exc)
            tmp_1m.unlink(missing_ok=True)
            return False

        coverage = _raster_valid_pixel_fraction(tmp_1m)
        if coverage < self.min_1m_coverage_ratio:
            logger.info(
                "LINZ 1 m LiDAR coverage incomplete (%.1f%% < %.1f%%); falling back cleanly to LINZ 8 m DEM",
                coverage * 100.0,
                self.min_1m_coverage_ratio * 100.0,
            )
            tmp_1m.unlink(missing_ok=True)
            return False

        tmp_1m.replace(out_tif)
        self._set_linz_resolved("1m-lidar")
        if self.use_clip_cache and out_tif.is_file() and out_tif.stat().st_size > 0:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_cache = cache_path.with_suffix(".tif.tmp")
            shutil.copy2(out_tif, tmp_cache)
            tmp_cache.replace(cache_path)
        return True

    def _fetch_8m_dem(
        self,
        layout: SheetLayout,
        clip_bbox: BBox,
        nztm_bbox: tuple[float, float, float, float],
        work_dir: Path,
        out_tif: Path,
    ) -> bool:
        key = _bbox_hash(clip_bbox)
        cache_path = self.cache_dir / "clips" / f"nz_8m_{layout.utm_epsg}_{key}.tif"
        if self.use_clip_cache and cache_path.is_file() and cache_path.stat().st_size > 0:
            logger.info("Using cached LINZ 8 m DEM clip: %s", cache_path)
            shutil.copy2(cache_path, out_tif)
            self._set_linz_resolved("8m-dem")
            return True

        sheets = topo50_sheets_for_nztm_bbox(*nztm_bbox)
        if not sheets:
            return False

        col_url = f"{self.s3_base_url}/new-zealand/new-zealand-contour/dem_8m/2193/collection.json"
        try:
            col = self._get_json_cached(col_url, self.cache_dir / "stac" / "dem_8m_collection.json")
        except Exception as exc:
            logger.warning("LINZ 8 m DEM collection fetch failed (%s)", exc)
            return False

        available = {
            str(lk.get("href") or "").rsplit("/", 1)[-1].removesuffix(".json")
            for lk in col.get("links", [])
            if lk.get("rel") == "item"
        }
        matched = [s for s in sheets if s in available]
        if not matched:
            return False

        col_dir = col_url.rsplit("/", 1)[0]
        cog_urls = [f"/vsicurl/{col_dir}/{s}.tiff" for s in matched]
        tmp_8m = work_dir / "dem_nz_8m_tmp.tif"
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
            "8.0",
            "8.0",
            "-r",
            "cubic",
            "-dstnodata",
            "-9999",
            "-ot",
            "Float32",
            "-co",
            "COMPRESS=DEFLATE",
            "-co",
            "TILED=YES",
            *cog_urls,
            str(tmp_8m),
        ]
        logger.info("Streaming %d LINZ 8 m DEM COG(s) (%s) -> %s", len(matched), ",".join(matched), out_tif)
        try:
            _run_subprocess(cmd, env=self._gdal_env(), context="gdalwarp (LINZ 8m DEM)")
        except RuntimeError as exc:
            logger.warning("LINZ 8 m DEM stream failed (%s)", exc)
            tmp_8m.unlink(missing_ok=True)
            return False

        coverage = _raster_valid_pixel_fraction(tmp_8m)
        if coverage < 0.05:
            logger.warning("LINZ 8 m DEM returned < 5%% valid pixels (%.1f%%)", coverage * 100.0)
            tmp_8m.unlink(missing_ok=True)
            return False

        tmp_8m.replace(out_tif)
        self._set_linz_resolved("8m-dem")
        if self.use_clip_cache and out_tif.is_file() and out_tif.stat().st_size > 0:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_cache = cache_path.with_suffix(".tif.tmp")
            shutil.copy2(out_tif, tmp_cache)
            tmp_cache.replace(cache_path)
        return True

    def _set_linz_resolved(self, tier: str) -> None:
        self.resolved_tier = tier
        self.resolved_source_name = self.name
        self.resolved_vertical_datum = "NZVD2016"
        self.vertical_datum = "NZVD2016"
        self.resolved_attribution = NZLinzDEMSource.attribution
        self.attribution = self.resolved_attribution

    def fetch_dem(self, layout: SheetLayout, work_dir: Path) -> Path:
        """Fetch LINZ 1 m LiDAR (preferred) or 8 m DEM (``NZVD2016``), falling back to GLO-30 (``EGM2008``)."""
        work_dir.mkdir(parents=True, exist_ok=True)
        clip_bbox = _round_enclosing_bbox(layout.wgs84_fetch_bbox, step_deg=0.001)
        out_tif = work_dir / "dem_raw.tif"

        if not is_in_nz_coverage(clip_bbox):
            raise RuntimeError(
                f"Outside New Zealand LINZ DEM coverage for requested footprint {clip_bbox.as_tuple()}"
            )

        nztm_bbox = wgs84_bbox_to_nztm2000(clip_bbox)
        if self.prefer_1m and self._fetch_1m_lidar(layout, clip_bbox, nztm_bbox, work_dir, out_tif):
            return out_tif

        if self._fetch_8m_dem(layout, clip_bbox, nztm_bbox, work_dir, out_tif):
            return out_tif

        logger.warning("LINZ 1 m & 8 m DEM unavailable for %s; falling back to Copernicus GLO-30", clip_bbox.as_tuple())
        fallback = self._copernicus_fallback or CopernicusGLO30Source(cache_dir=self._base_cache_dir)
        path = fallback.fetch_dem(layout, work_dir)
        self.resolved_tier = "glo30-fallback"
        self.resolved_source_name = fallback.name
        self.resolved_vertical_datum = "EGM2008"
        self.vertical_datum = "EGM2008"
        self.resolved_attribution = fallback.attribution
        self.attribution = fallback.attribution
        return path
