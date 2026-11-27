"""GPX / GeoJSON auto-strip-map planner, DEM elevation sampling, and elevation profile SVG."""

from __future__ import annotations

import json
import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Literal
from xml.sax.saxutils import escape

import mgrs
from osgeo import gdal, osr
from pydantic import BaseModel, Field
from pyproj import Transformer

from gridforge.config import (
    PAPER_DIMENSIONS_MM,
    VALID_SCALES,
    BBox,
    FrameMarginsMM,
    Orientation,
    PaperSize,
    Units,
)
from gridforge.geo.crs import (
    MAX_UTM_LAT,
    MIN_UTM_LAT,
    resolve_utm_zone,
    validate_utm_coords,
)

METERS_TO_FEET: float = 3.280839895013123
KM_TO_MILES: float = 0.621371192237334
_MGRS_SPLIT_RE = re.compile(r"^0?([1-9]|[1-5]\d|60)([C-HJ-NP-X])([A-HJ-NP-Z]{2})(\d*)$")


class StripSheetPlan(BaseModel):
    """Single true-scale UTM sheet frame within an auto-fitted route strip atlas."""

    sheet_id: str
    index: int
    center_lat: float
    center_lon: float
    center_mgrs_10m: str
    utm_bbox: BBox


class StripAtlasPlan(BaseModel):
    """Complete minimum-count strip atlas plan and MapLibre GeoJSON preview layers."""

    utm_epsg: int
    utm_zone: int
    utm_hemisphere: Literal["N", "S"]
    gzd: str
    scale: int
    paper: PaperSize
    orientation: Orientation
    route_distance_km: float
    sheet_count: int
    sheets: list[StripSheetPlan]
    route_geojson: dict[str, Any]
    footprint_geojson: dict[str, Any]
    grid_geojson: dict[str, Any]


class ElevationProfileData(BaseModel):
    """Sampled DEM elevation profile along a route polyline."""

    distances_km: list[float]
    elevations_m: list[float]
    total_distance_km: float
    total_ascent_m: float
    total_descent_m: float
    min_elev_m: float
    max_elev_m: float
    waypoint_ticks: list[tuple[float, str]] = Field(default_factory=list)


def _decode_mgrs(raw: str | bytes) -> str:
    return raw.decode("ascii") if isinstance(raw, bytes) else str(raw)


def _format_mgrs_10m(lat: float, lon: float) -> str:
    raw = _decode_mgrs(mgrs.MGRS().toMGRS(lat, lon, MGRSPrecision=4)).strip().upper()
    m = _MGRS_SPLIT_RE.match(raw)
    if not m:
        return raw
    gzd = f"{int(m.group(1))}{m.group(2)}"
    sq = m.group(3)
    digits = m.group(4)
    half = len(digits) // 2
    return f"{gzd} {sq} {digits[:half]} {digits[half:]}"


def _append_dedup(target: list[tuple[float, float]], lon: float, lat: float) -> None:
    validate_utm_coords(lat, lon)
    if target and abs(target[-1][0] - lon) < 1e-9 and abs(target[-1][1] - lat) < 1e-9:
        return
    target.append((float(lon), float(lat)))


_MAX_GEOMETRY_DEPTH = 8
_MAX_ROUTE_VERTICES = 50_000


def _extract_coords_from_geometry(
    geom: dict[str, Any],
    line_pts: list[tuple[float, float]],
    point_pts: list[tuple[float, float]],
    *,
    depth: int = 0,
) -> None:
    if depth > _MAX_GEOMETRY_DEPTH:
        raise ValueError(f"GeoJSON GeometryCollection nesting exceeds limit ({_MAX_GEOMETRY_DEPTH}).")
    if len(line_pts) >= _MAX_ROUTE_VERTICES and len(point_pts) >= _MAX_ROUTE_VERTICES:
        raise ValueError(f"Route exceeds maximum vertex count ({_MAX_ROUTE_VERTICES:,}).")
    gtype = geom.get("type")
    coords = geom.get("coordinates")
    if gtype == "LineString" and isinstance(coords, list):
        for c in coords:
            if len(line_pts) >= _MAX_ROUTE_VERTICES:
                raise ValueError(f"Route exceeds maximum vertex count ({_MAX_ROUTE_VERTICES:,}).")
            if isinstance(c, (list, tuple)) and len(c) >= 2:
                _append_dedup(line_pts, float(c[0]), float(c[1]))
    elif gtype == "MultiLineString" and isinstance(coords, list):
        for seg in coords:
            if isinstance(seg, list):
                for c in seg:
                    if len(line_pts) >= _MAX_ROUTE_VERTICES:
                        raise ValueError(f"Route exceeds maximum vertex count ({_MAX_ROUTE_VERTICES:,}).")
                    if isinstance(c, (list, tuple)) and len(c) >= 2:
                        _append_dedup(line_pts, float(c[0]), float(c[1]))
    elif gtype == "Polygon" and isinstance(coords, list) and coords:
        outer = coords[0]
        if isinstance(outer, list):
            for c in outer:
                if len(line_pts) >= _MAX_ROUTE_VERTICES:
                    raise ValueError(f"Route exceeds maximum vertex count ({_MAX_ROUTE_VERTICES:,}).")
                if isinstance(c, (list, tuple)) and len(c) >= 2:
                    _append_dedup(line_pts, float(c[0]), float(c[1]))
    elif gtype == "Point" and isinstance(coords, (list, tuple)) and len(coords) >= 2:
        if len(point_pts) >= _MAX_ROUTE_VERTICES:
            raise ValueError(f"Route exceeds maximum vertex count ({_MAX_ROUTE_VERTICES:,}).")
        _append_dedup(point_pts, float(coords[0]), float(coords[1]))
    elif gtype == "GeometryCollection":
        for sub in geom.get("geometries", []):
            if isinstance(sub, dict):
                _extract_coords_from_geometry(sub, line_pts, point_pts, depth=depth + 1)


