"""Tactical waypoints, geodesic route leg azimuths (True/Grid/Magnetic), and GeoPackage export."""

from __future__ import annotations

import math
import re
from pathlib import Path

import mgrs
from osgeo import gdal, ogr, osr
from pydantic import BaseModel, Field
from pyproj import Geod, Transformer

from gridforge.config import SheetSpec, Units, WaypointCategory, WaypointInput
from gridforge.geo.crs import compute_sheet_layout, resolve_utm_zone
from gridforge.geo.declination import DEG_TO_NATO_MILS, compute_declination

KM_TO_MILES: float = 0.621371192237334
_MGRS_SPLIT_RE = re.compile(r"^0?([1-9]|[1-5]\d|60)([C-HJ-NP-X])([A-HJ-NP-Z]{2})(\d*)$")
_WGS84_GEOD = Geod(ellps="WGS84")


class WaypointMetric(BaseModel):
    """Resolved coordinate, MGRS, elevation, and incoming leg readout for a single waypoint."""

    seq: int
    code: str
    label: str
    category: WaypointCategory
    lat: float
    lon: float
    mgrs_10m: str
    utm_epsg: int
    elev_m: float | None = None
    notes: str | None = None
    leg_dist_km: float | None = None
    leg_dist_mi: float | None = None
    cum_dist_km: float = 0.0
    true_az_deg: float | None = None
    grid_az_deg: float | None = None
    mag_az_deg: float | None = None
    mag_az_mils: float | None = None


class RouteLegMetric(BaseModel):
    """True, Grid, and WMM2025 Magnetic azimuth and distance for one WP[i] -> WP[i+1] leg."""

    leg_index: int
    from_code: str
    to_code: str
    from_label: str
    to_label: str
    leg_dist_km: float
    leg_dist_mi: float
    cum_dist_km: float
    cum_dist_mi: float
    true_az_deg: float
    grid_az_deg: float
    mag_az_deg: float
    mag_az_mils: float
    declination_deg: float
    gm_angle_deg: float


class RouteMetrics(BaseModel):
    """Complete tactical waypoint log and route leg metrics."""

    waypoints: list[WaypointMetric] = Field(default_factory=list)
    legs: list[RouteLegMetric] = Field(default_factory=list)
    total_distance_km: float = 0.0
    total_distance_mi: float = 0.0
    units: Units = Units.METRIC
    date_iso: str | None = None
    utm_epsg: int | None = None


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


def _sample_point_elev_m(dem_path: Path, lat: float, lon: float) -> float | None:
    """Sample elevation (meters) at (lat, lon) from a local GeoTIFF DEM if covered."""
    if not dem_path.is_file():
        return None
    gdal.UseExceptions()
    osr.UseExceptions()
    ds = gdal.Open(str(dem_path))
    if ds is None:
        return None
    try:
        band = ds.GetRasterBand(1)
        nodata = band.GetNoDataValue()
        gt = ds.GetGeoTransform()
        inv_gt = gdal.InvGeoTransform(gt)
        wkt = ds.GetProjection()

        x, y = lon, lat
        if wkt:
            src_srs = osr.SpatialReference()
            src_srs.ImportFromEPSG(4326)
            src_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            dst_srs = osr.SpatialReference(wkt=wkt)
            dst_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            if not src_srs.IsSame(dst_srs):
                ct = osr.CoordinateTransformation(src_srs, dst_srs)
                x, y, _ = ct.TransformPoint(lon, lat)

        px = int(math.floor(inv_gt[0] + inv_gt[1] * x + inv_gt[2] * y))
        py = int(math.floor(inv_gt[3] + inv_gt[4] * x + inv_gt[5] * y))
        if not (0 <= px < ds.RasterXSize and 0 <= py < ds.RasterYSize):
            return None
        arr = band.ReadAsArray(px, py, 1, 1)
        if arr is None:
            return None
        val = float(arr[0, 0])
        if not math.isfinite(val):
            return None
        if nodata is not None and math.isclose(val, nodata, rel_tol=1e-5, abs_tol=1e-4):
            return None
        return round(val, 1)
    finally:
        ds = None


