"""Pydantic v2 API schemas for the GridForge Phase 4 web backend and worker."""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from gridforge.config import (
    VALID_SCALES,
    AUOverlayKind,
    BBox,
    CAOverlayKind,
    CartoOptions,
    ColorMode,
    DeclinationInfo,
    DemSourceKind,
    GarminProfile,
    MBTilesFormat,
    NZOverlayKind,
    Orientation,
    PaperSize,
    RFLinkSpec,
    TacticalGraphic,
    Units,
    USOverlayKind,
    ViewshedObserver,
    WaypointInput,
)
from gridforge.geo.strip_atlas import StripAtlasPlan
from gridforge.geo.tactical import RulerMetricsResponse, RulerSegment
from gridforge.geo.waypoints import RouteMetrics
from gridforge.web.basemap import BasemapInfoResponse
from gridforge.web.pack_tasks import (
    PackBuildRequest,
    PackRegionsResponse,
    PackRegionSummary,
    PackTaskRecord,
    PackTasksListResponse,
)
from gridforge.web.projects import (
    ProjectCreateRequest,
    ProjectRecord,
    ProjectsListResponse,
    ProjectUpdateRequest,
)
from gridforge.web.shares import (
    ShareCreateRequest,
    ShareSnapshotPublic,
    ShareSnapshotSummary,
    SharesListResponse,
)


