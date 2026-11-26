"""GDAL terrain processing: UTM DEM warp, multi-directional hillshade, and indexed contours."""

from __future__ import annotations

from array import array
import logging
import math
import struct
import subprocess
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel

from gridforge.config import (
    ColorMode,
    DensityLevel,
    HillshadeStrength,
    SheetLayout,
    Units,
    ViewshedObserver,
)

logger = logging.getLogger(__name__)

FEET_PER_METER: Final[float] = 3.280839895013123
DEM_NODATA: Final[float] = -9999.0
DEFAULT_TARGET_RES_M: Final[float] = 10.0
MIN_TARGET_RES_M: Final[float] = 2.0
LIDAR_NATIVE_THRESHOLD_M: Final[float] = 3.0


class TerrainResult(BaseModel):
    """Paths and terrain statistics produced by ``build_terrain_products``."""

    dem_utm_tif: Path
    hillshade_tif: Path
    terrain_gpkg: Path
    contour_interval_display: str
    contour_interval: float
    index_interval: float
    min_elev: float
    max_elev: float
    center_elev_m: float
    slope_tif: Path | None = None
    viewshed_tif: Path | None = None
    viewshed_observers_gpkg: Path | None = None


def choose_contour_interval(
    scale: int,
    units: Units,
    relief_m: float,
    density: DensityLevel = DensityLevel.NORMAL,
) -> tuple[float, int, str]:
    """Select military topographic contour interval, index multiplier, and collar display text.

    Honors ``density``:
    - ``LOW``: ``2x`` normal interval
    - ``NORMAL``: standard military topographic interval
    - ``HIGH``: ``0.5x`` normal interval (clamped to minimum ``5 m`` / ``10 ft``)

    Returns:
        ``(interval_in_unit, index_multiplier, display_str)`` where ``index_multiplier`` is ``5``.
    """
    index_mult = 5

    if units == Units.IMPERIAL:
        if scale <= 15000:
            base = 20.0
        elif scale <= 25000:
            base = 40.0
        else:
            base = 80.0
        min_interval = 10.0
        unit_label = "ft"
    else:
        if scale <= 15000:
            base = 10.0
        elif scale <= 25000:
            base = 20.0 if relief_m > 1200.0 else 10.0
        else:
            base = 20.0
        min_interval = 5.0
        unit_label = "m"

    if density == DensityLevel.LOW:
        interval = base * 2.0
    elif density == DensityLevel.HIGH:
        interval = max(min_interval, base * 0.5)
    else:
        interval = base

    index_val = int(round(interval * index_mult))
    return (interval, index_mult, f"{int(round(interval))} {unit_label} (index {index_val} {unit_label})")


def choose_warp_resolution_and_z(native_res_m: float) -> tuple[float, float]:
    """Choose ``(target_res_m, hillshade_z_factor)`` from input DEM native pixel resolution.

    Preserves crisp 2.0 m LiDAR micro-terrain (with ``z=1.0``) when input DEM resolution
    is ``<= 3.0 m`` (e.g., USGS 3DEP 1 m), preserves finer intermediate DEMs (3–10 m),
    and upsamples 30 m GLO-30 / 10 m DEMs smoothly to ``10.0 m`` (with ``z=1.15``).
    """
    if native_res_m <= LIDAR_NATIVE_THRESHOLD_M:
        target = max(MIN_TARGET_RES_M, round(native_res_m, 2))
        return (target, 1.0)
    if native_res_m < DEFAULT_TARGET_RES_M:
        return (round(native_res_m, 2), 1.15)
    return (DEFAULT_TARGET_RES_M, 1.15)


def _estimate_native_resolution_m(dem_raw_path: Path, center_lat: float) -> float:
    """Estimate native horizontal pixel size of ``dem_raw_path`` in meters."""
    from osgeo import gdal, osr  # type: ignore[import-untyped]

    gdal.UseExceptions()
    ds = gdal.Open(str(dem_raw_path), gdal.GA_ReadOnly)
    if ds is None:
        raise RuntimeError(f"GDAL could not open input DEM: {dem_raw_path}")

    try:
        gt = ds.GetGeoTransform()
        px_x = abs(gt[1])
        px_y = abs(gt[5])
        if px_x <= 0 or px_y <= 0:
            return DEFAULT_TARGET_RES_M

        srs = osr.SpatialReference()
        wkt = ds.GetProjection()
        if wkt:
            srs.ImportFromWkt(wkt)

        if srs.IsGeographic():
            cos_lat = max(0.1, math.cos(math.radians(center_lat)))
            return min(px_x * 111320.0 * cos_lat, px_y * 111320.0)

        linear_units = srs.GetLinearUnits() or 1.0
        return min(px_x, px_y) * linear_units
    finally:
        ds = None


