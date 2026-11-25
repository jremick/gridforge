"""MGRS 1 km / 100 km grid generation, neatline collar ticks, and GeoPackage export."""

from __future__ import annotations

import math
import re
from collections import defaultdict
from pathlib import Path

import mgrs
from osgeo import ogr, osr
from pyproj import Transformer
from shapely.geometry import LineString, MultiLineString, Point, box

from gridforge.config import GridTick, MGRSGridMeta, SheetLayout
from gridforge.geo.crs import utm_zone_lon_bounds

_MGRS_SPLIT_RE = re.compile(r"^0?([1-9]|[1-5]\d|60)([C-HJ-NP-X])([A-HJ-NP-Z]{2})(\d*)$")


def _decode_mgrs(raw: str | bytes) -> str:
    return raw.decode("ascii") if isinstance(raw, bytes) else str(raw)


def _split_mgrs(mgrs_str: str) -> tuple[str, str, str]:
    """Split a compact MGRS string into (gzd, square_id_100km, digits)."""
    cleaned = mgrs_str.strip().upper()
    match = _MGRS_SPLIT_RE.match(cleaned)
    if not match:
        raise ValueError(f"Unexpected MGRS string format: '{mgrs_str}'")
    zone_num = int(match.group(1))
    band = match.group(2)
    square_id = match.group(3)
    digits = match.group(4)
    return (f"{zone_num}{band}", square_id, digits)


def _format_center_mgrs_10m(lat: float, lon: float) -> str:
    """Format sheet center coordinate as 10 m precision MGRS, e.g. '56H KH 5100 6400'."""
    converter = mgrs.MGRS()
    raw = _decode_mgrs(converter.toMGRS(lat, lon, MGRSPrecision=4))
    gzd, square_id, digits = _split_mgrs(raw)
    half = len(digits) // 2
    return f"{gzd} {square_id} {digits[:half]} {digits[half:]}"


def _format_tick_digits(coord_m: int) -> tuple[str, str]:
    """Return (principal_digits, prefix_digits) for a 1 km grid coordinate."""
    # Guard negative UTM northings if a northern-hemisphere sheet straddles the equator.
    effective = coord_m if coord_m >= 0 else 10_000_000 + coord_m
    principal = f"{(effective // 1000) % 100:02d}"
    prefix = f"{effective // 100000}"
    return (principal, prefix)


def _compute_zone_boundaries(
    layout: SheetLayout,
    to_utm: Transformer,
) -> list[tuple[LineString, str]]:
    """Compute projected UTM zone boundary meridian polylines clipped to layout.utm_bbox."""
    band = layout.gzd[-1]
    west_lon, east_lon = utm_zone_lon_bounds(layout.utm_zone, band)
    frame_poly = box(*layout.utm_bbox.as_tuple())

    lat_min = max(-80.0, layout.wgs84_bbox.min_y - 0.1)
    lat_max = min(84.0, layout.wgs84_bbox.max_y + 0.1)
    num_samples = 48

    boundaries: list[tuple[LineString, str]] = []
    for bnd_lon in (west_lon, east_lon):
        if bnd_lon <= -180.0 or bnd_lon >= 180.0:
            continue
        if not (layout.wgs84_bbox.min_x - 0.05 <= bnd_lon <= layout.wgs84_bbox.max_x + 0.05):
            continue

        lats = [
            lat_min + (lat_max - lat_min) * i / (num_samples - 1)
            for i in range(num_samples)
        ]
        lons = [bnd_lon] * num_samples
        xs, ys = to_utm.transform(lons, lats)
        line = LineString(zip(xs, ys))
        clipped = line.intersection(frame_poly)
        if clipped.is_empty:
            continue

        hemi_lbl = f"{abs(bnd_lon):g}°{'E' if bnd_lon >= 0 else 'W'}"
        label = f"UTM ZONE BOUNDARY ({hemi_lbl})"

        if isinstance(clipped, LineString) and clipped.length > 0.0:
            boundaries.append((clipped, label))
        elif isinstance(clipped, MultiLineString):
            for geom in clipped.geoms:
                if geom.length > 0.0:
                    boundaries.append((geom, label))

    return boundaries


