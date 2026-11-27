"""FastAPI web studio backend for GridForge (zero QGIS imports, < 120 MB RAM)."""

from __future__ import annotations

import html as html_lib
import json
import logging
import math
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import aclosing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import anyio
import httpx
import mgrs
from fastapi import FastAPI, HTTPException, Query, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.types import ASGIApp, Receive, Scope, Send

from gridforge import __version__
from gridforge.config import BBox, Orientation, PaperSize, SheetSpec, Units
from gridforge.geo.crs import (
    MAX_UTM_LAT,
    MIN_UTM_LAT,
    compute_atlas_layouts,
    compute_sheet_layout,
    get_utm_to_wgs84_transformer,
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
from gridforge.web import db, projects as projects_store, shares as shares_store
from gridforge.web.access import (
    OriginProxyAuthMiddleware,
    SameOriginWritesMiddleware,
    get_client_ip,
    get_effective_scheme,
)
from gridforge.web.auth import (
    OAUTH_STATE_COOKIE_NAME,
    AuthConfig,
    AuthContext,
    AuthManager,
    sha256_hex,
)
from gridforge.web.basemap import BasemapInfoResponse, MBTilesBasemapStore
from gridforge.web.limits import (
    LimitsConfig,
    effective_share_ttl_hours,
    enforce_and_create_job,
    enforce_and_record_auth_attempt,
    enforce_and_record_geocode_miss,
    enforce_and_record_preview,
    enforce_and_record_project_create,
    enforce_and_record_share_create,
    enforce_pre_upload_job_limits,
    get_capabilities,
    validate_job_capabilities,
)
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
    ProjectPayloadTooLargeError,
    ProjectRecord,
    ProjectsListResponse,
    ProjectUpdateRequest,
)
from gridforge.web.shares import (
    ShareCreateRequest,
    SharePayloadTooLargeError,
    ShareSnapshotPublic,
    SharesListResponse,
)
from gridforge.web.schemas import (
    AcceptInviteRequest,
    AdminInviteRecord,
    AdminSettingsRequest,
    AdminSettingsResponse,
    AdminUsageEvent,
    AdminUsageResponse,
    AdminUsageSummary,
    AdminUserRecord,
    AdminUsersResponse,
    AuthSessionResponse,
    CreateInviteRequest,
    CreateInviteResponse,
    CreateJobRequest,
    FootprintRequest,
    FootprintResponse,
    GeocodeResponse,
    GeocodeResult,
    HealthResponse,
    JobRecord,
    JobsListResponse,
    LoginRequest,
    PacksResponse,
    PackSummary,
    RevokeInviteResponse,
    RFLinkProfileRequest,
    RFLinkProfileResponse,
    RouteMetricsRequest,
    RouteMetricsResponse,
    RulerMetricsRequest,
    StripPlanRequest,
    StripPlanResponse,
    UpdateUserRequest,
    UpdateUserResponse,
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
_HTTP_413: int = getattr(status, "HTTP_413_CONTENT_TOO_LARGE", 413)
MAX_UPLOAD_BYTES: int = 25 * 1024 * 1024  # 25 MiB
ALLOWED_OVERLAY_EXTS: frozenset[str] = frozenset({".geojson", ".json", ".gpx", ".csv"})
_JOB_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{8,64}$")
_PACK_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_MGRS_SPLIT_RE = re.compile(r"^0?([1-9]|[1-5]\d|60)([C-HJ-NP-X])([A-HJ-NP-Z]{2})(\d*)$")

NOMINATIM_URL: str = "https://nominatim.openstreetmap.org/search"
NOMINATIM_USER_AGENT: str = "gridforge/0.1 (+https://github.com/jremick/gridforge)"
_GEOCODE_CACHE_TTL_S: float = 300.0
_GEOCODE_CACHE_MAX: int = 256
_geocode_lock = threading.Lock()
_geocode_cache: OrderedDict[tuple[str, int], tuple[float, list[GeocodeResult]]] = OrderedDict()

_ORIG_HTTPX_GET = httpx.get
_NOMINATIM_HTTP_CLIENT: httpx.Client | None = None

_MGRS_LOCAL = threading.local()

_HEAVY_PREVIEW_SEM = threading.Semaphore(2)
_THUMB_LOCKS_GUARD = threading.Lock()
_THUMB_LOCKS: dict[str, threading.Lock] = {}

_PACKS_CACHE_LOCK = threading.Lock()
_PACKS_CACHE: dict[str, tuple[float, Any, list[Any]]] = {}
_PACKS_CACHE_TTL_S: float = 5.0

_DEFAULT_MAX_JSON_BODY_BYTES: int = 256 * 1024
_UPLOAD_EXEMPT_PATHS: frozenset[str] = frozenset({"/api/jobs", "/api/strip-plan"})

_CONTENT_SECURITY_POLICY: str = (
    "default-src 'self'; "
    "script-src 'self' https://challenges.cloudflare.com; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob: https:; "
    "font-src 'self' data: https:; "
    "connect-src 'self' https:; "
    "worker-src 'self' blob:; "
    "child-src 'self' blob:; "
    "frame-src https://challenges.cloudflare.com; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "object-src 'none'"
)


def _get_mgrs_converter() -> mgrs.MGRS:
    conv = getattr(_MGRS_LOCAL, "converter", None)
    if conv is None:
        conv = mgrs.MGRS()
        _MGRS_LOCAL.converter = conv
    return conv


def _nominatim_http_get(*args: Any, **kwargs: Any) -> httpx.Response:
    global _NOMINATIM_HTTP_CLIENT
    if httpx.get is not _ORIG_HTTPX_GET:
        return httpx.get(*args, **kwargs)
    if _NOMINATIM_HTTP_CLIENT is None or _NOMINATIM_HTTP_CLIENT.is_closed:
        _NOMINATIM_HTTP_CLIENT = httpx.Client(
            timeout=httpx.Timeout(5.0, connect=4.0),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )
    return _NOMINATIM_HTTP_CLIENT.get(*args, **kwargs)


@contextmanager
def _acquire_preview_slot(timeout_s: float = 2.0) -> Iterator[None]:
    """Bound concurrent heavy GDAL/Poppler executions inside the 512 MB web process."""
    acquired = _HEAVY_PREVIEW_SEM.acquire(timeout=timeout_s)
    if not acquired:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Terrain preview worker is busy; please retry in a few seconds.",
            headers={"Retry-After": "5"},
        )
    try:
        yield
    finally:
        _HEAVY_PREVIEW_SEM.release()


def _get_thumb_lock(job_id: str) -> threading.Lock:
    with _THUMB_LOCKS_GUARD:
        lock = _THUMB_LOCKS.get(job_id)
        if lock is None:
            if len(_THUMB_LOCKS) > 512:
                _THUMB_LOCKS.clear()
            lock = threading.Lock()
            _THUMB_LOCKS[job_id] = lock
        return lock


def _packs_dir_fingerprint(packs_dir: Path) -> tuple[int, tuple[tuple[str, int], ...]] | None:
    try:
        dir_stat = packs_dir.stat()
    except OSError:
        return None
    children: list[tuple[str, int]] = []
    try:
        with os.scandir(packs_dir) as it:
            for entry in it:
                if entry.is_dir(follow_symlinks=False):
                    try:
                        manifest_mtime = (packs_dir / entry.name / "pack.json").stat().st_mtime_ns
                    except OSError:
                        manifest_mtime = -1
                    children.append((entry.name, manifest_mtime))
    except OSError:
        return None
    children.sort()
    return (dir_stat.st_mtime_ns, tuple(children))


def _discover_packs_cached(packs_dir: Path) -> list[Any]:
    key = str(packs_dir.resolve())
    fp = _packs_dir_fingerprint(packs_dir)
    now = time.monotonic()
    with _PACKS_CACHE_LOCK:
        entry = _PACKS_CACHE.get(key)
        if entry is not None and entry[1] == fp and (now - entry[0]) <= _PACKS_CACHE_TTL_S:
            return list(entry[2])
    packs = discover_packs(packs_dir)
    with _PACKS_CACHE_LOCK:
        _PACKS_CACHE[key] = (now, fp, packs)
    return list(packs)


def _invalidate_packs_cache(packs_dir: Path | None = None) -> None:
    with _PACKS_CACHE_LOCK:
        if packs_dir is None:
            _PACKS_CACHE.clear()
        else:
            _PACKS_CACHE.pop(str(packs_dir.resolve()), None)


