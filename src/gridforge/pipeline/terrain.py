"""GDAL terrain processing: UTM DEM warp, multi-directional hillshade, and indexed contours."""

from __future__ import annotations

from array import array
from concurrent.futures import ThreadPoolExecutor
import logging
import math
import shutil
import struct
import subprocess
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel

from gridforge.config import (
    ColorMode,
    DensityLevel,
    HillshadeStrength,
    RFLinkSpec,
    SheetLayout,
    Units,
    ViewshedObserver,
)
from gridforge.subprocess_env import build_safe_subprocess_env

logger = logging.getLogger(__name__)

FEET_PER_METER: Final[float] = 3.280839895013123
DEM_NODATA: Final[float] = -9999.0
DEFAULT_TARGET_RES_M: Final[float] = 10.0
MIN_TARGET_RES_M: Final[float] = 1.0
STANDARD_MIN_TARGET_RES_M: Final[float] = 2.0
LIDAR_NATIVE_THRESHOLD_M: Final[float] = 3.0
HIGH_RES_CONTOUR_DEM_THRESHOLD_M: Final[float] = 10.0
MAX_DEM_WARP_DIM_PX: Final[int] = 4096


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
    *,
    native_res_m: float | None = None,
) -> tuple[float, int, str]:
    """Select military topographic contour interval, index multiplier, and collar display text.

    Honors ``density`` and supports ultra-fine ``2 m`` / ``5 ft`` contours on fine
    tactical scales (``<= 1:12,500``) when backed by high-resolution DEMs (``<= 10 m``):
    - ``LOW``: ``2x`` normal interval
    - ``NORMAL``: standard military topographic interval (``5 m`` / ``10 ft`` at ``<= 1:12.5k``)
    - ``HIGH``: ``0.5x`` normal interval (down to ``2 m`` / ``5 ft`` on fine LiDAR scales,
      otherwise clamped to ``5 m`` / ``10 ft``)

    Returns:
        ``(interval_in_unit, index_multiplier, display_str)`` where ``index_multiplier`` is ``5``.
    """
    index_mult = 5
    can_ultra_fine = (
        scale <= 12500
        and (native_res_m is None or native_res_m <= HIGH_RES_CONTOUR_DEM_THRESHOLD_M)
    )

    if units == Units.IMPERIAL:
        if scale <= 12500:
            base = 10.0
            min_interval = 5.0 if can_ultra_fine else 10.0
        elif scale <= 15000:
            base = 20.0
            min_interval = 10.0
        elif scale <= 25000:
            base = 40.0
            min_interval = 10.0
        else:
            base = 80.0
            min_interval = 10.0
        unit_label = "ft"
    else:
        if scale <= 12500:
            base = 5.0
            min_interval = 2.0 if can_ultra_fine else 5.0
        elif scale <= 15000:
            base = 10.0
            min_interval = 5.0
        elif scale <= 25000:
            base = 20.0 if relief_m > 1200.0 else 10.0
            min_interval = 5.0
        else:
            base = 20.0
            min_interval = 5.0
        unit_label = "m"

    if density == DensityLevel.LOW:
        interval = base * 2.0
    elif density == DensityLevel.HIGH:
        half = base * 0.5
        # Snap 2.5 m -> standard cartographic 2 m contour interval when ultra-fine allowed.
        if units == Units.METRIC and half < 3.0 and min_interval <= 2.0:
            interval = 2.0
        else:
            interval = max(min_interval, half)
    else:
        interval = base

    index_val = int(round(interval * index_mult))
    return (interval, index_mult, f"{int(round(interval))} {unit_label} (index {index_val} {unit_label})")