def _discover_100km_squares(
    layout: SheetLayout,
    eastings_100km: list[int],
    northings_100km: list[int],
    to_wgs84: Transformer,
) -> tuple[list[str], list[tuple[Point, str, str, str]]]:
    """Identify all 100 km MGRS square regions inside utm_bbox and compute clean label points.

    Returns:
        (square_ids_100km, square_label_features) where each feature is
        (Point(x, y), gzd, square_id, full_id).
    """
    bbox = layout.utm_bbox
    x_splits = sorted(e for e in eastings_100km if bbox.min_x < e < bbox.max_x)
    y_splits = sorted(n for n in northings_100km if bbox.min_y < n < bbox.max_y)
    x_breaks = [bbox.min_x, *x_splits, bbox.max_x]
    y_breaks = [bbox.min_y, *y_splits, bbox.max_y]

    converter = mgrs.MGRS()
    region_points: dict[tuple[str, str], list[tuple[float, float]]] = defaultdict(list)

    # Sample a 3x3 interior grid inside every 100 km sub-rectangle so we reliably catch
    # both 100 km square crossings and any UTM zone / latitude band crossings.
    fracs = (0.15, 0.5, 0.85)
    for ix in range(len(x_breaks) - 1):
        x0, x1 = x_breaks[ix], x_breaks[ix + 1]
        for iy in range(len(y_breaks) - 1):
            y0, y1 = y_breaks[iy], y_breaks[iy + 1]
            for fx in fracs:
                for fy in fracs:
                    px = x0 + fx * (x1 - x0)
                    py = y0 + fy * (y1 - y0)
                    lon, lat = to_wgs84.transform(px, py)
                    if not (-80.0 <= lat <= 84.0):
                        continue
                    raw = _decode_mgrs(converter.toMGRS(lat, lon, MGRSPrecision=0))
                    pt_gzd, pt_sq, _ = _split_mgrs(raw)
                    region_points[(pt_gzd, pt_sq)].append((px, py))

    # Guarantee sheet center square is always present even on pathological edge cases.
    center_raw = _decode_mgrs(
        converter.toMGRS(layout.spec.center_lat, layout.spec.center_lon, MGRSPrecision=0)
    )
    center_gzd, center_sq, _ = _split_mgrs(center_raw)
    if (center_gzd, center_sq) not in region_points:
        region_points[(center_gzd, center_sq)].append(
            (layout.center_easting, layout.center_northing)
        )

    # Sort deterministically: primary sheet GZD first, then west-to-east, north-to-south.
    def _sort_key(item: tuple[tuple[str, str], list[tuple[float, float]]]) -> tuple[int, float, float, str, str]:
        (gzd, sq), pts = item
        avg_x = sum(p[0] for p in pts) / len(pts)
        avg_y = sum(p[1] for p in pts) / len(pts)
        return (0 if gzd == layout.gzd else 1, avg_x, -avg_y, gzd, sq)

    square_ids: list[str] = []
    label_features: list[tuple[Point, str, str, str]] = []

    for (pt_gzd, pt_sq), pts in sorted(region_points.items(), key=_sort_key):
        avg_x = sum(p[0] for p in pts) / len(pts)
        avg_y = sum(p[1] for p in pts) / len(pts)

        # Verify centroid maps back to the same (gzd, sq); otherwise pick closest sample point.
        lon, lat = to_wgs84.transform(avg_x, avg_y)
        use_x, use_y = avg_x, avg_y
        if -80.0 <= lat <= 84.0:
            chk_gzd, chk_sq, _ = _split_mgrs(
                _decode_mgrs(converter.toMGRS(lat, lon, MGRSPrecision=0))
            )
            if (chk_gzd, chk_sq) != (pt_gzd, pt_sq):
                best = min(pts, key=lambda p: (p[0] - avg_x) ** 2 + (p[1] - avg_y) ** 2)
                use_x, use_y = best
        else:
            use_x, use_y = pts[0]

        if pt_sq not in square_ids:
            square_ids.append(pt_sq)
        label_features.append((Point(use_x, use_y), pt_gzd, pt_sq, f"{pt_gzd} {pt_sq}"))

    return (square_ids, label_features)


def _create_field(layer: ogr.Layer, name: str, field_type: int, width: int = 0) -> None:
    defn = ogr.FieldDefn(name, field_type)
    if width > 0:
        defn.SetWidth(width)
    if layer.CreateField(defn) != 0:
        raise RuntimeError(f"Failed to create field '{name}' on layer '{layer.GetName()}'.")


