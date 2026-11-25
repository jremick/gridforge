"""Coordinate parsing, UTM/MGRS zone resolution, and true-scale sheet footprint math."""

from __future__ import annotations

import functools
import math
import re
from typing import Literal

import mgrs
from pyproj import Transformer

from gridforge.config import (
    PAPER_DIMENSIONS_MM,
    BBox,
    FrameMarginsMM,
    Orientation,
    SheetLayout,
    SheetSpec,
)

MIN_UTM_LAT: float = -80.0
MAX_UTM_LAT: float = 84.0

# 20 MGRS latitude bands from 80°S to 84°N (omits I and O; X spans 72°N..84°N).
_MGRS_LAT_BANDS: str = "CDEFGHJKLMNPQRSTUVWX"

# Pattern for UTM-domain MGRS strings (1-2 digit zone + band C..X + 2 square letters + even digits).
_MGRS_UTM_RE = re.compile(
    r"^(0?[1-9]|[1-5]\d|60)([C-HJ-NP-X])([A-HJ-NP-Z]{2})(\d{0,10})$"
)
# Pattern for polar UPS MGRS strings (A, B in south; Y, Z in north).
_MGRS_UPS_RE = re.compile(r"^[ABYZ][A-HJ-NP-Z]{2}\d{0,10}$")


def validate_utm_coords(lat: float, lon: float) -> None:
    """Validate that (lat, lon) lies within WGS84 UTM/MGRS bounds [-80, 84] x [-180, 180]."""
    if not (math.isfinite(lat) and math.isfinite(lon)):
        raise ValueError(f"Coordinates must be finite numbers, got lat={lat}, lon={lon}.")
    if not (MIN_UTM_LAT <= lat <= MAX_UTM_LAT):
        raise ValueError(
            f"Latitude {lat} is outside UTM/MGRS bounds [{MIN_UTM_LAT}, {MAX_UTM_LAT}]; "
            "polar UPS regions are not supported."
        )
    if not (-180.0 <= lon <= 180.0):
        raise ValueError(f"Longitude {lon} must be within [-180.0, 180.0] degrees.")


def _decode_mgrs_bytes(value: str | bytes) -> str:
    return value.decode("ascii") if isinstance(value, bytes) else str(value)


def parse_center(center_str: str) -> tuple[float, float, str | None]:
    """Parse a sheet center specified as 'lat,lon' or an MGRS string.

    Returns:
        (lat, lon, canonical_mgrs) where canonical_mgrs is None for lat/lon inputs
        and an uppercase, whitespace-stripped MGRS string for MGRS inputs.
    """
    if not isinstance(center_str, str) or not center_str.strip():
        raise ValueError("Center coordinate string must be non-empty.")

    cleaned = center_str.strip()

    # Check numeric lat,lon format first (supports decimal & scientific notation).
    _num_pair_re = re.compile(
        r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?"
        r"(?:\s*,\s*|\s+)"
        r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$"
    )
    if _num_pair_re.match(cleaned) or "," in cleaned:
        parts = [p.strip() for p in cleaned.split(",")] if "," in cleaned else cleaned.split()
        if len(parts) != 2:
            raise ValueError(
                f"Invalid lat,lon center '{center_str}'. Expected 'lat,lon' or an MGRS string."
            )
        try:
            lat = float(parts[0])
            lon = float(parts[1])
        except ValueError as exc:
            raise ValueError(
                f"Could not parse numeric lat,lon from '{center_str}'."
            ) from exc
        validate_utm_coords(lat, lon)
        return (lat, lon, None)

    # Otherwise parse as MGRS (whitespace and case tolerant).
    normalized = re.sub(r"\s+", "", cleaned).upper()

    if _MGRS_UPS_RE.match(normalized):
        raise ValueError(
            f"MGRS coordinate '{center_str}' lies in a polar UPS region "
            f"(outside UTM coverage [{MIN_UTM_LAT}, {MAX_UTM_LAT}])."
        )

    match = _MGRS_UTM_RE.match(normalized)
    if not match:
        raise ValueError(
            f"Invalid MGRS coordinate '{center_str}'. Expected e.g. '56HKH5100064000' "
            "or '56H KH 510 640'."
        )

    zone_str, band, square_id, digits = match.groups()
    if len(digits) % 2 != 0:
        raise ValueError(
            f"Invalid MGRS coordinate '{center_str}': numerical Easting/Northing part "
            f"'{digits}' must have an even number of digits."
        )

    zone_num = int(zone_str)
    if band == "X" and zone_num in (32, 34, 36):
        raise ValueError(
            f"Invalid MGRS Grid Zone Designation '{zone_num}{band}': "
            "zones 32X, 34X, and 36X do not exist in Svalbard."
        )

    canonical_mgrs = f"{zone_num}{band}{square_id}{digits}"

    # mgrs>=1.6.0 returns the grid cell center as (lat, lon).
    converter = mgrs.MGRS()
    try:
        lat_raw, lon_raw = converter.toLatLon(canonical_mgrs)
    except Exception as exc:
        raise ValueError(
            f"Failed to convert MGRS coordinate '{center_str}' to WGS84: {exc}"
        ) from exc

    lat = float(lat_raw)
    lon = float(lon_raw)
    validate_utm_coords(lat, lon)
    return (lat, lon, canonical_mgrs)