def _compute_dem_stats_and_center(
    dem_utm_tif: Path,
    center_easting: float,
    center_northing: float,
) -> tuple[float, float, float]:
    """Compute ``(min_elev_m, max_elev_m, center_elev_m)`` from ``dem_utm_tif``."""
    from osgeo import gdal  # type: ignore[import-untyped]

    gdal.UseExceptions()
    ds = gdal.Open(str(dem_utm_tif), gdal.GA_ReadOnly)
    if ds is None:
        raise RuntimeError(f"GDAL could not open warped UTM DEM: {dem_utm_tif}")

    try:
        band = ds.GetRasterBand(1)
        nodata = band.GetNoDataValue()
        try:
            min_elev, max_elev, mean_elev, _stddev = band.ComputeStatistics(False)
        except RuntimeError as exc:
            raise RuntimeError(
                f"Warped UTM DEM {dem_utm_tif} contains no valid elevation pixels "
                "(input DEM may not cover the requested sheet footprint)."
            ) from exc

        gt = ds.GetGeoTransform()
        px = int(math.floor((center_easting - gt[0]) / gt[1]))
        py = int(math.floor((center_northing - gt[3]) / gt[5]))
        px = max(0, min(ds.RasterXSize - 1, px))
        py = max(0, min(ds.RasterYSize - 1, py))

        raw = band.ReadRaster(px, py, 1, 1, 1, 1, gdal.GDT_Float32)
        center_val = float(struct.unpack("=f", raw)[0]) if raw else float(mean_elev)
        if math.isnan(center_val) or (nodata is not None and abs(center_val - nodata) < 1e-3):
            center_val = float(mean_elev)

        return (float(min_elev), float(max_elev), center_val)
    finally:
        ds = None


def _write_neutral_hillshade(dem_utm_tif: Path, hillshade_tif: Path) -> Path:
    """Write a lightweight flat-white (255) Byte hillshade raster for ``HillshadeStrength.OFF``."""
    from osgeo import gdal  # type: ignore[import-untyped]

    gdal.UseExceptions()
    src = gdal.Open(str(dem_utm_tif), gdal.GA_ReadOnly)
    if src is None:
        raise RuntimeError(f"Could not open UTM DEM for neutral hillshade: {dem_utm_tif}")
    try:
        gt = src.GetGeoTransform()
        proj = src.GetProjection()
        width_m = src.RasterXSize * gt[1]
        height_m = src.RasterYSize * abs(gt[5])
    finally:
        src = None

    size = 16
    driver = gdal.GetDriverByName("GTiff")
    dst = driver.Create(
        str(hillshade_tif),
        size,
        size,
        1,
        gdal.GDT_Byte,
        options=["COMPRESS=DEFLATE"],
    )
    if dst is None:
        raise RuntimeError(f"Could not create neutral hillshade GeoTIFF at {hillshade_tif}")
    try:
        dst.SetGeoTransform((gt[0], width_m / size, 0.0, gt[3], 0.0, -(height_m / size)))
        dst.SetProjection(proj)
        band = dst.GetRasterBand(1)
        band.WriteRaster(0, 0, size, size, bytes([255] * (size * size)))
        band.FlushCache()
        dst.FlushCache()
    finally:
        dst = None
    return hillshade_tif


def _scale_dem_meters_to_feet(dem_utm_m: Path, dem_utm_ft: Path) -> Path:
    """Create a Float32 feet-scaled copy of ``dem_utm_m`` preserving ``DEM_NODATA`` pixels."""
    from osgeo import gdal  # type: ignore[import-untyped]

    gdal.UseExceptions()
    src_ds = gdal.Open(str(dem_utm_m), gdal.GA_ReadOnly)
    if src_ds is None:
        raise RuntimeError(f"Could not open UTM DEM for imperial scaling: {dem_utm_m}")

    try:
        cols = src_ds.RasterXSize
        rows = src_ds.RasterYSize
        src_band = src_ds.GetRasterBand(1)
        nodata = src_band.GetNoDataValue()
        if nodata is None:
            nodata = DEM_NODATA

        driver = gdal.GetDriverByName("GTiff")
        dst_ds = driver.Create(
            str(dem_utm_ft),
            cols,
            rows,
            1,
            gdal.GDT_Float32,
            options=["COMPRESS=DEFLATE", "TILED=YES"],
        )
        if dst_ds is None:
            raise RuntimeError(f"Could not create imperial DEM raster: {dem_utm_ft}")

        try:
            dst_ds.SetGeoTransform(src_ds.GetGeoTransform())
            dst_ds.SetProjection(src_ds.GetProjection())
            dst_band = dst_ds.GetRasterBand(1)
            dst_band.SetNoDataValue(float(nodata))

            try:
                import numpy as np  # type: ignore[import-not-found]

                arr = src_band.ReadAsArray().astype(np.float32, copy=False)
                valid = np.isfinite(arr) & (np.abs(arr - np.float32(nodata)) > 1e-3)
                scaled = np.where(valid, arr * np.float32(FEET_PER_METER), np.float32(nodata))
                dst_band.WriteArray(scaled)
            except ImportError:
                raw = src_band.ReadRaster(0, 0, cols, rows, cols, rows, gdal.GDT_Float32)
                vals = array("f")
                vals.frombytes(raw)
                scaled_vals = array(
                    "f",
                    (
                        v * FEET_PER_METER
                        if (math.isfinite(v) and abs(v - nodata) > 1e-3)
                        else nodata
                        for v in vals
                    ),
                )
                dst_band.WriteRaster(0, 0, cols, rows, scaled_vals.tobytes(), cols, rows, gdal.GDT_Float32)

            dst_band.FlushCache()
            dst_ds.FlushCache()
        finally:
            dst_ds = None
    finally:
        src_ds = None

    return dem_utm_ft