def parse_route_geojson(data: dict[str, Any]) -> list[tuple[float, float]]:
    """Extract ordered ``(lon, lat)`` route vertices from a GeoJSON object."""
    if not isinstance(data, dict):
        raise ValueError("GeoJSON route payload must be a JSON object.")

    line_pts: list[tuple[float, float]] = []
    point_pts: list[tuple[float, float]] = []

    dtype = data.get("type")
    if dtype == "FeatureCollection":
        for feat in data.get("features", []):
            if isinstance(feat, dict) and isinstance(feat.get("geometry"), dict):
                _extract_coords_from_geometry(feat["geometry"], line_pts, point_pts)
    elif dtype == "Feature":
        geom = data.get("geometry")
        if isinstance(geom, dict):
            _extract_coords_from_geometry(geom, line_pts, point_pts)
    else:
        _extract_coords_from_geometry(data, line_pts, point_pts)

    pts = line_pts if len(line_pts) >= 2 else point_pts
    if len(pts) < 2:
        raise ValueError(
            "GeoJSON route must contain a LineString/MultiLineString or at least 2 Point vertices."
        )
    return pts


def _parse_gpx_bytes(raw_bytes: bytes) -> list[tuple[float, float]]:
    upper_head = raw_bytes[:4096].upper()
    if b"<!ENTITY" in upper_head or b"<!DOCTYPE" in upper_head:
        raise ValueError("GPX XML must not contain DTD or ENTITY declarations.")
    try:
        root = ET.fromstring(raw_bytes)
    except ET.ParseError as exc:
        raise ValueError(f"Invalid GPX XML file: {exc}") from exc

    def _collect_tag(local_name: str) -> list[tuple[float, float]]:
        out: list[tuple[float, float]] = []
        for el in root.iter():
            tag = el.tag.rsplit("}", 1)[-1].lower()
            if tag == local_name:
                if len(out) >= _MAX_ROUTE_VERTICES:
                    raise ValueError(f"Route exceeds maximum vertex count ({_MAX_ROUTE_VERTICES:,}).")
                lat_s = el.attrib.get("lat")
                lon_s = el.attrib.get("lon")
                if lat_s is not None and lon_s is not None:
                    _append_dedup(out, float(lon_s), float(lat_s))
        return out

    for tag_name in ("trkpt", "rtept", "wpt"):
        pts = _collect_tag(tag_name)
        if len(pts) >= 2:
            return pts

    raise ValueError("GPX file must contain at least 2 track (<trkpt>), route (<rtept>), or waypoint (<wpt>) points.")


def parse_route_bytes(raw_bytes: bytes, filename: str = "route.geojson") -> list[tuple[float, float]]:
    """Parse ordered ``(lon, lat)`` route vertices from raw `.gpx`, `.geojson`, or `.json` bytes."""
    ext = Path(filename).suffix.lower()
    if ext == ".gpx" or raw_bytes.lstrip().startswith(b"<"):
        return _parse_gpx_bytes(raw_bytes)
    try:
        data = json.loads(raw_bytes.decode("utf-8"))
    except Exception as exc:
        raise ValueError(f"Failed to decode GeoJSON route '{filename}': {exc}") from exc
    return parse_route_geojson(data)


def parse_route_file(path: Path | str) -> list[tuple[float, float]]:
    """Parse ordered ``(lon, lat)`` route vertices from a `.gpx`, `.geojson`, or `.json` file."""
    resolved = Path(path)
    if not resolved.is_file():
        raise FileNotFoundError(f"Route file not found: '{resolved}'")
    return parse_route_bytes(resolved.read_bytes(), resolved.name)


