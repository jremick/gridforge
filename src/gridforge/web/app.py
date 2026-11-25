"""FastAPI web studio backend for GridForge (zero QGIS imports, < 120 MB RAM)."""

from __future__ import annotations

import math
import os
import re
import uuid
from pathlib import Path
from typing import Any

import mgrs
from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from pyproj import Transformer

from gridforge.config import SheetSpec
from gridforge.geo.crs import compute_sheet_layout, parse_center
from gridforge.geo.declination import compute_declination
from gridforge.packs.builder import discover_packs
from gridforge.web import db
from gridforge.web.schemas import (
    CreateJobRequest,
    FootprintRequest,
    FootprintResponse,
    HealthResponse,
    JobRecord,
    JobsListResponse,
    PacksResponse,
    PackSummary,
)

MAX_UPLOAD_BYTES: int = 25 * 1024 * 1024  # 25 MiB
ALLOWED_OVERLAY_EXTS: frozenset[str] = frozenset({".geojson", ".json", ".gpx", ".csv"})
_JOB_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{8,64}$")
_PACK_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_MGRS_SPLIT_RE = re.compile(r"^0?([1-9]|[1-5]\d|60)([C-HJ-NP-X])([A-HJ-NP-Z]{2})(\d*)$")

_MEDIA_TYPES: dict[str, str] = {
    ".pdf": "application/pdf",
    ".mbtiles": "application/vnd.sqlite3",
    ".kmz": "application/vnd.google-earth.kmz",
}


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


def _build_footprint_preview(req: FootprintRequest) -> FootprintResponse:
    """Compute live true-scale UTM footprint, 1 km MGRS preview lines, and WMM2025 telemetry."""
    try:
        lat, lon, canonical_mgrs = parse_center(req.center)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

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
        declination = compute_declination(layout, elevation_m=0.0)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    to_wgs84 = Transformer.from_crs(f"EPSG:{layout.utm_epsg}", "EPSG:4326", always_xy=True)
    bbox = layout.utm_bbox

    # 1. Densified perimeter ring of layout.utm_bbox (CCW in WGS84: SW -> SE -> NE -> NW -> SW)
    edge_steps = 8
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

    footprint_geojson: dict[str, Any] = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "scale": spec.scale,
                    "paper": spec.paper.value,
                    "orientation": spec.orientation.value,
                    "utm_epsg": layout.utm_epsg,
                    "gzd": layout.gzd,
                },
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [ring_coords],
                },
            }
        ],
    }

    # 2. Pure in-memory 1 km MGRS Easting & Northing preview lines
    e_start = int(math.ceil(bbox.min_x / 1000.0)) * 1000
    e_end = int(math.floor(bbox.max_x / 1000.0)) * 1000
    n_start = int(math.ceil(bbox.min_y / 1000.0)) * 1000
    n_end = int(math.floor(bbox.max_y / 1000.0)) * 1000

    grid_features: list[dict[str, Any]] = []
    line_samples = 6

    for e in range(e_start, e_end + 1, 1000):
        xs = [float(e)] * line_samples
        ys = [bbox.min_y + bbox.height * i / (line_samples - 1) for i in range(line_samples)]
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
        xs = [bbox.min_x + bbox.width * i / (line_samples - 1) for i in range(line_samples)]
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

    # 3. Center 10 m MGRS & visible 100 km square IDs (fast in-memory sampling)
    converter = mgrs.MGRS()
    c_gzd, c_sq, c_digits = _split_mgrs(
        _decode_mgrs(converter.toMGRS(lat, lon, MGRSPrecision=4))
    )
    center_mgrs_10m = f"{c_gzd} {c_sq} {c_digits[:4]} {c_digits[4:]}"

    square_ids: list[str] = [c_sq]
    for fx in (0.05, 0.5, 0.95):
        for fy in (0.05, 0.5, 0.95):
            px = bbox.min_x + fx * bbox.width
            py = bbox.min_y + fy * bbox.height
            plon, plat = to_wgs84.transform(px, py)
            if -80.0 <= plat <= 84.0:
                _, sq, _ = _split_mgrs(
                    _decode_mgrs(converter.toMGRS(plat, plon, MGRSPrecision=0))
                )
                if sq not in square_ids:
                    square_ids.append(sq)

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
        ground_width_km=round(bbox.width / 1000.0, 3),
        ground_height_km=round(bbox.height / 1000.0, 3),
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
) -> FastAPI:
    """Create and configure the GridForge Phase 4 FastAPI application."""
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

    application = FastAPI(
        title="GridForge Map Studio API",
        version="0.4.0",
        description="Self-hosted military-style MGRS topographic map generator & offline pack studio.",
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    application.state.data_dir = resolved_data_dir
    application.state.packs_dir = resolved_packs_dir
    application.state.db_path = db_path
    application.state.web_dist_dir = resolved_web_dist

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
                overlays=sorted(m.overlays.keys()),
            )
            for m in manifests
        ]
        return PacksResponse(packs=summaries)

    @application.post("/api/footprint", response_model=FootprintResponse)
    def api_footprint(req: FootprintRequest) -> FootprintResponse:
        return _build_footprint_preview(req)

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
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Form request must include a JSON string 'payload' field.",
                )
            try:
                job_req = CreateJobRequest.model_validate_json(raw_payload)
            except ValidationError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
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
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=exc.errors(),
                ) from exc

        assert job_req.center is not None
        try:
            lat, lon, canonical_mgrs = parse_center(job_req.center)
            # Validate full SheetSpec bounds early before enqueueing.
            SheetSpec(
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
            )
        except (ValueError, ValidationError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
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
    def api_download_artifact(job_id: str, filename: str) -> FileResponse:
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
        return FileResponse(
            path=candidate,
            media_type=media_type,
            filename=candidate.name,
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
