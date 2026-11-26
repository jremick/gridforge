"""FastAPI web studio backend for GridForge (zero QGIS imports, < 120 MB RAM)."""

from __future__ import annotations

import logging
import math
import os
import re
import subprocess
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import mgrs
from fastapi import FastAPI, HTTPException, Query, Request, Response, status
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from pyproj import Transformer

from gridforge import __version__
from gridforge.config import BBox, Orientation, PaperSize, SheetSpec, Units
from gridforge.geo.crs import (
    MAX_UTM_LAT,
    MIN_UTM_LAT,
    compute_atlas_layouts,
    compute_sheet_layout,
    parse_center,
)
from gridforge.geo.declination import compute_declination
from gridforge.geo.strip_atlas import (
    parse_route_bytes,
    parse_route_geojson,
    plan_strip_atlas,
)
from gridforge.geo.tactical import RulerMetricsResponse
from gridforge.geo.waypoints import compute_route_metrics
from gridforge.packs.builder import discover_packs
from gridforge.web import db, projects as projects_store
from gridforge.web.access import SameOriginWritesMiddleware
from gridforge.web.basemap import BasemapInfoResponse, MBTilesBasemapStore
from gridforge.web.pack_tasks import (
    _TASK_ID_RE,
    PackBuildRequest,
    PackRegionsResponse,
    PackTaskManager,
    PackTaskRecord,
    PackTasksListResponse,
    get_pack_regions_response,
)
from gridforge.web.projects import (
    ProjectCreateRequest,
    ProjectRecord,
    ProjectsListResponse,
    ProjectUpdateRequest,
)
from gridforge.web.schemas import (
    CreateJobRequest,
    FootprintRequest,
    FootprintResponse,
    GeocodeResponse,
    GeocodeResult,
    HealthResponse,
    JobRecord,
    JobsListResponse,
    PacksResponse,
    PackSummary,
    RFLinkProfileRequest,
    RFLinkProfileResponse,
    RouteMetricsRequest,
    RouteMetricsResponse,
    RulerMetricsRequest,
    StripPlanRequest,
    StripPlanResponse,
    ViewshedPreviewRequest,
    ViewshedPreviewResponse,
)
from gridforge.web.viewshed import (
    compute_rf_link_preview,
    compute_ruler_preview,
    compute_viewshed_preview,
)

logger = logging.getLogger(__name__)

_HTTP_422: int = getattr(status, "HTTP_422_UNPROCESSABLE_CONTENT", 422)
MAX_UPLOAD_BYTES: int = 25 * 1024 * 1024  # 25 MiB
ALLOWED_OVERLAY_EXTS: frozenset[str] = frozenset({".geojson", ".json", ".gpx", ".csv"})
_JOB_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{8,64}$")
_PACK_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_MGRS_SPLIT_RE = re.compile(r"^0?([1-9]|[1-5]\d|60)([C-HJ-NP-X])([A-HJ-NP-Z]{2})(\d*)$")

NOMINATIM_URL: str = "https://nominatim.openstreetmap.org/search"
NOMINATIM_USER_AGENT: str = "gridforge/0.1 (+https://github.com/jremick/gridforge)"
_GEOCODE_CACHE_TTL_S: float = 300.0
_GEOCODE_CACHE_MAX: int = 256
_geocode_cache: OrderedDict[tuple[str, int], tuple[float, list[GeocodeResult]]] = OrderedDict()

_MEDIA_TYPES: dict[str, str] = {
    ".pdf": "application/pdf",
    ".mbtiles": "application/vnd.sqlite3",
    ".kmz": "application/vnd.google-earth.kmz",
    ".gpkg": "application/geopackage+sqlite3",
    ".sqlitedb": "application/vnd.sqlite3",
    ".zip": "application/zip",
}