def _densify_utm_polyline(
    utm_pts: list[tuple[float, float]],
    max_step_m: float,
) -> list[tuple[float, float]]:
    """Insert intermediate vertices along long straight segments so strip fitting stays exact."""
    if len(utm_pts) < 2:
        return list(utm_pts)
    step = max(25.0, float(max_step_m))
    out: list[tuple[float, float]] = [utm_pts[0]]
    for (x0, y0), (x1, y1) in zip(utm_pts, utm_pts[1:]):
        dist = math.hypot(x1 - x0, y1 - y0)
        if dist <= 1e-6:
            continue
        n_segs = max(1, int(math.ceil(dist / step)))
        for k in range(1, n_segs + 1):
            frac = k / n_segs
            out.append((x0 + frac * (x1 - x0), y0 + frac * (y1 - y0)))
    return out


def _utm_bbox_to_wgs84_ring(
    bbox: BBox, to_wgs84: Transformer, edge_steps: int = 8
) -> list[list[float]]:
    xs: list[float] = []
    ys: list[float] = []
    for i in range(edge_steps):
        xs.append(bbox.min_x + bbox.width * i / edge_steps)
        ys.append(bbox.min_y)
    for i in range(edge_steps):
        xs.append(bbox.max_x)
        ys.append(bbox.min_y + bbox.height * i / edge_steps)
    for i in range(edge_steps):
        xs.append(bbox.max_x - bbox.width * i / edge_steps)
        ys.append(bbox.max_y)
    for i in range(edge_steps):
        xs.append(bbox.min_x)
        ys.append(bbox.max_y - bbox.height * i / edge_steps)

    lons, lats = to_wgs84.transform(xs, ys)
    ring = [[round(float(lo), 7), round(float(la), 7)] for lo, la in zip(lons, lats)]
    ring.append(ring[0])
    return ring