def _postprocess_contours_gpkg(terrain_gpkg: Path, index_interval: float) -> None:
    """Add ``is_index`` (0/1) and integer ``elev_label`` columns to the ``contours`` table.

    Executes through GDAL/OGR's native GeoPackage SQLite connection rather than bare
    ``sqlite3`` so GeoPackage R-Tree triggers (``ST_IsEmpty`` / ``ST_MinX``) work cleanly.
    """
    from osgeo import ogr  # type: ignore[import-untyped]

    ogr.UseExceptions()
    ds = ogr.Open(str(terrain_gpkg), 1)
    if ds is None:
        raise RuntimeError(f"GDAL/OGR could not open terrain GeoPackage: {terrain_gpkg}")

    try:
        layer = ds.GetLayerByName("contours")
        if layer is None:
            raise RuntimeError(f"Missing 'contours' layer in {terrain_gpkg}")

        defn = layer.GetLayerDefn()
        existing = {defn.GetFieldDefn(i).GetName() for i in range(defn.GetFieldCount())}
        if "is_index" not in existing:
            layer.CreateField(ogr.FieldDefn("is_index", ogr.OFTInteger))
        if "elev_label" not in existing:
            layer.CreateField(ogr.FieldDefn("elev_label", ogr.OFTString))

        idx = float(index_interval)
        ds.ExecuteSQL(
            f"""
            UPDATE contours
            SET is_index = CASE
                    WHEN ABS(elev - ROUND(elev / {idx:.12g}) * {idx:.12g}) < 1e-3 THEN 1
                    ELSE 0
                END,
                elev_label = CAST(CAST(ROUND(elev) AS INTEGER) AS TEXT)
            """
        )
        ds.ExecuteSQL(
            "CREATE INDEX IF NOT EXISTS idx_contours_is_index ON contours(is_index)"
        )
        ds.FlushCache()
    finally:
        ds = None


SLOPE_COLOR_TABLE_COLOR: Final[str] = """\
nv 0 0 0 0
0.0 0 0 0 0
26.999 0 0 0 0
27.0 246 224 94 115
29.999 246 224 94 115
30.0 237 137 54 155
34.999 237 137 54 155
35.0 229 62 62 185
44.999 229 62 62 185
45.0 128 90 213 195
49.999 128 90 213 195
50.0 59 7 100 205
90.0 59 7 100 205
"""

SLOPE_COLOR_TABLE_BW: Final[str] = """\
nv 0 0 0 0
0.0 0 0 0 0
29.999 0 0 0 0
30.0 120 120 120 85
34.999 120 120 120 85
35.0 55 55 55 135
44.999 55 55 55 135
45.0 15 15 15 180
90.0 15 15 15 180
"""

VIEWSHED_RGBA_COLOR: Final[tuple[int, int, int, int]] = (0, 212, 146, 130)
VIEWSHED_RGBA_BW: Final[tuple[int, int, int, int]] = (80, 80, 80, 95)


def _sample_dem_point_elev(ds: Any, x: float, y: float) -> float | None:
    """Sample Float32 ground elevation in meters at projected ``(x, y)`` from an open GDAL dataset."""
    from osgeo import gdal  # type: ignore[import-untyped]

    gt = ds.GetGeoTransform()
    px = int(math.floor((x - gt[0]) / gt[1]))
    py = int(math.floor((y - gt[3]) / gt[5]))
    if not (0 <= px < ds.RasterXSize and 0 <= py < ds.RasterYSize):
        return None
    band = ds.GetRasterBand(1)
    nodata = band.GetNoDataValue()
    raw = band.ReadRaster(px, py, 1, 1, 1, 1, gdal.GDT_Float32)
    if not raw:
        return None
    val = float(struct.unpack("=f", raw)[0])
    if not math.isfinite(val):
        return None
    if nodata is not None and abs(val - nodata) < 1e-3:
        return None
    return val


def _make_ring_linestring(cx: float, cy: float, radius_m: float, segments: int = 96) -> Any:
    """Build a closed 2D OGR ``LineString`` ring centered at ``(cx, cy)`` in projected meters."""
    from osgeo import ogr  # type: ignore[import-untyped]

    line = ogr.Geometry(ogr.wkbLineString)
    for i in range(segments + 1):
        theta = (2.0 * math.pi * i) / segments
        line.AddPoint_2D(cx + radius_m * math.cos(theta), cy + radius_m * math.sin(theta))
    line.FlattenTo2D()
    return line


def _clip_line_to_extent(line_geom: Any, min_x: float, min_y: float, max_x: float, max_y: float) -> list[Any]:
    """Clip a 2D OGR ``LineString`` to an extent box and return non-empty 2D ``LineString`` parts."""
    from osgeo import ogr  # type: ignore[import-untyped]

    ring = ogr.Geometry(ogr.wkbLinearRing)
    ring.AddPoint_2D(min_x, min_y)
    ring.AddPoint_2D(max_x, min_y)
    ring.AddPoint_2D(max_x, max_y)
    ring.AddPoint_2D(min_x, max_y)
    ring.AddPoint_2D(min_x, min_y)
    clip_poly = ogr.Geometry(ogr.wkbPolygon)
    clip_poly.AddGeometry(ring)

    clipped = line_geom.Intersection(clip_poly)
    if clipped is None or clipped.IsEmpty():
        return []

    parts: list[Any] = []
    gtype = ogr.GT_Flatten(clipped.GetGeometryType())
    if gtype == ogr.wkbLineString:
        clipped.FlattenTo2D()
        if clipped.GetPointCount() >= 2:
            parts.append(clipped)
    elif gtype in (ogr.wkbMultiLineString, ogr.wkbGeometryCollection):
        for i in range(clipped.GetGeometryCount()):
            sub = clipped.GetGeometryRef(i)
            if sub is None or sub.IsEmpty():
                continue
            if ogr.GT_Flatten(sub.GetGeometryType()) == ogr.wkbLineString:
                clone = sub.Clone()
                clone.FlattenTo2D()
                if clone.GetPointCount() >= 2:
                    parts.append(clone)
    return parts