def choose_warp_resolution_and_z(
    native_res_m: float,
    *,
    scale: int = 25000,
    max_extent_m: float | None = None,
) -> tuple[float, float]:
    """Choose ``(target_res_m, hillshade_z_factor)`` from input DEM native pixel resolution.

    Preserves true ``1.0 m`` LiDAR micro-terrain on fine tactical scales (``<= 1:15,000``)
    and crisp ``2.0 m`` LiDAR on standard scales (``> 1:15,000``) when input DEM resolution
    is ``<= 3.0 m`` (e.g., USGS 3DEP 1 m, Canada HRDEM 1 m, NZ LINZ 1 m, QLD 1 m LiDAR),
    while enforcing ``MAX_DEM_WARP_DIM_PX`` (4096 px) to keep worker memory safely < 2 GB.
    """
    if native_res_m <= LIDAR_NATIVE_THRESHOLD_M:
        floor_res = MIN_TARGET_RES_M if scale <= 15000 else STANDARD_MIN_TARGET_RES_M
        target = max(floor_res, round(native_res_m, 2))
        z_factor = 1.0
    elif native_res_m < DEFAULT_TARGET_RES_M:
        target = round(native_res_m, 2)
        z_factor = 1.15
    else:
        target = DEFAULT_TARGET_RES_M
        z_factor = 1.15

    if max_extent_m is not None and max_extent_m > 0.0:
        min_safe_res = math.ceil((max_extent_m / float(MAX_DEM_WARP_DIM_PX)) * 10.0) / 10.0
        target = max(target, round(min_safe_res, 2))

    return (target, z_factor)


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


def _postprocess_contours_gpkg(
    terrain_gpkg: Path,
    index_interval: float,
    *,
    elev_scale: float = 1.0,
) -> None:
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
        scale_f = float(elev_scale)
        if abs(scale_f - 1.0) > 1e-9:
            ds.ExecuteSQL(
                f"""
                UPDATE contours
                SET elev = ROUND(elev * {scale_f:.15g}, 4)
                """
            )
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

# Red-headlamp-safe slope shading (cool dark slate/navy tones that remain legible under red LED)
SLOPE_COLOR_TABLE_NIGHT_RED: Final[str] = """\
nv 0 0 0 0
0.0 0 0 0 0
29.999 0 0 0 0
30.0 71 85 105 110
34.999 71 85 105 110
35.0 30 58 95 150
44.999 30 58 95 150
45.0 15 23 42 185
90.0 15 23 42 185
"""