class _SecurityHeadersMiddleware:
    """Attach hardened HTTP security headers and strict CSP to every response."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        is_https = get_effective_scheme(scope) == "https"

        async def _send_with_headers(message: Any) -> None:
            if message["type"] == "http.response.start":
                raw_headers = list(message.get("headers", []))
                existing = {k.lower() for k, _ in raw_headers}
                additions = [
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"DENY"),
                    (b"referrer-policy", b"strict-origin-when-cross-origin"),
                    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
                    (b"content-security-policy", _CONTENT_SECURITY_POLICY.encode("ascii")),
                ]
                if is_https:
                    additions.append(
                        (b"strict-transport-security", b"max-age=31536000; includeSubDomains")
                    )
                for k, v in additions:
                    if k not in existing:
                        raw_headers.append((k, v))
                message = {**message, "headers": raw_headers}
            await send(message)

        await self.app(scope, receive, _send_with_headers)


class _MaxBodySizeMiddleware:
    """Enforce a global streaming body size limit on non-upload endpoints."""

    def __init__(self, app: ASGIApp, *, max_bytes: int = _DEFAULT_MAX_JSON_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] in {"GET", "HEAD", "OPTIONS"}:
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path") or "")
        if path in _UPLOAD_EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        for k, v in scope.get("headers", ()):
            if k.lower() == b"content-length":
                try:
                    if int(v.decode("ascii").strip()) > self.max_bytes:
                        resp = PlainTextResponse("Request body too large", status_code=_HTTP_413)
                        await resp(scope, receive, send)
                        return
                except ValueError:
                    pass
                break

        received = 0
        too_large = False

        async def _bounded_receive() -> Any:
            nonlocal received, too_large
            msg = await receive()
            if msg["type"] == "http.request":
                body = msg.get("body", b"")
                received += len(body)
                if received > self.max_bytes:
                    too_large = True
                    raise _BodyLimitExceededError()
            return msg

        try:
            await self.app(scope, _bounded_receive, send)
        except _BodyLimitExceededError:
            if too_large:
                resp = PlainTextResponse("Request body too large", status_code=_HTTP_413)
                await resp(scope, receive, send)


class _BodyLimitExceededError(Exception):
    pass


class _ImmutableAssetsStaticFiles(StaticFiles):
    """Serve Vite content-hashed ``/assets/*`` files with immutable long-term caching."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        resp = await super().get_response(path, scope)
        if resp.status_code == 200 and "cache-control" not in {k.lower() for k in resp.headers.keys()}:
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp

_MEDIA_TYPES: dict[str, str] = {
    ".pdf": "application/pdf",
    ".mbtiles": "application/vnd.sqlite3",
    ".kmz": "application/vnd.google-earth.kmz",
    ".gpkg": "application/geopackage+sqlite3",
    ".sqlitedb": "application/vnd.sqlite3",
    ".zip": "application/zip",
}


def _search_nominatim(
    query: str,
    limit: int = 5,
    *,
    on_cache_miss: Callable[[], None] | None = None,
) -> list[GeocodeResult]:
    """Search OSM Nominatim with in-memory LRU/TTL cache and graceful offline fallback."""
    cleaned = query.strip()
    if len(cleaned) < 2:
        return []

    cache_key = (cleaned.lower(), limit)
    now = time.monotonic()
    with _geocode_lock:
        cached = _geocode_cache.get(cache_key)
        if cached is not None:
            ts, items = cached
            if now - ts <= _GEOCODE_CACHE_TTL_S:
                _geocode_cache.move_to_end(cache_key)
                return list(items)
            _geocode_cache.pop(cache_key, None)

    if on_cache_miss is not None:
        on_cache_miss()

    url = os.environ.get("GRIDFORGE_NOMINATIM_URL", NOMINATIM_URL).strip()
    try:
        resp = _nominatim_http_get(
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

    with _geocode_lock:
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


def _parse_content_length(request: Request) -> int | None:
    raw = request.headers.get("content-length")
    if raw is None:
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    try:
        val = int(cleaned)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Content-Length header.",
        ) from exc
    if val < 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Content-Length header.",
        )
    return val


class _OversizeUploadError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class _BoundedMultiPartParser(MultiPartParser):
    def __init__(
        self,
        headers: Any,
        stream: Any,
        *,
        max_file_bytes: int,
        oversize_detail: str,
        max_files: int | float = 1000,
        max_fields: int | float = 1000,
        max_part_size: int = 1024 * 1024,
    ) -> None:
        super().__init__(
            headers,
            stream,
            max_files=max_files,
            max_fields=max_fields,
            max_part_size=max_part_size,
        )
        self._max_file_bytes = max_file_bytes
        self._oversize_detail = oversize_detail
        self._total_file_bytes = 0

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self._current_part.file is not None:
            self._total_file_bytes += end - start
            if self._total_file_bytes > self._max_file_bytes:
                raise _OversizeUploadError(self._oversize_detail)
        super().on_part_data(data, start, end)


def _make_bounded_request(
    request: Request,
    *,
    max_body_bytes: int,
    detail: str,
) -> Request:
    orig_receive = request.receive
    received_bytes = 0

    async def bounded_receive() -> dict[str, Any]:
        nonlocal received_bytes
        message = await orig_receive()
        if message.get("type") == "http.request":
            chunk = message.get("body") or b""
            received_bytes += len(chunk)
            if received_bytes > max_body_bytes:
                raise HTTPException(status_code=_HTTP_413, detail=detail)
        return message

    return Request(request.scope, bounded_receive)


async def _parse_bounded_multipart_form(
    bounded_request: Request,
    *,
    max_file_bytes: int,
    detail: str,
) -> Any:
    try:
        async with aclosing(bounded_request.stream()) as stream:
            parser = _BoundedMultiPartParser(
                bounded_request.headers,
                stream,
                max_file_bytes=max_file_bytes,
                oversize_detail=detail,
            )
            return await parser.parse()
    except _OversizeUploadError as exc:
        raise HTTPException(status_code=_HTTP_413, detail=exc.detail) from exc
    except MultiPartException as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=exc.message,
        ) from exc


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

    to_wgs84 = get_utm_to_wgs84_transformer(layout.utm_epsg)
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

    # 2. Pure in-memory 1 km MGRS Easting & Northing preview lines across full atlas extent (single batched PROJ call)
    e_start = int(math.ceil(atlas_bbox.min_x / 1000.0)) * 1000
    e_end = int(math.floor(atlas_bbox.max_x / 1000.0)) * 1000
    n_start = int(math.ceil(atlas_bbox.min_y / 1000.0)) * 1000
    n_end = int(math.floor(atlas_bbox.max_y / 1000.0)) * 1000

    grid_features: list[dict[str, Any]] = []
    line_samples = max(6, 4 * max(cols, rows))

    e_vals = list(range(e_start, e_end + 1, 1000))
    n_vals = list(range(n_start, n_end + 1, 1000))
    batch_xs: list[float] = []
    batch_ys: list[float] = []
    y_steps = [
        atlas_bbox.min_y + atlas_bbox.height * i / (line_samples - 1)
        for i in range(line_samples)
    ]
    x_steps = [
        atlas_bbox.min_x + atlas_bbox.width * i / (line_samples - 1)
        for i in range(line_samples)
    ]
    for e in e_vals:
        batch_xs.extend([float(e)] * line_samples)
        batch_ys.extend(y_steps)
    for n in n_vals:
        batch_xs.extend(x_steps)
        batch_ys.extend([float(n)] * line_samples)

    if batch_xs:
        all_lons, all_lats = to_wgs84.transform(batch_xs, batch_ys)
    else:
        all_lons, all_lats = [], []

    offset = 0
    for e in e_vals:
        seg_lons = all_lons[offset : offset + line_samples]
        seg_lats = all_lats[offset : offset + line_samples]
        offset += line_samples
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
                        for lo, la in zip(seg_lons, seg_lats)
                    ],
                },
            }
        )

    for n in n_vals:
        seg_lons = all_lons[offset : offset + line_samples]
        seg_lats = all_lats[offset : offset + line_samples]
        offset += line_samples
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
                        for lo, la in zip(seg_lons, seg_lats)
                    ],
                },
            }
        )

    grid_geojson: dict[str, Any] = {
        "type": "FeatureCollection",
        "features": grid_features,
    }

    # 3. Center 10 m MGRS & visible 100 km square IDs across the full atlas extent
    converter = _get_mgrs_converter()
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


def _resolve_default_samples_dir() -> Path | None:
    env_samples = os.environ.get("GRIDFORGE_SAMPLES_DIR", "").strip()
    if env_samples:
        p = Path(env_samples).resolve()
        return p if p.is_dir() else None
    for candidate in (
        Path("/app/samples"),
        Path("/data/samples"),
        Path(__file__).resolve().parents[3] / "samples",
    ):
        if candidate.is_dir():
            return candidate.resolve()
    return None


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

    auth_config = AuthConfig.from_env()
    limits_config = LimitsConfig.from_env(auth_mode=auth_config.auth_mode)
    auth_manager = AuthManager(db_path=db_path, config=auth_config)
    if auth_config.auth_mode == "hosted":
        auth_manager.bootstrap_admin()

    basemap_store = MBTilesBasemapStore(
        packs_dir=resolved_packs_dir,
        explicit_path=basemap_mbtiles,
        allow_default_data_path=(packs_dir is None and basemap_mbtiles is None),
    )
    pack_manager = PackTaskManager(
        resolved_packs_dir,
        pack_builder=pack_builder,
        hosted_mode=(auth_config.auth_mode == "hosted"),
    )

    enable_docs = (
        auth_config.auth_mode != "hosted"
        or os.environ.get("GRIDFORGE_ENABLE_DOCS", "").strip().lower() in {"1", "true", "yes"}
    )
    application = FastAPI(
        title=f"{auth_config.brand_name} Map Studio API",
        version=__version__,
        docs_url="/docs" if enable_docs else None,
        redoc_url="/redoc" if enable_docs else None,
        openapi_url="/openapi.json" if enable_docs else None,
        description=(
            f"{auth_config.brand_name} — true-scale local UTM MGRS topographic map generator & print studio "
            "(powered by open-source GridForge)."
            if auth_config.brand_name.lower() != "gridforge"
            else "Self-hosted military-style MGRS topographic map generator & offline pack studio."
        ),
    )
    application.add_middleware(GZipMiddleware, minimum_size=500)
    application.add_middleware(_SecurityHeadersMiddleware)
    application.add_middleware(_MaxBodySizeMiddleware, max_bytes=_DEFAULT_MAX_JSON_BODY_BYTES)
    application.add_middleware(SameOriginWritesMiddleware)
    application.add_middleware(OriginProxyAuthMiddleware)
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
    application.state.auth_config = auth_config
    application.state.limits_config = limits_config
    application.state.auth_manager = auth_manager

    def _caps_for(ctx: AuthContext):
        return get_capabilities(
            ctx.role,
            limits_config,
            turnstile_sitekey=auth_config.turnstile_site_key,
            turnstile_configured=auth_config.turnstile_configured,
            google_oauth_enabled=auth_config.google_oauth_enabled,
        )

    def _require_admin(request: Request) -> AuthContext:
        ctx = auth_manager.resolve_request_context(request, issue_guest_cookie=False)
        if auth_config.auth_mode != "hosted" or ctx.role != "admin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Admin privileges required.",
            )
        return ctx

    @application.get("/api/health", response_model=HealthResponse)
    def api_health() -> HealthResponse:
        worker_alive, last_seen = db.get_heartbeat(db_path)
        queue_depth = db.get_queue_depth(db_path)
        packs = _discover_packs_cached(resolved_packs_dir)
        return HealthResponse(
            status="ok",
            queue_depth=queue_depth,
            worker_alive=worker_alive,
            worker_last_seen=last_seen,
            memory_budget_mb=resolved_mem_mb,
            packs_count=len(packs),
        )

    @application.get("/api/auth/session", response_model=AuthSessionResponse)
    def api_auth_session(request: Request, response: Response) -> AuthSessionResponse:
        ctx = auth_manager.resolve_request_context(request, issue_guest_cookie=True)
        payload = auth_manager.build_session_response(ctx, limits_config)
        auth_manager.apply_auth_cookies(response, ctx, request)
        return payload

    @application.post("/api/auth/login", response_model=AuthSessionResponse)
    def api_auth_login(
        req: LoginRequest,
        request: Request,
        response: Response,
    ) -> AuthSessionResponse:
        client_ip = get_client_ip(request)
        if auth_config.auth_mode == "hosted":
            enforce_and_record_auth_attempt(
                db_path,
                event_type="auth_login",
                client_ip=client_ip,
                cfg=limits_config,
                account_key=req.email,
            )
        user, raw_session_token = auth_manager.authenticate_password(req.email, req.password)
        auth_manager.set_session_cookie(response, raw_session_token, request)
        role_val = "admin" if user["role"] == "admin" else "friend"
        ctx = AuthContext(
            auth_mode="hosted",
            role=role_val,
            authenticated=True,
            owner_id=str(user["user_id"]),
            user=user,
            client_ip=client_ip,
        )
        return auth_manager.build_session_response(ctx, limits_config)

    @application.post("/api/auth/logout", response_model=AuthSessionResponse)
    def api_auth_logout(request: Request, response: Response) -> AuthSessionResponse:
        ctx = auth_manager.resolve_request_context(request, issue_guest_cookie=False)
        auth_manager.logout_session(ctx)
        auth_manager.delete_session_cookie(response, request)
        if auth_config.auth_mode == "none":
            next_ctx = AuthContext(
                auth_mode="none",
                role="none",
                authenticated=False,
                owner_id="local",
                user=None,
                client_ip=ctx.client_ip,
            )
        else:
            existing_guest_id = auth_manager._parse_guest_cookie(
                request.cookies.get("gf_guest")
            )
            new_guest_cookie: str | None = None
            if existing_guest_id is None:
                existing_guest_id, new_guest_cookie = auth_manager._mint_guest_cookie()
            next_ctx = AuthContext(
                auth_mode="hosted",
                role="guest",
                authenticated=False,
                owner_id=f"guest:{existing_guest_id}",
                user=None,
                client_ip=ctx.client_ip,
                new_guest_cookie_value=new_guest_cookie,
            )
            auth_manager.apply_auth_cookies(response, next_ctx, request)
        return auth_manager.build_session_response(next_ctx, limits_config)

    @application.post("/api/auth/register")
    def api_auth_register() -> None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Public signup is disabled; an admin invite is required.",
        )

    @application.post("/api/auth/accept-invite", response_model=AuthSessionResponse)
    def api_auth_accept_invite(
        req: AcceptInviteRequest,
        request: Request,
        response: Response,
    ) -> AuthSessionResponse:
        client_ip = get_client_ip(request)
        if auth_config.auth_mode == "hosted":
            enforce_and_record_auth_attempt(
                db_path,
                event_type="auth_accept_invite",
                client_ip=client_ip,
                cfg=limits_config,
            )
        user, raw_session_token = auth_manager.accept_invite(req)
        auth_manager.set_session_cookie(response, raw_session_token, request)
        role_val = "admin" if user["role"] == "admin" else "friend"
        ctx = AuthContext(
            auth_mode="hosted",
            role=role_val,
            authenticated=True,
            owner_id=str(user["user_id"]),
            user=user,
            client_ip=client_ip,
        )
        return auth_manager.build_session_response(ctx, limits_config)

    @application.get("/api/auth/google/start", include_in_schema=False)
    def api_google_start(
        request: Request,
        invite_token: str | None = Query(default=None),
    ) -> RedirectResponse:
        auth_url, signed_state = auth_manager.build_google_oauth_start(
            request, invite_token=invite_token
        )
        resp = RedirectResponse(url=auth_url, status_code=status.HTTP_302_FOUND)
        resp.set_cookie(
            key=OAUTH_STATE_COOKIE_NAME,
            value=signed_state,
            max_age=600,
            path="/",
            httponly=True,
            samesite="lax",
            secure=auth_manager._cookie_is_secure(request),
        )
        return resp

    @application.get("/api/auth/google/callback", name="api_google_callback", include_in_schema=False)
    def api_google_callback(
        request: Request,
        code: str | None = Query(default=None),
        state: str | None = Query(default=None),
        error: str | None = Query(default=None),
    ) -> Response:
        secure = auth_manager._cookie_is_secure(request)
        try:
            _, raw_session_token = auth_manager.handle_google_oauth_callback(
                request, code=code, state=state, error=error
            )
        except HTTPException as exc:
            err_resp = JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
            err_resp.delete_cookie(
                key=OAUTH_STATE_COOKIE_NAME,
                path="/",
                httponly=True,
                samesite="lax",
                secure=secure,
            )
            return err_resp

        resp = RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
        resp.delete_cookie(
            key=OAUTH_STATE_COOKIE_NAME,
            path="/",
            httponly=True,
            samesite="lax",
            secure=secure,
        )
        auth_manager.set_session_cookie(resp, raw_session_token, request)
        return resp

    @application.get("/api/admin/usage", response_model=AdminUsageResponse)
    def api_admin_usage(request: Request) -> AdminUsageResponse:
        _require_admin(request)
        report = db.get_admin_usage_report(
            db_path,
            global_daily_public_limit=limits_config.global_daily_public_renders,
        )
        return AdminUsageResponse(
            summary=AdminUsageSummary(**report["summary"]),
            by_role=report["by_role"],
            recent_events=[AdminUsageEvent(**ev) for ev in report["recent_events"]],
        )

    @application.get("/api/admin/users", response_model=AdminUsersResponse)
    def api_admin_users(request: Request) -> AdminUsersResponse:
        _require_admin(request)
        users = [
            AdminUserRecord(
                user_id=u["user_id"],
                email=u["email"],
                display_name=u["display_name"],
                role=u["role"],
                disabled=bool(u["disabled"]),
                daily_render_limit=u["daily_render_limit"],
                hourly_preview_limit=u["hourly_preview_limit"],
                has_password=bool(u.get("password_hash")),
                has_google=bool(u.get("google_sub")),
                created_at=u["created_at"],
                last_login_at=u.get("last_login_at"),
            )
            for u in db.list_users(db_path)
        ]
        invites = [
            AdminInviteRecord(
                invite_id=inv["invite_id"],
                email=inv["email"],
                role=inv["role"],
                created_by=inv["created_by"],
                created_at=inv["created_at"],
                expires_at=inv["expires_at"],
                used_at=inv["used_at"],
                used_by_user_id=inv["used_by_user_id"],
                revoked_at=inv["revoked_at"],
                status=inv["status"],
            )
            for inv in db.list_invites(db_path)
        ]
        return AdminUsersResponse(users=users, invites=invites)

    @application.post(
        "/api/admin/invites",
        response_model=CreateInviteResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def api_admin_create_invite(
        req: CreateInviteRequest,
        request: Request,
    ) -> CreateInviteResponse:
        ctx = _require_admin(request)
        raw_token = secrets.token_urlsafe(24)
        invite_id = f"inv_{uuid.uuid4().hex[:12]}"
        expires_at = db._dt_to_iso(
            datetime.now(timezone.utc) + timedelta(hours=req.expires_hours)
        )
        db.create_invite(
            db_path,
            invite_id=invite_id,
            token_hash=sha256_hex(raw_token),
            email=req.email,
            role=req.role,
            created_by=ctx.owner_id,
            expires_at=expires_at,
        )
        return CreateInviteResponse(
            invite_id=invite_id,
            token=raw_token,
            invite_path=f"/?invite={raw_token}",
            expires_at=expires_at,
        )

    @application.delete("/api/admin/invites/{invite_id}", response_model=RevokeInviteResponse)
    def api_admin_revoke_invite(invite_id: str, request: Request) -> RevokeInviteResponse:
        _require_admin(request)
        if not _JOB_ID_RE.match(invite_id):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid invite_id.")
        revoked = db.revoke_invite(db_path, invite_id)
        if not revoked:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invite not found.")
        return RevokeInviteResponse(revoked=True, invite_id=invite_id)

    @application.patch("/api/admin/users/{user_id}", response_model=UpdateUserResponse)
    def api_admin_update_user(
        user_id: str,
        req: UpdateUserRequest,
        request: Request,
    ) -> UpdateUserResponse:
        ctx = _require_admin(request)
        if not _JOB_ID_RE.match(user_id):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid user_id.")
        fields = req.model_fields_set
        if user_id == ctx.owner_id and (
            ("disabled" in fields and req.disabled is True)
            or ("role" in fields and req.role is not None and req.role != "admin")
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Administrators cannot disable or demote their own account.",
            )
        updated = db.update_user(
            db_path,
            user_id,
            disabled=req.disabled,
            set_disabled=("disabled" in fields and req.disabled is not None),
            daily_render_limit=req.daily_render_limit,
            set_daily_render_limit=("daily_render_limit" in fields),
            hourly_preview_limit=req.hourly_preview_limit,
            set_hourly_preview_limit=("hourly_preview_limit" in fields),
            role=req.role,
            set_role=("role" in fields and req.role is not None),
        )
        if updated is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
        if updated.get("disabled"):
            db.delete_sessions_for_user(db_path, user_id)
        return UpdateUserResponse(
            user=AdminUserRecord(
                user_id=updated["user_id"],
                email=updated["email"],
                display_name=updated["display_name"],
                role=updated["role"],
                disabled=bool(updated["disabled"]),
                daily_render_limit=updated["daily_render_limit"],
                hourly_preview_limit=updated["hourly_preview_limit"],
                has_password=bool(updated.get("password_hash")),
                has_google=bool(updated.get("google_sub")),
                created_at=updated["created_at"],
                last_login_at=updated.get("last_login_at"),
            )
        )

    @application.post("/api/admin/settings", response_model=AdminSettingsResponse)
    def api_admin_settings(
        req: AdminSettingsRequest,
        request: Request,
    ) -> AdminSettingsResponse:
        _require_admin(request)
        db.set_setting(db_path, "public_renders_paused", bool(req.public_renders_paused))
        return AdminSettingsResponse(public_renders_paused=bool(req.public_renders_paused))

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
        manifests = _discover_packs_cached(resolved_packs_dir)
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
    def api_build_pack(req: PackBuildRequest, request: Request) -> PackTaskRecord:
        ctx = auth_manager.resolve_request_context(request, issue_guest_cookie=False)
        if auth_config.auth_mode == "hosted" and ctx.role != "admin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Admin role required to manage offline packs.",
            )
        try:
            rec = pack_manager.enqueue_build(req)
            _invalidate_packs_cache(resolved_packs_dir)
            basemap_store.invalidate_cache()
            return rec
        except RuntimeError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=_HTTP_422, detail=str(exc)) from exc

    @application.get("/api/packs/tasks", response_model=PackTasksListResponse)
    def api_list_pack_tasks(request: Request) -> PackTasksListResponse:
        ctx = auth_manager.resolve_request_context(request, issue_guest_cookie=False)
        if auth_config.auth_mode == "hosted" and ctx.role != "admin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Admin role required to manage offline packs.",
            )
        return PackTasksListResponse(tasks=pack_manager.list_tasks())

    @application.get("/api/packs/tasks/{task_id}", response_model=PackTaskRecord)
    def api_get_pack_task(task_id: str, request: Request) -> PackTaskRecord:
        ctx = auth_manager.resolve_request_context(request, issue_guest_cookie=False)
        if auth_config.auth_mode == "hosted" and ctx.role != "admin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Admin role required to manage offline packs.",
            )
        if not _TASK_ID_RE.match(task_id):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid task_id.")
        record = pack_manager.get_task(task_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pack task not found.")
        return record

    @application.delete("/api/packs/{pack_id}")
    def api_delete_pack(pack_id: str, request: Request) -> dict[str, Any]:
        ctx = auth_manager.resolve_request_context(request, issue_guest_cookie=False)
        if auth_config.auth_mode == "hosted" and ctx.role != "admin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Admin role required to manage offline packs.",
            )
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
        _invalidate_packs_cache(resolved_packs_dir)
        basemap_store.invalidate_cache()
        return {"deleted": True, "pack_id": pack_id}

    @application.post("/api/viewshed-preview", response_model=ViewshedPreviewResponse)
    def api_viewshed_preview(
        req: ViewshedPreviewRequest,
        request: Request,
        response: Response,
    ) -> ViewshedPreviewResponse:
        ctx = auth_manager.resolve_request_context(request, issue_guest_cookie=True)
        auth_manager.apply_auth_cookies(response, ctx, request)
        if auth_config.auth_mode == "hosted":
            auth_manager.verify_turnstile_if_required(
                request, ctx, body_token=req.turnstile_token
            )
        if req.pack_id is not None:
            if not _PACK_ID_RE.match(req.pack_id):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid pack_id '{req.pack_id}'.",
                )
            installed_ids = {p.pack_id for p in _discover_packs_cached(resolved_packs_dir)}
            if req.pack_id not in installed_ids:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Offline pack '{req.pack_id}' is not installed.",
                )
        if auth_config.auth_mode == "hosted":
            enforce_and_record_preview(
                db_path,
                role=ctx.role,
                owner_id=ctx.owner_id,
                client_ip=ctx.client_ip,
                user_dict=ctx.user,
                cfg=limits_config,
                event_type="viewshed_preview",
                detail={"pack_id": req.pack_id},
            )
        try:
            with _acquire_preview_slot():
                return compute_viewshed_preview(
                    req,
                    packs_dir=resolved_packs_dir,
                    pack_dir_finder=pack_manager.find_pack_dir,
                )
        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=_HTTP_422, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("Viewshed preview failed: %s", exc)
            detail = (
                "Viewshed preview failed due to an internal terrain processing error."
                if auth_config.auth_mode == "hosted"
                else f"Viewshed preview failed: {exc}"
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=detail,
            ) from exc

    @application.post("/api/rf-link-profile", response_model=RFLinkProfileResponse)
    def api_rf_link_profile(
        req: RFLinkProfileRequest,
        request: Request,
        response: Response,
    ) -> RFLinkProfileResponse:
        ctx = auth_manager.resolve_request_context(request, issue_guest_cookie=True)
        auth_manager.apply_auth_cookies(response, ctx, request)
        if auth_config.auth_mode == "hosted":
            auth_manager.verify_turnstile_if_required(
                request, ctx, body_token=req.turnstile_token
            )
        if req.pack_id is not None:
            if not _PACK_ID_RE.match(req.pack_id):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid pack_id '{req.pack_id}'.",
                )
            installed_ids = {p.pack_id for p in _discover_packs_cached(resolved_packs_dir)}
            if req.pack_id not in installed_ids:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Offline pack '{req.pack_id}' is not installed.",
                )
        if auth_config.auth_mode == "hosted":
            enforce_and_record_preview(
                db_path,
                role=ctx.role,
                owner_id=ctx.owner_id,
                client_ip=ctx.client_ip,
                user_dict=ctx.user,
                cfg=limits_config,
                event_type="rf_link_profile",
                detail={"pack_id": req.pack_id},
            )
        try:
            with _acquire_preview_slot():
                return compute_rf_link_preview(
                    req,
                    packs_dir=resolved_packs_dir,
                    pack_dir_finder=pack_manager.find_pack_dir,
                )
        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=_HTTP_422, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("RF link profile failed: %s", exc)
            detail = (
                "RF link profile failed due to an internal terrain processing error."
                if auth_config.auth_mode == "hosted"
                else f"RF link profile failed: {exc}"
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=detail,
            ) from exc

    @application.post("/api/ruler-metrics", response_model=RulerMetricsResponse)
    def api_ruler_metrics(
        req: RulerMetricsRequest,
        request: Request,
        response: Response,
    ) -> RulerMetricsResponse:
        wants_dem = req.dem_source is not None or req.pack_id is not None
        ctx: AuthContext | None = None
        if wants_dem and auth_config.auth_mode == "hosted":
            ctx = auth_manager.resolve_request_context(request, issue_guest_cookie=True)
            auth_manager.apply_auth_cookies(response, ctx, request)
            auth_manager.verify_turnstile_if_required(
                request, ctx, body_token=req.turnstile_token
            )
        if req.pack_id is not None:
            if not _PACK_ID_RE.match(req.pack_id):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid pack_id '{req.pack_id}'.",
                )
            installed_ids = {p.pack_id for p in _discover_packs_cached(resolved_packs_dir)}
            if req.pack_id not in installed_ids:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Offline pack '{req.pack_id}' is not installed.",
                )
        if wants_dem and auth_config.auth_mode == "hosted":
            assert ctx is not None
            enforce_and_record_preview(
                db_path,
                role=ctx.role,
                owner_id=ctx.owner_id,
                client_ip=ctx.client_ip,
                user_dict=ctx.user,
                cfg=limits_config,
                event_type="ruler_metrics",
                detail={
                    "dem_source": req.dem_source.value if req.dem_source is not None else None,
                    "pack_id": req.pack_id,
                },
            )
        try:
            if wants_dem:
                with _acquire_preview_slot():
                    return compute_ruler_preview(
                        req,
                        packs_dir=resolved_packs_dir,
                        pack_dir_finder=pack_manager.find_pack_dir,
                    )
            return compute_ruler_preview(
                req,
                packs_dir=resolved_packs_dir,
                pack_dir_finder=pack_manager.find_pack_dir,
            )
        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=_HTTP_422, detail=str(exc)) from exc

    @application.get("/api/projects", response_model=ProjectsListResponse)
    def api_list_projects(
        request: Request,
        response: Response,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> ProjectsListResponse:
        ctx = auth_manager.resolve_request_context(request, issue_guest_cookie=True)
        auth_manager.apply_auth_cookies(response, ctx, request)
        owner_filter = ctx.owner_id if auth_config.auth_mode == "hosted" else None
        return ProjectsListResponse(
            projects=projects_store.list_projects(db_path, limit=limit, owner_id=owner_filter)
        )

    @application.post(
        "/api/projects",
        response_model=ProjectRecord,
        status_code=status.HTTP_201_CREATED,
    )
    def api_create_project(
        req: ProjectCreateRequest,
        request: Request,
        response: Response,
    ) -> ProjectRecord:
        ctx = auth_manager.resolve_request_context(request, issue_guest_cookie=True)
        auth_manager.apply_auth_cookies(response, ctx, request)
        if req.last_job_id is not None:
            if not _JOB_ID_RE.match(req.last_job_id):
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid last_job_id.")
            if auth_config.auth_mode == "hosted":
                linked = db.get_job(db_path, req.last_job_id)
                if linked is not None and linked.owner_id != ctx.owner_id:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Cannot link a job owned by another account.",
                    )
        if auth_config.auth_mode == "hosted":
            enforce_and_record_project_create(
                db_path,
                role=ctx.role,
                owner_id=ctx.owner_id,
                client_ip=ctx.client_ip,
                cfg=limits_config,
            )
        try:
            return projects_store.create_project(
                db_path, req, owner_id=ctx.owner_id, owner_role=ctx.role
            )
        except ProjectPayloadTooLargeError as exc:
            raise HTTPException(status_code=_HTTP_413, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=_HTTP_422, detail=str(exc)) from exc

    @application.get("/api/projects/{project_id}", response_model=ProjectRecord)
    def api_get_project(
        project_id: str,
        request: Request,
        response: Response,
    ) -> ProjectRecord:
        ctx = auth_manager.resolve_request_context(request, issue_guest_cookie=True)
        auth_manager.apply_auth_cookies(response, ctx, request)
        if not projects_store.is_valid_project_id(project_id):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid project_id.")
        record = projects_store.get_project(db_path, project_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found.")
        if auth_config.auth_mode == "hosted" and record.owner_id != ctx.owner_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to access this project.",
            )
        return record

    @application.patch("/api/projects/{project_id}", response_model=ProjectRecord)
    def api_update_project(
        project_id: str,
        req: ProjectUpdateRequest,
        request: Request,
        response: Response,
    ) -> ProjectRecord:
        ctx = auth_manager.resolve_request_context(request, issue_guest_cookie=True)
        auth_manager.apply_auth_cookies(response, ctx, request)
        if not projects_store.is_valid_project_id(project_id):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid project_id.")
        if req.last_job_id is not None:
            if not _JOB_ID_RE.match(req.last_job_id):
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid last_job_id.")
            if auth_config.auth_mode == "hosted":
                linked = db.get_job(db_path, req.last_job_id)
                if linked is not None and linked.owner_id != ctx.owner_id:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Cannot link a job owned by another account.",
                    )
        existing = projects_store.get_project(db_path, project_id)
        if existing is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found.")
        if auth_config.auth_mode == "hosted" and existing.owner_id != ctx.owner_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to modify this project.",
            )
        try:
            record = projects_store.update_project(db_path, project_id, req)
        except ProjectPayloadTooLargeError as exc:
            raise HTTPException(status_code=_HTTP_413, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=_HTTP_422, detail=str(exc)) from exc
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found.")
        return record

    @application.delete("/api/projects/{project_id}")
    def api_delete_project(
        project_id: str,
        request: Request,
        response: Response,
    ) -> dict[str, Any]:
        ctx = auth_manager.resolve_request_context(request, issue_guest_cookie=True)
        auth_manager.apply_auth_cookies(response, ctx, request)
        if not projects_store.is_valid_project_id(project_id):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid project_id.")
        existing = projects_store.get_project(db_path, project_id)
        if existing is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found.")
        if auth_config.auth_mode == "hosted" and existing.owner_id != ctx.owner_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to delete this project.",
            )
        deleted = projects_store.delete_project(db_path, project_id)
        if not deleted:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found.")
        return {"deleted": True, "id": project_id, "project_id": project_id}

    @application.get("/api/shares", response_model=SharesListResponse)
    def api_list_shares(
        request: Request,
        response: Response,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> SharesListResponse:
        ctx = auth_manager.resolve_request_context(request, issue_guest_cookie=True)
        auth_manager.apply_auth_cookies(response, ctx, request)
        owner_filter = ctx.owner_id if auth_config.auth_mode == "hosted" else None
        return SharesListResponse(
            shares=shares_store.list_shares(db_path, limit=limit, owner_id=owner_filter)
        )

    @application.post(
        "/api/shares",
        response_model=ShareSnapshotPublic,
        status_code=status.HTTP_201_CREATED,
    )
    def api_create_share(
        req: ShareCreateRequest,
        request: Request,
        response: Response,
    ) -> ShareSnapshotPublic:
        ctx = auth_manager.resolve_request_context(request, issue_guest_cookie=True)
        auth_manager.apply_auth_cookies(response, ctx, request)
        if auth_config.auth_mode == "hosted":
            auth_manager.verify_turnstile_if_required(request, ctx)
            # Pre-validate explicit state payload size before burning quota; still rate-limit
            # project_id share attempts so cross-tenant project_id probing is throttled.
            if req.project_id is None and isinstance(req.state, dict) and req.state:
                try:
                    shares_store.sanitize_share_state(req.state)
                except SharePayloadTooLargeError as exc:
                    raise HTTPException(status_code=_HTTP_413, detail=str(exc)) from exc
                except ValueError as exc:
                    raise HTTPException(status_code=_HTTP_422, detail=str(exc)) from exc
            enforce_and_record_share_create(
                db_path,
                role=ctx.role,
                owner_id=ctx.owner_id,
                client_ip=ctx.client_ip,
                cfg=limits_config,
            )

        resolved_name = req.name
        resolved_notes = req.notes
        resolved_state = req.state
        source_project_id: str | None = None

        if req.project_id is not None:
            if not projects_store.is_valid_project_id(req.project_id):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid project_id.",
                )
            proj = projects_store.get_project(db_path, req.project_id)
            if proj is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Project not found.",
                )
            if auth_config.auth_mode == "hosted" and proj.owner_id != ctx.owner_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You do not have permission to share this project.",
                )
            source_project_id = proj.project_id
            resolved_name = resolved_name or proj.name
            resolved_notes = resolved_notes or proj.notes
            resolved_state = proj.state

        if not isinstance(resolved_state, dict) or not resolved_state:
            raise HTTPException(
                status_code=_HTTP_422,
                detail="Share snapshot requires a non-empty map state.",
            )

        ttl_hours = effective_share_ttl_hours(
            ctx.role, limits_config, requested_hours=req.expires_in_hours
        )
        try:
            return shares_store.create_share(
                db_path,
                name=resolved_name,
                notes=resolved_notes,
                state=resolved_state,
                source_project_id=source_project_id,
                owner_id=ctx.owner_id,
                owner_role=ctx.role,
                ttl_hours=ttl_hours,
            )
        except SharePayloadTooLargeError as exc:
            raise HTTPException(status_code=_HTTP_413, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=_HTTP_422, detail=str(exc)) from exc

    @application.get("/api/shares/{share_id}", response_model=ShareSnapshotPublic)
    def api_get_share(
        share_id: str,
        response: Response,
    ) -> ShareSnapshotPublic:
        if not shares_store.is_valid_share_id(share_id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid share_id.",
            )
        record = shares_store.get_public_share(db_path, share_id, increment_views=True)
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Shared map snapshot not found or expired.",
            )
        response.headers["Cache-Control"] = "no-cache"
        return record

    @application.delete("/api/shares/{share_id}")
    def api_delete_share(
        share_id: str,
        request: Request,
        response: Response,
    ) -> dict[str, Any]:
        ctx = auth_manager.resolve_request_context(request, issue_guest_cookie=True)
        auth_manager.apply_auth_cookies(response, ctx, request)
        if not shares_store.is_valid_share_id(share_id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid share_id.",
            )
        owner = shares_store.get_share_owner(db_path, share_id)
        if owner is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Shared map snapshot not found.",
            )
        owner_id, _owner_role = owner
        if (
            auth_config.auth_mode == "hosted"
            and ctx.role != "admin"
            and owner_id != ctx.owner_id
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to delete this shared snapshot.",
            )
        deleted = shares_store.delete_share(db_path, share_id)
        if not deleted:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Shared map snapshot not found.",
            )
        return {"deleted": True, "share_id": share_id}

    @application.get("/api/geocode", response_model=GeocodeResponse)
    def api_geocode(
        request: Request,
        q: str = Query(default="", max_length=200),
        limit: int = Query(default=5, ge=1, le=10),
    ) -> GeocodeResponse:
        ctx = auth_manager.resolve_request_context(request, issue_guest_cookie=False)
        on_miss = (
            lambda: enforce_and_record_geocode_miss(
                db_path,
                role=ctx.role,
                owner_id=ctx.owner_id,
                client_ip=ctx.client_ip,
                cfg=limits_config,
                query=q,
            )
        ) if auth_config.auth_mode == "hosted" else None
        return GeocodeResponse(results=_search_nominatim(q, limit=limit, on_cache_miss=on_miss))

    @application.post("/api/footprint", response_model=FootprintResponse)
    def api_footprint(req: FootprintRequest) -> FootprintResponse:
        return _build_footprint_preview(req)

    @application.post("/api/strip-plan", response_model=StripPlanResponse)
    async def api_strip_plan(request: Request, response: Response) -> StripPlanResponse:
        ctx = auth_manager.resolve_request_context(request, issue_guest_cookie=True)
        auth_manager.apply_auth_cookies(response, ctx, request)
        caps = _caps_for(ctx)
        if auth_config.auth_mode == "hosted" and not caps.strip_atlas_allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Strip atlas planning requires an invited Friend or Admin account.",
            )

        content_type = request.headers.get("content-type", "").lower()
        is_multipart = "multipart/form-data" in content_type
        content_length = _parse_content_length(request)
        max_body_bytes = caps.max_upload_bytes + (64 * 1024 if is_multipart else 0)
        oversize_detail = (
            f"Route file exceeds maximum size of "
            f"{max(1, caps.max_upload_bytes // (1024 * 1024))} MB."
        )
        if content_length is not None and content_length > max_body_bytes:
            raise HTTPException(
                status_code=_HTTP_413,
                detail=oversize_detail,
            )

        bounded_request = _make_bounded_request(
            request,
            max_body_bytes=max_body_bytes,
            detail=oversize_detail,
        )

        if is_multipart:
            form = await _parse_bounded_multipart_form(
                bounded_request,
                max_file_bytes=caps.max_upload_bytes,
                detail=oversize_detail,
            )
            try:
                route_file = form.get("route_file")
                if route_file is None or not hasattr(route_file, "read"):
                    raise HTTPException(
                        status_code=_HTTP_422,
                        detail="Multipart /api/strip-plan requires a 'route_file' (.gpx or .geojson) upload.",
                    )
                max_bytes = caps.max_upload_bytes
                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk = await route_file.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise HTTPException(
                            status_code=_HTTP_413,
                            detail=f"Route file exceeds maximum size of {max(1, max_bytes // (1024 * 1024))} MB.",
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
                    if auth_config.auth_mode == "hosted" and max_sheets > caps.max_strip_sheets:
                        raise HTTPException(
                            status_code=status.HTTP_403_FORBIDDEN,
                            detail=f"Requested strip sheet count ({max_sheets}) exceeds your limit ({caps.max_strip_sheets}).",
                        )
                    route_lonlat = await anyio.to_thread.run_sync(
                        parse_route_bytes, raw_bytes, filename
                    )
                    return await anyio.to_thread.run_sync(
                        lambda: plan_strip_atlas(
                            route_lonlat,
                            scale=scale,
                            paper=paper,
                            orientation=orientation,
                            units=units,
                            overlap_pct=overlap_pct,
                            max_sheets=max_sheets,
                        )
                    )
                except HTTPException:
                    raise
                except (ValueError, ValidationError) as exc:
                    raise HTTPException(status_code=_HTTP_422, detail=str(exc)) from exc
            finally:
                await form.close()

        try:
            raw_body = await bounded_request.body()
            plan_req = StripPlanRequest.model_validate_json(raw_body)
            if auth_config.auth_mode == "hosted" and plan_req.max_sheets > caps.max_strip_sheets:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Requested strip sheet count ({plan_req.max_sheets}) exceeds your limit ({caps.max_strip_sheets}).",
                )
            route_lonlat = await anyio.to_thread.run_sync(
                parse_route_geojson, plan_req.route_geojson
            )
            return await anyio.to_thread.run_sync(
                lambda: plan_strip_atlas(
                    route_lonlat,
                    scale=plan_req.scale,
                    paper=plan_req.paper,
                    orientation=plan_req.orientation,
                    units=plan_req.units,
                    overlap_pct=plan_req.overlap_pct,
                    max_sheets=plan_req.max_sheets,
                )
            )
        except HTTPException:
            raise
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
    async def api_create_job(request: Request, response: Response) -> JobRecord:
        ctx = auth_manager.resolve_request_context(request, issue_guest_cookie=True)
        auth_manager.apply_auth_cookies(response, ctx, request)
        caps = _caps_for(ctx)

        content_type = request.headers.get("content-type", "").lower()
        is_form = (
            "multipart/form-data" in content_type
            or "application/x-www-form-urlencoded" in content_type
        )
        content_length = _parse_content_length(request)
        max_body_bytes = (
            caps.max_upload_bytes + 512 * 1024
            if is_form
            else 512 * 1024
        )
        oversize_detail = (
            f"Overlay file exceeds maximum size of "
            f"{max(1, caps.max_upload_bytes // (1024 * 1024))} MB."
        )
        if content_length is not None and content_length > max_body_bytes:
            raise HTTPException(
                status_code=_HTTP_413,
                detail=oversize_detail,
            )

        header_turnstile_tok = (request.headers.get("x-turnstile-token") or "").strip()
        needs_guest_turnstile = (
            auth_config.auth_mode == "hosted"
            and ctx.role == "guest"
            and auth_config.turnstile_configured
        )
        turnstile_verified = False

        if needs_guest_turnstile:
            if header_turnstile_tok:
                await anyio.to_thread.run_sync(
                    lambda: auth_manager.verify_turnstile_if_required(
                        request, ctx, body_token=None
                    )
                )
                turnstile_verified = True
            elif is_form and (content_length is None or content_length > 512 * 1024):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Human verification (Turnstile) is required.",
                )

        pre_upload_limits_checked = False
        if not needs_guest_turnstile or turnstile_verified:
            enforce_pre_upload_job_limits(
                db_path,
                role=ctx.role,
                owner_id=ctx.owner_id,
                client_ip=ctx.client_ip,
                user_dict=ctx.user,
                cfg=limits_config,
            )
            pre_upload_limits_checked = True

        bounded_request = _make_bounded_request(
            request,
            max_body_bytes=max_body_bytes,
            detail=oversize_detail,
        )

        form = None
        upload_file = None
        form_turnstile_token: str | None = None

        try:
            if is_form:
                if "multipart/form-data" in content_type:
                    form = await _parse_bounded_multipart_form(
                        bounded_request,
                        max_file_bytes=caps.max_upload_bytes,
                        detail=oversize_detail,
                    )
                else:
                    form = await bounded_request.form()
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
                        detail=jsonable_encoder(exc.errors()),
                    ) from exc
                raw_ft = form.get("turnstile_token")
                if isinstance(raw_ft, str) and raw_ft.strip():
                    form_turnstile_token = raw_ft.strip()
                maybe_file = form.get("overlay_file")
                if maybe_file is not None and hasattr(maybe_file, "read"):
                    # Ignore empty browser file input fields with no filename
                    if getattr(maybe_file, "filename", ""):
                        upload_file = maybe_file
            else:
                try:
                    raw_body = await bounded_request.body()
                    job_req = CreateJobRequest.model_validate_json(raw_body)
                except ValidationError as exc:
                    raise HTTPException(
                        status_code=_HTTP_422,
                        detail=jsonable_encoder(exc.errors()),
                    ) from exc

            if auth_config.auth_mode == "hosted":
                if not turnstile_verified:
                    turnstile_tok = job_req.turnstile_token or form_turnstile_token
                    await anyio.to_thread.run_sync(
                        lambda: auth_manager.verify_turnstile_if_required(
                            request, ctx, body_token=turnstile_tok
                        )
                    )
                validate_job_capabilities(ctx.role, job_req, caps)
                if not pre_upload_limits_checked:
                    enforce_pre_upload_job_limits(
                        db_path,
                        role=ctx.role,
                        owner_id=ctx.owner_id,
                        client_ip=ctx.client_ip,
                        user_dict=ctx.user,
                        cfg=limits_config,
                    )

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
                installed_ids = {
                    p.pack_id
                    for p in await anyio.to_thread.run_sync(
                        _discover_packs_cached, resolved_packs_dir
                    )
                }
                if job_req.pack_id not in installed_ids:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=f"Offline pack '{job_req.pack_id}' is not installed.",
                    )

            # Compute 10 m MGRS label for job card display
            c_gzd, c_sq, c_digits = _split_mgrs(
                _decode_mgrs(_get_mgrs_converter().toMGRS(lat, lon, MGRSPrecision=4))
            )
            center_mgrs_10m = f"{c_gzd} {c_sq} {c_digits[:4]} {c_digits[4:]}"

            job_id = uuid.uuid4().hex[:16]
            saved_overlay: Path | None = None

            if upload_file is not None:
                safe_name = _sanitize_upload_filename(getattr(upload_file, "filename", None))
                staging_root = resolved_data_dir / f".tmp_upload_{job_id}"
                staging_upload_dir = staging_root / "uploads"
                staging_upload_dir.mkdir(parents=True, exist_ok=True)
                staging_dest = (staging_upload_dir / safe_name).resolve()
                if not staging_dest.is_relative_to(staging_upload_dir.resolve()):
                    shutil.rmtree(staging_root, ignore_errors=True)
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Invalid overlay filename.",
                    )

                max_bytes = caps.max_upload_bytes
                written = 0
                try:
                    with staging_dest.open("wb") as fh:
                        while True:
                            chunk = await upload_file.read(65536)
                            if not chunk:
                                break
                            written += len(chunk)
                            if written > max_bytes:
                                raise HTTPException(
                                    status_code=_HTTP_413,
                                    detail=f"Overlay file exceeds maximum size of {max(1, max_bytes // (1024 * 1024))} MB.",
                                )
                            fh.write(chunk)
                    final_job_dir = resolved_data_dir / job_id
                    staging_root.rename(final_job_dir)
                    saved_overlay = (final_job_dir / "uploads" / safe_name).resolve()
                except Exception:
                    if staging_dest.exists():
                        staging_dest.unlink()
                    shutil.rmtree(staging_root, ignore_errors=True)
                    shutil.rmtree(resolved_data_dir / job_id, ignore_errors=True)
                    raise

            try:
                return enforce_and_create_job(
                    db_path,
                    role=ctx.role,
                    owner_id=ctx.owner_id,
                    client_ip=ctx.client_ip,
                    user_dict=ctx.user,
                    cfg=limits_config,
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
            except Exception:
                if saved_overlay is not None:
                    shutil.rmtree(resolved_data_dir / job_id, ignore_errors=True)
                raise
        finally:
            if form is not None:
                await form.close()

    @application.get("/api/jobs", response_model=JobsListResponse)
    def api_list_jobs(
        request: Request,
        response: Response,
        limit: int = Query(default=50, ge=1, le=200),
    ) -> JobsListResponse:
        ctx = auth_manager.resolve_request_context(request, issue_guest_cookie=True)
        auth_manager.apply_auth_cookies(response, ctx, request)
        owner_filter = ctx.owner_id if auth_config.auth_mode == "hosted" else None
        return JobsListResponse(jobs=db.list_jobs(db_path, limit=limit, owner_id=owner_filter))

    @application.get("/api/jobs/{job_id}", response_model=JobRecord)
    def api_get_job(job_id: str, request: Request, response: Response) -> JobRecord:
        ctx = auth_manager.resolve_request_context(request, issue_guest_cookie=True)
        auth_manager.apply_auth_cookies(response, ctx, request)
        if not _JOB_ID_RE.match(job_id):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid job_id.")
        record = db.get_job(db_path, job_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
        if auth_config.auth_mode == "hosted" and record.owner_id != ctx.owner_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to access this job.",
            )
        return record

    @application.post("/api/jobs/{job_id}/cancel", response_model=JobRecord)
    def api_cancel_job(job_id: str, request: Request, response: Response) -> JobRecord:
        ctx = auth_manager.resolve_request_context(request, issue_guest_cookie=True)
        auth_manager.apply_auth_cookies(response, ctx, request)
        if not _JOB_ID_RE.match(job_id):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid job_id.")
        existing = db.get_job(db_path, job_id)
        if existing is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
        if auth_config.auth_mode == "hosted" and existing.owner_id != ctx.owner_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to cancel this job.",
            )
        record = db.cancel_job(db_path, job_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
        return record

    @application.delete("/api/jobs/{job_id}")
    def api_delete_job(job_id: str, request: Request, response: Response) -> dict[str, Any]:
        ctx = auth_manager.resolve_request_context(request, issue_guest_cookie=True)
        auth_manager.apply_auth_cookies(response, ctx, request)
        if not _JOB_ID_RE.match(job_id):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid job_id.")
        existing = db.get_job(db_path, job_id)
        if existing is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
        if auth_config.auth_mode == "hosted" and existing.owner_id != ctx.owner_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to delete this job.",
            )
        deleted = db.delete_job(db_path, job_id, data_dir=resolved_data_dir)
        if not deleted:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
        return {"deleted": True, "job_id": job_id}

    @application.get("/api/jobs/{job_id}/artifacts/{filename:path}")
    def api_download_artifact(
        job_id: str,
        filename: str,
        request: Request,
        response: Response,
        inline: bool = Query(default=False),
    ) -> FileResponse:
        ctx = auth_manager.resolve_request_context(request, issue_guest_cookie=True)
        auth_manager.apply_auth_cookies(response, ctx, request)
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

        if auth_config.auth_mode == "hosted":
            record = db.get_job(db_path, job_id)
            if record is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
            if record.owner_id != ctx.owner_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You do not have permission to download this artifact.",
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
        headers: dict[str, str] = {}
        if auth_config.auth_mode == "hosted":
            headers["Cache-Control"] = "private, no-store"
        if inline:
            headers["Content-Disposition"] = f'inline; filename="{candidate.name}"'
            return FileResponse(
                path=candidate,
                media_type=media_type,
                headers=headers,
            )
        return FileResponse(
            path=candidate,
            media_type=media_type,
            filename=candidate.name,
            headers=headers or None,
        )

    @application.get("/api/jobs/{job_id}/thumbnail.png")
    def api_job_thumbnail(
        job_id: str,
        request: Request,
        response: Response,
    ) -> FileResponse:
        ctx = auth_manager.resolve_request_context(request, issue_guest_cookie=True)
        auth_manager.apply_auth_cookies(response, ctx, request)
        if not _JOB_ID_RE.match(job_id):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid job_id.")
        record = db.get_job(db_path, job_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
        if auth_config.auth_mode == "hosted" and record.owner_id != ctx.owner_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to access this thumbnail.",
            )

        job_root = (resolved_data_dir / job_id).resolve()
        if not job_root.is_relative_to(resolved_data_dir):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid job_id.")

        thumb_path = (job_root / "thumbnail.png").resolve()
        if not thumb_path.is_file():
            with _get_thumb_lock(job_id):
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
                    with _acquire_preview_slot():
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
                        if db.get_job(db_path, job_id) is None:
                            shutil.rmtree(job_root, ignore_errors=True)
                            raise HTTPException(
                                status_code=status.HTTP_404_NOT_FOUND, detail="Job not found."
                            )
                        raise HTTPException(
                            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail="Failed to rasterize PDF thumbnail.",
                        )
                    if db.get_job(db_path, job_id) is None:
                        rendered_png.unlink(missing_ok=True)
                        shutil.rmtree(job_root, ignore_errors=True)
                        raise HTTPException(
                            status_code=status.HTTP_404_NOT_FOUND, detail="Job not found."
                        )
                    rendered_png.replace(thumb_path)

        cache_header = (
            "private, max-age=3600"
            if auth_config.auth_mode == "hosted"
            else "public, max-age=3600"
        )
        return FileResponse(
            path=thumb_path,
            media_type="image/png",
            headers={"Cache-Control": cache_header},
        )

    @application.get("/samples/{filename}", include_in_schema=False)
    def serve_sample_pdf(filename: str) -> FileResponse:
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
                detail="Invalid sample filename.",
            )
        samples_dir = _resolve_default_samples_dir()
        if samples_dir is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sample not found.")
        candidate = (samples_dir / filename).resolve()
        if not candidate.is_relative_to(samples_dir) or not candidate.is_file():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sample not found.")
        media_type = (
            "application/pdf"
            if candidate.suffix.lower() == ".pdf"
            else "application/octet-stream"
        )
        return FileResponse(
            path=candidate,
            media_type=media_type,
            headers={"Cache-Control": "public, max-age=604800"},
        )

    # Mount built SPA frontend if present; otherwise expose clean JSON root for headless/API mode.
    index_html = resolved_web_dist / "index.html"
    if index_html.is_file():
        assets_dir = resolved_web_dist / "assets"
        if assets_dir.is_dir():
            application.mount(
                "/assets",
                _ImmutableAssetsStaticFiles(directory=str(assets_dir)),
                name="assets",
            )

        def _render_branded_index_html() -> str:
            raw_html = index_html.read_text(encoding="utf-8")
            brand_name = auth_config.brand_name
            brand_subtitle = auth_config.brand_subtitle
            brand_tagline = auth_config.brand_tagline
            full_title = f"{brand_name} — {brand_tagline}"
            esc_title = html_lib.escape(full_title, quote=True)
            esc_name = html_lib.escape(brand_name, quote=True)

            rendered = re.sub(
                r"<title>.*?</title>",
                lambda _m: f"<title>{esc_title}</title>",
                raw_html,
                count=1,
                flags=re.DOTALL,
            )
            rendered = re.sub(
                r'(<meta\s+property="og:title"\s+content=")[^"]*(")',
                lambda m: f"{m.group(1)}{esc_title}{m.group(2)}",
                rendered,
                count=1,
            )
            rendered = re.sub(
                r'(<meta\s+name="description"\s+content=")GridForge([^"]*")',
                lambda m: f"{m.group(1)}{esc_name}{m.group(2)}",
                rendered,
                count=1,
            )

            brand_json = (
                json.dumps(
                    {
                        "name": brand_name,
                        "subtitle": brand_subtitle,
                        "tagline": brand_tagline,
                    },
                    separators=(",", ":"),
                )
                .replace("<", "\\u003c")
                .replace(">", "\\u003e")
                .replace("&", "\\u0026")
            )
            brand_script = f'<script id="gf-brand" type="application/json">{brand_json}</script>'
            if "</head>" in rendered:
                rendered = rendered.replace("</head>", f"    {brand_script}\n  </head>", 1)
            return rendered

        cached_index_html = _render_branded_index_html()

        @application.get("/", include_in_schema=False)
        def serve_spa_root() -> HTMLResponse:
            return HTMLResponse(
                content=cached_index_html,
                headers={"Cache-Control": "no-cache"},
            )

        @application.get("/{full_path:path}", include_in_schema=False)
        def serve_spa_fallback(full_path: str) -> Response:
            if (
                full_path.startswith("api/")
                or full_path == "samples"
                or full_path.startswith("samples/")
                or (
                    not enable_docs
                    and full_path in ("docs", "docs/", "redoc", "redoc/", "openapi.json")
                )
            ):
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")
            candidate = (resolved_web_dist / full_path).resolve()
            if (
                full_path
                and candidate.is_relative_to(resolved_web_dist)
                and candidate.is_file()
            ):
                return FileResponse(path=candidate)
            return HTMLResponse(
                content=cached_index_html,
                headers={"Cache-Control": "no-cache"},
            )
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