def compute_route_metrics(
    waypoints: list[WaypointInput],
    *,
    units: Units = Units.METRIC,
    date_iso: str | None = None,
    dem_path: Path | None = None,
) -> RouteMetrics:
    """Compute MGRS, elevations, distances, and True/Grid/WMM2025 Magnetic azimuths for waypoints."""
    if not waypoints:
        return RouteMetrics(units=units, date_iso=date_iso)

    wp_metrics: list[WaypointMetric] = []
    legs: list[RouteLegMetric] = []
    cum_km = 0.0

    for idx, wp in enumerate(waypoints, start=1):
        _, _, epsg, _ = resolve_utm_zone(wp.lat, wp.lon)
        elev_m = (
            _sample_point_elev_m(dem_path, wp.lat, wp.lon)
            if dem_path is not None
            else None
        )
        wp_metrics.append(
            WaypointMetric(
                seq=idx,
                code=f"WP{idx}",
                label=wp.label,
                category=wp.category,
                lat=round(wp.lat, 6),
                lon=round(wp.lon, 6),
                mgrs_10m=_format_mgrs_10m(wp.lat, wp.lon),
                utm_epsg=epsg,
                elev_m=elev_m,
                notes=wp.notes,
                cum_dist_km=0.0,
            )
        )

    for i in range(len(wp_metrics) - 1):
        w1 = wp_metrics[i]
        w2 = wp_metrics[i + 1]
        fwd_az, _back_az, dist_m = _WGS84_GEOD.inv(w1.lon, w1.lat, w2.lon, w2.lat)
        leg_km = dist_m / 1000.0
        leg_mi = leg_km * KM_TO_MILES
        cum_km += leg_km
        cum_mi = cum_km * KM_TO_MILES

        true_az = fwd_az % 360.0 if dist_m > 1e-6 else 0.0

        mid_lat = (w1.lat + w2.lat) / 2.0
        mid_lon = (w1.lon + w2.lon) / 2.0
        mid_elev = 0.0
        if w1.elev_m is not None and w2.elev_m is not None:
            mid_elev = (w1.elev_m + w2.elev_m) / 2.0
        elif w1.elev_m is not None:
            mid_elev = w1.elev_m
        elif w2.elev_m is not None:
            mid_elev = w2.elev_m

        mid_layout = compute_sheet_layout(
            SheetSpec(center_lat=mid_lat, center_lon=mid_lon, date_iso=date_iso)
        )
        decl = compute_declination(mid_layout, elevation_m=mid_elev)

        grid_az = (true_az - decl.grid_convergence_deg) % 360.0
        mag_az = (true_az - decl.magnetic_declination_deg) % 360.0
        mag_mils = (mag_az * DEG_TO_NATO_MILS) % 6400.0

        leg = RouteLegMetric(
            leg_index=i + 1,
            from_code=w1.code,
            to_code=w2.code,
            from_label=w1.label,
            to_label=w2.label,
            leg_dist_km=round(leg_km, 3),
            leg_dist_mi=round(leg_mi, 3),
            cum_dist_km=round(cum_km, 3),
            cum_dist_mi=round(cum_mi, 3),
            true_az_deg=round(true_az, 1),
            grid_az_deg=round(grid_az, 1),
            mag_az_deg=round(mag_az, 1),
            mag_az_mils=round(mag_mils, 0),
            declination_deg=round(decl.magnetic_declination_deg, 2),
            gm_angle_deg=round(decl.gm_angle_deg, 2),
        )
        legs.append(leg)

        # Mirror incoming leg summary directly onto destination waypoint for table ergonomics
        wp_metrics[i + 1] = w2.model_copy(
            update={
                "leg_dist_km": leg.leg_dist_km,
                "leg_dist_mi": leg.leg_dist_mi,
                "cum_dist_km": leg.cum_dist_km,
                "true_az_deg": leg.true_az_deg,
                "grid_az_deg": leg.grid_az_deg,
                "mag_az_deg": leg.mag_az_deg,
                "mag_az_mils": leg.mag_az_mils,
            }
        )

    anchor_epsg = wp_metrics[0].utm_epsg if wp_metrics else None
    return RouteMetrics(
        waypoints=wp_metrics,
        legs=legs,
        total_distance_km=round(cum_km, 3),
        total_distance_mi=round(cum_km * KM_TO_MILES, 3),
        units=units,
        date_iso=date_iso,
        utm_epsg=anchor_epsg,
    )


def write_waypoints_gpkg(
    metrics: RouteMetrics,
    utm_epsg: int,
    output_gpkg: Path,
) -> Path:
    """Write ``waypoint_points`` and ``waypoint_route`` layers in ``EPSG:{utm_epsg}`` GeoPackage."""
    ogr.UseExceptions()
    osr.UseExceptions()

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
        raise RuntimeError(f"Failed to create waypoints GeoPackage at '{output_gpkg}'.")

    try:
        # 1. waypoint_points
        lyr_pts = ds.CreateLayer("waypoint_points", srs=srs, geom_type=ogr.wkbPoint)
        for fname, ftype, width in (
            ("seq", ogr.OFTInteger, 0),
            ("code", ogr.OFTString, 16),
            ("label", ogr.OFTString, 80),
            ("category", ogr.OFTString, 24),
            ("mgrs_10m", ogr.OFTString, 32),
            ("elev_m", ogr.OFTReal, 0),
        ):
            defn = ogr.FieldDefn(fname, ftype)
            if width:
                defn.SetWidth(width)
            lyr_pts.CreateField(defn)

        pts_defn = lyr_pts.GetLayerDefn()
        utm_coords: list[tuple[float, float]] = []
        for wp in metrics.waypoints:
            ux, uy = to_utm.transform(wp.lon, wp.lat)
            utm_coords.append((float(ux), float(uy)))

            feat = ogr.Feature(pts_defn)
            geom = ogr.Geometry(ogr.wkbPoint)
            geom.AddPoint_2D(float(ux), float(uy))
            feat.SetGeometry(geom)
            feat.SetField("seq", int(wp.seq))
            feat.SetField("code", wp.code)
            feat.SetField("label", wp.label)
            feat.SetField("category", wp.category.value)
            feat.SetField("mgrs_10m", wp.mgrs_10m)
            if wp.elev_m is not None:
                feat.SetField("elev_m", float(wp.elev_m))
            lyr_pts.CreateFeature(feat)

        # 2. waypoint_route
        lyr_route = ds.CreateLayer("waypoint_route", srs=srs, geom_type=ogr.wkbLineString)
        lyr_route.CreateField(ogr.FieldDefn("total_km", ogr.OFTReal))
        if len(utm_coords) >= 2:
            rfeat = ogr.Feature(lyr_route.GetLayerDefn())
            line = ogr.Geometry(ogr.wkbLineString)
            for ux, uy in utm_coords:
                line.AddPoint_2D(ux, uy)
            rfeat.SetGeometry(line)
            rfeat.SetField("total_km", float(metrics.total_distance_km))
            lyr_route.CreateFeature(rfeat)

        ds.FlushCache()
    finally:
        ds = None

    return output_gpkg