def _write_mgrs_gpkg(
    output_gpkg: Path,
    utm_epsg: int,
    lines_1km: list[tuple[LineString, str, str, int, int]],
    lines_100km: list[tuple[LineString, str, str, int]],
    zone_boundaries: list[tuple[LineString, str]],
    square_labels: list[tuple[Point, str, str, str]],
) -> None:
    """Write MGRS vector layers to a GeoPackage in EPSG:{utm_epsg} using GDAL/OGR."""
    output_gpkg.parent.mkdir(parents=True, exist_ok=True)
    if output_gpkg.exists():
        output_gpkg.unlink()

    driver = ogr.GetDriverByName("GPKG")
    if driver is None:
        raise RuntimeError("GDAL/OGR GPKG driver is unavailable.")

    srs = osr.SpatialReference()
    if srs.ImportFromEPSG(utm_epsg) != 0:
        raise RuntimeError(f"Failed to import EPSG:{utm_epsg} into OSR SpatialReference.")
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

    ds = driver.CreateDataSource(str(output_gpkg))
    if ds is None:
        raise RuntimeError(f"Failed to create GeoPackage at '{output_gpkg}'.")

    try:
        # 1. grid_1km
        lyr_1km = ds.CreateLayer("grid_1km", srs=srs, geom_type=ogr.wkbLineString)
        _create_field(lyr_1km, "label", ogr.OFTString, 32)
        _create_field(lyr_1km, "axis", ogr.OFTString, 4)
        _create_field(lyr_1km, "coord_m", ogr.OFTInteger)
        _create_field(lyr_1km, "is_100km", ogr.OFTInteger)
        defn_1km = lyr_1km.GetLayerDefn()
        for geom, label, axis, coord_m, is_100km in lines_1km:
            feat = ogr.Feature(defn_1km)
            feat.SetGeometry(ogr.CreateGeometryFromWkb(geom.wkb))
            feat.SetField("label", label)
            feat.SetField("axis", axis)
            feat.SetField("coord_m", int(coord_m))
            feat.SetField("is_100km", int(is_100km))
            lyr_1km.CreateFeature(feat)

        # 2. grid_100km
        lyr_100km = ds.CreateLayer("grid_100km", srs=srs, geom_type=ogr.wkbLineString)
        _create_field(lyr_100km, "label", ogr.OFTString, 32)
        _create_field(lyr_100km, "axis", ogr.OFTString, 4)
        _create_field(lyr_100km, "coord_m", ogr.OFTInteger)
        defn_100km = lyr_100km.GetLayerDefn()
        for geom, label, axis, coord_m in lines_100km:
            feat = ogr.Feature(defn_100km)
            feat.SetGeometry(ogr.CreateGeometryFromWkb(geom.wkb))
            feat.SetField("label", label)
            feat.SetField("axis", axis)
            feat.SetField("coord_m", int(coord_m))
            lyr_100km.CreateFeature(feat)

        # 3. zone_boundary
        lyr_zb = ds.CreateLayer("zone_boundary", srs=srs, geom_type=ogr.wkbLineString)
        _create_field(lyr_zb, "label", ogr.OFTString, 64)
        defn_zb = lyr_zb.GetLayerDefn()
        for geom, label in zone_boundaries:
            feat = ogr.Feature(defn_zb)
            feat.SetGeometry(ogr.CreateGeometryFromWkb(geom.wkb))
            feat.SetField("label", label)
            lyr_zb.CreateFeature(feat)

        # 4. square_labels
        lyr_sq = ds.CreateLayer("square_labels", srs=srs, geom_type=ogr.wkbPoint)
        _create_field(lyr_sq, "gzd", ogr.OFTString, 8)
        _create_field(lyr_sq, "square_id", ogr.OFTString, 8)
        _create_field(lyr_sq, "full_id", ogr.OFTString, 16)
        defn_sq = lyr_sq.GetLayerDefn()
        for geom, gzd, sq_id, full_id in square_labels:
            feat = ogr.Feature(defn_sq)
            feat.SetGeometry(ogr.CreateGeometryFromWkb(geom.wkb))
            feat.SetField("gzd", gzd)
            feat.SetField("square_id", sq_id)
            feat.SetField("full_id", full_id)
            lyr_sq.CreateFeature(feat)

        ds.FlushCache()
    finally:
        ds = None


