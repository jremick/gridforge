"""Tactical graphics GeoPackage writer (rings, sectors, corridors, bearing lines) & ruler metrics."""

from __future__ import annotations

import math
from collections.abc import Sequence
from pathlib import Path

from osgeo import ogr, osr
from pydantic import BaseModel, Field
from pyproj import Geod, Transformer
from shapely.geometry import LineString, Polygon

from gridforge.config import RFLinkSpec, TacticalGraphic
from gridforge.geo.crs import resolve_utm_zone
from gridforge.geo.declination import DEG_TO_NATO_MILS, compute_point_declination
from gridforge.geo.waypoints import _sample_points_elev_m

_WGS84_GEOD = Geod(ellps="WGS84")


class RulerSegment(BaseModel):
    """Single leg distance, True/Grid/Magnetic bearings, mils, and optional elevation grade."""

    from_idx: int
    to_idx: int
    distance_m: float
    bearing_true_deg: float
    bearing_grid_deg: float
    bearing_mag_deg: float
    bearing_mils: float
    elev_start_m: float | None = None
    elev_end_m: float | None = None
    grade_pct: float | None = None


class RulerMetricsResponse(BaseModel):
    """Geodesic multi-segment ruler readout returned by ``POST /api/ruler-metrics``."""

    total_distance_m: float = 0.0
    segments: list[RulerSegment] = Field(default_factory=list)


def _compute_leg_bearings(
    lat1: float,
    lon1: float,
    lat2: float,
    lon2: float,
    *,
    date_iso: str | None = None,
    elev_m: float = 0.0,
) -> tuple[float, float, float, float, float]:
    """Return ``(distance_m, true_deg, grid_deg, mag_deg, mag_mils)`` on WGS84 + WMM2025."""
    fwd_az, _back_az, dist_m = _WGS84_GEOD.inv(lon1, lat1, lon2, lat2)
    dist_f = max(0.0, float(dist_m))
    true_az = float(fwd_az) % 360.0 if dist_f > 1e-6 else 0.0

    mid_lat = 0.5 * (lat1 + lat2)
    mid_lon = 0.5 * (lon1 + lon2)
    _, _, mid_epsg, _ = resolve_utm_zone(mid_lat, mid_lon)
    decl = compute_point_declination(
        mid_lat,
        mid_lon,
        utm_epsg=mid_epsg,
        date_iso=date_iso,
        elevation_m=elev_m,
    )
    grid_az = (true_az - decl.grid_convergence_deg) % 360.0
    mag_az = (true_az - decl.magnetic_declination_deg) % 360.0
    mag_mils = (mag_az * DEG_TO_NATO_MILS) % 6400.0
    return (dist_f, true_az, grid_az, mag_az, mag_mils)


def compute_ruler_metrics(
    points: Sequence[tuple[float, float]],
    *,
    dem_path: Path | None = None,
    date_iso: str | None = None,
) -> RulerMetricsResponse:
    """Compute geodesic distance, True/Grid/WMM2025 Magnetic bearings, mils, and grade for ``(lat, lon)`` points."""
    if len(points) < 2:
        raise ValueError("Ruler metrics require at least 2 (lat, lon) points.")

    for lat, lon in points:
        if not -80.0 <= float(lat) <= 84.0:
            raise ValueError(f"Latitude {lat} is outside UTM/MGRS bounds [-80.0, 84.0].")
        if not -180.0 <= float(lon) <= 180.0:
            raise ValueError(f"Longitude {lon} must be within [-180.0, 180.0].")

    elevs: list[float | None] = (
        _sample_points_elev_m(dem_path, [(float(lat), float(lon)) for lat, lon in points])
        if dem_path is not None
        else [None] * len(points)
    )

    segments: list[RulerSegment] = []
    total_m = 0.0
    for idx in range(len(points) - 1):
        lat1, lon1 = float(points[idx][0]), float(points[idx][1])
        lat2, lon2 = float(points[idx + 1][0]), float(points[idx + 1][1])
        e1 = elevs[idx]
        e2 = elevs[idx + 1]
        mid_elev = 0.0
        if e1 is not None and e2 is not None:
            mid_elev = 0.5 * (e1 + e2)
        elif e1 is not None:
            mid_elev = e1
        elif e2 is not None:
            mid_elev = e2

        dist_m, true_az, grid_az, mag_az, mag_mils = _compute_leg_bearings(
            lat1, lon1, lat2, lon2, date_iso=date_iso, elev_m=mid_elev
        )
        total_m += dist_m

        grade_pct: float | None = None
        if e1 is not None and e2 is not None and dist_m >= 1.0:
            grade_pct = round(((e2 - e1) / dist_m) * 100.0, 2)

        segments.append(
            RulerSegment(
                from_idx=idx,
                to_idx=idx + 1,
                distance_m=round(dist_m, 2),
                bearing_true_deg=round(true_az, 1),
                bearing_grid_deg=round(grid_az, 1),
                bearing_mag_deg=round(mag_az, 1),
                bearing_mils=round(mag_mils, 0),
                elev_start_m=round(e1, 1) if e1 is not None else None,
                elev_end_m=round(e2, 1) if e2 is not None else None,
                grade_pct=grade_pct,
            )
        )

    return RulerMetricsResponse(
        total_distance_m=round(total_m, 2),
        segments=segments,
    )