def utm_zone_lon_bounds(zone: int, band: str | None = None) -> tuple[float, float]:
    """Return exact (west_lon, east_lon) bounds for a UTM/MGRS zone, honoring Norway & Svalbard."""
    if not 1 <= zone <= 60:
        raise ValueError(f"UTM zone must be in 1..60, got {zone}.")

    band_upper = band.upper() if band else None
    if band_upper == "V":
        if zone == 31:
            return (0.0, 3.0)
        if zone == 32:
            return (3.0, 12.0)
    elif band_upper == "X":
        svalbard_bounds = {
            31: (0.0, 9.0),
            33: (9.0, 21.0),
            35: (21.0, 33.0),
            37: (33.0, 42.0),
        }
        if zone in svalbard_bounds:
            return svalbard_bounds[zone]
        if zone in (32, 34, 36):
            raise ValueError(f"Zone {zone}X does not exist in Svalbard MGRS.")

    west = (zone - 1) * 6.0 - 180.0
    return (west, west + 6.0)


def resolve_utm_zone(lat: float, lon: float) -> tuple[int, Literal["N", "S"], int, str]:
    """Resolve UTM zone, hemisphere, EPSG code, and MGRS GZD for (lat, lon).

    Honors exact NGA/MGRS UTM zone exceptions for Norway (32V) and Svalbard
    (31X, 33X, 35X, 37X) and verifies consistency against `mgrs.MGRS().toMGRS()`.

    Returns:
        (utm_zone, hemisphere, utm_epsg, gzd)
    """
    validate_utm_coords(lat, lon)

    # Standard 6-degree zone; clamp lon == 180.0 to zone 60.
    if lon == 180.0:
        zone = 60
    else:
        zone = int(math.floor((lon + 180.0) / 6.0)) + 1
        zone = max(1, min(60, zone))

    # Norway exception: band V (56°N..64°N), 3°E..12°E belongs to zone 32.
    if 56.0 <= lat < 64.0 and 3.0 <= lon < 12.0:
        zone = 32
    # Svalbard exception: band X (72°N..84°N), zones 31X/33X/35X/37X widen.
    elif 72.0 <= lat <= 84.0:
        if 0.0 <= lon < 9.0:
            zone = 31
        elif 9.0 <= lon < 21.0:
            zone = 33
        elif 21.0 <= lon < 33.0:
            zone = 35
        elif 33.0 <= lon < 42.0:
            zone = 37

    band_idx = min(len(_MGRS_LAT_BANDS) - 1, int(math.floor((lat + 80.0) / 8.0)))
    band = _MGRS_LAT_BANDS[band_idx]
    gzd = f"{zone}{band}"

    # Verify consistency with NGA GEOTRANS via mgrs library (skip lon==180 wrap ambiguity).
    if lon < 180.0:
        mgrs_raw = _decode_mgrs_bytes(mgrs.MGRS().toMGRS(lat, lon, MGRSPrecision=0))
        m_match = _MGRS_UTM_RE.match(mgrs_raw.strip().upper())
        if m_match:
            lib_zone = int(m_match.group(1))
            lib_band = m_match.group(2)
            # Only enforce when not strictly on a floating-point band/meridian seam.
            if lib_zone != zone or lib_band != band:
                zone = lib_zone
                band = lib_band
                gzd = f"{zone}{band}"

    hemisphere: Literal["N", "S"] = "N" if lat >= 0.0 else "S"
    utm_epsg = (32600 if hemisphere == "N" else 32700) + zone
    return (zone, hemisphere, utm_epsg, gzd)


@functools.lru_cache(maxsize=64)
def _get_transformer(src_crs: str, dst_crs: str) -> Transformer:
    return Transformer.from_crs(src_crs, dst_crs, always_xy=True)


def _utm_bbox_to_wgs84(utm_bbox: BBox, utm_epsg: int, samples_per_edge: int = 5) -> BBox:
    """Project corners and edge samples of a UTM bbox to an enclosing WGS84 bbox."""
    to_wgs84 = _get_transformer(f"EPSG:{utm_epsg}", "EPSG:4326")

    xs: list[float] = []
    ys: list[float] = []
    steps = max(2, samples_per_edge)
    for i in range(steps):
        frac = i / (steps - 1)
        x = utm_bbox.min_x + frac * utm_bbox.width
        y = utm_bbox.min_y + frac * utm_bbox.height
        # Bottom edge, top edge, left edge, right edge (includes all 4 corners & midpoints).
        xs.extend((x, x, utm_bbox.min_x, utm_bbox.max_x))
        ys.extend((utm_bbox.min_y, utm_bbox.max_y, y, y))

    lons, lats = to_wgs84.transform(xs, ys)
    west, east = float(min(lons)), float(max(lons))
    # Each supported paper footprint is local. A near-global interval here means
    # the projected edges wrapped across ±180°, which a single BBox cannot encode.
    if east - west > 180.0:
        raise ValueError(
            "The sheet or its data-fetch margin crosses the antimeridian. "
            "Move the center farther from 180° longitude or reduce the sheet extent."
        )
    return BBox(
        min_x=west,
        min_y=float(min(lats)),
        max_x=east,
        max_y=float(max(lats)),
    )


