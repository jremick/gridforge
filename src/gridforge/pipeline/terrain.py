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

from gridforge.config import SheetLayout, Units

logger = logging.getLogger(__name__)

FEET_PER_METER: Final[float] = 3.280839895013123
DEM_NODATA: Final[float] = -9999.0
DEFAULT_TARGET_RES_M: Final[float] = 10.0
MIN_TARGET_RES_M: Final[float] = 2.0


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


def choose_contour_interval(
    scale: int,
    units: Units,
    relief_m: float,
) -> tuple[float, int, str]:
    """Select military topographic contour interval, index multiplier, and collar display text.

    Returns:
        ``(interval_in_unit, index_multiplier, display_str)`` where ``index_multiplier`` is ``5``.
    """
    index_mult = 5

    if units == Units.IMPERIAL:
        if scale <= 15000:
            interval = 20.0
        elif scale <= 25000:
            interval = 40.0
        else:
            interval = 80.0
        index_val = int(round(interval * index_mult))
        return (interval, index_mult, f"{int(round(interval))} ft (index {index_val} ft)")

    # Metric defaults
    if scale <= 15000:
        interval = 10.0
    elif scale <= 25000:
        interval = 20.0 if relief_m > 1200.0 else 10.0
    else:
        interval = 20.0
    index_val = int(round(interval * index_mult))
    return (interval, index_mult, f"{int(round(interval))} m (index {index_val} m)")


def _estimate_target_resolution_m(dem_raw_path: Path, center_lat: float) -> float:
    """Choose UTM warp pixel resolution (10 m for 30 m GLO-30; preserve finer local DEMs)."""
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
            native_m = min(px_x * 111320.0 * cos_lat, px_y * 111320.0)
        else:
            linear_units = srs.GetLinearUnits() or 1.0
            native_m = min(px_x, px_y) * linear_units

        if native_m < DEFAULT_TARGET_RES_M:
            return max(MIN_TARGET_RES_M, round(native_m, 2))
        return DEFAULT_TARGET_RES_M
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

    Outputs inside ``work_dir`` (all in ``EPSG:{layout.utm_epsg}``):
    - ``dem_utm.tif``: Float32 cubic-warped UTM elevation raster covering ``layout.utm_fetch_bbox``
    - ``hillshade.tif``: Byte multi-directional hillshade (``gdaldem hillshade -multidirectional``)
    - ``terrain.gpkg``: GeoPackage layer ``contours`` with ``elev``, ``is_index``, and ``elev_label``
    """
    src_dem = Path(dem_raw_path).expanduser().resolve()
    if not src_dem.is_file():
        raise FileNotFoundError(f"Input DEM does not exist: {src_dem}")

    work_dir.mkdir(parents=True, exist_ok=True)
    dem_utm_tif = work_dir / "dem_utm.tif"
    hillshade_tif = work_dir / "hillshade.tif"
    terrain_gpkg = work_dir / "terrain.gpkg"

    target_res = _estimate_target_resolution_m(src_dem, layout.spec.center_lat)
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
        "Warping DEM to EPSG:%d at %.2f m resolution -> %s",
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

    # 3. Multi-directional hillshade (NOTE: -multidirectional and -combined are mutually exclusive in GDAL)
    hillshade_cmd = [
        "gdaldem",
        "hillshade",
        "-multidirectional",
        "-z",
        "1.15",
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
    logger.info("Generating multi-directional hillshade -> %s", hillshade_tif)
    _run_subprocess(hillshade_cmd, context="gdaldem hillshade")

    # 4. Generate contours in metric (m) or imperial (ft) units
    interval, index_mult, interval_display = choose_contour_interval(
        layout.spec.scale,
        layout.spec.units,
        relief_m,
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
    )