def generate_mgrs_grid(layout: SheetLayout, output_gpkg: Path) -> MGRSGridMeta:
    """Generate MGRS 1 km / 100 km grid GeoPackage and neatline collar ticks for a sheet."""
    bbox = layout.utm_bbox
    scale = layout.spec.scale

    # All 1,000 m grid coordinates within utm_bbox.
    e_start = int(math.ceil(bbox.min_x / 1000.0)) * 1000
    e_end = int(math.floor(bbox.max_x / 1000.0)) * 1000
    eastings = list(range(e_start, e_end + 1, 1000))

    n_start = int(math.ceil(bbox.min_y / 1000.0)) * 1000
    n_end = int(math.floor(bbox.max_y / 1000.0)) * 1000
    northings = list(range(n_start, n_end + 1, 1000))

    lines_1km: list[tuple[LineString, str, str, int, int]] = []
    lines_100km: list[tuple[LineString, str, str, int]] = []
    eastings_100km: list[int] = []
    northings_100km: list[int] = []

    for e in eastings:
        geom = LineString([(float(e), bbox.min_y), (float(e), bbox.max_y)])
        principal, _ = _format_tick_digits(e)
        is_100k = e % 100000 == 0
        lines_1km.append((geom, principal, "E", e, 1 if is_100k else 0))
        if is_100k:
            eastings_100km.append(e)
            lines_100km.append((geom, f"{e}m E", "E", e))

    for n in northings:
        geom = LineString([(bbox.min_x, float(n)), (bbox.max_x, float(n))])
        principal, _ = _format_tick_digits(n)
        is_100k = n % 100000 == 0
        lines_1km.append((geom, principal, "N", n, 1 if is_100k else 0))
        if is_100k:
            northings_100km.append(n)
            lines_100km.append((geom, f"{n}m N", "N", n))

    to_utm = Transformer.from_crs("EPSG:4326", f"EPSG:{layout.utm_epsg}", always_xy=True)
    to_wgs84 = Transformer.from_crs(f"EPSG:{layout.utm_epsg}", "EPSG:4326", always_xy=True)

    zone_boundaries = _compute_zone_boundaries(layout, to_utm)
    crosses_utm_zone_boundary = len(zone_boundaries) > 0

    square_ids_100km, square_labels = _discover_100km_squares(
        layout=layout,
        eastings_100km=eastings_100km,
        northings_100km=northings_100km,
        to_wgs84=to_wgs84,
    )

    crosses_100km_boundary = (
        any(bbox.min_x < e < bbox.max_x for e in eastings_100km)
        or any(bbox.min_y < n < bbox.max_y for n in northings_100km)
        or len(lines_100km) > 0
    )

    # Generate neatline ticks for all 4 edges in ascending offset_mm order.
    ticks: list[GridTick] = []
    for edge in ("top", "bottom"):
        for idx, e in enumerate(eastings):
            offset_mm = (e - bbox.min_x) / scale * 1000.0
            if abs(offset_mm) < 1e-9:
                offset_mm = 0.0
            principal, prefix = _format_tick_digits(e)
            ticks.append(
                GridTick(
                    edge=edge,
                    offset_mm=offset_mm,
                    easting_or_northing_m=e,
                    full_label=f"{e}m E",
                    principal_digits=principal,
                    prefix_digits=prefix,
                    is_100km=(e % 100000 == 0),
                    is_first_on_edge=(idx == 0),
                )
            )

    # Top-down layout coordinate for left/right edges (max_y -> 0 mm, min_y -> frame_height_mm).
    for edge in ("left", "right"):
        for idx, n in enumerate(reversed(northings)):
            offset_mm = (bbox.max_y - n) / scale * 1000.0
            if abs(offset_mm) < 1e-9:
                offset_mm = 0.0
            principal, prefix = _format_tick_digits(n)
            ticks.append(
                GridTick(
                    edge=edge,
                    offset_mm=offset_mm,
                    easting_or_northing_m=n,
                    full_label=f"{n}m N",
                    principal_digits=principal,
                    prefix_digits=prefix,
                    is_100km=(n % 100000 == 0),
                    is_first_on_edge=(idx == 0),
                )
            )

    _write_mgrs_gpkg(
        output_gpkg=output_gpkg,
        utm_epsg=layout.utm_epsg,
        lines_1km=lines_1km,
        lines_100km=lines_100km,
        zone_boundaries=zone_boundaries,
        square_labels=square_labels,
    )

    grid_spacing_mm = (1000.0 / scale) * 1000.0
    center_mgrs_10m = _format_center_mgrs_10m(
        layout.spec.center_lat, layout.spec.center_lon
    )

    return MGRSGridMeta(
        gzd=layout.gzd,
        square_ids_100km=square_ids_100km,
        center_mgrs_10m=center_mgrs_10m,
        grid_spacing_m=1000,
        grid_spacing_mm=grid_spacing_mm,
        crosses_100km_boundary=crosses_100km_boundary,
        crosses_utm_zone_boundary=crosses_utm_zone_boundary,
        ticks=ticks,
    )