def plan_strip_atlas(
    route_lonlat: list[tuple[float, float]],
    *,
    scale: int = 25000,
    paper: PaperSize = PaperSize.A4,
    orientation: Orientation = Orientation.PORTRAIT,
    units: Units = Units.METRIC,
    overlap_pct: float = 10.0,
    max_sheets: int = 12,
    margins: FrameMarginsMM | None = None,
) -> StripAtlasPlan:
    """Plan minimum-count true-scale UTM strip sheets covering 100% of ``route_lonlat``."""
    _ = units  # Reserved for consistent signature across callers
    if scale not in VALID_SCALES:
        allowed = ", ".join(f"1:{s:,}" for s in VALID_SCALES)
        raise ValueError(f"Unsupported scale {scale}. Allowed: {allowed}.")
    if len(route_lonlat) < 2:
        raise ValueError("Route must contain at least 2 (lon, lat) vertices.")
    if not (0.0 <= overlap_pct <= 40.0):
        raise ValueError(f"overlap_pct must be within [0.0, 40.0], got {overlap_pct}.")
    if max_sheets < 1:
        raise ValueError(f"max_sheets must be >= 1, got {max_sheets}.")

    for lon, lat in route_lonlat:
        validate_utm_coords(lat, lon)

    # Resolve ONE shared anchor UTM zone at the geographic midpoint of the route bbox.
    lons_in = [p[0] for p in route_lonlat]
    lats_in = [p[1] for p in route_lonlat]
    mid_lon = (min(lons_in) + max(lons_in)) / 2.0
    mid_lat = (min(lats_in) + max(lats_in)) / 2.0
    utm_zone, utm_hemi, utm_epsg, gzd = resolve_utm_zone(mid_lat, mid_lon)

    to_utm = Transformer.from_crs("EPSG:4326", f"EPSG:{utm_epsg}", always_xy=True)
    to_wgs84 = Transformer.from_crs(f"EPSG:{utm_epsg}", "EPSG:4326", always_xy=True)

    raw_xs, raw_ys = to_utm.transform(lons_in, lats_in)
    utm_raw = [(float(x), float(y)) for x, y in zip(raw_xs, raw_ys)]

    # Exact projected route length in km
    total_m = sum(
        math.hypot(x1 - x0, y1 - y0) for (x0, y0), (x1, y1) in zip(utm_raw, utm_raw[1:])
    )
    route_distance_km = round(total_m / 1000.0, 3)

    # True-scale frame ground footprint in meters
    resolved_margins = margins if margins is not None else FrameMarginsMM()
    portrait_w, portrait_h = PAPER_DIMENSIONS_MM[paper]
    page_w, page_h = (
        (portrait_h, portrait_w)
        if orientation == Orientation.LANDSCAPE
        else (portrait_w, portrait_h)
    )
    frame_w_mm = page_w - resolved_margins.left - resolved_margins.right
    frame_h_mm = page_h - resolved_margins.top - resolved_margins.bottom
    ground_w = frame_w_mm * scale / 1000.0
    ground_h = frame_h_mm * scale / 1000.0

    # Safe usable interior leaving >= overlap_pct/2 inset from every neatline edge
    usable_factor = max(0.2, 1.0 - (overlap_pct / 100.0))
    safe_w = ground_w * usable_factor
    safe_h = ground_h * usable_factor

    # Fail fast BEFORE densification if route length cannot possibly fit within max_sheets
    # (prevents OOM DoS from short zigzag routes spanning huge distances at 1:10,000).
    max_feasible_m = max_sheets * math.hypot(safe_w, safe_h) * 3.0
    if len(utm_raw) > _MAX_ROUTE_VERTICES:
        raise ValueError(f"Route exceeds maximum vertex count ({_MAX_ROUTE_VERTICES:,}).")
    if total_m > max_feasible_m:
        raise ValueError(
            f"Route ({route_distance_km:.1f} km) requires more than {max_sheets} sheets "
            f"at 1:{scale:,} ({paper.value.upper()} {orientation.value}). "
            "Choose a smaller scale (e.g. 1:50,000), larger paper (A3), or landscape orientation."
        )

    # Densify long straight legs so greedy walk guarantees smooth transitions & overlap
    dense_step = min(safe_w, safe_h) * 0.05
    dense_utm = _densify_utm_polyline(utm_raw, dense_step)

    # Greedy sequential strip cover start -> finish
    sheet_bboxes: list[BBox] = []
    idx = 0
    n_pts = len(dense_utm)
    while idx < n_pts:
        min_x = max_x = dense_utm[idx][0]
        min_y = max_y = dense_utm[idx][1]
        j = idx
        while j + 1 < n_pts:
            nx, ny = dense_utm[j + 1]
            cand_min_x = min(min_x, nx)
            cand_max_x = max(max_x, nx)
            cand_min_y = min(min_y, ny)
            cand_max_y = max(max_y, ny)
            if (cand_max_x - cand_min_x) > safe_w or (cand_max_y - cand_min_y) > safe_h:
                break
            min_x, max_x, min_y, max_y = cand_min_x, cand_max_x, cand_min_y, cand_max_y
            j += 1

        cx = (min_x + max_x) / 2.0
        cy = (min_y + max_y) / 2.0
        sheet_bboxes.append(
            BBox(
                min_x=cx - ground_w / 2.0,
                min_y=cy - ground_h / 2.0,
                max_x=cx + ground_w / 2.0,
                max_y=cy + ground_h / 2.0,
            )
        )
        if len(sheet_bboxes) > max_sheets:
            raise ValueError(
                f"Route ({route_distance_km:.1f} km) requires more than {max_sheets} sheets "
                f"at 1:{scale:,} ({paper.value.upper()} {orientation.value}). "
                "Choose a smaller scale (e.g. 1:50,000), larger paper (A3), or landscape orientation."
            )

        if j + 1 >= n_pts:
            break
        # Start next sheet at transition vertex j so consecutive sheets overlap around the trail crossing
        idx = j if j > idx else j + 1

    sheets: list[StripSheetPlan] = []
    footprint_features: list[dict[str, Any]] = []
    grid_features: list[dict[str, Any]] = []
    line_samples = 6

    for sheet_num, sbbox in enumerate(sheet_bboxes, start=1):
        sheet_id = f"S{sheet_num}"
        scx = (sbbox.min_x + sbbox.max_x) / 2.0
        scy = (sbbox.min_y + sbbox.max_y) / 2.0
        slon, slat = to_wgs84.transform(scx, scy)
        slon_f, slat_f = float(slon), float(slat)
        validate_utm_coords(slat_f, slon_f)

        sheets.append(
            StripSheetPlan(
                sheet_id=sheet_id,
                index=sheet_num,
                center_lat=round(slat_f, 6),
                center_lon=round(slon_f, 6),
                center_mgrs_10m=_format_mgrs_10m(slat_f, slon_f),
                utm_bbox=sbbox,
            )
        )

        ring = _utm_bbox_to_wgs84_ring(sbbox, to_wgs84)
        footprint_features.append(
            {
                "type": "Feature",
                "properties": {
                    "sheet_id": sheet_id,
                    "label": sheet_id,
                    "index": sheet_num,
                    "is_primary": (sheet_num == 1),
                    "scale": scale,
                    "paper": paper.value,
                    "orientation": orientation.value,
                    "utm_epsg": utm_epsg,
                    "gzd": gzd,
                },
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [ring],
                },
            }
        )

        # Interior 1 km MGRS preview grid lines for this strip sheet
        e_start = int(math.ceil(sbbox.min_x / 1000.0)) * 1000
        e_end = int(math.floor(sbbox.max_x / 1000.0)) * 1000
        for e in range(e_start, e_end + 1, 1000):
            xs = [float(e)] * line_samples
            ys = [sbbox.min_y + sbbox.height * k / (line_samples - 1) for k in range(line_samples)]
            glons, glats = to_wgs84.transform(xs, ys)
            grid_features.append(
                {
                    "type": "Feature",
                    "properties": {
                        "sheet_id": sheet_id,
                        "axis": "E",
                        "coord_m": e,
                        "label": f"{e}m E",
                        "principal_digits": f"{(e // 1000) % 100:02d}",
                        "is_100km": (e % 100000 == 0),
                    },
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [
                            [round(float(lo), 7), round(float(la), 7)]
                            for lo, la in zip(glons, glats)
                        ],
                    },
                }
            )

        n_start = int(math.ceil(sbbox.min_y / 1000.0)) * 1000
        n_end = int(math.floor(sbbox.max_y / 1000.0)) * 1000
        for n in range(n_start, n_end + 1, 1000):
            xs = [sbbox.min_x + sbbox.width * k / (line_samples - 1) for k in range(line_samples)]
            ys = [float(n)] * line_samples
            glons, glats = to_wgs84.transform(xs, ys)
            eff_n = n if n >= 0 else 10_000_000 + n
            grid_features.append(
                {
                    "type": "Feature",
                    "properties": {
                        "sheet_id": sheet_id,
                        "axis": "N",
                        "coord_m": n,
                        "label": f"{n}m N",
                        "principal_digits": f"{(eff_n // 1000) % 100:02d}",
                        "is_100km": (n % 100000 == 0),
                    },
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [
                            [round(float(lo), 7), round(float(la), 7)]
                            for lo, la in zip(glons, glats)
                        ],
                    },
                }
            )

    route_geojson: dict[str, Any] = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "route_distance_km": route_distance_km,
                    "sheet_count": len(sheets),
                },
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[round(lo, 7), round(la, 7)] for lo, la in route_lonlat],
                },
            }
        ],
    }

    return StripAtlasPlan(
        utm_epsg=utm_epsg,
        utm_zone=utm_zone,
        utm_hemisphere=utm_hemi,
        gzd=gzd,
        scale=scale,
        paper=paper,
        orientation=orientation,
        route_distance_km=route_distance_km,
        sheet_count=len(sheets),
        sheets=sheets,
        route_geojson=route_geojson,
        footprint_geojson={"type": "FeatureCollection", "features": footprint_features},
        grid_geojson={"type": "FeatureCollection", "features": grid_features},
    )


