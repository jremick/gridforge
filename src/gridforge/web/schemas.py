"""Pydantic v2 API schemas for the GridForge Phase 4 web backend and worker."""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

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

    center: str = Field(..., min_length=1, description="Center as 'lat,lon' or MGRS string.")
    scale: int = 25000
    paper: PaperSize = PaperSize.A4
    orientation: Orientation = Orientation.PORTRAIT
    units: Units = Units.METRIC
    date_iso: str | None = None
    grid_cols: int = Field(default=1, ge=1, le=3)
    grid_rows: int = Field(default=1, ge=1, le=3)

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


class StripPlanRequest(BaseModel):
    """JSON request payload for ``POST /api/strip-plan``."""

    route_geojson: dict[str, Any]
    scale: int = 25000
    paper: PaperSize = PaperSize.A4
    orientation: Orientation = Orientation.PORTRAIT
    units: Units = Units.METRIC
    overlap_pct: float = Field(default=10.0, ge=0.0, le=40.0)
    max_sheets: int = Field(default=12, ge=1, le=24)

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

    waypoints: list[WaypointInput] = Field(default_factory=list)
    units: Units = Units.METRIC
    date_iso: str | None = None


RouteMetricsResponse = RouteMetrics


class ViewshedPreviewRequest(BaseModel):
    """Request payload for ``POST /api/viewshed-preview`` live terrain-masked RF/LOS preview."""

    observer: ViewshedObserver
    dem_source: DemSourceKind = DemSourceKind.AUTO
    pack_id: str | None = None
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

    dem_source: DemSourceKind = DemSourceKind.AUTO
    pack_id: str | None = None
    date_iso: str | None = None
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

    points: list[RulerPoint] = Field(..., min_length=2)
    dem_source: DemSourceKind | None = None
    pack_id: str | None = None
    date_iso: str | None = None
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

    pdf: bool = True
    geopdf: bool = False
    mbtiles: bool = False
    mbtiles_format: MBTilesFormat = MBTilesFormat.PNG
    kmz: bool = False
    gpkg_tiles: bool = False
    osmand_sqlitedb: bool = False
    field_bundle_zip: bool = False
    garmin_profile: GarminProfile = GarminProfile.STANDARD
    max_tiles: int | None = Field(default=None, ge=1, le=500)
    include_grid_on_digital: bool = True
    export_dpi: int | None = Field(default=None, ge=72, le=600)
    include_calibration_page: bool = False
    include_route_card: bool = False
    include_elevation_profile: bool = False
    include_atlas_index: bool = False
    include_rf_link_card: bool = False

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

    center: str | None = None
    center_lat: float | None = None
    center_lon: float | None = None
    scale: int = 25000
    paper: PaperSize = PaperSize.A4
    orientation: Orientation = Orientation.PORTRAIT
    color_mode: ColorMode = ColorMode.COLOR
    units: Units = Units.METRIC
    title: str | None = None
    date_iso: str | None = None
    dpi: int = Field(default=300, ge=72, le=600)
    dem_source: DemSourceKind = DemSourceKind.AUTO
    pack_id: str | None = None
    us_overlays: list[USOverlayKind] = Field(default_factory=list)
    au_overlays: list[AUOverlayKind] = Field(default_factory=list)
    nz_overlays: list[NZOverlayKind] = Field(default_factory=list)
    ca_overlays: list[CAOverlayKind] = Field(default_factory=list)
    carto: CartoOptions = Field(default_factory=CartoOptions)
    outputs: OutputOptions = Field(default_factory=OutputOptions)
    grid_cols: int = Field(default=1, ge=1, le=3)
    grid_rows: int = Field(default=1, ge=1, le=3)
    waypoints: list[WaypointInput] = Field(default_factory=list)
    viewsheds: list[ViewshedObserver] = Field(default_factory=list)
    rf_links: list[RFLinkSpec] = Field(default_factory=list)
    tactical_graphics: list[TacticalGraphic] = Field(default_factory=list)
    strip_sheets: list[StripSheetInput] = Field(default_factory=list)
    include_calibration_page: bool = False
    include_route_card: bool = False
    include_elevation_profile: bool = False
    include_atlas_index: bool = False
    include_rf_link_card: bool = False
    show_fold_marks: bool = False
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
