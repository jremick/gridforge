"""Geodesy, UTM sheet footprint, MGRS grid, and magnetic declination modules."""

from gridforge.geo.crs import (
    MAX_UTM_LAT,
    MIN_UTM_LAT,
    compute_sheet_layout,
    parse_center,
    resolve_utm_zone,
    utm_zone_lon_bounds,
)
from gridforge.geo.declination import (
    compute_declination,
    render_declination_svg,
    render_grid_ref_box_svg,
)
from gridforge.geo.mgrs_grid import generate_mgrs_grid

__all__ = [
    "MAX_UTM_LAT",
    "MIN_UTM_LAT",
    "compute_declination",
    "compute_sheet_layout",
    "generate_mgrs_grid",
    "parse_center",
    "render_declination_svg",
    "render_grid_ref_box_svg",
    "resolve_utm_zone",
    "utm_zone_lon_bounds",
]
