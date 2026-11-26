"""Shared Pydantic v2 models, enums, and constants for GridForge."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class PaperSize(str, Enum):
    """Supported physical print paper sizes."""

    LETTER = "letter"
    A4 = "a4"
    LEGAL = "legal"
    A3 = "a3"


PAPER_DIMENSIONS_MM: dict[PaperSize, tuple[float, float]] = {
    PaperSize.LETTER: (215.9, 279.4),
    PaperSize.A4: (210.0, 297.0),
    PaperSize.LEGAL: (215.9, 355.6),
    PaperSize.A3: (297.0, 420.0),
}


class Orientation(str, Enum):
    """Page orientation."""

    PORTRAIT = "portrait"
    LANDSCAPE = "landscape"


class ColorMode(str, Enum):
    """Cartographic color theme mode."""

    COLOR = "color"
    BW = "bw"
    NIGHT_RED = "night-red"
    ALPINE = "alpine"


class Units(str, Enum):
    """Elevation and scale bar measurement system."""

    METRIC = "metric"
    IMPERIAL = "imperial"


class DensityLevel(str, Enum):
    """Relative cartographic detail density for contours and labels."""

    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


class HillshadeStrength(str, Enum):
    """Multi-directional shaded relief intensity."""

    OFF = "off"
    LIGHT = "light"
    NORMAL = "normal"
    STRONG = "strong"


class DemSourceKind(str, Enum):
    """Elevation source selection strategy."""

    AUTO = "auto"
    GLO30 = "glo30"
    USGS3DEP = "usgs3dep"
    AU_ELVIS = "au-elvis"
    NZ_LINZ = "nz-linz"
    CA_HRDEM = "ca-hrdem"


class USOverlayKind(str, Enum):
    """Optional US public-domain thematic overlay layers."""

    FEMA = "fema"
    NHD = "nhd"
    TOWERS = "towers"
    ENC = "enc"
    US_TRAILS = "us-trails"
    US_PUBLIC_LANDS = "us-public-lands"


class AUOverlayKind(str, Enum):
    """Optional Australia-wide & state CC BY 4.0 thematic overlay layers."""

    AU_HYDRO = "au-hydro"
    AU_PARKS = "au-parks"
    AU_INFRA = "au-infra"
    AU_AVIATION = "au-aviation"
    AU_STATE_TRAILS = "au-state-trails"
    AU_NSW_TRAILS = "au-nsw-trails"
    AU_VIC_TRAILS = "au-vic-trails"
    AU_TAS_TRAILS = "au-tas-trails"
    AU_QLD_TRAILS = "au-qld-trails"
    AU_WA_TRAILS = "au-wa-trails"
    AU_SA_TRAILS = "au-sa-trails"
    AU_NT_TRAILS = "au-nt-trails"
    AU_ACT_TRAILS = "au-act-trails"


AU_STATE_TRAIL_OVERLAY_KINDS: tuple[AUOverlayKind, ...] = (
    AUOverlayKind.AU_STATE_TRAILS,
    AUOverlayKind.AU_NSW_TRAILS,
    AUOverlayKind.AU_VIC_TRAILS,
    AUOverlayKind.AU_TAS_TRAILS,
    AUOverlayKind.AU_QLD_TRAILS,
    AUOverlayKind.AU_WA_TRAILS,
    AUOverlayKind.AU_SA_TRAILS,
    AUOverlayKind.AU_NT_TRAILS,
    AUOverlayKind.AU_ACT_TRAILS,
)


class NZOverlayKind(str, Enum):
    """Optional New Zealand CC BY 4.0 thematic overlay layers."""

    NZ_DOC_TRAILS = "nz-doc-trails"


class CAOverlayKind(str, Enum):
    """Optional Canada OGL-Canada thematic overlay layers."""

    CA_CANVEC_TRAILS = "ca-canvec-trails"


class ExportFormat(str, Enum):
    """Offline handheld/ATAK/OsmAnd map archive export formats."""

    MBTILES = "mbtiles"
    KMZ = "kmz"
    BOTH = "both"
    GPKG = "gpkg"
    OSMAND = "osmand"
    BUNDLE = "bundle"


class GarminProfile(str, Enum):
    """Garmin Custom Maps tile-count cap profiles."""

    STANDARD = "standard"
    MODERN = "modern"


GARMIN_PROFILE_MAX_TILES: dict[GarminProfile, int] = {
    GarminProfile.STANDARD: 100,
    GarminProfile.MODERN: 500,
}


class MBTilesFormat(str, Enum):
    """Raster tile encoding inside ATAK MBTiles archives."""

    PNG = "png"
    JPG = "jpg"


class WaypointCategory(str, Enum):
    """Tactical waypoint classification for custom markup and route cards."""

    WAYPOINT = "waypoint"
    CAMP = "camp"
    WATER = "water"
    HAZARD = "hazard"
    LZ = "lz"


class WaypointInput(BaseModel):
    """User-supplied tactical waypoint coordinate and metadata."""

    label: str = Field(..., min_length=1, max_length=60)
    lat: float = Field(..., ge=-80.0, le=84.0)
    lon: float = Field(..., ge=-180.0, le=180.0)
    category: WaypointCategory = WaypointCategory.WAYPOINT
    notes: str | None = None


class ViewshedObserver(BaseModel):
    """Observer point and RF/line-of-sight viewshed parameters."""

    label: str = Field(default="Observer", min_length=1, max_length=60)
    lat: float = Field(..., ge=-80.0, le=84.0)
    lon: float = Field(..., ge=-180.0, le=180.0)
    observer_height_m: float = Field(default=2.0, ge=0.0, le=500.0)
    target_height_m: float = Field(default=1.5, ge=0.0, le=500.0)
    max_distance_m: float = Field(default=10000.0, ge=250.0, le=25000.0)


class RFLinkSpec(BaseModel):
    """Point-to-point RF / Fresnel zone line-of-sight link specification."""

    from_lat: float = Field(..., ge=-80.0, le=84.0)
    from_lon: float = Field(..., ge=-180.0, le=180.0)
    to_lat: float = Field(..., ge=-80.0, le=84.0)
    to_lon: float = Field(..., ge=-180.0, le=180.0)
    from_height_m: float = Field(default=2.0, ge=0.0, le=500.0)
    to_height_m: float = Field(default=2.0, ge=0.0, le=500.0)
    freq_mhz: float = Field(default=915.0, gt=0.0, le=100000.0)
    k_factor: float = Field(default=4.0 / 3.0, gt=0.1, le=5.0)
    from_label: str = Field(default="OBS1", min_length=1, max_length=60)
    to_label: str = Field(default="OBS2", min_length=1, max_length=60)


class TacticalGraphic(BaseModel):
    """User-authored tactical range ring, bearing line, corridor, or sector AOI."""

    kind: Literal["bearing_line", "range_ring", "corridor", "sector"]
    label: str = Field(default="", max_length=80)
    points: list[tuple[float, float]] = Field(default_factory=list)
    radii_km: list[float] = Field(default_factory=lambda: [1.0, 3.0, 5.0])
    color: str | None = None
    notes: str | None = None

    @field_validator("points")
    @classmethod
    def _validate_points(cls, pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
        for lat, lon in pts:
            if not -80.0 <= lat <= 84.0:
                raise ValueError(f"Tactical graphic latitude {lat} is outside UTM bounds [-80.0, 84.0].")
            if not -180.0 <= lon <= 180.0:
                raise ValueError(f"Tactical graphic longitude {lon} must be within [-180.0, 180.0].")
        return pts

    @field_validator("radii_km")
    @classmethod
    def _validate_radii_km(cls, radii: list[float]) -> list[float]:
        for r in radii:
            if not 0.0 < r <= 500.0:
                raise ValueError(f"Tactical graphic radius {r} km must be within (0, 500].")
        return radii


class CartoOptions(BaseModel):
    """Phase 2+ cartographic density and layer visibility controls."""

    contour_density: DensityLevel = DensityLevel.NORMAL
    hillshade_strength: HillshadeStrength = HillshadeStrength.NORMAL
    label_density: DensityLevel = DensityLevel.NORMAL
    show_buildings: bool = True
    show_trees: bool = True
    show_power: bool = True
    show_pipelines: bool = True
    show_grid: bool = True
    show_river_arrows: bool = True
    show_slope_shading: bool = False


VALID_SCALES: tuple[int, ...] = (15000, 24000, 25000, 48000, 50000)


class SheetSpec(BaseModel):
    """User-requested specification for a single topographic map sheet."""

    center_lat: float = Field(
        ...,
        description="Sheet center latitude in WGS84 decimal degrees (-80.0 to 84.0 for UTM).",
    )
    center_lon: float = Field(
        ...,
        description="Sheet center longitude in WGS84 decimal degrees (-180.0 to 180.0).",
    )
    center_mgrs: str | None = None
    scale: int = 25000
    paper: PaperSize = PaperSize.A4
    orientation: Orientation = Orientation.PORTRAIT
    color_mode: ColorMode = ColorMode.COLOR
    units: Units = Units.METRIC
    title: str | None = None
    date_iso: str | None = None
    geopdf: bool = False
    dpi: int = Field(default=300, ge=72, le=1200)
    wmm_cof_path: Path | None = None
    carto: CartoOptions = Field(default_factory=CartoOptions)
    dem_source: DemSourceKind = DemSourceKind.AUTO
    us_overlays: list[USOverlayKind] = Field(default_factory=list)
    au_overlays: list[AUOverlayKind] = Field(default_factory=list)
    nz_overlays: list[NZOverlayKind] = Field(default_factory=list)
    ca_overlays: list[CAOverlayKind] = Field(default_factory=list)
    waypoints: list[WaypointInput] = Field(default_factory=list)
    viewsheds: list[ViewshedObserver] = Field(default_factory=list)
    rf_links: list[RFLinkSpec] = Field(default_factory=list)
    tactical_graphics: list[TacticalGraphic] = Field(default_factory=list)
    include_calibration_page: bool = False
    include_route_card: bool = False
    include_elevation_profile: bool = False
    include_atlas_index: bool = False
    include_rf_link_card: bool = False
    show_fold_marks: bool = False

    @field_validator("center_lat")
    @classmethod
    def _validate_center_lat(cls, value: float) -> float:
        if not -80.0 <= value <= 84.0:
            raise ValueError(
                f"center_lat={value} is outside UTM/MGRS bounds [-80.0, 84.0] "
                "(polar UPS regions are not supported in UTM sheets)."
            )
        return value

    @field_validator("center_lon")
    @classmethod
    def _validate_center_lon(cls, value: float) -> float:
        if not -180.0 <= value <= 180.0:
            raise ValueError(
                f"center_lon={value} must be within [-180.0, 180.0] degrees."
            )
        return value

    @field_validator("scale")
    @classmethod
    def _validate_scale(cls, value: int) -> int:
        if value not in VALID_SCALES:
            allowed = ", ".join(f"1:{s:,}" for s in VALID_SCALES)
            raise ValueError(f"Unsupported scale {value}. Allowed scales: {allowed}.")
        return value


class BBox(BaseModel):
    """Axis-aligned 2D bounding box in a projected or geographic CRS."""

    min_x: float
    min_y: float
    max_x: float
    max_y: float

    @property
    def width(self) -> float:
        return self.max_x - self.min_x

    @property
    def height(self) -> float:
        return self.max_y - self.min_y

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.min_x, self.min_y, self.max_x, self.max_y)


class FrameMarginsMM(BaseModel):
    """Physical page margins around the map neatline in millimeters."""

    top: float = 14.0
    right: float = 12.0
    bottom: float = 48.0
    left: float = 12.0


class SheetLayout(BaseModel):
    """Resolved UTM projection, physical page geometry, and ground footprint for a sheet."""

    spec: SheetSpec
    utm_epsg: int
    utm_zone: int
    utm_hemisphere: Literal["N", "S"]
    gzd: str
    center_easting: float
    center_northing: float
    page_width_mm: float
    page_height_mm: float
    margins_mm: FrameMarginsMM
    frame_x_mm: float
    frame_y_mm: float
    frame_width_mm: float
    frame_height_mm: float
    utm_bbox: BBox
    utm_fetch_bbox: BBox
    wgs84_bbox: BBox
    wgs84_fetch_bbox: BBox


class DeclinationInfo(BaseModel):
    """True North / Grid North / Magnetic North angles at sheet center."""

    decimal_year: float
    magnetic_declination_deg: float
    annual_change_deg: float
    grid_convergence_deg: float
    gm_angle_deg: float
    gm_angle_mils: float
    grid_convergence_mils: float
    model_name: str = "WMM2025"


class GridTick(BaseModel):
    """Single MGRS 1 km neatline tick coordinate and formatted collar label."""

    edge: Literal["top", "bottom", "left", "right"]
    offset_mm: float
    easting_or_northing_m: int
    full_label: str
    principal_digits: str
    prefix_digits: str
    is_100km: bool = False
    is_first_on_edge: bool = False


class MGRSGridMeta(BaseModel):
    """Metadata describing the generated MGRS grid and neatline ticks for a sheet."""

    gzd: str
    square_ids_100km: list[str]
    center_mgrs_10m: str
    grid_spacing_m: int = 1000
    grid_spacing_mm: float
    crosses_100km_boundary: bool
    crosses_utm_zone_boundary: bool
    ticks: list[GridTick]


class JobWorkspace(BaseModel):
    """Normalized, render-ready workspace artifacts produced for one sheet job."""

    job_dir: Path
    layout: SheetLayout
    declination: DeclinationInfo
    mgrs_meta: MGRSGridMeta
    osm_gpkg: Path
    terrain_gpkg: Path
    hillshade_tif: Path
    dem_utm_tif: Path
    mgrs_gpkg: Path
    overlays_gpkg: Path | None = None
    declination_svg: Path
    grid_ref_svg: Path
    contour_interval_display: str
    attributions: list[str]
    vertical_datum: str = "EGM2008"
    dem_source_name: str = "copernicus-glo30"
    regional_overlays: dict[str, Path] = Field(default_factory=dict)
    slope_tif: Path | None = None
    waypoints_gpkg: Path | None = None
    viewshed_tif: Path | None = None
    viewshed_observers_gpkg: Path | None = None
    tactical_graphics_gpkg: Path | None = None
    gpkg_tiles_path: Path | None = None
    osmand_sqlitedb_path: Path | None = None
    field_bundle_zip_path: Path | None = None


class RenderResult(BaseModel):
    """Output metadata returned by a MapRenderer."""

    output_pdf: Path
    page_width_mm: float
    page_height_mm: float
    scale: int
    utm_epsg: int
    elapsed_seconds: float
    workspace_dir: Path | None = None


class ExportResult(BaseModel):
    """Output metadata returned by an offline device archive exporter (MBTiles / Garmin KMZ)."""

    format: ExportFormat
    output_path: Path
    file_size_bytes: int
    tile_count: int
    min_zoom: int | None = None
    max_zoom: int | None = None
    max_tile_px: tuple[int, int] | None = None
    max_tile_bytes: int | None = None
    elapsed_seconds: float


class PackManifest(BaseModel):
    """Manifest describing a self-contained air-gapped offline regional data pack."""

    pack_id: str
    name: str
    region: str
    bbox_wgs84: BBox
    utm_zones: list[str]
    dem_path: str | None = None
    vertical_datum: str = "AHD"
    dem_attribution: str | None = None
    osm_pbf_path: str | None = None
    basemap_path: str | None = None
    overlays: dict[str, str] = Field(default_factory=dict)
    attributions: list[str] = Field(default_factory=list)
    created_at: str
    size_bytes: int = 0