def _create_str_field(layer: ogr.Layer, name: str, width: int = 128) -> None:
    defn = ogr.FieldDefn(name, ogr.OFTString)
    defn.SetWidth(width)
    layer.CreateField(defn)


def write_tactical_graphics_gpkg(
    graphics: Sequence[TacticalGraphic],
    utm_epsg: int,
    output_gpkg: Path,
    *,
    rf_links: Sequence[RFLinkSpec] | None = None,
    date_iso: str | None = None,
) -> Path | None:
    """Write tactical markup layers in ``EPSG:{utm_epsg}`` GeoPackage.

    Creates three layers consumed by ``gridforge.render.styles``:
      - ``tactical_sectors`` (Polygon): sector AOIs & buffered corridors
      - ``tactical_rings`` (LineString): concentric range ring circles
      - ``tactical_lines`` (LineString): bearing lines, corridor centerlines & RF links

    Returns ``output_gpkg`` if at least one feature was written, else ``None``.
    """
    links = list(rf_links or [])
    if not graphics and not links:
        return None

    ogr.UseExceptions()
    osr.UseExceptions()

    output_gpkg = Path(output_gpkg)
    output_gpkg.parent.mkdir(parents=True, exist_ok=True)
    if output_gpkg.exists():
        output_gpkg.unlink()

    driver = ogr.GetDriverByName("GPKG")
    if driver is None:
        raise RuntimeError("GDAL/OGR GPKG driver unavailable.")

    srs = osr.SpatialReference()
    srs.ImportFromEPSG(utm_epsg)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    to_utm = Transformer.from_crs("EPSG:4326", f"EPSG:{utm_epsg}", always_xy=True)

    ds = driver.CreateDataSource(str(output_gpkg))
    if ds is None:
        raise RuntimeError(f"Failed to create tactical graphics GeoPackage at '{output_gpkg}'.")

    total_written = 0
    try:
        # 1. tactical_sectors (Polygon)
        lyr_sectors = ds.CreateLayer("tactical_sectors", srs=srs, geom_type=ogr.wkbPolygon)
        _create_str_field(lyr_sectors, "label", 120)
        _create_str_field(lyr_sectors, "kind", 32)
        lyr_sectors.CreateField(ogr.FieldDefn("area_km2", ogr.OFTReal))
        _create_str_field(lyr_sectors, "color", 24)
        _create_str_field(lyr_sectors, "notes", 255)

        # 2. tactical_rings (LineString)
        lyr_rings = ds.CreateLayer("tactical_rings", srs=srs, geom_type=ogr.wkbLineString)
        _create_str_field(lyr_rings, "label", 120)
        _create_str_field(lyr_rings, "base_label", 80)
        lyr_rings.CreateField(ogr.FieldDefn("radius_km", ogr.OFTReal))
        lyr_rings.CreateField(ogr.FieldDefn("radius_m", ogr.OFTReal))
        _create_str_field(lyr_rings, "color", 24)
        _create_str_field(lyr_rings, "notes", 255)

        # 3. tactical_lines (LineString)
        lyr_lines = ds.CreateLayer("tactical_lines", srs=srs, geom_type=ogr.wkbLineString)
        _create_str_field(lyr_lines, "label", 160)
        _create_str_field(lyr_lines, "kind", 32)
        lyr_lines.CreateField(ogr.FieldDefn("distance_km", ogr.OFTReal))
        lyr_lines.CreateField(ogr.FieldDefn("bearing_true_deg", ogr.OFTReal))
        lyr_lines.CreateField(ogr.FieldDefn("bearing_grid_deg", ogr.OFTReal))
        lyr_lines.CreateField(ogr.FieldDefn("bearing_mag_deg", ogr.OFTReal))
        lyr_lines.CreateField(ogr.FieldDefn("bearing_mils", ogr.OFTReal))
        _create_str_field(lyr_lines, "color", 24)
        _create_str_field(lyr_lines, "notes", 255)

        sec_defn = lyr_sectors.GetLayerDefn()
        ring_defn = lyr_rings.GetLayerDefn()
        line_defn = lyr_lines.GetLayerDefn()

        ds.StartTransaction()
        try:
            def _add_sector(poly: Polygon, label: str, kind: str, color: str, notes: str) -> None:
                nonlocal total_written
                if poly.is_empty or not poly.is_valid:
                    poly = poly.buffer(0)
                if poly.is_empty:
                    return
                geom = ogr.CreateGeometryFromWkb(poly.wkb)
                if geom is None:
                    return
                feat = ogr.Feature(sec_defn)
                feat.SetGeometry(geom)
                feat.SetField("label", label)
                feat.SetField("kind", kind)
                feat.SetField("area_km2", round(float(poly.area) / 1_000_000.0, 4))
                feat.SetField("color", color)
                feat.SetField("notes", notes)
                lyr_sectors.CreateFeature(feat)
                total_written += 1

            def _add_line(
                utm_pts: list[tuple[float, float]],
                *,
                label: str,
                kind: str,
                distance_km: float,
                bearing_true: float,
                bearing_grid: float,
                bearing_mag: float,
                bearing_mils: float,
                color: str,
                notes: str,
            ) -> None:
                nonlocal total_written
                if len(utm_pts) < 2:
                    return
                geom = ogr.Geometry(ogr.wkbLineString)
                for ux, uy in utm_pts:
                    geom.AddPoint_2D(float(ux), float(uy))
                feat = ogr.Feature(line_defn)
                feat.SetGeometry(geom)
                feat.SetField("label", label)
                feat.SetField("kind", kind)
                feat.SetField("distance_km", round(distance_km, 3))
                feat.SetField("bearing_true_deg", round(bearing_true, 1))
                feat.SetField("bearing_grid_deg", round(bearing_grid, 1))
                feat.SetField("bearing_mag_deg", round(bearing_mag, 1))
                feat.SetField("bearing_mils", round(bearing_mils, 0))
                feat.SetField("color", color)
                feat.SetField("notes", notes)
                lyr_lines.CreateFeature(feat)
                total_written += 1

            for g in graphics:
                base_label = (g.label or "").strip()
                color = (g.color or "").strip()
                notes = (g.notes or "").strip()

                if g.kind == "range_ring":
                    if not g.points:
                        continue
                    for lat, lon in g.points:
                        cx, cy = to_utm.transform(float(lon), float(lat))
                        cx_f, cy_f = float(cx), float(cy)
                        for r_km in g.radii_km:
                            r_m = float(r_km) * 1000.0
                            ring_geom = ogr.Geometry(ogr.wkbLineString)
                            n_verts = 96
                            for k in range(n_verts + 1):
                                ang = 2.0 * math.pi * (k % n_verts) / n_verts
                                ring_geom.AddPoint_2D(cx_f + r_m * math.cos(ang), cy_f + r_m * math.sin(ang))
                            ring_lbl = f"{base_label} {r_km:g} km" if base_label else f"{r_km:g} km"
                            feat = ogr.Feature(ring_defn)
                            feat.SetGeometry(ring_geom)
                            feat.SetField("label", ring_lbl)
                            feat.SetField("base_label", base_label)
                            feat.SetField("radius_km", float(r_km))
                            feat.SetField("radius_m", round(r_m, 1))
                            feat.SetField("color", color)
                            feat.SetField("notes", notes)
                            lyr_rings.CreateFeature(feat)
                            total_written += 1

                elif g.kind == "sector":
                    if len(g.points) < 3:
                        continue
                    utm_pts = [
                        (float(x), float(y))
                        for x, y in (to_utm.transform(float(lon), float(lat)) for lat, lon in g.points)
                    ]
                    if utm_pts[0] != utm_pts[-1]:
                        utm_pts.append(utm_pts[0])
                    poly = Polygon(utm_pts)
                    _add_sector(poly, base_label or "AOI", "sector", color, notes)

                elif g.kind in ("bearing_line", "corridor"):
                    if len(g.points) < 2:
                        continue
                    utm_pts = [
                        (float(x), float(y))
                        for x, y in (to_utm.transform(float(lon), float(lat)) for lat, lon in g.points)
                    ]
                    metrics = compute_ruler_metrics(g.points, date_iso=date_iso)
                    first_seg = metrics.segments[0]
                    dist_km = metrics.total_distance_m / 1000.0
                    if g.kind == "bearing_line":
                        bearing_summary = f"{dist_km:.2f} km · {first_seg.bearing_mag_deg:05.1f}°M"
                        line_lbl = f"{base_label} ({bearing_summary})" if base_label else bearing_summary
                    else:
                        line_lbl = base_label or f"Corridor ({dist_km:.2f} km)"
                        half_w_m = max(25.0, (g.radii_km[0] if g.radii_km else 0.25) * 1000.0)
                        buf_geom = LineString(utm_pts).buffer(half_w_m, cap_style=2, join_style=2)
                        if isinstance(buf_geom, Polygon):
                            _add_sector(buf_geom, line_lbl, "corridor", color, notes)

                    _add_line(
                        utm_pts,
                        label=line_lbl,
                        kind=g.kind,
                        distance_km=dist_km,
                        bearing_true=first_seg.bearing_true_deg,
                        bearing_grid=first_seg.bearing_grid_deg,
                        bearing_mag=first_seg.bearing_mag_deg,
                        bearing_mils=first_seg.bearing_mils,
                        color=color,
                        notes=notes,
                    )

            # Optional pinned RF link lines rendered directly onto tactical_lines
            for link in links:
                x1, y1 = to_utm.transform(float(link.from_lon), float(link.from_lat))
                x2, y2 = to_utm.transform(float(link.to_lon), float(link.to_lat))
                dist_m, true_az, grid_az, mag_az, mag_mils = _compute_leg_bearings(
                    link.from_lat, link.from_lon, link.to_lat, link.to_lon, date_iso=date_iso
                )
                dist_km = dist_m / 1000.0
                link_lbl = f"{link.from_label} <-> {link.to_label} ({dist_km:.2f} km · {link.freq_mhz:g} MHz)"
                _add_line(
                    [(float(x1), float(y1)), (float(x2), float(y2))],
                    label=link_lbl,
                    kind="rf_link",
                    distance_km=dist_km,
                    bearing_true=true_az,
                    bearing_grid=grid_az,
                    bearing_mag=mag_az,
                    bearing_mils=mag_mils,
                    color="",
                    notes=f"{link.from_height_m:g}m/{link.to_height_m:g}m AGL",
                )
            ds.CommitTransaction()
        except Exception:
            ds.RollbackTransaction()
            raise

        ds.FlushCache()
    finally:
        ds = None

    if total_written == 0:
        output_gpkg.unlink(missing_ok=True)
        return None
    return output_gpkg