class JobStatus(str, Enum):
    """Lifecycle status of an asynchronous map generation job."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobArtifact(BaseModel):
    """Downloadable output file produced by a completed render/export job."""

    kind: Literal["pdf", "mbtiles", "kmz", "gpkg", "sqlitedb", "zip"]
    filename: str
    size_bytes: int
    download_url: str
    signed_download_url: str | None = Field(
        default=None,
        description="Short-lived HMAC-signed download URL allowing direct download without cookie or Bearer auth.",
    )


class JobPageInfo(BaseModel):
    """Rasterized PNG preview URLs for one page of a rendered PDF map."""

    page: int = Field(..., ge=1, description="1-based page number.")
    preview_url: str = Field(..., description="Authenticated page PNG preview path.")
    signed_preview_url: str = Field(
        ...,
        description="Short-lived HMAC-signed page PNG preview URL.",
    )


class JobPagesResponse(BaseModel):
    """Response envelope for ``GET /api/jobs/{job_id}/pages``."""

    job_id: str
    page_count: int = Field(..., ge=0)
    pages: list[JobPageInfo] = Field(default_factory=list)


class JobRecord(BaseModel):
    """Public status and metadata record for a web job."""

    job_id: str
    status: JobStatus
    stage: str = "queued"
    progress_pct: int = Field(default=0, ge=0, le=100)
    title: str | None = None
    center: str
    center_mgrs: str | None = None
    scale: int
    paper: PaperSize
    orientation: Orientation
    color_mode: ColorMode
    grid_cols: int = 1
    grid_rows: int = 1
    owner_id: str = "local"
    owner_role: str = "none"
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    elapsed_seconds: float | None = None
    peak_rss_mb: float | None = None
    error: str | None = None
    artifacts: list[JobArtifact] = Field(default_factory=list)


class JobsListResponse(BaseModel):
    """Response envelope for ``GET /api/jobs``."""

    jobs: list[JobRecord] = Field(default_factory=list)


class FootprintRequest(BaseModel):
    """Request payload for ``POST /api/footprint`` live MapLibre footprint preview."""

    center: str = Field(
        ...,
        min_length=1,
        description="Map center as 'lat,lon' decimal degrees (e.g. '-33.732, 150.312') or MGRS string (e.g. '56H KH 510 640').",
    )
    scale: int = Field(
        default=25000,
        description="True-scale denominator: 10000, 12500, 15000, 24000, 25000, 48000, or 50000.",
    )
    paper: PaperSize = Field(default=PaperSize.A4, description="Physical print paper size (a4, letter, legal, a3).")
    orientation: Orientation = Field(default=Orientation.PORTRAIT, description="Page orientation (portrait or landscape).")
    units: Units = Field(default=Units.METRIC, description="Elevation and scale bar unit system (metric or imperial).")
    date_iso: str | None = Field(default=None, description="Optional ISO date (YYYY-MM-DD) for WMM2025 declination calculation.")
    grid_cols: int = Field(default=1, ge=1, le=3, description="Multi-sheet atlas columns (1–3).")
    grid_rows: int = Field(default=1, ge=1, le=3, description="Multi-sheet atlas rows (1–3).")

    @field_validator("scale")
    @classmethod
    def _validate_scale(cls, value: int) -> int:
        if value not in VALID_SCALES:
            allowed = ", ".join(f"1:{s:,}" for s in VALID_SCALES)
            raise ValueError(f"Unsupported scale {value}. Allowed scales: {allowed}.")
        return value


class FootprintResponse(BaseModel):
    """True-scale UTM footprint, MGRS grid preview GeoJSON, and WMM2025 telemetry."""

    center_lat: float
    center_lon: float
    center_mgrs_10m: str
    utm_epsg: int
    utm_zone: int
    utm_hemisphere: Literal["N", "S"]
    gzd: str
    square_ids_100km: list[str]
    page_width_mm: float
    page_height_mm: float
    frame_width_mm: float
    frame_height_mm: float
    ground_width_km: float
    ground_height_km: float
    grid_cols: int = 1
    grid_rows: int = 1
    sheet_count: int = 1
    total_ground_width_km: float
    total_ground_height_km: float
    grid_spacing_mm: float
    declination: DeclinationInfo
    footprint_geojson: dict[str, Any]
    grid_geojson: dict[str, Any]


class GeocodeResult(BaseModel):
    """Single place-name search match returned by ``GET /api/geocode``."""

    display_name: str
    short_name: str
    lat: float
    lon: float
    type: str = "place"


class GeocodeResponse(BaseModel):
    """Response envelope for ``GET /api/geocode``."""

    results: list[GeocodeResult] = Field(default_factory=list)


class StripSheetInput(BaseModel):
    """Strip sheet center passed from Studio Strip Atlas preview to ``POST /api/jobs``."""

    model_config = ConfigDict(extra="ignore")

    sheet_id: str = Field(..., min_length=1, max_length=16)
    center_lat: float = Field(..., ge=-80.0, le=84.0)
    center_lon: float = Field(..., ge=-180.0, le=180.0)
    index: int | None = None

    @field_validator("sheet_id")
    @classmethod
    def _validate_sheet_id(cls, value: str) -> str:
        cleaned = "".join(ch for ch in value if ord(ch) >= 0x20 and ord(ch) != 0x7F).strip()
        if not cleaned:
            raise ValueError("sheet_id must contain visible characters.")
        return cleaned[:16]


class StripPlanRequest(BaseModel):
    """JSON request payload for ``POST /api/strip-plan``."""

    route_geojson: dict[str, Any] = Field(
        ...,
        description="GeoJSON LineString, MultiLineString, Feature, or FeatureCollection representing the route corridor.",
    )
    scale: int = Field(default=25000, description="True-scale denominator (10000–50000).")
    paper: PaperSize = Field(default=PaperSize.A4, description="Physical print paper size.")
    orientation: Orientation = Field(default=Orientation.PORTRAIT, description="Page orientation.")
    units: Units = Field(default=Units.METRIC, description="Unit system (metric or imperial).")
    overlap_pct: float = Field(default=10.0, ge=0.0, le=40.0, description="Overlap percentage between adjacent strip sheets (0–40).")
    max_sheets: int = Field(default=12, ge=1, le=24, description="Maximum number of strip sheets to plan (1–24).")

    @field_validator("scale")
    @classmethod
    def _validate_scale(cls, value: int) -> int:
        if value not in VALID_SCALES:
            allowed = ", ".join(f"1:{s:,}" for s in VALID_SCALES)
            raise ValueError(f"Unsupported scale {value}. Allowed scales: {allowed}.")
        return value


StripPlanResponse = StripAtlasPlan


class RouteMetricsRequest(BaseModel):
    """Request payload for ``POST /api/route-metrics``."""

    waypoints: list[WaypointInput] = Field(
        default_factory=list,
        description="Ordered list of waypoints to compute leg distances, MGRS coordinates, and True/Grid/Magnetic bearings.",
    )
    units: Units = Field(default=Units.METRIC, description="Output distance units (metric or imperial).")
    date_iso: str | None = Field(default=None, description="Optional ISO date (YYYY-MM-DD) for WMM2025 magnetic bearings.")


RouteMetricsResponse = RouteMetrics


class ViewshedPreviewRequest(BaseModel):
    """Request payload for ``POST /api/viewshed-preview`` live terrain-masked RF/LOS preview."""

    observer: ViewshedObserver = Field(..., description="Observer location, antenna heights, and max viewshed radius.")
    dem_source: DemSourceKind = Field(default=DemSourceKind.AUTO, description="DEM elevation source (auto, glo30, usgs3dep, au-elvis, nz-linz, ca-hrdem).")
    pack_id: str | None = Field(default=None, description="Optional installed offline regional pack ID.")
    turnstile_token: str | None = Field(default=None, exclude=True)


class ViewshedPreviewResponse(BaseModel):
    """Live terrain-masked RF / Line-of-Sight viewshed coverage preview for MapLibre GL JS."""

    observer_mgrs_10m: str
    observer_elev_m: float
    utm_epsg: int
    max_distance_m: float
    visible_area_km2: float
    viewshed_geojson: dict[str, Any]
    range_ring_geojson: dict[str, Any]


class RFLinkProfileRequest(RFLinkSpec):
    """Request payload for ``POST /api/rf-link-profile`` point-to-point Fresnel analysis."""

    dem_source: DemSourceKind = Field(default=DemSourceKind.AUTO, description="DEM elevation source for terrain clearance profiling.")
    pack_id: str | None = Field(default=None, description="Optional installed offline regional pack ID.")
    date_iso: str | None = Field(default=None, description="Optional ISO date (YYYY-MM-DD) for WMM2025 magnetic azimuth.")
    turnstile_token: str | None = Field(default=None, exclude=True)


class RFLinkSample(BaseModel):
    """Single sample station along a curved-Earth RF link profile."""

    dist_km: float
    terrain_m: float
    los_m: float
    fresnel_lo_m: float
    fresnel_hi_m: float
    fresnel60_lo_m: float
    clearance_m: float


class RFLinkProfileResponse(BaseModel):
    """Point-to-point RF Fresnel zone & terrain clearance response."""

    status: Literal["CLEAR", "MARGINAL", "OBSTRUCTED"]
    distance_km: float
    freq_mhz: float
    fspl_db: float
    min_clearance_m: float
    min_fresnel_pct: float
    los_clear: bool
    fresnel_60pct_clear: bool
    bearing_true_deg: float
    bearing_grid_deg: float
    bearing_mag_deg: float
    bearing_mils: float
    from_elev_m: float
    to_elev_m: float
    from_mgrs: str
    to_mgrs: str
    from_label: str = "OBS1"
    to_label: str = "OBS2"
    from_height_m: float = 2.0
    to_height_m: float = 2.0
    samples: list[RFLinkSample] = Field(default_factory=list)


class RulerPoint(BaseModel):
    """Single ``(lat, lon)`` point on a ruler measurement path."""

    lat: float = Field(..., ge=-80.0, le=84.0)
    lon: float = Field(..., ge=-180.0, le=180.0)


class RulerMetricsRequest(BaseModel):
    """Request payload for ``POST /api/ruler-metrics``."""

    points: list[RulerPoint] = Field(..., min_length=2, description="Ordered list of at least two [lat, lon] or {lat, lon} points to measure.")
    dem_source: DemSourceKind | None = Field(default=None, description="Optional DEM source to compute terrain elevation gain/loss and slope.")
    pack_id: str | None = Field(default=None, description="Optional installed offline regional pack ID.")
    date_iso: str | None = Field(default=None, description="Optional ISO date (YYYY-MM-DD) for WMM2025 magnetic bearings.")
    turnstile_token: str | None = Field(default=None, exclude=True)

    @model_validator(mode="before")
    @classmethod
    def _accept_tuple_points(cls, data: Any) -> Any:
        if isinstance(data, dict) and isinstance(data.get("points"), list):
            norm = []
            for pt in data["points"]:
                if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                    norm.append({"lat": float(pt[0]), "lon": float(pt[1])})
                else:
                    norm.append(pt)
            data = dict(data)
            data["points"] = norm
        return data


class OutputOptions(BaseModel):
    """Requested print and offline digital export outputs for a job."""

    pdf: bool = Field(default=True, description="Generate a true-scale vector print PDF.")
    geopdf: bool = Field(default=False, description="Embed ISO 32000 GeoPDF georeferencing inside the PDF (Avenza Maps compatible).")
    mbtiles: bool = Field(default=False, description="Export an ATAK/offline raster MBTiles archive.")
    mbtiles_format: MBTilesFormat = Field(default=MBTilesFormat.PNG, description="MBTiles tile encoding (png or jpg).")
    kmz: bool = Field(default=False, description="Export a Garmin Custom Maps KMZ archive.")
    gpkg_tiles: bool = Field(default=False, description="Export an OGC GeoPackage raster tiles archive.")
    osmand_sqlitedb: bool = Field(default=False, description="Export an OsmAnd SQLiteDB offline map.")
    field_bundle_zip: bool = Field(default=False, description="Bundle selected outputs and metadata into one field-ready ZIP.")
    garmin_profile: GarminProfile = Field(default=GarminProfile.STANDARD, description="Garmin KMZ tile cap profile (standard=100, modern=500).")
    max_tiles: int | None = Field(default=None, ge=1, le=500, description="Optional override for maximum Garmin KMZ tiles.")
    include_grid_on_digital: bool = Field(default=True, description="Render the 1 km MGRS grid onto digital exports.")
    export_dpi: int | None = Field(default=None, ge=72, le=600, description="Optional raster DPI for digital exports (72–600).")
    include_calibration_page: bool = Field(default=False, description="Append a 1:1 print scale calibration verification sheet.")
    include_route_card: bool = Field(default=False, description="Append a tactical waypoint route card page.")
    include_elevation_profile: bool = Field(default=False, description="Append a route elevation profile page.")
    include_atlas_index: bool = Field(default=False, description="Prepend a multi-sheet atlas overview index page.")
    include_rf_link_card: bool = Field(default=False, description="Append a point-to-point RF Fresnel link card page.")

    @model_validator(mode="before")
    @classmethod
    def _accept_export_grid_alias(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "include_grid_on_digital" not in data and "include_grid_on_exports" in data:
                data = dict(data)
                data["include_grid_on_digital"] = data["include_grid_on_exports"]
        return data

    @model_validator(mode="after")
    def _require_at_least_one_output(self) -> OutputOptions:
        if not (
            self.pdf
            or self.mbtiles
            or self.kmz
            or self.gpkg_tiles
            or self.osmand_sqlitedb
            or self.field_bundle_zip
        ):
            raise ValueError(
                "At least one output format (pdf, mbtiles, kmz, gpkg_tiles, osmand_sqlitedb, or field_bundle_zip) must be enabled."
            )
        return self


class CreateJobRequest(BaseModel):
    """Request payload for ``POST /api/jobs`` (JSON or multipart ``payload`` field)."""

    center: str | None = Field(
        default=None,
        description="Sheet center as 'lat,lon' decimal degrees (e.g. '-33.732, 150.312') or MGRS string (e.g. '56H KH 510 640'). Required unless center_lat/center_lon or strip_sheets are provided.",
    )
    center_lat: float | None = Field(default=None, description="Alternative WGS84 center latitude (-80.0 to 84.0).")
    center_lon: float | None = Field(default=None, description="Alternative WGS84 center longitude (-180.0 to 180.0).")
    scale: int = Field(
        default=25000,
        description="True-scale denominator: 10000, 12500, 15000, 24000, 25000, 48000, or 50000.",
    )
    paper: PaperSize = Field(default=PaperSize.A4, description="Physical print paper size (a4, letter, legal, a3).")
    orientation: Orientation = Field(default=Orientation.PORTRAIT, description="Page orientation (portrait or landscape).")
    color_mode: ColorMode = Field(default=ColorMode.COLOR, description="Cartography theme: color, bw, night-red, or alpine.")
    units: Units = Field(default=Units.METRIC, description="Elevation and scale bar units (metric or imperial).")
    title: str | None = Field(default=None, max_length=120, description="Optional map sheet title printed in the collar.")
    date_iso: str | None = Field(
        default=None,
        max_length=10,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="Optional ISO date (YYYY-MM-DD) for WMM2025 declination calculation.",
    )
    dpi: int = Field(default=300, ge=72, le=600, description="Print PDF raster resolution in DPI (72–600; vectors stay razor-sharp).")
    dem_source: DemSourceKind = Field(default=DemSourceKind.AUTO, description="DEM elevation source (auto, glo30, usgs3dep, au-elvis, nz-linz, ca-hrdem).")
    pack_id: str | None = Field(default=None, description="Optional installed offline regional pack ID for air-gapped rendering.")
    us_overlays: list[USOverlayKind] = Field(default_factory=list, max_length=16, description="Optional US thematic overlays (fema, nhd, towers, enc, us-trails, us-public-lands).")
    au_overlays: list[AUOverlayKind] = Field(default_factory=list, max_length=16, description="Optional Australian thematic overlays (au-hydro, au-parks, au-infra, au-aviation, au-state-trails, etc.).")
    nz_overlays: list[NZOverlayKind] = Field(default_factory=list, max_length=8, description="Optional New Zealand overlays (nz-doc-trails).")
    ca_overlays: list[CAOverlayKind] = Field(default_factory=list, max_length=8, description="Optional Canadian overlays (ca-canvec-trails).")
    carto: CartoOptions = Field(default_factory=CartoOptions, description="Cartographic density, hillshade, slope shading, and layer visibility options.")
    outputs: OutputOptions = Field(default_factory=OutputOptions, description="Output print PDF and offline digital device export options.")
    grid_cols: int = Field(default=1, ge=1, le=3, description="Multi-sheet grid atlas columns (1–3).")
    grid_rows: int = Field(default=1, ge=1, le=3, description="Multi-sheet grid atlas rows (1–3).")
    waypoints: list[WaypointInput] = Field(default_factory=list, max_length=200, description="Custom tactical waypoints and route points to plot on the map.")
    viewsheds: list[ViewshedObserver] = Field(default_factory=list, max_length=10, description="RF / Line-of-Sight viewshed observers to render on the map.")
    rf_links: list[RFLinkSpec] = Field(default_factory=list, max_length=20, description="Point-to-point RF / Fresnel links to plot and include in RF Link Card.")
    tactical_graphics: list[TacticalGraphic] = Field(default_factory=list, max_length=50, description="Custom range rings, bearing lines, corridors, or sectors.")
    strip_sheets: list[StripSheetInput] = Field(default_factory=list, max_length=24, description="Ordered strip-atlas sheet centers from POST /api/strip-plan.")
    include_calibration_page: bool = Field(default=False, description="Append a 1:1 scale verification calibration page.")
    include_route_card: bool = Field(default=False, description="Append a waypoint tactical route card page.")
    include_elevation_profile: bool = Field(default=False, description="Append a route elevation profile page.")
    include_atlas_index: bool = Field(default=False, description="Prepend a multi-sheet atlas index page.")
    include_rf_link_card: bool = Field(default=False, description="Append an RF Fresnel link card page.")
    show_fold_marks: bool = Field(default=False, description="Draw map folding registration ticks on the page margin.")
    turnstile_token: str | None = Field(default=None, exclude=True)

    @field_validator("scale")
    @classmethod
    def _validate_scale(cls, value: int) -> int:
        if value not in VALID_SCALES:
            allowed = ", ".join(f"1:{s:,}" for s in VALID_SCALES)
            raise ValueError(f"Unsupported scale {value}. Allowed scales: {allowed}.")
        return value

    @model_validator(mode="after")
    def _ensure_center_present(self) -> CreateJobRequest:
        # Sync supplemental page flags whether provided top-level or inside outputs.
        self.include_calibration_page = (
            self.include_calibration_page or self.outputs.include_calibration_page
        )
        self.include_route_card = (
            self.include_route_card or self.outputs.include_route_card
        )
        self.include_elevation_profile = (
            self.include_elevation_profile or self.outputs.include_elevation_profile
        )
        self.include_atlas_index = (
            self.include_atlas_index or self.outputs.include_atlas_index
        )
        self.include_rf_link_card = (
            self.include_rf_link_card or self.outputs.include_rf_link_card
        )
        if not self.center and self.strip_sheets:
            s0 = self.strip_sheets[0]
            self.center = f"{s0.center_lat:.6f}, {s0.center_lon:.6f}"
        if not self.center and (self.center_lat is None or self.center_lon is None):
            raise ValueError("Provide either 'center' ('lat,lon' or MGRS) or both 'center_lat' and 'center_lon'.")
        if not self.center and self.center_lat is not None and self.center_lon is not None:
            self.center = f"{self.center_lat:.6f}, {self.center_lon:.6f}"
        return self


class PackSummary(BaseModel):
    """Public summary of an installed air-gapped offline regional pack."""

    pack_id: str
    name: str
    region: str
    bbox_wgs84: BBox
    utm_zones: list[str]
    vertical_datum: str
    size_bytes: int
    has_dem: bool = True
    has_osm: bool = True
    has_basemap: bool = False
    overlays: list[str] = Field(default_factory=list)


class PacksResponse(BaseModel):
    """Response envelope for ``GET /api/packs``."""

    packs: list[PackSummary] = Field(default_factory=list)


class HealthResponse(BaseModel):
    """Response envelope for ``GET /api/health``."""

    status: Literal["ok", "degraded"] = "ok"
    queue_depth: int = 0
    worker_alive: bool = False
    worker_last_seen: str | None = None
    memory_budget_mb: int = 3072
    packs_count: int = 0


# ---------------------------------------------------------------------------
# Opt-in Hosted Authentication, Session & Admin Contracts
# ---------------------------------------------------------------------------

AuthRole = Literal["none", "guest", "friend", "admin"]


class AuthUserSummary(BaseModel):
    """Authenticated user summary returned inside ``GET /api/auth/session``."""

    user_id: str
    email: str
    display_name: str = ""
    role: Literal["friend", "admin"]


class AuthCapabilities(BaseModel):
    """Effective per-role feature caps and client flags."""

    max_grid_cells: int
    max_grid_cols: int
    max_grid_rows: int
    strip_atlas_allowed: bool
    max_strip_sheets: int
    allowed_outputs: list[str]
    packs_manage_allowed: bool
    max_upload_bytes: int
    max_dpi: int = 600
    max_export_dpi: int = 600
    turnstile_required: bool
    turnstile_sitekey: str | None = None
    google_oauth_enabled: bool = False


class AuthQuotas(BaseModel):
    """Live quota usage and global public render budget snapshot."""

    daily_renders_limit: int | None = None
    daily_renders_used: int = 0
    daily_renders_remaining: int | None = None
    hourly_previews_limit: int | None = None
    hourly_previews_used: int = 0
    hourly_previews_remaining: int | None = None
    public_renders_paused: bool = False
    global_daily_public_limit: int | None = None
    global_daily_public_used: int = 0


class AuthSessionResponse(BaseModel):
    """Response envelope for ``GET /api/auth/session`` and auth mutations."""

    auth_mode: Literal["none", "hosted"]
    authenticated: bool
    user: AuthUserSummary | None = None
    role: AuthRole
    capabilities: AuthCapabilities
    quotas: AuthQuotas
    brand_name: str = "GridForge"
    brand_subtitle: str = "Studio"
    brand_tagline: str = "Tactical Topographic Print Studio"


class LoginRequest(BaseModel):
    """Request payload for ``POST /api/auth/login``."""

    email: str = Field(..., min_length=3, max_length=254)
    password: str = Field(..., min_length=1, max_length=256)


class AcceptInviteRequest(BaseModel):
    """Request payload for ``POST /api/auth/accept-invite``."""

    token: str = Field(..., min_length=8, max_length=256)
    email: str | None = Field(default=None, min_length=3, max_length=254)
    password: str = Field(..., min_length=8, max_length=256)
    display_name: str | None = Field(default=None, max_length=120)


class AdminUsageSummary(BaseModel):
    """Aggregate usage & telemetry metrics for ``GET /api/admin/usage``."""

    renders_today: int = 0
    renders_7d: int = 0
    previews_today: int = 0
    active_queue_depth: int = 0
    global_daily_public_used: int = 0
    global_daily_public_limit: int | None = None
    public_renders_paused: bool = False
    avg_elapsed_seconds: float | None = None
    max_peak_rss_mb: float | None = None


class AdminUsageEvent(BaseModel):
    """Recent metered event returned inside ``GET /api/admin/usage``."""

    event_id: int
    event_type: str
    actor_id: str
    actor_role: str
    client_ip: str = ""
    job_id: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class AdminUsageResponse(BaseModel):
    """Response envelope for ``GET /api/admin/usage``."""

    summary: AdminUsageSummary
    by_role: dict[str, dict[str, int]] = Field(default_factory=dict)
    recent_events: list[AdminUsageEvent] = Field(default_factory=list)


class AdminUserRecord(BaseModel):
    """User account summary returned inside ``GET /api/admin/users``."""

    user_id: str
    email: str
    display_name: str = ""
    role: Literal["friend", "admin"]
    disabled: bool = False
    daily_render_limit: int | None = None
    hourly_preview_limit: int | None = None
    has_password: bool = False
    has_google: bool = False
    created_at: str
    last_login_at: str | None = None


class AdminInviteRecord(BaseModel):
    """Invite record summary returned inside ``GET /api/admin/users``."""

    invite_id: str
    email: str | None = None
    role: Literal["friend", "admin"]
    created_by: str | None = None
    created_at: str
    expires_at: str
    used_at: str | None = None
    used_by_user_id: str | None = None
    revoked_at: str | None = None
    status: Literal["pending", "used", "revoked", "expired"]


class AdminUsersResponse(BaseModel):
    """Response envelope for ``GET /api/admin/users``."""

    users: list[AdminUserRecord] = Field(default_factory=list)
    invites: list[AdminInviteRecord] = Field(default_factory=list)


class CreateInviteRequest(BaseModel):
    """Request payload for ``POST /api/admin/invites``."""

    email: str | None = Field(default=None, max_length=254)
    role: Literal["friend", "admin"] = "friend"
    expires_hours: int = Field(default=168, ge=1, le=8760)


class CreateInviteResponse(BaseModel):
    """Response payload for ``POST /api/admin/invites``."""

    invite_id: str
    token: str
    invite_path: str
    expires_at: str


class RevokeInviteResponse(BaseModel):
    """Response payload for ``DELETE /api/admin/invites/{invite_id}``."""

    revoked: bool = True
    invite_id: str


class UpdateUserRequest(BaseModel):
    """Request payload for ``PATCH /api/admin/users/{user_id}``."""

    disabled: bool | None = None
    daily_render_limit: int | None = Field(default=None, ge=0, le=10000)
    hourly_preview_limit: int | None = Field(default=None, ge=0, le=10000)
    role: Literal["friend", "admin"] | None = None


class UpdateUserResponse(BaseModel):
    """Response payload for ``PATCH /api/admin/users/{user_id}``."""

    user: AdminUserRecord


class AdminSettingsRequest(BaseModel):
    """Request payload for ``POST /api/admin/settings``."""

    public_renders_paused: bool


class AdminSettingsResponse(BaseModel):
    """Response payload for ``POST /api/admin/settings``."""

    public_renders_paused: bool


# ---------------------------------------------------------------------------
# Scoped Bearer API Keys (Personal Access Tokens)
# ---------------------------------------------------------------------------

ApiKeyScope = Literal[
    "all",
    "planning",
    "jobs:read",
    "jobs:write",
    "projects:read",
    "projects:write",
    "shares:read",
    "shares:write",
]
API_KEY_SCOPES: frozenset[str] = frozenset(get_args(ApiKeyScope))


class ApiKeyRecord(BaseModel):
    """Public metadata for a user-scoped Bearer API key (never includes secret token)."""

    key_id: str
    name: str
    key_prefix: str
    scopes: list[str] = Field(default_factory=list)
    created_at: str
    expires_at: str | None = None
    revoked_at: str | None = None
    last_used_at: str | None = None
    status: Literal["active", "revoked", "expired"] = "active"


class ApiKeysListResponse(BaseModel):
    """Response envelope for ``GET /api/auth/api-keys``."""

    api_keys: list[ApiKeyRecord] = Field(default_factory=list)


class CreateApiKeyRequest(BaseModel):
    """Request payload for ``POST /api/auth/api-keys``."""

    name: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Human-readable label for the API key (e.g. 'Claude Desktop', 'ChatGPT Actions').",
    )
    expires_days: int | None = Field(
        default=90,
        ge=1,
        le=3650,
        description="Optional lifetime in days (1–3650). Set to null for a non-expiring key.",
    )
    scopes: list[ApiKeyScope] = Field(
        default_factory=lambda: ["all"],
        min_length=1,
        max_length=len(API_KEY_SCOPES) - 1,
        description="Allowed Studio operations. Each write scope includes read access for the same resource. Use ['all'] by itself for all Studio operations.",
    )

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("API key name must not be empty.")
        return cleaned

    @field_validator("scopes")
    @classmethod
    def _validate_scopes(cls, scopes: list[ApiKeyScope]) -> list[ApiKeyScope]:
        if "all" in scopes and len(scopes) != 1:
            raise ValueError("The 'all' scope must be used by itself.")
        if len(set(scopes)) != len(scopes):
            raise ValueError("API key scopes must be unique.")
        return scopes


class CreateApiKeyResponse(BaseModel):
    """Response payload for ``POST /api/auth/api-keys`` (plaintext token returned once)."""

    api_key: ApiKeyRecord
    token: str = Field(..., description="Plaintext Bearer token ('gf_...'). Store immediately; never shown again.")


class RevokeApiKeyResponse(BaseModel):
    """Response payload for ``DELETE /api/auth/api-keys/{key_id}``."""

    revoked: bool = True
    key_id: str
