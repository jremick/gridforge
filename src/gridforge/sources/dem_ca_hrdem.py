"""Cloud-native Canada elevation source (NRCan HRDEM 1 m/2 m LiDAR -> MRDEM-30 CGVD2013 -> GLO-30)."""

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
from typing import Any, Final, Literal
from urllib.parse import urlparse

import httpx

from gridforge.config import BBox, SheetLayout
from gridforge.sources.dem_copernicus import CopernicusGLO30Source, GDAL_VSICURL_ENV
from gridforge.subprocess_env import build_safe_subprocess_env

logger = logging.getLogger(__name__)

_ALLOWED_CA_HOST_SUFFIXES: Final[tuple[str, ...]] = (
    "nrcan.gc.ca",
    "geo.ca",
    "canada.ca",
    "amazonaws.com",
)


def _is_allowed_ca_tile_url(url: str) -> bool:
    if any(ch in url for ch in ("\r", "\n", "\x00")):
        return False
    parsed = urlparse(url)
    if parsed.scheme.lower() != "https" or parsed.username or parsed.password:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host or ".." in parsed.path.split("/"):
        return False
    return any(host == suffix or host.endswith(f".{suffix}") for suffix in _ALLOWED_CA_HOST_SUFFIXES)

NRCAN_STAC_SEARCH_URL: Final[str] = "https://datacube.services.geo.ca/stac/api/search"
NRCAN_MRDEM_30_DTM_URL: Final[str] = (
    "https://canelevation-dem.s3.ca-central-1.amazonaws.com/mrdem-30/mrdem-30-dtm.tif"
)
USER_AGENT: Final[str] = "gridforge/1.4 (+https://github.com/jremick/gridforge)"

# Outer bounding box of Canada (Yukon 141°W to Newfoundland 52.5°W, Pelee Island 41.65°N to Ellesmere 83.2°N)
CA_COVERAGE_BBOX: Final[tuple[float, float, float, float]] = (-141.05, 41.65, -52.5, 83.2)

# Piecewise US-Canada southern international border (longitude ascending, latitude of border).
# Points NORTH of this curve (inside [-141.0, -52.5]) are Canada; points SOUTH are United States.
_US_CA_BORDER_LINE: Final[tuple[tuple[float, float], ...]] = (
    (-130.6, 54.7),   # Dixon Entrance / BC-Alaska southern tip
    (-124.8, 48.5),   # Strait of Juan de Fuca West (keeps Victoria BC in CA)
    (-123.3, 48.25),  # Victoria / Haro Strait dip
    (-122.76, 49.0),  # Blaine WA / Peace Arch
    (-95.15, 49.0),   # 49th parallel (BC/AB/SK/MB vs WA/ID/MT/ND/MN)
    (-94.7, 48.7),    # Rainy River
    (-90.0, 48.0),    # Pigeon River / Western Lake Superior
    (-84.5, 46.5),    # Sault Ste. Marie / St. Marys River
    (-82.4, 43.0),    # St. Clair River / Sarnia-Port Huron
    (-83.05, 42.33),  # Detroit River (Detroit US, Windsor CA)
    (-82.7, 41.68),   # Pelee Island (southernmost Canada)
    (-79.05, 42.90),  # Niagara River / Buffalo NY
    (-76.3, 44.1),    # Eastern Lake Ontario / Thousand Islands
    (-74.7, 45.0),    # St. Lawrence / Cornwall-Massena
    (-71.5, 45.0),    # 45th parallel (QC vs NY/VT/NH)
    (-70.8, 45.3),    # NH/ME notch
    (-69.2, 47.45),   # Northern Maine salient (Madawaska)
    (-67.8, 47.2),    # St. John River / NB-ME
    (-67.8, 45.7),    # Houlton ME / Woodstock NB
    (-67.0, 44.9),    # Passamaquoddy Bay / Eastport ME
    (-65.0, 43.0),    # Offshore Nova Scotia
    (-52.5, 43.0),    # Offshore Newfoundland
)

# Alaska-Yukon/BC eastern border polyline (latitude ascending, border longitude).
# Points WEST of this curve (at lat >= 54.7) are Alaska (US); points EAST are Yukon/BC (CA).
_AK_CA_BORDER_LINE: Final[tuple[tuple[float, float], ...]] = (
    (54.7, -130.6),   # Dixon Entrance / Portland Canal
    (56.0, -130.0),   # Hyder AK / Stewart BC
    (58.5, -133.5),   # Juneau / Taku River coast range
    (59.6, -135.3),   # Skagway / White Pass
    (60.3, -141.0),   # Mount St. Elias -> 141st meridian
    (70.5, -141.0),   # Beaufort Sea coast at 141°W
)