def _search_nominatim(query: str, limit: int = 5) -> list[GeocodeResult]:
    """Search OSM Nominatim with in-memory LRU/TTL cache and graceful offline fallback."""
    cleaned = query.strip()
    if len(cleaned) < 2:
        return []

    cache_key = (cleaned.lower(), limit)
    now = time.monotonic()
    cached = _geocode_cache.get(cache_key)
    if cached is not None:
        ts, items = cached
        if now - ts <= _GEOCODE_CACHE_TTL_S:
            _geocode_cache.move_to_end(cache_key)
            return list(items)
        _geocode_cache.pop(cache_key, None)

    url = os.environ.get("GRIDFORGE_NOMINATIM_URL", NOMINATIM_URL).strip()
    try:
        resp = httpx.get(
            url,
            params={"format": "jsonv2", "limit": str(limit), "q": cleaned},
            headers={"User-Agent": NOMINATIM_USER_AGENT, "Accept": "application/json"},
            timeout=5.0,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        logger.debug("Geocode lookup unavailable for %r (%s); returning empty results", cleaned, exc)
        return []

    if not isinstance(payload, list):
        return []

    results: list[GeocodeResult] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        try:
            lat = float(item["lat"])
            lon = float(item["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (MIN_UTM_LAT <= lat <= MAX_UTM_LAT and -180.0 <= lon <= 180.0):
            continue
        display_name = str(item.get("display_name") or "").strip()
        if not display_name:
            continue
        short_name = str(item.get("name") or "").strip() or display_name.split(",", 1)[0].strip()
        place_type = str(item.get("type") or item.get("class") or "place").strip() or "place"
        results.append(
            GeocodeResult(
                display_name=display_name,
                short_name=short_name,
                lat=round(lat, 6),
                lon=round(lon, 6),
                type=place_type,
            )
        )

    _geocode_cache[cache_key] = (now, results)
    while len(_geocode_cache) > _GEOCODE_CACHE_MAX:
        _geocode_cache.popitem(last=False)
    return list(results)


def _decode_mgrs(raw: str | bytes) -> str:
    return raw.decode("ascii") if isinstance(raw, bytes) else str(raw)


def _split_mgrs(raw: str) -> tuple[str, str, str]:
    match = _MGRS_SPLIT_RE.match(raw.strip().upper())
    if not match:
        raise ValueError(f"Unexpected MGRS string: '{raw}'")
    return (f"{int(match.group(1))}{match.group(2)}", match.group(3), match.group(4))


def _sanitize_upload_filename(raw_name: str | None) -> str:
    leaf = Path(raw_name or "overlay.geojson").name
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", leaf).strip("._")
    if not safe:
        safe = "overlay.geojson"
    ext = Path(safe).suffix.lower()
    if ext not in ALLOWED_OVERLAY_EXTS:
        allowed = ", ".join(sorted(ALLOWED_OVERLAY_EXTS))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported overlay file type '{ext}'. Allowed: {allowed}.",
        )
    return safe


def _utm_box_to_ring(bbox: BBox, to_wgs84: Transformer, edge_steps: int = 8) -> list[list[float]]:
    """Build a closed densified WGS84 polygon exterior ring (CCW) for a UTM BBox."""
    ring_xs: list[float] = []
    ring_ys: list[float] = []
    # Bottom (SW -> SE)
    for i in range(edge_steps):
        ring_xs.append(bbox.min_x + bbox.width * i / edge_steps)
        ring_ys.append(bbox.min_y)
    # Right (SE -> NE)
    for i in range(edge_steps):
        ring_xs.append(bbox.max_x)
        ring_ys.append(bbox.min_y + bbox.height * i / edge_steps)
    # Top (NE -> NW)
    for i in range(edge_steps):
        ring_xs.append(bbox.max_x - bbox.width * i / edge_steps)
        ring_ys.append(bbox.max_y)
    # Left (NW -> SW)
    for i in range(edge_steps):
        ring_xs.append(bbox.min_x)
        ring_ys.append(bbox.max_y - bbox.height * i / edge_steps)

    ring_lons, ring_lats = to_wgs84.transform(ring_xs, ring_ys)
    ring_coords = [[round(float(lo), 7), round(float(la), 7)] for lo, la in zip(ring_lons, ring_lats)]
    ring_coords.append(ring_coords[0])
    return ring_coords


def _build_footprint_preview(req: FootprintRequest) -> FootprintResponse:
    """Compute live true-scale UTM footprint (single or multi-sheet atlas), 1 km MGRS preview, and WMM2025."""
    try:
        lat, lon, canonical_mgrs = parse_center(req.center)
    except ValueError as exc:
        raise HTTPException(status_code=_HTTP_422, detail=str(exc)) from exc

    try:
        spec = SheetSpec(
            center_lat=lat,
            center_lon=lon,
            center_mgrs=canonical_mgrs,
            scale=req.scale,
            paper=req.paper,
            orientation=req.orientation,
            units=req.units,
            date_iso=req.date_iso,
        )
        layout = compute_sheet_layout(spec)
        sheets = compute_atlas_layouts(layout, req.grid_cols, req.grid_rows)
        declination = compute_declination(layout, elevation_m=0.0)
    except ValueError as exc:
        raise HTTPException(status_code=_HTTP_422, detail=str(exc)) from exc

    to_wgs84 = Transformer.from_crs(f"EPSG:{layout.utm_epsg}", "EPSG:4326", always_xy=True)
    ground_w = layout.utm_bbox.width
    ground_h = layout.utm_bbox.height
    cols = req.grid_cols
    rows = req.grid_rows
    primary_row = rows // 2
    primary_col = cols // 2

    # 1. Edge-matched multi-sheet UTM polygons in shared EPSG:{layout.utm_epsg}
    sheet_features: list[dict[str, Any]] = []
    for idx, (sheet_id, page) in enumerate(sheets):
        r, c = divmod(idx, cols)
        ring_coords = _utm_box_to_ring(page.utm_bbox, to_wgs84)
        sheet_features.append(
            {
                "type": "Feature",
                "properties": {
                    "sheet_id": sheet_id,
                    "label": sheet_id,
                    "row": r,
                    "col": c,
                    "is_primary": (r == primary_row and c == primary_col),
                    "scale": spec.scale,
                    "paper": spec.paper.value,
                    "orientation": spec.orientation.value,
                    "utm_epsg": layout.utm_epsg,
                    "gzd": page.gzd,
                },
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [ring_coords],
                },
            }
        )

    footprint_geojson: dict[str, Any] = {
        "type": "FeatureCollection",
        "features": sheet_features,
    }

    # Combined atlas UTM bbox for continuous 1 km MGRS preview grid lines
    atlas_bbox = BBox(
        min_x=layout.center_easting - (cols * ground_w) / 2.0,
        min_y=layout.center_northing - (rows * ground_h) / 2.0,
        max_x=layout.center_easting + (cols * ground_w) / 2.0,
        max_y=layout.center_northing + (rows * ground_h) / 2.0,
    )

    # 2. Pure in-memory 1 km MGRS Easting & Northing preview lines across full atlas extent
    e_start = int(math.ceil(atlas_bbox.min_x / 1000.0)) * 1000
    e_end = int(math.floor(atlas_bbox.max_x / 1000.0)) * 1000
    n_start = int(math.ceil(atlas_bbox.min_y / 1000.0)) * 1000
    n_end = int(math.floor(atlas_bbox.max_y / 1000.0)) * 1000

    grid_features: list[dict[str, Any]] = []
    line_samples = max(6, 4 * max(cols, rows))

    for e in range(e_start, e_end + 1, 1000):
        xs = [float(e)] * line_samples
        ys = [
            atlas_bbox.min_y + atlas_bbox.height * i / (line_samples - 1)
            for i in range(line_samples)
        ]
        lons, lats = to_wgs84.transform(xs, ys)
        principal = f"{(e // 1000) % 100:02d}"
        grid_features.append(
            {
                "type": "Feature",
                "properties": {
                    "axis": "E",
                    "coord_m": e,
                    "label": f"{e}m E",
                    "principal_digits": principal,
                    "is_100km": (e % 100000 == 0),
                },
                "geometry": {
                    "type": "LineString",
                    "coordinates": [
                        [round(float(lo), 7), round(float(la), 7)]
                        for lo, la in zip(lons, lats)
                    ],
                },
            }
        )

    for n in range(n_start, n_end + 1, 1000):
        xs = [
            atlas_bbox.min_x + atlas_bbox.width * i / (line_samples - 1)
            for i in range(line_samples)
        ]
        ys = [float(n)] * line_samples
        lons, lats = to_wgs84.transform(xs, ys)
        eff_n = n if n >= 0 else 10_000_000 + n
        principal = f"{(eff_n // 1000) % 100:02d}"
        grid_features.append(
            {
                "type": "Feature",
                "properties": {
                    "axis": "N",
                    "coord_m": n,
                    "label": f"{n}m N",
                    "principal_digits": principal,
                    "is_100km": (n % 100000 == 0),
                },
                "geometry": {
                    "type": "LineString",
                    "coordinates": [
                        [round(float(lo), 7), round(float(la), 7)]
                        for lo, la in zip(lons, lats)
                    ],
                },
            }
        )

    grid_geojson: dict[str, Any] = {
        "type": "FeatureCollection",
        "features": grid_features,
    }

    # 3. Center 10 m MGRS & visible 100 km square IDs across the full atlas extent
    converter = mgrs.MGRS()
    c_gzd, c_sq, c_digits = _split_mgrs(
        _decode_mgrs(converter.toMGRS(lat, lon, MGRSPrecision=4))
    )
    center_mgrs_10m = f"{c_gzd} {c_sq} {c_digits[:4]} {c_digits[4:]}"

    square_ids: list[str] = [c_sq]
    for fx in (0.05, 0.5, 0.95):
        for fy in (0.05, 0.5, 0.95):
            px = atlas_bbox.min_x + fx * atlas_bbox.width
            py = atlas_bbox.min_y + fy * atlas_bbox.height
            plon, plat = to_wgs84.transform(px, py)
            if MIN_UTM_LAT <= plat <= MAX_UTM_LAT:
                _, sq, _ = _split_mgrs(
                    _decode_mgrs(converter.toMGRS(plat, plon, MGRSPrecision=0))
                )
                if sq not in square_ids:
                    square_ids.append(sq)

    ground_w_km = round(ground_w / 1000.0, 3)
    ground_h_km = round(ground_h / 1000.0, 3)

    return FootprintResponse(
        center_lat=round(lat, 6),
        center_lon=round(lon, 6),
        center_mgrs_10m=center_mgrs_10m,
        utm_epsg=layout.utm_epsg,
        utm_zone=layout.utm_zone,
        utm_hemisphere=layout.utm_hemisphere,
        gzd=layout.gzd,
        square_ids_100km=square_ids,
        page_width_mm=layout.page_width_mm,
        page_height_mm=layout.page_height_mm,
        frame_width_mm=layout.frame_width_mm,
        frame_height_mm=layout.frame_height_mm,
        ground_width_km=ground_w_km,
        ground_height_km=ground_h_km,
        grid_cols=cols,
        grid_rows=rows,
        sheet_count=cols * rows,
        total_ground_width_km=round((ground_w * cols) / 1000.0, 3),
        total_ground_height_km=round((ground_h * rows) / 1000.0, 3),
        grid_spacing_mm=round(1000.0 / spec.scale * 1000.0, 3),
        declination=declination,
        footprint_geojson=footprint_geojson,
        grid_geojson=grid_geojson,
    )


def _resolve_default_web_dist() -> Path:
    env_dist = os.environ.get("GRIDFORGE_WEB_DIST")
    if env_dist:
        return Path(env_dist)
    for candidate in (
        Path("/app/web/dist"),
        Path(__file__).resolve().parents[3] / "web" / "dist",
    ):
        if (candidate / "index.html").is_file():
            return candidate
    return Path("/app/web/dist")


def _resolve_default_data_dir() -> Path:
    env_dir = os.environ.get("GRIDFORGE_DATA_DIR")
    if env_dir:
        return Path(env_dir)
    preferred = Path("/data/jobs")
    if preferred.exists() or (preferred.parent.exists() and os.access(preferred.parent, os.W_OK)):
        return preferred
    return Path("/tmp/gridforge-data/jobs")


def create_app(
    *,
    data_dir: Path | str | None = None,
    packs_dir: Path | str | None = None,
    web_dist_dir: Path | str | None = None,
    memory_budget_mb: int | None = None,
    basemap_mbtiles: Path | str | None = None,
    pack_builder: Callable[..., Any] | None = None,
) -> FastAPI:
    """Create and configure the GridForge FastAPI application."""
    resolved_data_dir = (
        Path(data_dir).resolve()
        if data_dir is not None
        else _resolve_default_data_dir().resolve()
    )
    resolved_packs_dir = Path(
        packs_dir if packs_dir is not None else os.environ.get("GRIDFORGE_PACKS_DIR", "/data/packs")
    ).resolve()
    resolved_web_dist = (
        Path(web_dist_dir).resolve()
        if web_dist_dir is not None
        else _resolve_default_web_dist().resolve()
    )
    resolved_mem_mb = (
        memory_budget_mb
        if memory_budget_mb is not None
        else int(os.environ.get("GRIDFORGE_MEMORY_BUDGET_MB", "3072"))
    )

    db_path = resolved_data_dir / "gridforge_jobs.sqlite3"
    db.init_db(db_path)

    basemap_store = MBTilesBasemapStore(
        packs_dir=resolved_packs_dir,
        explicit_path=basemap_mbtiles,
        allow_default_data_path=(packs_dir is None and basemap_mbtiles is None),
    )
    pack_manager = PackTaskManager(
        resolved_packs_dir,
        pack_builder=pack_builder,
    )

    application = FastAPI(
        title="GridForge Map Studio API",
        version=__version__,
        description="Self-hosted military-style MGRS topographic map generator & offline pack studio.",
    )
    application.add_middleware(SameOriginWritesMiddleware)
    allowed_hosts = ["localhost", "127.0.0.1", "[::1]"]
    extra_hosts = os.environ.get("GRIDFORGE_ALLOWED_HOSTS", "").split(",")
    allowed_hosts.extend(host.strip() for host in extra_hosts if host.strip())
    application.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=allowed_hosts,
        www_redirect=False,
    )
    application.state.data_dir = resolved_data_dir
    application.state.packs_dir = resolved_packs_dir
    application.state.db_path = db_path
    application.state.web_dist_dir = resolved_web_dist
    application.state.basemap_store = basemap_store
    application.state.pack_manager = pack_manager

    @application.get("/api/health", response_model=HealthResponse)
    def api_health() -> HealthResponse:
        worker_alive, last_seen = db.get_heartbeat(db_path)
        queue_depth = db.get_queue_depth(db_path)
        packs = discover_packs(resolved_packs_dir)
        return HealthResponse(
            status="ok",
            queue_depth=queue_depth,
            worker_alive=worker_alive,
            worker_last_seen=last_seen,
            memory_budget_mb=resolved_mem_mb,
            packs_count=len(packs),
        )

    @application.get("/api/basemap/info", response_model=BasemapInfoResponse)
    def api_basemap_info(pack_id: str | None = Query(default=None)) -> BasemapInfoResponse:
        if pack_id is not None and not _PACK_ID_RE.match(pack_id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid pack_id '{pack_id}'.",
            )
        return basemap_store.get_info(pack_id=pack_id)

    @application.get("/api/basemap/{z}/{x}/{y}.{ext}")
    def api_basemap_tile(
        z: int,
        x: int,
        y: int,
        ext: str,
        pack_id: str | None = Query(default=None),
    ) -> Response:
        if ext.lower() not in {"png", "jpg", "jpeg", "webp"}:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unsupported basemap tile extension '.{ext}'.",
            )
        if pack_id is not None and not _PACK_ID_RE.match(pack_id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid pack_id '{pack_id}'.",
            )
        result = basemap_store.get_tile(z, x, y, requested_ext=ext, pack_id=pack_id)
        if result is None:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        tile_bytes, media_type = result
        return Response(
            content=tile_bytes,
            media_type=media_type,
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @application.get("/api/packs", response_model=PacksResponse)
    def api_packs() -> PacksResponse:
        manifests = discover_packs(resolved_packs_dir)
        summaries = [
            PackSummary(
                pack_id=m.pack_id,
                name=m.name,
                region=m.region,
                bbox_wgs84=m.bbox_wgs84,
                utm_zones=m.utm_zones,
                vertical_datum=m.vertical_datum,
                size_bytes=m.size_bytes,
                has_dem=m.dem_path is not None,
                has_osm=m.osm_pbf_path is not None,
                has_basemap=bool(
                    m.basemap_path or basemap_store._resolve_pack_basemap(m.pack_id) is not None
                ),
                overlays=sorted(m.overlays.keys()),
            )
            for m in manifests
        ]
        return PacksResponse(packs=summaries)

    @application.get("/api/packs/regions", response_model=PackRegionsResponse)
    def api_pack_regions() -> PackRegionsResponse:
        return get_pack_regions_response()

    @application.post(
        "/api/packs/build",
        response_model=PackTaskRecord,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def api_build_pack(req: PackBuildRequest) -> PackTaskRecord:
        try:
            return pack_manager.enqueue_build(req)
        except ValueError as exc:
            raise HTTPException(status_code=_HTTP_422, detail=str(exc)) from exc

    @application.get("/api/packs/tasks", response_model=PackTasksListResponse)
    def api_list_pack_tasks() -> PackTasksListResponse:
        return PackTasksListResponse(tasks=pack_manager.list_tasks())

    @application.get("/api/packs/tasks/{task_id}", response_model=PackTaskRecord)
    def api_get_pack_task(task_id: str) -> PackTaskRecord:
        if not _TASK_ID_RE.match(task_id):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid task_id.")
        record = pack_manager.get_task(task_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pack task not found.")
        return record

    @application.delete("/api/packs/{pack_id}")
    def api_delete_pack(pack_id: str) -> dict[str, Any]:
        try:
            deleted = pack_manager.delete_pack(pack_id)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        if not deleted:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Offline pack '{pack_id}' is not installed.",
            )
        return {"deleted": True, "pack_id": pack_id}

    @application.post("/api/viewshed-preview", response_model=ViewshedPreviewResponse)
    def api_viewshed_preview(req: ViewshedPreviewRequest) -> ViewshedPreviewResponse:
        if req.pack_id is not None:
            if not _PACK_ID_RE.match(req.pack_id):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid pack_id '{req.pack_id}'.",
                )
            installed_ids = {p.pack_id for p in discover_packs(resolved_packs_dir)}
            if req.pack_id not in installed_ids:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Offline pack '{req.pack_id}' is not installed.",
                )
        try:
            return compute_viewshed_preview(
                req,
                packs_dir=resolved_packs_dir,
                pack_dir_finder=pack_manager.find_pack_dir,
            )
        except ValueError as exc:
            raise HTTPException(status_code=_HTTP_422, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("Viewshed preview failed: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Viewshed preview failed: {exc}",
            ) from exc

    @application.post("/api/rf-link-profile", response_model=RFLinkProfileResponse)
    def api_rf_link_profile(req: RFLinkProfileRequest) -> RFLinkProfileResponse:
        if req.pack_id is not None:
            if not _PACK_ID_RE.match(req.pack_id):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid pack_id '{req.pack_id}'.",
                )
            installed_ids = {p.pack_id for p in discover_packs(resolved_packs_dir)}
            if req.pack_id not in installed_ids:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Offline pack '{req.pack_id}' is not installed.",
                )
        try:
            return compute_rf_link_preview(
                req,
                packs_dir=resolved_packs_dir,
                pack_dir_finder=pack_manager.find_pack_dir,
            )
        except ValueError as exc:
            raise HTTPException(status_code=_HTTP_422, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("RF link profile failed: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"RF link profile failed: {exc}",
            ) from exc

    @application.post("/api/ruler-metrics", response_model=RulerMetricsResponse)
    def api_ruler_metrics(req: RulerMetricsRequest) -> RulerMetricsResponse:
        if req.pack_id is not None:
            if not _PACK_ID_RE.match(req.pack_id):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid pack_id '{req.pack_id}'.",
                )
            installed_ids = {p.pack_id for p in discover_packs(resolved_packs_dir)}
            if req.pack_id not in installed_ids:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Offline pack '{req.pack_id}' is not installed.",
                )
        try:
            return compute_ruler_preview(
                req,
                packs_dir=resolved_packs_dir,
                pack_dir_finder=pack_manager.find_pack_dir,
            )
        except ValueError as exc:
            raise HTTPException(status_code=_HTTP_422, detail=str(exc)) from exc

    @application.get("/api/projects", response_model=ProjectsListResponse)
    def api_list_projects(limit: int = Query(default=100, ge=1, le=500)) -> ProjectsListResponse:
        return ProjectsListResponse(projects=projects_store.list_projects(db_path, limit=limit))

    @application.post(
        "/api/projects",
        response_model=ProjectRecord,
        status_code=status.HTTP_201_CREATED,
    )
    def api_create_project(req: ProjectCreateRequest) -> ProjectRecord:
        if req.last_job_id is not None and not _JOB_ID_RE.match(req.last_job_id):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid last_job_id.")
        return projects_store.create_project(db_path, req)

    @application.get("/api/projects/{project_id}", response_model=ProjectRecord)
    def api_get_project(project_id: str) -> ProjectRecord:
        if not projects_store.is_valid_project_id(project_id):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid project_id.")
        record = projects_store.get_project(db_path, project_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found.")
        return record

    @application.patch("/api/projects/{project_id}", response_model=ProjectRecord)
    def api_update_project(project_id: str, req: ProjectUpdateRequest) -> ProjectRecord:
        if not projects_store.is_valid_project_id(project_id):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid project_id.")
        if req.last_job_id is not None and not _JOB_ID_RE.match(req.last_job_id):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid last_job_id.")
        record = projects_store.update_project(db_path, project_id, req)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found.")
        return record

    @application.delete("/api/projects/{project_id}")
    def api_delete_project(project_id: str) -> dict[str, Any]:
        if not projects_store.is_valid_project_id(project_id):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid project_id.")
        deleted = projects_store.delete_project(db_path, project_id)
        if not deleted:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found.")
        return {"deleted": True, "id": project_id, "project_id": project_id}

    @application.get("/api/geocode", response_model=GeocodeResponse)
    def api_geocode(
        q: str = Query(default="", max_length=200),
        limit: int = Query(default=5, ge=1, le=10),
    ) -> GeocodeResponse:
        return GeocodeResponse(results=_search_nominatim(q, limit=limit))

    @application.post("/api/footprint", response_model=FootprintResponse)
    def api_footprint(req: FootprintRequest) -> FootprintResponse:
        return _build_footprint_preview(req)

    @application.post("/api/strip-plan", response_model=StripPlanResponse)
    async def api_strip_plan(request: Request) -> StripPlanResponse:
        content_type = request.headers.get("content-type", "").lower()
        if "multipart/form-data" in content_type:
            form = await request.form()
            route_file = form.get("route_file")
            if route_file is None or not hasattr(route_file, "read"):
                raise HTTPException(
                    status_code=_HTTP_422,
                    detail="Multipart /api/strip-plan requires a 'route_file' (.gpx or .geojson) upload.",
                )
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = await route_file.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=f"Route file exceeds maximum size of {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
                    )
                chunks.append(chunk)
            raw_bytes = b"".join(chunks)
            filename = str(getattr(route_file, "filename", None) or "route.geojson")

            try:
                scale = int(form.get("scale", 25000))
                paper = PaperSize(str(form.get("paper", "a4")).lower())
                orientation = Orientation(str(form.get("orientation", "portrait")).lower())
                units = Units(str(form.get("units", "metric")).lower())
                overlap_pct = float(form.get("overlap_pct", 10.0))
                max_sheets = int(form.get("max_sheets", 12))
                route_lonlat = parse_route_bytes(raw_bytes, filename)
                return plan_strip_atlas(
                    route_lonlat,
                    scale=scale,
                    paper=paper,
                    orientation=orientation,
                    units=units,
                    overlap_pct=overlap_pct,
                    max_sheets=max_sheets,
                )
            except (ValueError, ValidationError) as exc:
                raise HTTPException(status_code=_HTTP_422, detail=str(exc)) from exc

        try:
            raw_body = await request.body()
            plan_req = StripPlanRequest.model_validate_json(raw_body)
            route_lonlat = parse_route_geojson(plan_req.route_geojson)
            return plan_strip_atlas(
                route_lonlat,
                scale=plan_req.scale,
                paper=plan_req.paper,
                orientation=plan_req.orientation,
                units=plan_req.units,
                overlap_pct=plan_req.overlap_pct,
                max_sheets=plan_req.max_sheets,
            )
        except (ValueError, ValidationError) as exc:
            raise HTTPException(status_code=_HTTP_422, detail=str(exc)) from exc

    @application.post("/api/route-metrics", response_model=RouteMetricsResponse)
    def api_route_metrics(req: RouteMetricsRequest) -> RouteMetricsResponse:
        try:
            return compute_route_metrics(
                req.waypoints,
                units=req.units,
                date_iso=req.date_iso,
            )
        except ValueError as exc:
            raise HTTPException(status_code=_HTTP_422, detail=str(exc)) from exc

    @application.post(
        "/api/jobs",
        response_model=JobRecord,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def api_create_job(request: Request) -> JobRecord:
        content_type = request.headers.get("content-type", "").lower()
        upload_file = None

        if (
            "multipart/form-data" in content_type
            or "application/x-www-form-urlencoded" in content_type
        ):
            form = await request.form()
            raw_payload = form.get("payload")
            if raw_payload is None or not isinstance(raw_payload, str):
                raise HTTPException(
                    status_code=_HTTP_422,
                    detail="Form request must include a JSON string 'payload' field.",
                )
            try:
                job_req = CreateJobRequest.model_validate_json(raw_payload)
            except ValidationError as exc:
                raise HTTPException(
                    status_code=_HTTP_422,
                    detail=exc.errors(),
                ) from exc
            maybe_file = form.get("overlay_file")
            if maybe_file is not None and hasattr(maybe_file, "read"):
                # Ignore empty browser file input fields with no filename
                if getattr(maybe_file, "filename", ""):
                    upload_file = maybe_file
        else:
            try:
                raw_body = await request.body()
                job_req = CreateJobRequest.model_validate_json(raw_body)
            except ValidationError as exc:
                raise HTTPException(
                    status_code=_HTTP_422,
                    detail=exc.errors(),
                ) from exc

        assert job_req.center is not None
        try:
            lat, lon, canonical_mgrs = parse_center(job_req.center)
            # Validate full SheetSpec bounds and atlas projection early before enqueueing.
            spec = SheetSpec(
                center_lat=lat,
                center_lon=lon,
                center_mgrs=canonical_mgrs,
                scale=job_req.scale,
                paper=job_req.paper,
                orientation=job_req.orientation,
                color_mode=job_req.color_mode,
                units=job_req.units,
                title=job_req.title,
                date_iso=job_req.date_iso,
                geopdf=job_req.outputs.geopdf,
                dpi=job_req.dpi,
                carto=job_req.carto,
                dem_source=job_req.dem_source,
                us_overlays=job_req.us_overlays,
                au_overlays=job_req.au_overlays,
                nz_overlays=job_req.nz_overlays,
                ca_overlays=job_req.ca_overlays,
                waypoints=job_req.waypoints,
                viewsheds=job_req.viewsheds,
                rf_links=job_req.rf_links,
                tactical_graphics=job_req.tactical_graphics,
                include_calibration_page=job_req.include_calibration_page,
                include_route_card=job_req.include_route_card,
                include_elevation_profile=job_req.include_elevation_profile,
                include_atlas_index=job_req.include_atlas_index,
                include_rf_link_card=job_req.include_rf_link_card,
                show_fold_marks=job_req.show_fold_marks,
            )
            anchor = compute_sheet_layout(spec)
            if job_req.outputs.pdf and not job_req.strip_sheets and job_req.grid_cols * job_req.grid_rows > 1:
                compute_atlas_layouts(anchor, job_req.grid_cols, job_req.grid_rows)
            elif job_req.outputs.pdf and job_req.strip_sheets:
                for sh in job_req.strip_sheets:
                    compute_sheet_layout(
                        spec.model_copy(
                            update={
                                "center_lat": float(sh.center_lat),
                                "center_lon": float(sh.center_lon),
                                "center_mgrs": None,
                            }
                        )
                    )
        except (ValueError, ValidationError) as exc:
            raise HTTPException(
                status_code=_HTTP_422,
                detail=str(exc),
            ) from exc

        if job_req.pack_id is not None:
            if not _PACK_ID_RE.match(job_req.pack_id):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid pack_id '{job_req.pack_id}'.",
                )
            installed_ids = {p.pack_id for p in discover_packs(resolved_packs_dir)}
            if job_req.pack_id not in installed_ids:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Offline pack '{job_req.pack_id}' is not installed.",
                )

        # Compute 10 m MGRS label for job card display
        c_gzd, c_sq, c_digits = _split_mgrs(
            _decode_mgrs(mgrs.MGRS().toMGRS(lat, lon, MGRSPrecision=4))
        )
        center_mgrs_10m = f"{c_gzd} {c_sq} {c_digits[:4]} {c_digits[4:]}"

        job_id = uuid.uuid4().hex[:16]
        saved_overlay: Path | None = None

        if upload_file is not None:
            safe_name = _sanitize_upload_filename(getattr(upload_file, "filename", None))
            upload_dir = resolved_data_dir / job_id / "uploads"
            upload_dir.mkdir(parents=True, exist_ok=True)
            dest = (upload_dir / safe_name).resolve()
            if not dest.is_relative_to(upload_dir.resolve()):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid overlay filename.",
                )

            written = 0
            try:
                with dest.open("wb") as fh:
                    while True:
                        chunk = await upload_file.read(65536)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > MAX_UPLOAD_BYTES:
                            raise HTTPException(
                                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                                detail=f"Overlay file exceeds maximum size of {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
                            )
                        fh.write(chunk)
            except Exception:
                if dest.exists():
                    dest.unlink()
                raise
            saved_overlay = dest

        return db.create_job(
            db_path,
            job_id=job_id,
            title=job_req.title,
            center=job_req.center,
            center_mgrs=center_mgrs_10m,
            scale=job_req.scale,
            paper=job_req.paper,
            orientation=job_req.orientation,
            color_mode=job_req.color_mode,
            grid_cols=job_req.grid_cols,
            grid_rows=job_req.grid_rows,
            request_json=job_req.model_dump_json(),
            overlay_path=saved_overlay,
        )

    @application.get("/api/jobs", response_model=JobsListResponse)
    def api_list_jobs(limit: int = Query(default=50, ge=1, le=200)) -> JobsListResponse:
        return JobsListResponse(jobs=db.list_jobs(db_path, limit=limit))

    @application.get("/api/jobs/{job_id}", response_model=JobRecord)
    def api_get_job(job_id: str) -> JobRecord:
        if not _JOB_ID_RE.match(job_id):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid job_id.")
        record = db.get_job(db_path, job_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
        return record

    @application.post("/api/jobs/{job_id}/cancel", response_model=JobRecord)
    def api_cancel_job(job_id: str) -> JobRecord:
        if not _JOB_ID_RE.match(job_id):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid job_id.")
        record = db.cancel_job(db_path, job_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
        return record

    @application.delete("/api/jobs/{job_id}")
    def api_delete_job(job_id: str) -> dict[str, Any]:
        if not _JOB_ID_RE.match(job_id):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid job_id.")
        deleted = db.delete_job(db_path, job_id, data_dir=resolved_data_dir)
        if not deleted:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
        return {"deleted": True, "job_id": job_id}

    @application.get("/api/jobs/{job_id}/artifacts/{filename:path}")
    def api_download_artifact(
        job_id: str,
        filename: str,
        inline: bool = Query(default=False),
    ) -> FileResponse:
        if not _JOB_ID_RE.match(job_id):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid job_id.")
        # Strictly reject path separators, null bytes, and parent directory references.
        if (
            not filename
            or "\x00" in filename
            or "/" in filename
            or "\\" in filename
            or ".." in filename
            or Path(filename).name != filename
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid artifact filename.",
            )

        artifacts_dir = (resolved_data_dir / job_id / "artifacts").resolve()
        candidate = (artifacts_dir / filename).resolve()
        if not candidate.is_relative_to(artifacts_dir):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Artifact path escapes job directory.",
            )
        if not candidate.is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Artifact not found.",
            )

        media_type = _MEDIA_TYPES.get(candidate.suffix.lower(), "application/octet-stream")
        if inline:
            return FileResponse(
                path=candidate,
                media_type=media_type,
                headers={"Content-Disposition": f'inline; filename="{candidate.name}"'},
            )
        return FileResponse(
            path=candidate,
            media_type=media_type,
            filename=candidate.name,
        )

    @application.get("/api/jobs/{job_id}/thumbnail.png")
    def api_job_thumbnail(job_id: str) -> FileResponse:
        if not _JOB_ID_RE.match(job_id):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid job_id.")
        record = db.get_job(db_path, job_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")

        job_root = (resolved_data_dir / job_id).resolve()
        if not job_root.is_relative_to(resolved_data_dir):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid job_id.")

        thumb_path = (job_root / "thumbnail.png").resolve()
        if not thumb_path.is_file():
            artifacts_dir = (job_root / "artifacts").resolve()
            pdf_candidates = [
                artifacts_dir / a.filename for a in record.artifacts if a.kind == "pdf"
            ]
            if not pdf_candidates and artifacts_dir.is_dir():
                pdf_candidates = sorted(artifacts_dir.glob("*.pdf"))
            source_pdf = next((p.resolve() for p in pdf_candidates if p.is_file()), None)
            if source_pdf is None or not source_pdf.is_relative_to(artifacts_dir):
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="No PDF artifact available for thumbnail.",
                )

            job_root.mkdir(parents=True, exist_ok=True)
            tmp_prefix = job_root / f".thumb_{uuid.uuid4().hex[:8]}"
            proc = subprocess.run(
                [
                    "pdftoppm",
                    "-png",
                    "-f",
                    "1",
                    "-l",
                    "1",
                    "-scale-to",
                    "480",
                    "-singlefile",
                    str(source_pdf),
                    str(tmp_prefix),
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            rendered_png = tmp_prefix.with_suffix(".png")
            if proc.returncode != 0 or not rendered_png.is_file():
                rendered_png.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Failed to rasterize PDF thumbnail.",
                )
            rendered_png.replace(thumb_path)

        return FileResponse(
            path=thumb_path,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=3600"},
        )

    # Mount built SPA frontend if present; otherwise expose clean JSON root for headless/API mode.
    index_html = resolved_web_dist / "index.html"
    if index_html.is_file():
        assets_dir = resolved_web_dist / "assets"
        if assets_dir.is_dir():
            application.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

        @application.get("/", include_in_schema=False)
        def serve_spa_root() -> FileResponse:
            return FileResponse(path=index_html, media_type="text/html")

        @application.get("/{full_path:path}", include_in_schema=False)
        def serve_spa_fallback(full_path: str) -> FileResponse:
            if full_path.startswith("api/"):
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")
            candidate = (resolved_web_dist / full_path).resolve()
            if (
                full_path
                and candidate.is_relative_to(resolved_web_dist)
                and candidate.is_file()
            ):
                return FileResponse(path=candidate)
            return FileResponse(path=index_html, media_type="text/html")
    else:

        @application.get("/", include_in_schema=False)
        def api_root() -> JSONResponse:
            return JSONResponse(
                {
                    "service": "gridforge",
                    "status": "ok",
                    "frontend_built": False,
                    "docs": "/docs",
                }
            )

    return application


app: FastAPI = create_app()