VIEWSHED_RGBA_COLOR: Final[tuple[int, int, int, int]] = (0, 212, 146, 130)
VIEWSHED_RGBA_BW: Final[tuple[int, int, int, int]] = (80, 80, 80, 95)
VIEWSHED_RGBA_NIGHT_RED: Final[tuple[int, int, int, int]] = (0, 168, 196, 120)
SPEED_OF_LIGHT_M_S: Final[float] = 299_792_458.0
EARTH_RADIUS_M: Final[float] = 6_371_000.0


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

        ds.StartTransaction()
        try:
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
            ds.CommitTransaction()
        except Exception:
            ds.RollbackTransaction()
            raise

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

        # On 1–2 m LiDAR DEMs (up to 4096x4096), downsample viewshed input to 10 m so
        # gdal_viewshed completes in < 1s while retaining sub-mm print accuracy.
        vs_dem_path = dem_utm_tif
        vs_ds = src_ds
        if min(px_w, px_h) < 9.5:
            vs_res = 10.0
            tmp_vs_dem = work_dir / "_viewshed_dem_10m.tif"
            temp_files.append(tmp_vs_dem)
            _run_subprocess(
                [
                    "gdalwarp",
                    "-overwrite",
                    "-multi",
                    "-wo",
                    "NUM_THREADS=2",
                    "-tr",
                    f"{vs_res:g}",
                    f"{vs_res:g}",
                    "-r",
                    "bilinear",
                    "-dstnodata",
                    str(DEM_NODATA),
                    "-ot",
                    "Float32",
                    "-co",
                    "COMPRESS=NONE",
                    "-co",
                    "TILED=YES",
                    str(dem_utm_tif),
                    str(tmp_vs_dem),
                ],
                context="gdalwarp (viewshed 10m downsample)",
            )
            vs_dem_path = tmp_vs_dem
            vs_ds = gdal.Open(str(tmp_vs_dem), gdal.GA_ReadOnly) or src_ds

        vs_cols = vs_ds.RasterXSize
        vs_rows = vs_ds.RasterYSize
        vs_gt = vs_ds.GetGeoTransform()
        vs_px_w = abs(vs_gt[1])
        vs_px_h = abs(vs_gt[5])
        combined_mask = np.zeros((vs_rows, vs_cols), dtype=np.uint8)

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
                str(vs_dem_path),
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
                x_off = int(round((obs_gt[0] - vs_gt[0]) / vs_gt[1]))
                y_off = int(round((obs_gt[3] - vs_gt[3]) / vs_gt[5]))

                src_x0 = max(0, -x_off)
                src_y0 = max(0, -y_off)
                dst_x0 = max(0, x_off)
                dst_y0 = max(0, y_off)

                copy_w = min(obs_cols - src_x0, vs_cols - dst_x0)
                copy_h = min(obs_rows - src_y0, vs_rows - dst_y0)
                if copy_w > 0 and copy_h > 0:
                    sub = obs_arr[src_y0 : src_y0 + copy_h, src_x0 : src_x0 + copy_w]
                    np.maximum(
                        combined_mask[dst_y0 : dst_y0 + copy_h, dst_x0 : dst_x0 + copy_w],
                        np.where(sub > 0, np.uint8(255), np.uint8(0)),
                        out=combined_mask[dst_y0 : dst_y0 + copy_h, dst_x0 : dst_x0 + copy_w],
                    )
            finally:
                obs_ds = None

        if vs_ds is not src_ds:
            vs_ds = None

        # Emit 4-band RGBA Byte GeoTIFF covering the DEM extent
        if color_mode == ColorMode.BW:
            r_val, g_val, b_val, a_val = VIEWSHED_RGBA_BW
        elif color_mode == ColorMode.NIGHT_RED:
            r_val, g_val, b_val, a_val = VIEWSHED_RGBA_NIGHT_RED
        else:
            r_val, g_val, b_val, a_val = VIEWSHED_RGBA_COLOR
        visible_bool = combined_mask > 0
        driver = gdal.GetDriverByName("GTiff")
        dst_ds = driver.Create(
            str(viewshed_tif),
            vs_cols,
            vs_rows,
            4,
            gdal.GDT_Byte,
            options=["COMPRESS=NONE", "TILED=YES", "PHOTOMETRIC=RGB", "ALPHA=YES"],
        )
        if dst_ds is None:
            raise RuntimeError(f"Could not create viewshed RGBA GeoTIFF at {viewshed_tif}")
        try:
            dst_ds.SetGeoTransform(vs_gt)
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
            float(np.count_nonzero(visible_bool)) * vs_px_w * vs_px_h / 1_000_000.0,
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


def _sample_dem_bilinear(ds: Any, x: float, y: float) -> float | None:
    """Bilinear Float32 elevation sample at projected ``(x, y)`` with nearest-valid fallback."""
    from osgeo import gdal  # type: ignore[import-untyped]

    gt = ds.GetGeoTransform()
    cols = ds.RasterXSize
    rows = ds.RasterYSize
    fx = (x - gt[0]) / gt[1] - 0.5
    fy = (y - gt[3]) / gt[5] - 0.5
    if fx < -0.5 or fx > cols - 0.5 or fy < -0.5 or fy > rows - 0.5:
        return None

    x0 = max(0, min(cols - 1, int(math.floor(fx))))
    y0 = max(0, min(rows - 1, int(math.floor(fy))))
    x1 = min(cols - 1, x0 + 1)
    y1 = min(rows - 1, y0 + 1)
    wx = max(0.0, min(1.0, fx - x0))
    wy = max(0.0, min(1.0, fy - y0))

    band = ds.GetRasterBand(1)
    nodata = band.GetNoDataValue()
    w = x1 - x0 + 1
    h = y1 - y0 + 1
    raw = band.ReadRaster(x0, y0, w, h, w, h, gdal.GDT_Float32)
    if not raw:
        return None
    vals = struct.unpack(f"={w * h}f", raw)

    def _valid(v: float) -> bool:
        return math.isfinite(v) and (nodata is None or abs(v - nodata) > 1e-3)

    if w == 2 and h == 2:
        v00, v10, v01, v11 = vals
        if _valid(v00) and _valid(v10) and _valid(v01) and _valid(v11):
            return float(
                v00 * (1.0 - wx) * (1.0 - wy)
                + v10 * wx * (1.0 - wy)
                + v01 * (1.0 - wx) * wy
                + v11 * wx * wy
            )

    good = [v for v in vals if _valid(v)]
    return float(sum(good) / len(good)) if good else None


