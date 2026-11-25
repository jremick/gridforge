"""GDAL terrain processing: UTM DEM warp, multi-directional hillshade, and indexed contours."""

from __future__ import annotations

from array import array
import logging
import math
import struct
import subprocess
from pathlib import Path
from typing import Final

from pydantic import BaseModel

from gridforge.config import ColorMode, DensityLevel, HillshadeStrength, SheetLayout, Units

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
    )