def _interp_piecewise(x: float, pts: tuple[tuple[float, float], ...]) -> float:
    if x <= pts[0][0]:
        return pts[0][1]
    if x >= pts[-1][0]:
        return pts[-1][1]
    for i in range(len(pts) - 1):
        x0, y0 = pts[i]
        x1, y1 = pts[i + 1]
        if x0 <= x <= x1:
            if x1 == x0:
                return y1
            t = (x - x0) / (x1 - x0)
            return y0 + t * (y1 - y0)
    return pts[-1][1]


def classify_north_america_country(lat: float, lon: float) -> Literal["US", "CA", None]:
    """Classify a WGS84 point as ``'US'``, ``'CA'``, or ``None`` with accurate border handling.

    Cleanly separates the 49th parallel, Vancouver Island / Puget Sound, Great Lakes / St. Lawrence,
    northern Maine, and the Alaska Panhandle / 141st meridian Yukon border so Canadian cities and
    parks (Vancouver, Squamish, Banff, Toronto, Algonquin, Ottawa, Montreal, Halifax, Whitehorse)
    route to ``ca-hrdem`` while US locations (Seattle, Glacier, Boulder, Detroit, Buffalo, Acadia,
    Juneau, Anchorage, Hawaii, PR/USVI, Guam, American Samoa) route to ``usgs-3dep``.
    """
    # US Pacific / Caribbean territories
    if (
        (-161.0 <= lon <= -154.5 and 18.5 <= lat <= 22.5)      # Hawaii
        or (-68.0 <= lon <= -64.4 and 17.5 <= lat <= 18.7)     # Puerto Rico & USVI
        or (144.5 <= lon <= 146.1 and 13.1 <= lat <= 20.6)     # Guam & CNMI
        or (-171.2 <= lon <= -169.3 and -14.6 <= lat <= -14.0) # American Samoa
        or (172.0 <= lon <= 180.0 and 51.0 <= lat <= 55.5)     # Eastern Aleutians (Alaska)
    ):
        return "US"

    # Alaska mainland & western Aleutians (west of 141°W)
    if -179.9 <= lon < -141.0 and 51.0 <= lat <= 71.6:
        return "US"

    # Alaska Panhandle vs BC/Yukon coastal boundary (lat >= 54.7, lon in [-141, -129.5])
    if -141.0 <= lon <= -129.5 and 54.7 <= lat <= 70.5:
        ak_border_lon = _interp_piecewise(lat, _AK_CA_BORDER_LINE)
        if lon < ak_border_lon:
            return "US"
        return "CA"

    # Northern Canada (Arctic / Yukon / NWT / Nunavut / Northern provinces above 49.6°N)
    if -141.0 <= lon <= -52.5 and 49.6 <= lat <= 83.2:
        return "CA"

    # Southern Canada vs CONUS across [-130.6, -52.5] x [24.0, 49.6]
    if -130.6 <= lon <= -52.5 and 24.0 <= lat <= 49.6:
        border_lat = _interp_piecewise(lon, _US_CA_BORDER_LINE)
        if lat >= border_lat:
            return "CA"
        if -125.5 <= lon <= -66.5:
            return "US"
        return None

    return None


def is_in_canada_coverage(bbox: BBox) -> bool:
    """Return ``True`` if a WGS84 bounding box falls in Canadian NRCan DEM coverage."""
    cx = 0.5 * (bbox.min_x + bbox.max_x)
    cy = 0.5 * (bbox.min_y + bbox.max_y)
    if classify_north_america_country(cy, cx) == "CA":
        return True
    # Also accept border-straddling sheets that meaningfully overlap Canada
    for lon, lat in (
        (bbox.min_x, bbox.max_y),
        (bbox.max_x, bbox.max_y),
        (bbox.min_x, bbox.min_y),
        (bbox.max_x, bbox.min_y),
    ):
        if classify_north_america_country(lat, lon) == "CA":
            return True
    return False


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


def _raster_valid_pixel_fraction(tif_path: Path) -> float:
    """Compute fraction of finite non-nodata elevation pixels in ``tif_path``."""
    from osgeo import gdal  # type: ignore[import-untyped]

    gdal.UseExceptions()
    ds = gdal.Open(str(tif_path), gdal.GA_ReadOnly)
    if ds is None:
        return 0.0
    try:
        cols, rows = ds.RasterXSize, ds.RasterYSize
        if cols <= 0 or rows <= 0:
            return 0.0
        buf_x, buf_y = min(cols, 256), min(rows, 256)
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
            if -500.0 <= v <= 7000.0:
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