def compute_rf_link_profile(
    dem_utm_tif: Path,
    link: RFLinkSpec,
    utm_epsg: int,
    *,
    date_iso: str | None = None,
    num_samples: int = 200,
) -> dict[str, Any]:
    """Compute a curved-Earth point-to-point RF Fresnel zone & LOS link profile over ``dem_utm_tif``.

    Implements ITU/standard radio refraction (default ``k = 4/3``), 1st Fresnel zone radius
    ``r1 = sqrt(lambda * d1 * d2 / (d1 + d2))``, 60% Fresnel clearance check, Free-Space Path
    Loss (``FSPL = 20*log10(d_km) + 20*log10(f_MHz) + 32.45``), and WMM2025 True / Grid /
    Magnetic bearings + NATO mils + 10 m MGRS coordinates.
    """
    import pyproj
    from osgeo import gdal  # type: ignore[import-untyped]
    from pygeomag import GeoMag
    from pyproj import Geod, Transformer

    from gridforge.geo.declination import (
        DEG_TO_NATO_MILS,
        WMM2025_MAX_YEAR,
        WMM2025_MIN_YEAR,
        _parse_date_to_decimal_year,
    )
    from gridforge.geo.strip_atlas import _format_mgrs_10m

    gdal.UseExceptions()
    ds = gdal.Open(str(dem_utm_tif), gdal.GA_ReadOnly)
    if ds is None:
        raise RuntimeError(f"GDAL could not open UTM DEM for RF link profile: {dem_utm_tif}")

    geod = Geod(ellps="WGS84")
    fwd_az, _back_az, dist_m = geod.inv(
        float(link.from_lon),
        float(link.from_lat),
        float(link.to_lon),
        float(link.to_lat),
    )
    dist_m = max(0.0, float(dist_m))
    distance_km = dist_m / 1000.0
    bearing_true_deg = float(fwd_az) % 360.0

    to_utm = Transformer.from_crs("EPSG:4326", f"EPSG:{int(utm_epsg)}", always_xy=True)
    x0, y0 = to_utm.transform(float(link.from_lon), float(link.from_lat))
    x1, y1 = to_utm.transform(float(link.to_lon), float(link.to_lat))

    n_pts = max(2, int(num_samples))
    raw_elevs: list[float | None] = []
    try:
        band = ds.GetRasterBand(1)
        try:
            _, _, mean_elev, _ = band.ComputeStatistics(False)
            fallback_elev = float(mean_elev) if math.isfinite(mean_elev) else 0.0
        except Exception:
            fallback_elev = 0.0

        for i in range(n_pts):
            t = i / (n_pts - 1)
            sx = float(x0) + t * (float(x1) - float(x0))
            sy = float(y0) + t * (float(y1) - float(y0))
            raw_elevs.append(_sample_dem_bilinear(ds, sx, sy))
    finally:
        ds = None

    # Fill any isolated nodata gaps smoothly from neighbors or raster mean
    valid_vals = [v for v in raw_elevs if v is not None]
    default_elev = valid_vals[0] if valid_vals else fallback_elev
    elevs: list[float] = []
    last_good = default_elev
    for v in raw_elevs:
        if v is not None:
            last_good = v
            elevs.append(v)
        else:
            elevs.append(last_good)

    from_elev_m = elevs[0]
    to_elev_m = elevs[-1]

    # True -> Grid & WMM2025 Magnetic bearings + NATO mils (uses cached WMM/Proj helpers)
    from gridforge.geo.declination import compute_point_declination

    decl_info = compute_point_declination(
        float(link.from_lat),
        float(link.from_lon),
        utm_epsg=int(utm_epsg),
        date_iso=date_iso,
        elevation_m=from_elev_m,
    )
    bearing_grid_deg = (bearing_true_deg - decl_info.grid_convergence_deg) % 360.0
    bearing_mag_deg = (bearing_true_deg - decl_info.magnetic_declination_deg) % 360.0
    bearing_mils = (bearing_mag_deg * DEG_TO_NATO_MILS) % 6400.0

    # Wavelength (m) and effective curved-Earth radius (m)
    freq_mhz = float(link.freq_mhz)
    wavelength_m = SPEED_OF_LIGHT_M_S / (freq_mhz * 1_000_000.0)
    r_eff_m = float(link.k_factor) * EARTH_RADIUS_M

    tx_ant_m = from_elev_m + float(link.from_height_m)
    rx_ant_m = to_elev_m + float(link.to_height_m)

    samples: list[dict[str, float]] = []
    interior_clearances: list[float] = []
    interior_fresnel_pcts: list[float] = []

    for i in range(n_pts):
        t = i / (n_pts - 1)
        d1 = t * dist_m
        d2 = (1.0 - t) * dist_m
        bulge_m = (d1 * d2) / (2.0 * r_eff_m) if r_eff_m > 0 else 0.0
        eff_terrain_m = elevs[i] + bulge_m
        los_m = tx_ant_m * (1.0 - t) + rx_ant_m * t

        r1 = math.sqrt((wavelength_m * d1 * d2) / dist_m) if (dist_m > 0.0 and d1 > 0.0 and d2 > 0.0) else 0.0
        fresnel_lo_m = los_m - r1
        fresnel_hi_m = los_m + r1
        fresnel60_lo_m = los_m - 0.6 * r1
        clearance_m = los_m - eff_terrain_m

        if 0 < i < n_pts - 1:
            interior_clearances.append(clearance_m)
            if r1 > 0.01:
                interior_fresnel_pcts.append((clearance_m / r1) * 100.0)

        samples.append(
            {
                "dist_km": round(d1 / 1000.0, 4),
                "terrain_m": round(eff_terrain_m, 2),
                "los_m": round(los_m, 2),
                "fresnel_lo_m": round(fresnel_lo_m, 2),
                "fresnel_hi_m": round(fresnel_hi_m, 2),
                "fresnel60_lo_m": round(fresnel60_lo_m, 2),
                "clearance_m": round(clearance_m, 2),
            }
        )

    if interior_clearances:
        min_clearance_m = min(interior_clearances)
    else:
        min_clearance_m = min(float(link.from_height_m), float(link.to_height_m))

    if interior_fresnel_pcts:
        min_fresnel_pct = min(interior_fresnel_pcts)
    else:
        min_fresnel_pct = 100.0 if min_clearance_m > 0.0 else 0.0

    los_clear = bool(min_clearance_m > 0.0)
    fresnel_60pct_clear = bool(los_clear and min_fresnel_pct >= 60.0)
    if fresnel_60pct_clear:
        status = "CLEAR"
    elif los_clear:
        status = "MARGINAL"
    else:
        status = "OBSTRUCTED"

    fspl_db = (
        20.0 * math.log10(distance_km) + 20.0 * math.log10(freq_mhz) + 32.44778
        if distance_km > 1e-6
        else 0.0
    )

    return {
        "status": status,
        "from_label": link.from_label,
        "to_label": link.to_label,
        "from_height_m": float(link.from_height_m),
        "to_height_m": float(link.to_height_m),
        "k_factor": float(link.k_factor),
        "distance_km": round(distance_km, 4),
        "freq_mhz": round(freq_mhz, 3),
        "fspl_db": round(fspl_db, 2),
        "min_clearance_m": round(min_clearance_m, 2),
        "min_fresnel_pct": round(min_fresnel_pct, 1),
        "los_clear": los_clear,
        "fresnel_60pct_clear": fresnel_60pct_clear,
        "bearing_true_deg": round(bearing_true_deg, 2),
        "bearing_grid_deg": round(bearing_grid_deg, 2),
        "bearing_mag_deg": round(bearing_mag_deg, 2),
        "bearing_mils": round(bearing_mils, 1),
        "from_elev_m": round(from_elev_m, 2),
        "to_elev_m": round(to_elev_m, 2),
        "from_mgrs": _format_mgrs_10m(float(link.from_lat), float(link.from_lon)),
        "to_mgrs": _format_mgrs_10m(float(link.to_lat), float(link.to_lon)),
        "samples": samples,
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

    if color_mode == ColorMode.BW:
        table_text = SLOPE_COLOR_TABLE_BW
    elif color_mode == ColorMode.NIGHT_RED:
        table_text = SLOPE_COLOR_TABLE_NIGHT_RED
    else:
        table_text = SLOPE_COLOR_TABLE_COLOR
    try:
        slope_cmd = [
            "gdaldem",
            "slope",
            "-compute_edges",
            "-of",
            "GTiff",
            "-co",
            "COMPRESS=NONE",
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
            "COMPRESS=NONE",
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


def _run_subprocess(cmd: list[str], context: str, *, timeout: int = 300) -> None:
    env = build_safe_subprocess_env(
        extra={"GDAL_NUM_THREADS": "2"},
        allow_network=False,
    )
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"{context} failed (exit {proc.returncode}):\n"
            f"cmd: {' '.join(cmd)}\n"
            f"stderr: {proc.stderr.strip()}"
        )


def _dem_matches_utm_target(
    src_dem: Path,
    *,
    utm_epsg: int,
    bbox_min_x: float,
    bbox_min_y: float,
    bbox_max_x: float,
    bbox_max_y: float,
    target_res: float,
) -> bool:
    """Return True if ``src_dem`` is already in exact target UTM EPSG, resolution, and extent."""
    from osgeo import gdal, osr  # type: ignore[import-untyped]  # GDAL system bindings lack stubs

    gdal.UseExceptions()
    ds = gdal.Open(str(src_dem), gdal.GA_ReadOnly)
    if ds is None:
        return False
    try:
        wkt = ds.GetProjection()
        if not wkt:
            return False
        src_srs = osr.SpatialReference(wkt=wkt)
        dst_srs = osr.SpatialReference()
        dst_srs.ImportFromEPSG(int(utm_epsg))
        if not src_srs.IsSame(dst_srs):
            return False
        gt = ds.GetGeoTransform()
        if abs(gt[2]) > 1e-9 or abs(gt[4]) > 1e-9:
            return False
        px_w = abs(gt[1])
        px_h = abs(gt[5])
        if abs(px_w - target_res) > max(0.05, target_res * 0.05) or abs(px_h - target_res) > max(0.05, target_res * 0.05):
            return False
        min_x = gt[0]
        max_x = gt[0] + ds.RasterXSize * gt[1]
        max_y = gt[3]
        min_y = gt[3] + ds.RasterYSize * gt[5]
        tol = max(2.0 * target_res, 5.0)
        return (
            abs(min_x - bbox_min_x) <= tol
            and abs(max_x - bbox_max_x) <= tol
            and abs(min_y - bbox_min_y) <= tol
            and abs(max_y - bbox_max_y) <= tol
        )
    except (RuntimeError, ValueError, OSError) as exc:
        logger.debug("Could not inspect DEM target match for %s: %s", src_dem, exc)
        return False
    finally:
        ds = None


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
    b = layout.utm_fetch_bbox
    target_res, z_factor = choose_warp_resolution_and_z(
        native_res_m,
        scale=layout.spec.scale,
        max_extent_m=max(b.width, b.height),
    )

    # 1. Warp raw DEM to exact sheet UTM CRS (skip redundant warp if input already matches)
    if _dem_matches_utm_target(
        src_dem,
        utm_epsg=layout.utm_epsg,
        bbox_min_x=b.min_x,
        bbox_min_y=b.min_y,
        bbox_max_x=b.max_x,
        bbox_max_y=b.max_y,
        target_res=target_res,
    ):
        logger.info("Input DEM already matches EPSG:%d at %.2f m; reusing directly", layout.utm_epsg, target_res)
        if src_dem != dem_utm_tif.resolve():
            shutil.copy2(src_dem, dem_utm_tif)
    else:
        warp_cmd = [
            "gdalwarp",
            "-overwrite",
            "-multi",
            "-wo",
            "NUM_THREADS=2",
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
            "COMPRESS=NONE",
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

    # 3. Choose contour interval and prepare read-only derivative tasks (run concurrently, bounded to 2 workers)
    interval, index_mult, interval_display = choose_contour_interval(
        layout.spec.scale,
        layout.spec.units,
        relief_m,
        density=layout.spec.carto.contour_density,
        native_res_m=native_res_m,
    )
    index_interval = interval * index_mult

    def _build_hillshade() -> None:
        if layout.spec.carto.hillshade_strength == HillshadeStrength.OFF:
            logger.info("Hillshade strength is OFF; writing neutral white raster -> %s", hillshade_tif)
            _write_neutral_hillshade(dem_utm_tif, hillshade_tif)
            return
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
            "COMPRESS=NONE",
            "-co",
            "TILED=YES",
            str(dem_utm_tif),
            str(hillshade_tif),
        ]
        logger.info("Generating multi-directional hillshade (z=%g) -> %s", z_factor, hillshade_tif)
        _run_subprocess(hillshade_cmd, context="gdaldem hillshade")

    def _build_contours() -> None:
        if terrain_gpkg.exists():
            terrain_gpkg.unlink()
        if layout.spec.units == Units.IMPERIAL:
            contour_step_m = interval / FEET_PER_METER
            elev_scale = FEET_PER_METER
        else:
            contour_step_m = interval
            elev_scale = 1.0

        contour_cmd = [
            "gdal_contour",
            "-a",
            "elev",
            "-i",
            f"{contour_step_m:.12g}",
            "-f",
            "GPKG",
            "-nln",
            "contours",
            str(dem_utm_tif),
            str(terrain_gpkg),
        ]
        logger.info("Generating contours (%s) -> %s", interval_display, terrain_gpkg)
        _run_subprocess(contour_cmd, context="gdal_contour")
        _postprocess_contours_gpkg(terrain_gpkg, index_interval=index_interval, elev_scale=elev_scale)

    slope_tif: Path | None = None
    viewshed_tif: Path | None = None
    viewshed_observers_gpkg: Path | None = None

    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_hs = pool.submit(_build_hillshade)
        fut_ct = pool.submit(_build_contours)
        fut_slope = (
            pool.submit(
                build_slope_shading,
                dem_utm_tif,
                layout.spec.color_mode,
                work_dir,
            )
            if layout.spec.carto.show_slope_shading
            else None
        )
        fut_vs = (
            pool.submit(
                build_viewshed_products,
                dem_utm_tif,
                list(layout.spec.viewsheds),
                layout.utm_epsg,
                layout.spec.color_mode,
                work_dir,
            )
            if layout.spec.viewsheds
            else None
        )

        fut_hs.result()
        fut_ct.result()
        if fut_slope is not None:
            slope_tif = fut_slope.result()
        if fut_vs is not None:
            viewshed_tif, viewshed_observers_gpkg = fut_vs.result()

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