def _sample_dem_point_bilinear(
    ds: gdal.Dataset,
    band: gdal.Band,
    inv_gt: tuple[float, ...],
    nodata: float | None,
    x: float,
    y: float,
) -> float | None:
    """Bilinearly sample elevation at raster CRS coordinate (x, y)."""
    px = inv_gt[0] + inv_gt[1] * x + inv_gt[2] * y - 0.5
    py = inv_gt[3] + inv_gt[4] * x + inv_gt[5] * y - 0.5

    w = ds.RasterXSize
    h = ds.RasterYSize
    if px < -0.5 or py < -0.5 or px > w - 0.5 or py > h - 0.5:
        return None

    x0 = max(0, min(w - 1, int(math.floor(px))))
    y0 = max(0, min(h - 1, int(math.floor(py))))
    x1 = min(w - 1, x0 + 1)
    y1 = min(h - 1, y0 + 1)

    read_w = x1 - x0 + 1
    read_h = y1 - y0 + 1
    buf = band.ReadAsArray(x0, y0, read_w, read_h)
    if buf is None:
        return None

    def _valid(val: float) -> bool:
        if not math.isfinite(val):
            return False
        if nodata is not None and math.isclose(val, nodata, rel_tol=1e-5, abs_tol=1e-4):
            return False
        return True

    v00 = float(buf[0, 0])
    if read_w == 1 and read_h == 1:
        return v00 if _valid(v00) else None

    v10 = float(buf[0, read_w - 1])
    v01 = float(buf[read_h - 1, 0])
    v11 = float(buf[read_h - 1, read_w - 1])

    fx = max(0.0, min(1.0, px - x0))
    fy = max(0.0, min(1.0, py - y0))

    vals = [
        (v00, (1.0 - fx) * (1.0 - fy)),
        (v10, fx * (1.0 - fy)),
        (v01, (1.0 - fx) * fy),
        (v11, fx * fy),
    ]
    weighted_sum = 0.0
    weight_total = 0.0
    for val, wt in vals:
        if _valid(val):
            weighted_sum += val * wt
            weight_total += wt

    if weight_total <= 0.0:
        return None
    return weighted_sum / weight_total