def compute_sheet_layout(
    spec: SheetSpec,
    margins: FrameMarginsMM | None = None,
) -> SheetLayout:
    """Compute exact physical page geometry, UTM projection, and ground footprint for a sheet."""
    resolved_margins = margins if margins is not None else FrameMarginsMM()

    portrait_w, portrait_h = PAPER_DIMENSIONS_MM[spec.paper]
    if spec.orientation == Orientation.LANDSCAPE:
        page_width_mm, page_height_mm = portrait_h, portrait_w
    else:
        page_width_mm, page_height_mm = portrait_w, portrait_h

    frame_x_mm = resolved_margins.left
    frame_y_mm = resolved_margins.top
    frame_width_mm = page_width_mm - resolved_margins.left - resolved_margins.right
    frame_height_mm = page_height_mm - resolved_margins.top - resolved_margins.bottom

    if frame_width_mm <= 0.0 or frame_height_mm <= 0.0:
        raise ValueError(
            f"Margins {resolved_margins} leave non-positive map frame "
            f"({frame_width_mm:.2f} x {frame_height_mm:.2f} mm) on "
            f"{spec.paper.value} {spec.orientation.value}."
        )

    utm_zone, utm_hemisphere, utm_epsg, gzd = resolve_utm_zone(
        spec.center_lat, spec.center_lon
    )

    to_utm = _get_transformer("EPSG:4326", f"EPSG:{utm_epsg}")
    center_easting, center_northing = to_utm.transform(spec.center_lon, spec.center_lat)
    center_easting = float(center_easting)
    center_northing = float(center_northing)

    # True-scale physical ground footprint in UTM meters.
    ground_w = frame_width_mm * spec.scale / 1000.0
    ground_h = frame_height_mm * spec.scale / 1000.0

    utm_bbox = BBox(
        min_x=center_easting - ground_w / 2.0,
        min_y=center_northing - ground_h / 2.0,
        max_x=center_easting + ground_w / 2.0,
        max_y=center_northing + ground_h / 2.0,
    )

    fetch_buf = max(1000.0, 0.15 * max(ground_w, ground_h))
    utm_fetch_bbox = BBox(
        min_x=utm_bbox.min_x - fetch_buf,
        min_y=utm_bbox.min_y - fetch_buf,
        max_x=utm_bbox.max_x + fetch_buf,
        max_y=utm_bbox.max_y + fetch_buf,
    )

    wgs84_bbox = _utm_bbox_to_wgs84(utm_bbox, utm_epsg)
    wgs84_fetch_bbox = _utm_bbox_to_wgs84(utm_fetch_bbox, utm_epsg)

    return SheetLayout(
        spec=spec,
        utm_epsg=utm_epsg,
        utm_zone=utm_zone,
        utm_hemisphere=utm_hemisphere,
        gzd=gzd,
        center_easting=center_easting,
        center_northing=center_northing,
        page_width_mm=page_width_mm,
        page_height_mm=page_height_mm,
        margins_mm=resolved_margins,
        frame_x_mm=frame_x_mm,
        frame_y_mm=frame_y_mm,
        frame_width_mm=frame_width_mm,
        frame_height_mm=frame_height_mm,
        utm_bbox=utm_bbox,
        utm_fetch_bbox=utm_fetch_bbox,
        wgs84_bbox=wgs84_bbox,
        wgs84_fetch_bbox=wgs84_fetch_bbox,
    )


def compute_atlas_layouts(
    anchor: SheetLayout, cols: int, rows: int
) -> list[tuple[str, SheetLayout]]:
    """Lay out native-UTM sheets in row-major order, checking they share the anchor CRS."""
    if cols == rows == 1:
        return [("A1", anchor)]

    to_wgs84 = _get_transformer(f"EPSG:{anchor.utm_epsg}", "EPSG:4326")
    sheets = []
    for row in range(rows):
        for col in range(cols):
            sheet_id = f"{chr(ord('A') + row)}{col + 1}"
            x = anchor.center_easting + (col - (cols - 1) / 2) * anchor.utm_bbox.width
            y = anchor.center_northing + ((rows - 1) / 2 - row) * anchor.utm_bbox.height
            lon, lat = to_wgs84.transform(x, y)
            spec = anchor.spec.model_copy(
                update={"center_lat": lat, "center_lon": lon, "center_mgrs": None}
            )
            page = compute_sheet_layout(spec, anchor.margins_mm)
            if page.utm_epsg != anchor.utm_epsg:
                raise ValueError(
                    f"Atlas sheet {sheet_id} uses EPSG:{page.utm_epsg}, while the atlas center "
                    f"uses EPSG:{anchor.utm_epsg}. Choose a smaller grid, move the center, "
                    "or render single sheets so each atlas uses one UTM zone and hemisphere."
                )
            sheets.append((sheet_id, page))
    return sheets