def _write_viewshed_observers_gpkg(
    observers_meta: list[tuple[ViewshedObserver, float, float, float | None]],
    utm_epsg: int,
    dem_extent: tuple[float, float, float, float],
    output_gpkg: Path,
) -> Path:
    """Write ``viewshed_observers`` (Point) and ``viewshed_rings`` (LineString) layers to GeoPackage."""
    from osgeo import ogr, osr  # type: ignore[import-untyped]

    ogr.UseExceptions()
    osr.UseExceptions()

    for suffix in ("", "-wal", "-shm"):
        p = Path(f"{output_gpkg}{suffix}")
        p.unlink(missing_ok=True)

    srs = osr.SpatialReference()
    srs.ImportFromEPSG(int(utm_epsg))
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

    driver = ogr.GetDriverByName("GPKG")
    ds = driver.CreateDataSource(str(output_gpkg))
    if ds is None:
        raise RuntimeError(f"Could not create viewshed observers GeoPackage: {output_gpkg}")

    min_x, min_y, max_x, max_y = dem_extent
    try:
        obs_layer = ds.CreateLayer("viewshed_observers", srs=srs, geom_type=ogr.wkbPoint)
        obs_layer.CreateField(ogr.FieldDefn("label", ogr.OFTString))
        obs_layer.CreateField(ogr.FieldDefn("observer_height_m", ogr.OFTReal))
        obs_layer.CreateField(ogr.FieldDefn("target_height_m", ogr.OFTReal))
        obs_layer.CreateField(ogr.FieldDefn("max_distance_m", ogr.OFTReal))
        obs_layer.CreateField(ogr.FieldDefn("elev_m", ogr.OFTReal))

        ring_layer = ds.CreateLayer("viewshed_rings", srs=srs, geom_type=ogr.wkbLineString)
        ring_layer.CreateField(ogr.FieldDefn("label", ogr.OFTString))
        ring_layer.CreateField(ogr.FieldDefn("max_distance_m", ogr.OFTReal))

        obs_defn = obs_layer.GetLayerDefn()
        ring_defn = ring_layer.GetLayerDefn()

        for obs, ox, oy, elev_m in observers_meta:
            pt = ogr.Geometry(ogr.wkbPoint)
            pt.AddPoint_2D(ox, oy)
            pt.FlattenTo2D()
            feat = ogr.Feature(obs_defn)
            feat.SetGeometry(pt)
            feat.SetField("label", obs.label)
            feat.SetField("observer_height_m", float(obs.observer_height_m))
            feat.SetField("target_height_m", float(obs.target_height_m))
            feat.SetField("max_distance_m", float(obs.max_distance_m))
            if elev_m is not None:
                feat.SetField("elev_m", round(float(elev_m), 2))
            obs_layer.CreateFeature(feat)
            feat = None

            raw_ring = _make_ring_linestring(ox, oy, float(obs.max_distance_m))
            for part in _clip_line_to_extent(raw_ring, min_x, min_y, max_x, max_y):
                rfeat = ogr.Feature(ring_defn)
                rfeat.SetGeometry(part)
                rfeat.SetField("label", obs.label)
                rfeat.SetField("max_distance_m", float(obs.max_distance_m))
                ring_layer.CreateFeature(rfeat)
                rfeat = None

        ds.FlushCache()
    finally:
        ds = None

    return output_gpkg