def extract_stac_cog_hrefs(
    stac_payload: dict[str, Any],
    preferred_assets: tuple[str, ...] = ("dtm", "dem"),
) -> list[str]:
    """Extract deduplicated HTTPS COG asset URLs from an NRCan STAC search FeatureCollection."""
    hrefs: list[str] = []
    seen: set[str] = set()
    for feat in stac_payload.get("features") or []:
        if not isinstance(feat, dict):
            continue
        assets = feat.get("assets") or {}
        if not isinstance(assets, dict):
            continue
        chosen: str | None = None
        for key in preferred_assets:
            entry = assets.get(key)
            if isinstance(entry, dict) and entry.get("href"):
                chosen = str(entry["href"]).strip()
                break
        if chosen and _is_allowed_ca_tile_url(chosen) and chosen not in seen:
            seen.add(chosen)
            hrefs.append(chosen)
    return hrefs


class CanadaHRDEMSource:
    """Cloud-native Canada elevation source (NRCan HRDEM 1m/2m LiDAR -> MRDEM-30 CGVD2013 -> GLO-30).

    Uses fast 64×64 COG overview probing (< 0.6 s) on ``hrdem-mosaic-1m`` and ``hrdem-mosaic-2m``
    before streaming full-resolution LiDAR so sheets with partial or absent LiDAR coverage fall
    through rapidly to NRCan's nationwide ``mrdem-30`` (``CGVD2013``) without wasting bandwidth.
    Falls back cleanly to Copernicus GLO-30 (``EGM2008``) if NRCan endpoints are unreachable,
    updating ``resolved_vertical_datum``, ``resolved_source_name``, and ``resolved_attribution``.
    """

    name: str = "ca-hrdem"
    vertical_datum: str = "CGVD2013"
    attribution: str = (
        "Elevation data: Natural Resources Canada (NRCan) CanElevation HRDEM/MRDEM "
        "(Open Government Licence – Canada)"
    )

    def __init__(
        self,
        cache_dir: Path | str | None = None,
        *,
        stac_search_url: str | None = None,
        mrdem_url: str | None = None,
        prefer_hrdem: bool = True,
        lidar_warp_res_m: float = 2.0,
        min_hrdem_coverage_ratio: float = 0.95,
        timeout_s: float = 25.0,
        use_clip_cache: bool = True,
        copernicus_fallback: CopernicusGLO30Source | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        base_cache = Path(
            cache_dir
            if cache_dir is not None
            else os.environ.get("GRIDFORGE_CACHE_DIR", "/tmp/gridforge-cache")
        )
        self.cache_dir: Path = base_cache / "ca_hrdem"
        self.stac_search_url = (
            stac_search_url
            or os.environ.get("GRIDFORGE_NRCAN_STAC_URL", NRCAN_STAC_SEARCH_URL)
        )
        self.mrdem_url = mrdem_url or NRCAN_MRDEM_30_DTM_URL
        self.prefer_hrdem = prefer_hrdem
        self.lidar_warp_res_m = lidar_warp_res_m
        self.min_hrdem_coverage_ratio = min_hrdem_coverage_ratio
        self.timeout_s = timeout_s
        self.use_clip_cache = use_clip_cache
        self._copernicus_fallback = copernicus_fallback or CopernicusGLO30Source(cache_dir=base_cache)
        self._injected_client = client

        self.resolved_tier: str = "mrdem-30"
        self.resolved_source_name: str = self.name
        self.resolved_vertical_datum: str = self.vertical_datum
        self.resolved_attribution: str = self.attribution

    def _gdal_env(self) -> dict[str, str]:
        extra = dict(GDAL_VSICURL_ENV)
        extra["CPL_VSIL_CURL_ALLOWED_EXTENSIONS"] = ".tif,.tiff,.vrt"
        return build_safe_subprocess_env(extra=extra)

    def _stac_search(self, collection: str, clip_bbox: BBox) -> list[str]:
        key = _bbox_hash(clip_bbox)
        cache_path = self.cache_dir / "stac" / f"{collection}_{key}.json"
        if self.use_clip_cache and cache_path.is_file() and cache_path.stat().st_size > 0:
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                if isinstance(cached, list):
                    return [str(u) for u in cached]
            except json.JSONDecodeError:
                logger.warning("Ignoring corrupt cached NRCan STAC file: %s", cache_path)

        owns_client = self._injected_client is None
        http = (
            self._injected_client
            if self._injected_client is not None
            else httpx.Client(
                timeout=httpx.Timeout(self.timeout_s, connect=min(8.0, self.timeout_s)),
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT},
            )
        )
        try:
            resp = http.post(
                self.stac_search_url,
                json={
                    "collections": [collection],
                    "bbox": [clip_bbox.min_x, clip_bbox.min_y, clip_bbox.max_x, clip_bbox.max_y],
                    "limit": 12,
                },
                headers={"User-Agent": USER_AGENT},
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            logger.warning("NRCan STAC search for '%s' failed (%s)", collection, exc)
            return []
        finally:
            if owns_client:
                http.close()

        hrefs = extract_stac_cog_hrefs(payload)
        if self.use_clip_cache:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(hrefs), encoding="utf-8")
            tmp.replace(cache_path)
        return hrefs

    def _probe_cog_overview_coverage(
        self,
        hrefs: list[str],
        layout: SheetLayout,
        work_dir: Path,
        tag: str,
    ) -> float:
        """Fast 64×64 COG overview coverage probe so absent/partial HRDEM tiles fail in < 1 s."""
        probe_tif = work_dir / f"_ca_probe_{tag}.tif"
        b = layout.utm_fetch_bbox
        cmd = [
            "gdalwarp",
            "-overwrite",
            "-ovr",
            "AUTO",
            "-t_srs",
            f"EPSG:{layout.utm_epsg}",
            "-te",
            f"{b.min_x}",
            f"{b.min_y}",
            f"{b.max_x}",
            f"{b.max_y}",
            "-ts",
            "64",
            "64",
            "-r",
            "nearest",
            "-dstnodata",
            "-9999",
            "-ot",
            "Float32",
            *[f"/vsicurl/{u}" for u in hrefs],
            str(probe_tif),
        ]
        try:
            _run_subprocess(cmd, env=self._gdal_env(), context=f"gdalwarp (NRCan {tag} probe)")
            return _raster_valid_pixel_fraction(probe_tif)
        except Exception as exc:
            logger.debug("NRCan %s overview probe failed: %s", tag, exc)
            return 0.0
        finally:
            probe_tif.unlink(missing_ok=True)

    def _fetch_hrdem_tier(
        self,
        collection: str,
        tier_name: str,
        layout: SheetLayout,
        clip_bbox: BBox,
        work_dir: Path,
        out_tif: Path,
    ) -> bool:
        key = _bbox_hash(clip_bbox)
        cache_path = self.cache_dir / "clips" / f"ca_{tier_name}_{layout.utm_epsg}_{key}.tif"
        if self.use_clip_cache and cache_path.is_file() and cache_path.stat().st_size > 0:
            logger.info("Using cached NRCan %s clip: %s", tier_name, cache_path)
            shutil.copy2(cache_path, out_tif)
            self.resolved_tier = tier_name
            self.resolved_source_name = self.name
            self.resolved_vertical_datum = self.vertical_datum
            self.resolved_attribution = self.attribution
            return True

        hrefs = self._stac_search(collection, clip_bbox)
        if not hrefs:
            return False

        # Fast overview probe avoids multi-second downloads over national parks lacking LiDAR
        probe_cov = self._probe_cog_overview_coverage(hrefs, layout, work_dir, tier_name)
        if probe_cov < self.min_hrdem_coverage_ratio:
            logger.info(
                "NRCan %s coverage incomplete (overview=%.1f%% < %.1f%%); trying next tier",
                collection,
                probe_cov * 100.0,
                self.min_hrdem_coverage_ratio * 100.0,
            )
            return False

        tmp_tif = work_dir / f"_ca_{tier_name}_tmp.tif"
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
            *[f"/vsicurl/{u}" for u in hrefs],
            str(tmp_tif),
        ]
        logger.info("Streaming NRCan %s (%d COG(s)) at %.1f m -> %s", collection, len(hrefs), self.lidar_warp_res_m, out_tif)
        try:
            _run_subprocess(cmd, env=self._gdal_env(), context=f"gdalwarp (NRCan {collection})")
            cov = _raster_valid_pixel_fraction(tmp_tif)
            if cov < self.min_hrdem_coverage_ratio:
                logger.info("NRCan %s full-res coverage %.1f%% < %.1f%%; trying next tier", collection, cov * 100.0, self.min_hrdem_coverage_ratio * 100.0)
                tmp_tif.unlink(missing_ok=True)
                return False
        except Exception as exc:
            logger.warning("NRCan %s stream failed (%s); trying next tier", collection, exc)
            tmp_tif.unlink(missing_ok=True)
            return False

        tmp_tif.replace(out_tif)
        self.resolved_tier = tier_name
        self.resolved_source_name = self.name
        self.resolved_vertical_datum = self.vertical_datum
        self.resolved_attribution = self.attribution
        if self.use_clip_cache and out_tif.is_file() and out_tif.stat().st_size > 0:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_cache = cache_path.with_suffix(".tif.tmp")
            shutil.copy2(out_tif, tmp_cache)
            tmp_cache.replace(cache_path)
        return True

    def _fetch_mrdem_30(
        self,
        layout: SheetLayout,
        clip_bbox: BBox,
        work_dir: Path,
        out_tif: Path,
    ) -> bool:
        key = _bbox_hash(clip_bbox)
        cache_path = self.cache_dir / "clips" / f"ca_mrdem30_{layout.utm_epsg}_{key}.tif"
        if self.use_clip_cache and cache_path.is_file() and cache_path.stat().st_size > 0:
            logger.info("Using cached NRCan MRDEM-30 clip: %s", cache_path)
            shutil.copy2(cache_path, out_tif)
            self.resolved_tier = "mrdem-30"
            self.resolved_source_name = self.name
            self.resolved_vertical_datum = self.vertical_datum
            self.resolved_attribution = self.attribution
            return True

        hrefs = self._stac_search("mrdem-30", clip_bbox)
        if not hrefs:
            hrefs = [self.mrdem_url]

        tmp_tif = work_dir / "_ca_mrdem30_tmp.tif"
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
            "20",
            "20",
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
            *[f"/vsicurl/{u}" for u in hrefs],
            str(tmp_tif),
        ]
        logger.info("Streaming NRCan nationwide MRDEM-30 (CGVD2013) -> %s", out_tif)
        try:
            _run_subprocess(cmd, env=self._gdal_env(), context="gdalwarp (NRCan MRDEM-30)")
            cov = _raster_valid_pixel_fraction(tmp_tif)
            if cov < 0.05:
                tmp_tif.unlink(missing_ok=True)
                return False
        except Exception as exc:
            logger.warning("NRCan MRDEM-30 failed (%s); falling back", exc)
            tmp_tif.unlink(missing_ok=True)
            return False

        tmp_tif.replace(out_tif)
        self.resolved_tier = "mrdem-30"
        self.resolved_source_name = self.name
        self.resolved_vertical_datum = self.vertical_datum
        self.resolved_attribution = self.attribution
        if self.use_clip_cache and out_tif.is_file() and out_tif.stat().st_size > 0:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_cache = cache_path.with_suffix(".tif.tmp")
            shutil.copy2(out_tif, tmp_cache)
            tmp_cache.replace(cache_path)
        return True

    def fetch_dem(self, layout: SheetLayout, work_dir: Path) -> Path:
        """Fetch NRCan HRDEM 1m/2m LiDAR -> MRDEM-30 (CGVD2013) -> Copernicus GLO-30 (EGM2008)."""
        work_dir.mkdir(parents=True, exist_ok=True)
        clip_bbox = _round_enclosing_bbox(layout.wgs84_fetch_bbox, step_deg=0.001)
        out_tif = work_dir / "dem_raw.tif"

        if not is_in_canada_coverage(clip_bbox):
            raise RuntimeError(
                f"Outside Canadian NRCan DEM coverage for requested footprint {clip_bbox.as_tuple()}"
            )

        if self.prefer_hrdem and layout.spec.scale <= 25000:
            if self._fetch_hrdem_tier("hrdem-mosaic-1m", "hrdem-1m", layout, clip_bbox, work_dir, out_tif):
                return out_tif
            if self._fetch_hrdem_tier("hrdem-mosaic-2m", "hrdem-2m", layout, clip_bbox, work_dir, out_tif):
                return out_tif

        if self._fetch_mrdem_30(layout, clip_bbox, work_dir, out_tif):
            return out_tif

        logger.warning(
            "NRCan HRDEM & MRDEM-30 unavailable for %s; falling back to Copernicus GLO-30 (EGM2008)",
            clip_bbox.as_tuple(),
        )
        fallback_path = self._copernicus_fallback.fetch_dem(layout, work_dir)
        self.resolved_tier = "glo30-fallback"
        self.resolved_source_name = self._copernicus_fallback.name
        self.resolved_vertical_datum = "EGM2008"
        self.resolved_attribution = self._copernicus_fallback.attribution
        return fallback_path