def sample_route_elevation(
    route_lonlat: list[tuple[float, float]],
    dem_paths: list[Path],
    utm_epsg: int,
    *,
    sample_interval_m: float = 60.0,
    waypoint_ticks: list[tuple[float, str]] | None = None,
) -> ElevationProfileData:
    """Sample DEM elevations along a densified route polyline using GDAL."""
    if len(route_lonlat) < 2:
        raise ValueError("Route must contain at least 2 vertices for elevation profiling.")

    valid_dems = [Path(p).resolve() for p in dem_paths if Path(p).is_file()]
    if not valid_dems:
        raise FileNotFoundError("No valid DEM GeoTIFF files provided for elevation sampling.")

    to_utm = Transformer.from_crs("EPSG:4326", f"EPSG:{utm_epsg}", always_xy=True)
    lons = [p[0] for p in route_lonlat]
    lats = [p[1] for p in route_lonlat]
    xs, ys = to_utm.transform(lons, lats)
    utm_raw = [(float(x), float(y)) for x, y in zip(xs, ys)]

    total_m = sum(
        math.hypot(x1 - x0, y1 - y0) for (x0, y0), (x1, y1) in zip(utm_raw, utm_raw[1:])
    )
    # Choose step keeping sample count in a crisp [20..500] range
    eff_step = max(25.0, sample_interval_m)
    if total_m > 0:
        eff_step = max(eff_step, total_m / 500.0)
        eff_step = min(eff_step, max(10.0, total_m / 25.0))

    sampled_utm = _densify_utm_polyline(utm_raw, eff_step)

    gdal.UseExceptions()
    osr.UseExceptions()

    if len(valid_dems) == 1:
        ds = gdal.Open(str(valid_dems[0]))
    else:
        ds = gdal.BuildVRT("", [str(p) for p in valid_dems])

    if ds is None:
        raise RuntimeError(f"Failed to open DEM dataset(s): {valid_dems}")

    try:
        band = ds.GetRasterBand(1)
        nodata = band.GetNoDataValue()
        gt = ds.GetGeoTransform()
        inv_gt = gdal.InvGeoTransform(gt)
        dem_wkt = ds.GetProjection()

        utm_srs = osr.SpatialReference()
        utm_srs.ImportFromEPSG(utm_epsg)
        utm_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

        ct = None
        if dem_wkt:
            dem_srs = osr.SpatialReference(wkt=dem_wkt)
            dem_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            if not utm_srs.IsSame(dem_srs):
                ct = osr.CoordinateTransformation(utm_srs, dem_srs)

        distances_km: list[float] = []
        raw_elevs: list[float | None] = []
        cum_m = 0.0
        for idx, (ux, uy) in enumerate(sampled_utm):
            if idx > 0:
                px, py = sampled_utm[idx - 1]
                cum_m += math.hypot(ux - px, uy - py)
            distances_km.append(round(cum_m / 1000.0, 4))

            if ct is not None:
                rx, ry, _ = ct.TransformPoint(ux, uy)
            else:
                rx, ry = ux, uy
            raw_elevs.append(_sample_dem_point_bilinear(ds, band, inv_gt, nodata, rx, ry))
    finally:
        ds = None

    # Fill any sporadic nodata gaps cleanly via linear interpolation from valid neighbors
    valid_indices = [i for i, e in enumerate(raw_elevs) if e is not None]
    if not valid_indices:
        elevations_m = [0.0] * len(raw_elevs)
    else:
        elevations_m = [0.0] * len(raw_elevs)
        for i in range(len(raw_elevs)):
            val = raw_elevs[i]
            if val is not None:
                elevations_m[i] = round(val, 2)
                continue
            prev_i = max((vi for vi in valid_indices if vi < i), default=None)
            next_i = min((vi for vi in valid_indices if vi > i), default=None)
            if prev_i is not None and next_i is not None:
                frac = (i - prev_i) / (next_i - prev_i)
                e0 = raw_elevs[prev_i]
                e1 = raw_elevs[next_i]
                assert e0 is not None and e1 is not None
                elevations_m[i] = round(e0 + frac * (e1 - e0), 2)
            elif prev_i is not None:
                e0 = raw_elevs[prev_i]
                assert e0 is not None
                elevations_m[i] = round(e0, 2)
            else:
                assert next_i is not None
                e1 = raw_elevs[next_i]
                assert e1 is not None
                elevations_m[i] = round(e1, 2)

    ascent = 0.0
    descent = 0.0
    for e0, e1 in zip(elevations_m, elevations_m[1:]):
        diff = e1 - e0
        if diff > 0.0:
            ascent += diff
        elif diff < 0.0:
            descent += -diff

    return ElevationProfileData(
        distances_km=distances_km,
        elevations_m=elevations_m,
        total_distance_km=round(distances_km[-1], 3) if distances_km else 0.0,
        total_ascent_m=round(ascent, 1),
        total_descent_m=round(descent, 1),
        min_elev_m=round(min(elevations_m), 1) if elevations_m else 0.0,
        max_elev_m=round(max(elevations_m), 1) if elevations_m else 0.0,
        waypoint_ticks=list(waypoint_ticks or []),
    )