def build_viewshed_products(
    dem_utm_tif: Path,
    observers: list[ViewshedObserver],
    utm_epsg: int,
    color_mode: ColorMode,
    work_dir: Path,
) -> tuple[Path, Path]:
    """Compute RF / line-of-sight viewshed RGBA GeoTIFF and observer/range-ring GeoPackage.

    Runs ``gdal_viewshed`` for each observer whose projected UTM coordinate lies within
    ``dem_utm_tif``, merges multiple observers via elementwise maximum (union of visible
    terrain), colorizes visible pixels into a 4-band RGBA Byte GeoTIFF (transparent where
    shadowed or out of range), and writes observer points + clipped max-range rings into
    ``work_dir / "viewshed_observers.gpkg"``.
    """
    from osgeo import gdal  # type: ignore[import-untyped]
    from pyproj import Transformer

    gdal.UseExceptions()
    work_dir.mkdir(parents=True, exist_ok=True)
    viewshed_tif = work_dir / "viewshed.tif"
    observers_gpkg = work_dir / "viewshed_observers.gpkg"

    src_ds = gdal.Open(str(dem_utm_tif), gdal.GA_ReadOnly)
    if src_ds is None:
        raise RuntimeError(f"GDAL could not open UTM DEM for viewshed: {dem_utm_tif}")

    to_utm = Transformer.from_crs("EPSG:4326", f"EPSG:{int(utm_epsg)}", always_xy=True)
    temp_files: list[Path] = []
    observers_meta: list[tuple[ViewshedObserver, float, float, float | None]] = []

    try:
        cols = src_ds.RasterXSize
        rows = src_ds.RasterYSize
        gt = src_ds.GetGeoTransform()
        proj = src_ds.GetProjection()
        px_w = abs(gt[1])
        px_h = abs(gt[5])
        min_x = gt[0]
        max_x = gt[0] + cols * gt[1]
        max_y = gt[3]
        min_y = gt[3] + rows * gt[5]
        if min_x > max_x:
            min_x, max_x = max_x, min_x
        if min_y > max_y:
            min_y, max_y = max_y, min_y

        import numpy as np  # type: ignore[import-not-found]

        combined_mask = np.zeros((rows, cols), dtype=np.uint8)

        for idx, obs in enumerate(observers):
            ox, oy = to_utm.transform(float(obs.lon), float(obs.lat))
            elev_m = _sample_dem_point_elev(src_ds, float(ox), float(oy))
            observers_meta.append((obs, float(ox), float(oy), elev_m))

            if not (min_x <= ox <= max_x and min_y <= oy <= max_y):
                logger.warning(
                    "Viewshed observer '%s' (%.5f, %.5f -> UTM %.1f, %.1f) lies outside DEM extent "
                    "[%.1f..%.1f, %.1f..%.1f]; skipping LOS raster for this observer",
                    obs.label,
                    obs.lat,
                    obs.lon,
                    ox,
                    oy,
                    min_x,
                    max_x,
                    min_y,
                    max_y,
                )
                continue

            if elev_m is None:
                logger.warning(
                    "Viewshed observer '%s' sits on DEM nodata at UTM (%.1f, %.1f); skipping",
                    obs.label,
                    ox,
                    oy,
                )
                continue

            tmp_obs_tif = work_dir / f"_viewshed_obs_{idx}.tif"
            temp_files.append(tmp_obs_tif)
            cmd = [
                "gdal_viewshed",
                "-ox",
                f"{ox:.3f}",
                "-oy",
                f"{oy:.3f}",
                "-oz",
                f"{obs.observer_height_m:g}",
                "-tz",
                f"{obs.target_height_m:g}",
                "-md",
                f"{obs.max_distance_m:g}",
                "-vv",
                "255",
                "-iv",
                "0",
                "-ov",
                "0",
                "-f",
                "GTiff",
                str(dem_utm_tif),
                str(tmp_obs_tif),
            ]
            logger.info(
                "Computing GDAL viewshed for '%s' (oz=%gm, tz=%gm, md=%gm)",
                obs.label,
                obs.observer_height_m,
                obs.target_height_m,
                obs.max_distance_m,
            )
            _run_subprocess(cmd, context=f"gdal_viewshed ({obs.label})")

            obs_ds = gdal.Open(str(tmp_obs_tif), gdal.GA_ReadOnly)
            if obs_ds is None:
                continue
            try:
                obs_gt = obs_ds.GetGeoTransform()
                obs_arr = obs_ds.GetRasterBand(1).ReadAsArray()
                if obs_arr is None:
                    continue
                obs_rows, obs_cols = obs_arr.shape
                x_off = int(round((obs_gt[0] - gt[0]) / gt[1]))
                y_off = int(round((obs_gt[3] - gt[3]) / gt[5]))

                src_x0 = max(0, -x_off)
                src_y0 = max(0, -y_off)
                dst_x0 = max(0, x_off)
                dst_y0 = max(0, y_off)

                copy_w = min(obs_cols - src_x0, cols - dst_x0)
                copy_h = min(obs_rows - src_y0, rows - dst_y0)
                if copy_w > 0 and copy_h > 0:
                    sub = obs_arr[src_y0 : src_y0 + copy_h, src_x0 : src_x0 + copy_w]
                    np.maximum(
                        combined_mask[dst_y0 : dst_y0 + copy_h, dst_x0 : dst_x0 + copy_w],
                        np.where(sub > 0, np.uint8(255), np.uint8(0)),
                        out=combined_mask[dst_y0 : dst_y0 + copy_h, dst_x0 : dst_x0 + copy_w],
                    )
            finally:
                obs_ds = None

        # Emit 4-band RGBA Byte GeoTIFF aligned 1:1 with dem_utm_tif
        r_val, g_val, b_val, a_val = (
            VIEWSHED_RGBA_BW if color_mode == ColorMode.BW else VIEWSHED_RGBA_COLOR
        )
        visible_bool = combined_mask > 0
        driver = gdal.GetDriverByName("GTiff")
        dst_ds = driver.Create(
            str(viewshed_tif),
            cols,
            rows,
            4,
            gdal.GDT_Byte,
            options=["COMPRESS=DEFLATE", "TILED=YES", "PHOTOMETRIC=RGB", "ALPHA=YES"],
        )
        if dst_ds is None:
            raise RuntimeError(f"Could not create viewshed RGBA GeoTIFF at {viewshed_tif}")
        try:
            dst_ds.SetGeoTransform(gt)
            dst_ds.SetProjection(proj)
            interps = (
                gdal.GCI_RedBand,
                gdal.GCI_GreenBand,
                gdal.GCI_BlueBand,
                gdal.GCI_AlphaBand,
            )
            for b_idx, (val, interp) in enumerate(zip((r_val, g_val, b_val, a_val), interps), start=1):
                band = dst_ds.GetRasterBand(b_idx)
                band.SetColorInterpretation(interp)
                arr = np.where(visible_bool, np.uint8(val), np.uint8(0))
                band.WriteArray(arr)
                band.FlushCache()
            dst_ds.FlushCache()
        finally:
            dst_ds = None

        _write_viewshed_observers_gpkg(
            observers_meta,
            utm_epsg=utm_epsg,
            dem_extent=(min_x, min_y, max_x, max_y),
            output_gpkg=observers_gpkg,
        )
        logger.info(
            "Built viewshed products (%d observer(s), visible=%.2f km²) -> %s",
            len(observers),
            float(np.count_nonzero(visible_bool)) * px_w * px_h / 1_000_000.0,
            viewshed_tif,
        )
    finally:
        src_ds = None
        for tmp_p in temp_files:
            tmp_p.unlink(missing_ok=True)

    return viewshed_tif, observers_gpkg


