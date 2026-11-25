"""Pydantic v2 API schemas for the GridForge Phase 4 web backend and worker."""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from gridforge.config import (
    VALID_SCALES,
    AUOverlayKind,
    BBox,
    CartoOptions,
    ColorMode,
    DeclinationInfo,
    DemSourceKind,
    GarminProfile,
    MBTilesFormat,
    Orientation,
    PaperSize,
    Units,
    USOverlayKind,
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

    kind: Literal["pdf", "mbtiles", "kmz"]
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
    grid_spacing_mm: float
    declination: DeclinationInfo
    footprint_geojson: dict[str, Any]
    grid_geojson: dict[str, Any]


class OutputOptions(BaseModel):
    """Requested print and offline digital export outputs for a job."""

    pdf: bool = True
    geopdf: bool = False
    mbtiles: bool = False
    mbtiles_format: MBTilesFormat = MBTilesFormat.PNG
    kmz: bool = False
    garmin_profile: GarminProfile = GarminProfile.STANDARD
    max_tiles: int | None = Field(default=None, ge=1, le=500)
    include_grid_on_digital: bool = True
    export_dpi: int | None = Field(default=None, ge=72, le=600)

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
        if not (self.pdf or self.mbtiles or self.kmz):
            raise ValueError("At least one output format (pdf, mbtiles, or kmz) must be enabled.")
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
    carto: CartoOptions = Field(default_factory=CartoOptions)
    outputs: OutputOptions = Field(default_factory=OutputOptions)

    @field_validator("scale")
    @classmethod
    def _validate_scale(cls, value: int) -> int:
        if value not in VALID_SCALES:
            allowed = ", ".join(f"1:{s:,}" for s in VALID_SCALES)
            raise ValueError(f"Unsupported scale {value}. Allowed scales: {allowed}.")
        return value

    @model_validator(mode="after")
    def _ensure_center_present(self) -> CreateJobRequest:
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