def render_elevation_profile_svg(
    profile: ElevationProfileData,
    units: Units,
    output_svg: Path,
    *,
    bw: bool = False,
) -> Path:
    """Render a crisp, QtSvg-safe route elevation profile chart SVG."""
    output_svg.parent.mkdir(parents=True, exist_ok=True)

    is_imp = units == Units.IMPERIAL
    dist_factor = KM_TO_MILES if is_imp else 1.0
    elev_factor = METERS_TO_FEET if is_imp else 1.0
    dist_unit = "mi" if is_imp else "km"
    elev_unit = "ft" if is_imp else "m"

    dists = [d * dist_factor for d in profile.distances_km]
    elevs = [e * elev_factor for e in profile.elevations_m]
    max_d = max(dists[-1] if dists else 1.0, 0.01)

    min_e = min(elevs) if elevs else 0.0
    max_e = max(elevs) if elevs else 100.0
    e_span = max(max_e - min_e, 10.0)
    y_min = math.floor((min_e - e_span * 0.12) / 10.0) * 10.0
    y_max = math.ceil((max_e + e_span * 0.15) / 10.0) * 10.0
    if y_max <= y_min:
        y_max = y_min + 20.0

    # Palette (crisp Color & dedicated high-contrast B&W)
    ink = "#000000" if bw else "#1A1F24"
    muted = "#333333" if bw else "#5A6570"
    grid_col = "#BDBDBD" if bw else "#D5DCE2"
    fill_col = "#E3E3E3" if bw else "#CFE3C8"
    line_col = "#000000" if bw else "#245A34"
    accent_col = "#000000" if bw else "#B83B28"
    font = "Liberation Sans, DejaVu Sans, sans-serif"

    w, h = 640.0, 224.0
    plot_x0, plot_x1 = 56.0, 622.0
    plot_y0, plot_y1 = 36.0, 186.0
    plot_w = plot_x1 - plot_x0
    plot_h = plot_y1 - plot_y0

    def _sx(dist_val: float) -> float:
        return plot_x0 + (dist_val / max_d) * plot_w

    def _sy(elev_val: float) -> float:
        return plot_y1 - ((elev_val - y_min) / (y_max - y_min)) * plot_h

    header_stats = (
        f"DIST: {profile.total_distance_km * dist_factor:.2f} {dist_unit}   |   "
        f"GAIN: +{round(profile.total_ascent_m * elev_factor):,} {elev_unit}   |   "
        f"LOSS: -{round(profile.total_descent_m * elev_factor):,} {elev_unit}   |   "
        f"RANGE: {round(profile.min_elev_m * elev_factor):,}–"
        f"{round(profile.max_elev_m * elev_factor):,} {elev_unit}"
    )

    lines: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{int(w)}" height="{int(h)}" viewBox="0 0 {int(w)} {int(h)}">',
        f'  <rect x="0.75" y="0.75" width="{w - 1.5:.2f}" height="{h - 1.5:.2f}" '
        f'fill="#FFFFFF" stroke="{ink}" stroke-width="1.0"/>',
        f'  <rect x="1.0" y="1.0" width="{w - 2.0:.2f}" height="24.0" fill="{ink}"/>',
        f'  <text x="10.0" y="16.5" font-family="{font}" font-size="9.2" '
        f'font-weight="bold" fill="#FFFFFF">ELEVATION PROFILE</text>',
        f'  <text x="{w - 10.0:.1f}" y="16.2" font-family="{font}" font-size="8.0" '
        f'text-anchor="end" fill="#FFFFFF">{escape(header_stats)}</text>',
    ]

    # Horizontal elevation gridlines (5 bands)
    n_y = 4
    for i in range(n_y + 1):
        val = y_min + (y_max - y_min) * i / n_y
        yy = _sy(val)
        lines.append(
            f'  <line x1="{plot_x0:.1f}" y1="{yy:.2f}" x2="{plot_x1:.1f}" y2="{yy:.2f}" '
            f'stroke="{grid_col}" stroke-width="0.7"/>'
        )
        lines.append(
            f'  <text x="{plot_x0 - 6.0:.1f}" y="{yy + 3.0:.2f}" font-family="{font}" '
            f'font-size="8.0" text-anchor="end" fill="{muted}">{round(val):,}</text>'
        )

    # Vertical distance gridlines (6 ticks)
    n_x = 5
    for i in range(n_x + 1):
        dv = max_d * i / n_x
        xx = _sx(dv)
        lines.append(
            f'  <line x1="{xx:.2f}" y1="{plot_y0:.1f}" x2="{xx:.2f}" y2="{plot_y1:.1f}" '
            f'stroke="{grid_col}" stroke-width="0.6" stroke-dasharray="2,2"/>'
        )
        lines.append(
            f'  <text x="{xx:.2f}" y="{plot_y1 + 14.0:.1f}" font-family="{font}" '
            f'font-size="8.0" text-anchor="middle" fill="{muted}">{dv:.1f} {dist_unit}</text>'
        )

    # Profile filled area + crisp stroke
    if dists and elevs:
        pts_str = [f"{_sx(d):.2f},{_sy(e):.2f}" for d, e in zip(dists, elevs)]
        poly_pts = (
            f"{_sx(dists[0]):.2f},{plot_y1:.2f} "
            + " ".join(pts_str)
            + f" {_sx(dists[-1]):.2f},{plot_y1:.2f}"
        )
        path_d = "M " + " L ".join(p.replace(",", " ") for p in pts_str)
        lines.append(f'  <polygon points="{poly_pts}" fill="{fill_col}" fill-opacity="0.72"/>')
        lines.append(
            f'  <path d="{path_d}" fill="none" stroke="{line_col}" stroke-width="2.0" '
            f'stroke-linejoin="round" stroke-linecap="round"/>'
        )

        # Highlight highest & lowest points on route
        hi_idx = max(range(len(elevs)), key=lambda i: elevs[i])
        lo_idx = min(range(len(elevs)), key=lambda i: elevs[i])
        for idx_pt, tag_prefix, dy_lbl in (
            (hi_idx, "MAX", -7.0),
            (lo_idx, "MIN", 11.0),
        ):
            px = _sx(dists[idx_pt])
            py = _sy(elevs[idx_pt])
            lbl = f"{tag_prefix} {round(elevs[idx_pt]):,} {elev_unit}"
            anchor = "end" if px > plot_x1 - 75.0 else ("start" if px < plot_x0 + 75.0 else "middle")
            lines.append(
                f'  <circle cx="{px:.2f}" cy="{py:.2f}" r="3.0" fill="{accent_col}" '
                f'stroke="#FFFFFF" stroke-width="1.0"/>'
            )
            lines.append(
                f'  <text x="{px:.2f}" y="{max(plot_y0 + 9.0, min(plot_y1 - 3.0, py + dy_lbl)):.2f}" '
                f'font-family="{font}" font-size="7.5" font-weight="bold" '
                f'text-anchor="{anchor}" fill="{accent_col}">{escape(lbl)}</text>'
            )

    # Optional waypoint distance ticks
    for wp_dist_km, wp_lbl in profile.waypoint_ticks:
        wx = _sx(wp_dist_km * dist_factor)
        if plot_x0 <= wx <= plot_x1:
            lines.append(
                f'  <line x1="{wx:.2f}" y1="{plot_y0:.1f}" x2="{wx:.2f}" y2="{plot_y1:.1f}" '
                f'stroke="{accent_col}" stroke-width="0.9" stroke-dasharray="3,2"/>'
            )
            lines.append(
                f'  <text x="{wx:.2f}" y="{plot_y0 - 4.0:.1f}" font-family="{font}" '
                f'font-size="6.8" font-weight="bold" text-anchor="middle" '
                f'fill="{accent_col}">{escape(wp_lbl[:16])}</text>'
            )

    # Crisp plot frame + axis titles
    lines.extend(
        [
            f'  <rect x="{plot_x0:.1f}" y="{plot_y0:.1f}" width="{plot_w:.1f}" height="{plot_h:.1f}" '
            f'fill="none" stroke="{ink}" stroke-width="1.0"/>',
            f'  <text x="14.0" y="{(plot_y0 + plot_y1) / 2.0:.1f}" font-family="{font}" '
            f'font-size="8.0" font-weight="bold" text-anchor="middle" fill="{ink}" '
            f'transform="rotate(-90 14.0 {(plot_y0 + plot_y1) / 2.0:.1f})">ELEVATION ({elev_unit})</text>',
            f'  <text x="{(plot_x0 + plot_x1) / 2.0:.1f}" y="{h - 8.0:.1f}" font-family="{font}" '
            f'font-size="8.0" font-weight="bold" text-anchor="middle" fill="{ink}">ROUTE DISTANCE ({dist_unit})</text>',
            "</svg>",
        ]
    )

    output_svg.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return output_svg