def compute_viewshed_geojson(
    dem_utm_tif: Path,
    observer: ViewshedObserver,
    utm_epsg: int,
    work_dir: Path,
    preview_res_m: float = 30.0,
) -> dict[str, Any]:
    """Compute a fast polygonized WGS84 GeoJSON viewshed FeatureCollection for interactive preview.

    Downsamples fine DEMs to ``preview_res_m`` (~30 m) before running ``gdal_viewshed`` so
    browser previews return in < 1.5 s, polygonizes visible pixels, simplifies topologies,
    and reprojects polygons to ``EPSG:4326`` along with ``visible_area_km2`` and ``observer_elev_m``.
    """
    import json

    from osgeo import gdal, ogr, osr  # type: ignore[import-untyped]
    from pyproj import Transformer

    gdal.UseExceptions()
    ogr.UseExceptions()
    osr.UseExceptions()

    work_dir.mkdir(parents=True, exist_ok=True)
    tmp_dem = work_dir / "_viewshed_preview_dem.tif"
    tmp_vs = work_dir / "_viewshed_preview_raw.tif"

    to_utm = Transformer.from_crs("EPSG:4326", f"EPSG:{int(utm_epsg)}", always_xy=True)
    ox, oy = to_utm.transform(float(observer.lon), float(observer.lat))

    src_ds = gdal.Open(str(dem_utm_tif), gdal.GA_ReadOnly)
    if src_ds is None:
        raise RuntimeError(f"GDAL could not open UTM DEM for viewshed preview: {dem_utm_tif}")

    try:
        src_gt = src_ds.GetGeoTransform()
        native_res = min(abs(src_gt[1]), abs(src_gt[5]))
        min_x = src_gt[0]
        max_x = src_gt[0] + src_ds.RasterXSize * src_gt[1]
        max_y = src_gt[3]
        min_y = src_gt[3] + src_ds.RasterYSize * src_gt[5]
        if min_x > max_x:
            min_x, max_x = max_x, min_x
        if min_y > max_y:
            min_y, max_y = max_y, min_y
        observer_elev_m = _sample_dem_point_elev(src_ds, float(ox), float(oy))
    finally:
        src_ds = None

    if not (min_x <= ox <= max_x and min_y <= oy <= max_y) or observer_elev_m is None:
        return {
            "type": "FeatureCollection",
            "properties": {
                "label": observer.label,
                "observer_height_m": float(observer.observer_height_m),
                "target_height_m": float(observer.target_height_m),
                "max_distance_m": float(observer.max_distance_m),
                "observer_elev_m": round(observer_elev_m, 2) if observer_elev_m is not None else None,
                "visible_area_km2": 0.0,
            },
            "features": [],
        }

    try:
        dem_for_vs = dem_utm_tif
        eff_res = max(native_res, float(preview_res_m))
        if native_res > 0 and native_res < preview_res_m * 0.95:
            warp_cmd = [
                "gdalwarp",
                "-overwrite",
                "-tr",
                f"{preview_res_m:g}",
                f"{preview_res_m:g}",
                "-r",
                "bilinear",
                "-dstnodata",
                str(DEM_NODATA),
                "-ot",
                "Float32",
                str(dem_utm_tif),
                str(tmp_dem),
            ]
            _run_subprocess(warp_cmd, context="gdalwarp (viewshed preview downsample)")
            dem_for_vs = tmp_dem

        vs_cmd = [
            "gdal_viewshed",
            "-ox",
            f"{ox:.3f}",
            "-oy",
            f"{oy:.3f}",
            "-oz",
            f"{observer.observer_height_m:g}",
            "-tz",
            f"{observer.target_height_m:g}",
            "-md",
            f"{observer.max_distance_m:g}",
            "-vv",
            "255",
            "-iv",
            "0",
            "-ov",
            "0",
            "-f",
            "GTiff",
            str(dem_for_vs),
            str(tmp_vs),
        ]
        _run_subprocess(vs_cmd, context="gdal_viewshed (preview)")

        vs_ds = gdal.Open(str(tmp_vs), gdal.GA_ReadOnly)
        if vs_ds is None:
            raise RuntimeError(f"GDAL could not open preview viewshed raster: {tmp_vs}")

        try:
            import numpy as np  # type: ignore[import-not-found]

            band = vs_ds.GetRasterBand(1)
            vs_gt = vs_ds.GetGeoTransform()
            arr = band.ReadAsArray()
            visible_px = int(np.count_nonzero(arr > 0)) if arr is not None else 0
            visible_area_km2 = round(visible_px * abs(vs_gt[1] * vs_gt[5]) / 1_000_000.0, 4)

            utm_srs = osr.SpatialReference()
            utm_srs.ImportFromEPSG(int(utm_epsg))
            utm_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

            wgs84_srs = osr.SpatialReference()
            wgs84_srs.ImportFromEPSG(4326)
            wgs84_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            coord_tx = osr.CoordinateTransformation(utm_srs, wgs84_srs)

            mem_drv = ogr.GetDriverByName("Memory")
            mem_ds = mem_drv.CreateDataSource("viewshed_mem")
            mem_layer = mem_ds.CreateLayer("visible", srs=utm_srs, geom_type=ogr.wkbPolygon)
            mem_layer.CreateField(ogr.FieldDefn("val", ogr.OFTInteger))

            # Passing band as both source and mask polygonizes only non-zero (visible) cells.
            gdal.Polygonize(band, band, mem_layer, 0, [], callback=None)

            simplify_tol = max(10.0, eff_res * 0.75)
            features: list[dict[str, Any]] = []
            for feat in mem_layer:
                if feat.GetField("val") != 255:
                    continue
                geom = feat.GetGeometryRef()
                if geom is None or geom.IsEmpty():
                    continue
                simp = geom.SimplifyPreserveTopology(simplify_tol)
                if simp is None or simp.IsEmpty():
                    simp = geom.Clone()
                simp.FlattenTo2D()
                simp.Transform(coord_tx)
                features.append(
                    {
                        "type": "Feature",
                        "properties": {
                            "label": observer.label,
                            "observer_height_m": float(observer.observer_height_m),
                            "target_height_m": float(observer.target_height_m),
                            "max_distance_m": float(observer.max_distance_m),
                            "observer_elev_m": round(float(observer_elev_m), 2),
                            "visible_area_km2": visible_area_km2,
                        },
                        "geometry": json.loads(simp.ExportToJson()),
                    }
                )
            mem_ds = None
        finally:
            vs_ds = None
    finally:
        tmp_dem.unlink(missing_ok=True)
        tmp_vs.unlink(missing_ok=True)

    return {
        "type": "FeatureCollection",
        "properties": {
            "label": observer.label,
            "observer_height_m": float(observer.observer_height_m),
            "target_height_m": float(observer.target_height_m),
            "max_distance_m": float(observer.max_distance_m),
            "observer_elev_m": round(float(observer_elev_m), 2),
            "visible_area_km2": visible_area_km2,
        },
        "features": features,
    }


