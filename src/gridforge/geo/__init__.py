"""Geodesy, UTM sheet footprint, MGRS grid, declination, strip atlas, waypoints, and PDF supplements."""

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
from gridforge.geo.strip_atlas import (
    ElevationProfileData,
    StripAtlasPlan,
    StripSheetPlan,
    parse_route_bytes,
    parse_route_file,
    parse_route_geojson,
    plan_strip_atlas,
    render_elevation_profile_svg,
    sample_route_elevation,
)
from gridforge.geo.supplements import (
    merge_pdf_pages,
    render_calibration_pdf,
    render_elevation_profile_pdf,
    render_route_card_pdf,
)
from gridforge.geo.waypoints import (
    RouteLegMetric,
    RouteMetrics,
    WaypointMetric,
    compute_route_metrics,
    write_waypoints_gpkg,
)

__all__ = [
    "ElevationProfileData",
    "MAX_UTM_LAT",
    "MIN_UTM_LAT",
    "RouteLegMetric",
    "RouteMetrics",
    "StripAtlasPlan",
    "StripSheetPlan",
    "WaypointMetric",
    "compute_declination",
    "compute_route_metrics",
    "compute_sheet_layout",
    "generate_mgrs_grid",
    "merge_pdf_pages",
    "parse_center",
    "parse_route_bytes",
    "parse_route_file",
    "parse_route_geojson",
    "plan_strip_atlas",
    "render_calibration_pdf",
    "render_declination_svg",
    "render_elevation_profile_pdf",
    "render_elevation_profile_svg",
    "render_grid_ref_box_svg",
    "render_route_card_pdf",
    "resolve_utm_zone",
    "sample_route_elevation",
    "utm_zone_lon_bounds",
    "write_waypoints_gpkg",
]
