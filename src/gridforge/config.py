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


class Units(str, Enum):
    """Elevation and scale bar measurement system."""

    METRIC = "metric"
    IMPERIAL = "imperial"


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


class RenderResult(BaseModel):
    """Output metadata returned by a MapRenderer."""

    output_pdf: Path
    page_width_mm: float
    page_height_mm: float
    scale: int
    utm_epsg: int
    elapsed_seconds: float
    workspace_dir: Path | None = None