def build_slope_shading(
    dem_utm_tif: Path,
    color_mode: ColorMode,
    work_dir: Path,
) -> Path:
    """Compute discrete avalanche slope-angle shading RGBA GeoTIFF from ``dem_utm_tif``.

    Uses ``gdaldem slope -compute_edges`` followed by ``gdaldem color-relief -alpha``
    with tightly-stepped degree bands so flat terrain is fully transparent and steep
    backcountry slopes (>= 27° Color / >= 30° B&W) render as crisp discrete bands.
    Temporary slope rasters and color tables are removed after generation.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    slope_deg_tif = work_dir / "slope_deg.tif"
    color_table_txt = work_dir / "slope_color_table.txt"
    slope_tif = work_dir / "slope_shading.tif"

    table_text = SLOPE_COLOR_TABLE_BW if color_mode == ColorMode.BW else SLOPE_COLOR_TABLE_COLOR
    try:
        slope_cmd = [
            "gdaldem",
            "slope",
            "-compute_edges",
            "-of",
            "GTiff",
            "-co",
            "COMPRESS=DEFLATE",
            "-co",
            "TILED=YES",
            str(dem_utm_tif),
            str(slope_deg_tif),
        ]
        logger.info("Computing terrain slope in degrees -> %s", slope_deg_tif)
        _run_subprocess(slope_cmd, context="gdaldem slope")

        color_table_txt.write_text(table_text, encoding="ascii")
        relief_cmd = [
            "gdaldem",
            "color-relief",
            "-alpha",
            "-of",
            "GTiff",
            "-co",
            "COMPRESS=DEFLATE",
            "-co",
            "TILED=YES",
            str(slope_deg_tif),
            str(color_table_txt),
            str(slope_tif),
        ]
        logger.info("Generating %s slope-angle shading RGBA raster -> %s", color_mode.value, slope_tif)
        _run_subprocess(relief_cmd, context="gdaldem color-relief (slope shading)")
    finally:
        slope_deg_tif.unlink(missing_ok=True)
        color_table_txt.unlink(missing_ok=True)

    return slope_tif


def _run_subprocess(cmd: list[str], context: str) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"{context} failed (exit {proc.returncode}):\n"
            f"cmd: {' '.join(cmd)}\n"
            f"stderr: {proc.stderr.strip()}"
        )


def build_terrain_products(
    dem_raw_path: Path,
    layout: SheetLayout,
    work_dir: Path,
) -> TerrainResult:
    """Warp DEM to local UTM, generate multi-directional hillshade, and build indexed contours.

    Honors ``layout.spec.carto.contour_density``, ``layout.spec.carto.hillshade_strength``,
    and preserves fine LiDAR resolution (``2.0 m``, ``-z 1.0``) when input DEM pixels are
    ``<= 3.0 m`` (e.g. USGS 3DEP 1 m).
    """
    src_dem = Path(dem_raw_path).expanduser().resolve()
    if not src_dem.is_file():
        raise FileNotFoundError(f"Input DEM does not exist: {src_dem}")

    work_dir.mkdir(parents=True, exist_ok=True)
    dem_utm_tif = work_dir / "dem_utm.tif"
    hillshade_tif = work_dir / "hillshade.tif"
    terrain_gpkg = work_dir / "terrain.gpkg"

    native_res_m = _estimate_native_resolution_m(src_dem, layout.spec.center_lat)
    target_res, z_factor = choose_warp_resolution_and_z(native_res_m)
    b = layout.utm_fetch_bbox

    # 1. Warp raw DEM to exact sheet UTM CRS with smooth cubic resampling
    warp_cmd = [
        "gdalwarp",
        "-overwrite",
        "-t_srs",
        f"EPSG:{layout.utm_epsg}",
        "-te",
        f"{b.min_x}",
        f"{b.min_y}",
        f"{b.max_x}",
        f"{b.max_y}",
        "-tr",
        f"{target_res}",
        f"{target_res}",
        "-r",
        "cubic",
        "-dstnodata",
        str(DEM_NODATA),
        "-ot",
        "Float32",
        "-co",
        "COMPRESS=DEFLATE",
        "-co",
        "TILED=YES",
        str(src_dem),
        str(dem_utm_tif),
    ]
    logger.info(
        "Warping DEM (native %.2f m) to EPSG:%d at %.2f m resolution -> %s",
        native_res_m,
        layout.utm_epsg,
        target_res,
        dem_utm_tif,
    )
    _run_subprocess(warp_cmd, context="gdalwarp (DEM -> UTM)")

    # 2. Extract terrain elevation statistics and sheet-center altitude for WMM
    min_elev, max_elev, center_elev_m = _compute_dem_stats_and_center(
        dem_utm_tif,
        layout.center_easting,
        layout.center_northing,
    )
    relief_m = max(0.0, max_elev - min_elev)

    # 3. Multi-directional hillshade (skip cleanly with flat-white neutral raster when OFF)
    if layout.spec.carto.hillshade_strength == HillshadeStrength.OFF:
        logger.info("Hillshade strength is OFF; writing neutral white raster -> %s", hillshade_tif)
        _write_neutral_hillshade(dem_utm_tif, hillshade_tif)
    else:
        hillshade_cmd = [
            "gdaldem",
            "hillshade",
            "-multidirectional",
            "-z",
            f"{z_factor:g}",
            "-compute_edges",
            "-of",
            "GTiff",
            "-co",
            "COMPRESS=DEFLATE",
            "-co",
            "TILED=YES",
            str(dem_utm_tif),
            str(hillshade_tif),
        ]
        logger.info(
            "Generating multi-directional hillshade (z=%g) -> %s",
            z_factor,
            hillshade_tif,
        )
        _run_subprocess(hillshade_cmd, context="gdaldem hillshade")

    # 4. Generate contours honoring units + carto.contour_density
    interval, index_mult, interval_display = choose_contour_interval(
        layout.spec.scale,
        layout.spec.units,
        relief_m,
        density=layout.spec.carto.contour_density,
    )
    index_interval = interval * index_mult

    dem_for_contours = dem_utm_tif
    if layout.spec.units == Units.IMPERIAL:
        dem_for_contours = _scale_dem_meters_to_feet(dem_utm_tif, work_dir / "dem_utm_ft.tif")

    if terrain_gpkg.exists():
        terrain_gpkg.unlink()

    contour_cmd = [
        "gdal_contour",
        "-a",
        "elev",
        "-i",
        f"{interval:g}",
        "-f",
        "GPKG",
        "-nln",
        "contours",
        str(dem_for_contours),
        str(terrain_gpkg),
    ]
    logger.info("Generating contours (%s) -> %s", interval_display, terrain_gpkg)
    _run_subprocess(contour_cmd, context="gdal_contour")

    # 5. Tag index contours (is_index) and clean integer labels (elev_label)
    _postprocess_contours_gpkg(terrain_gpkg, index_interval=index_interval)

    # 6. Optional avalanche / steepness slope-angle shading RGBA GeoTIFF
    slope_tif: Path | None = None
    if layout.spec.carto.show_slope_shading:
        slope_tif = build_slope_shading(
            dem_utm_tif,
            color_mode=layout.spec.color_mode,
            work_dir=work_dir,
        )

    # 7. Optional RF / line-of-sight viewshed RGBA raster & observer/range-ring GeoPackage
    viewshed_tif: Path | None = None
    viewshed_observers_gpkg: Path | None = None
    if layout.spec.viewsheds:
        viewshed_tif, viewshed_observers_gpkg = build_viewshed_products(
            dem_utm_tif,
            list(layout.spec.viewsheds),
            layout.utm_epsg,
            layout.spec.color_mode,
            work_dir,
        )

    return TerrainResult(
        dem_utm_tif=dem_utm_tif,
        hillshade_tif=hillshade_tif,
        terrain_gpkg=terrain_gpkg,
        contour_interval_display=interval_display,
        contour_interval=interval,
        index_interval=index_interval,
        min_elev=min_elev,
        max_elev=max_elev,
        center_elev_m=center_elev_m,
        slope_tif=slope_tif,
        viewshed_tif=viewshed_tif,
        viewshed_observers_gpkg=viewshed_observers_gpkg,
    )
