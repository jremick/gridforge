"""Memory-bounded (< 300 MB) pure GDAL/numpy/PIL offline terrain basemap MBTiles builder.

Builds a crisp hypsometric color-relief + multidirectional hillshade Web Mercator
raster MBTiles archive (default zooms 7–12) suitable for GridForge Studio's
air-gapped MapLibre basemap switcher without importing PyQGIS or Qt.
"""

from __future__ import annotations

import io
import logging
import math
import os
import sqlite3
import subprocess
import tempfile
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
from typing import Literal

import numpy as np
from osgeo import gdal, osr
from PIL import Image
from pyproj import Transformer

from gridforge.config import BBox, FrameMarginsMM, SheetLayout, SheetSpec
from gridforge.export._atomic import staged_archive
from gridforge.geo.crs import resolve_utm_zone
from gridforge.packs.regions import resolve_region

logger = logging.getLogger(__name__)

DEFAULT_BASEMAP_MIN_ZOOM: int = 7
DEFAULT_BASEMAP_MAX_ZOOM: int = 12
MAX_BASEMAP_RASTER_DIM_PX: int = 4096
_WEB_MERCATOR_Z0_RES_M: float = 156543.03392804097
_MERCATOR_MAX_LAT: float = 85.05112878

# Smooth cartographic hypsometric elevation ramp (meters -> R G B)
_HYPSOMETRIC_COLOR_RAMP_TXT: str = """\
-500 186 211 226
-1 196 220 233
0 202 224 236
1 214 228 198
120 222 232 197
300 232 233 193
600 232 220 177
1000 220 198 156
1500 205 178 140
2100 198 182 162
2800 218 214 210
3800 245 246 248
nv 202 224 236
"""

_GDAL_ENV: dict[str, str] = {
    "GDAL_CACHEMAX": "128",
    "OMP_THREAD_LIMIT": "1",
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
}


def _run_cmd(
    cmd: list[str],
    *,
    step: str,
    extra_env: Mapping[str, str] | None = None,
) -> None:
    env = os.environ.copy()
    env.update(_GDAL_ENV)
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"{step} failed (exit={proc.returncode}).\n"
            f"Command: {' '.join(cmd)}\n"
            f"STDERR:\n{proc.stderr[-2500:]}\n"
            f"STDOUT:\n{proc.stdout[-1500:]}"
        )


def _bbox_wgs84_to_3857(bbox: BBox) -> tuple[float, float, float, float]:
    s = max(-_MERCATOR_MAX_LAT, min(_MERCATOR_MAX_LAT, bbox.min_y))
    n = max(-_MERCATOR_MAX_LAT, min(_MERCATOR_MAX_LAT, bbox.max_y))
    w = max(-180.0, min(180.0, bbox.min_x))
    e = max(-180.0, min(180.0, bbox.max_x))
    to_3857 = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    xs, ys = to_3857.transform([w, e, w, e], [s, s, n, n])
    return (float(min(xs)), float(min(ys)), float(max(xs)), float(max(ys)))


def _choose_effective_zoom(
    span_x_m: float,
    span_y_m: float,
    *,
    min_zoom: int,
    max_zoom: int,
    max_dim_px: int = MAX_BASEMAP_RASTER_DIM_PX,
) -> tuple[int, float]:
    """Pick the highest zoom <= max_zoom whose EPSG:3857 raster fits within max_dim_px."""
    peak_span = max(1.0, span_x_m, span_y_m)
    z = max_zoom
    while z > min_zoom:
        res = _WEB_MERCATOR_Z0_RES_M / (2**z)
        if math.ceil(peak_span / res) <= max_dim_px:
            return (z, res)
        z -= 1
    res = max(_WEB_MERCATOR_Z0_RES_M / (2**z), peak_span / max_dim_px)
    return (z, res)


def _blend_relief_and_hillshade_chunked(
    color_tif: Path,
    hillshade_tif: Path,
    out_tif: Path,
    *,
    chunk_rows: int = 512,
) -> Path:
    """Blend RGB hypsometric relief with multidirectional hillshade in low-RAM row strips."""
    gdal.UseExceptions()
    osr.UseExceptions()

    ds_col = gdal.Open(str(color_tif))
    ds_hs = gdal.Open(str(hillshade_tif))
    if ds_col is None or ds_hs is None:
        raise RuntimeError("Failed opening intermediate color-relief or hillshade GeoTIFF.")

    try:
        width = ds_col.RasterXSize
        height = ds_col.RasterYSize
        gt = ds_col.GetGeoTransform()
        proj = ds_col.GetProjection()
        hs_band = ds_hs.GetRasterBand(1)
        hs_nodata = hs_band.GetNoDataValue()

        driver = gdal.GetDriverByName("GTiff")
        if driver is None:
            raise RuntimeError("GDAL GTiff driver is unavailable.")
        ds_out = driver.Create(
            str(out_tif),
            width,
            height,
            3,
            gdal.GDT_Byte,
            options=["COMPRESS=LZW", "TILED=YES", "PHOTOMETRIC=RGB"],
        )
        if ds_out is None:
            raise RuntimeError(f"Failed creating blended basemap GeoTIFF '{out_tif}'.")

        try:
            ds_out.SetGeoTransform(gt)
            ds_out.SetProjection(proj)
            for idx, interp in (
                (1, gdal.GCI_RedBand),
                (2, gdal.GCI_GreenBand),
                (3, gdal.GCI_BlueBand),
            ):
                ds_out.GetRasterBand(idx).SetColorInterpretation(interp)

            col_bands = [ds_col.GetRasterBand(i) for i in (1, 2, 3)]
            out_bands = [ds_out.GetRasterBand(i) for i in (1, 2, 3)]

            for y0 in range(0, height, chunk_rows):
                bh = min(chunk_rows, height - y0)
                hs_arr = hs_band.ReadAsArray(0, y0, width, bh).astype(np.float32)
                if hs_nodata is not None:
                    hs_arr = np.where(hs_arr == hs_nodata, 185.0, hs_arr)
                # Natural shaded-relief multiplier preserving crisp highlights and deep ridges.
                shade = np.clip(0.50 + 0.58 * (hs_arr / 255.0), 0.42, 1.08)

                for c_idx in range(3):
                    ch = col_bands[c_idx].ReadAsArray(0, y0, width, bh).astype(np.float32)
                    blended = np.clip(ch * shade, 0.0, 255.0).astype(np.uint8)
                    out_bands[c_idx].WriteArray(blended, 0, y0)

            ds_out.FlushCache()
        finally:
            ds_out = None
    finally:
        ds_col = None
        ds_hs = None

    return out_tif


def _encode_tile_image(
    img: Image.Image,
    *,
    tile_format: Literal["png", "jpg"],
    jpeg_quality: int,
) -> bytes:
    buf = io.BytesIO()
    if tile_format == "jpg":
        img.convert("RGB").save(
            buf,
            format="JPEG",
            quality=jpeg_quality,
            progressive=False,
            optimize=True,
            subsampling="4:2:0",
        )
    else:
        img.convert("RGBA").save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _fill_missing_overview_zooms(
    mbtiles_path: Path,
    *,
    target_min_zoom: int,
    tile_format: Literal["png", "jpg"],
    jpeg_quality: int,
) -> None:
    """Synthesize lower-zoom overview tiles down to ``target_min_zoom`` via 2x2 PIL downsampling.

    GDAL's ``gdaladdo`` stops once the downsampled raster shrinks below one pixel on
    compact bounding boxes; this lightweight TMS parent pyramid guarantees full zoom
    coverage down to ``target_min_zoom`` (e.g. zoom 7) using negligible RAM.
    """
    bg_rgba = (202, 224, 236, 255) if tile_format == "jpg" else (0, 0, 0, 0)

    with closing(sqlite3.connect(mbtiles_path)) as conn:
        row = conn.execute("SELECT MIN(zoom_level) FROM tiles").fetchone()
        if row is None or row[0] is None:
            return
        cur_min = int(row[0])

        while cur_min > target_min_zoom:
            parent_z = cur_min - 1
            children = conn.execute(
                "SELECT tile_column, tile_row, tile_data FROM tiles WHERE zoom_level = ?",
                (cur_min,),
            ).fetchall()
            if not children:
                break

            # Group TMS children into parent (px, py) cells.
            # In TMS, row 0 is South (bottom), so child (cx % 2 == 1) is East (right)
            # and child (cy % 2 == 1) is North (top -> paste y=0 in PIL coordinates).
            grouped: dict[tuple[int, int], list[tuple[int, int, bytes]]] = {}
            for cx, cy, blob in children:
                px, py = int(cx) // 2, int(cy) // 2
                grouped.setdefault((px, py), []).append((int(cx), int(cy), bytes(blob)))

            inserts: list[tuple[int, int, int, bytes]] = []
            for (px, py), group in grouped.items():
                canvas = Image.new("RGBA", (512, 512), bg_rgba)
                for cx, cy, blob in group:
                    with Image.open(io.BytesIO(blob)) as child_img:
                        tile_rgba = child_img.convert("RGBA")
                        if tile_rgba.size != (256, 256):
                            tile_rgba = tile_rgba.resize((256, 256), Image.Resampling.LANCZOS)
                        paste_x = 256 if (cx % 2 == 1) else 0
                        paste_y = 0 if (cy % 2 == 1) else 256
                        canvas.paste(tile_rgba, (paste_x, paste_y))
                downsampled = canvas.resize((256, 256), Image.Resampling.LANCZOS)
                tile_bytes = _encode_tile_image(
                    downsampled, tile_format=tile_format, jpeg_quality=jpeg_quality
                )
                inserts.append((parent_z, px, py, tile_bytes))

            conn.executemany(
                "INSERT OR REPLACE INTO tiles (zoom_level, tile_column, tile_row, tile_data) "
                "VALUES (?, ?, ?, ?)",
                inserts,
            )
            conn.commit()
            cur_min = parent_z


def _fetch_online_dem_for_bbox(bbox: BBox, work_dir: Path, cache_dir: Path | None) -> tuple[Path, str]:
    """Fetch a regional DEM using AutoDEMSource when no local source DEM is provided."""
    from gridforge.sources.dem_usgs3dep import AutoDEMSource

    c_lon = (bbox.min_x + bbox.max_x) / 2.0
    c_lat = (bbox.min_y + bbox.max_y) / 2.0
    zone, hemi, epsg, gzd = resolve_utm_zone(c_lat, c_lon)
    spec = SheetSpec(center_lat=c_lat, center_lon=c_lon, scale=50000)
    to_utm = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)
    cx, cy = to_utm.transform(c_lon, c_lat)
    xs, ys = to_utm.transform(
        [bbox.min_x, bbox.max_x, bbox.min_x, bbox.max_x],
        [bbox.min_y, bbox.min_y, bbox.max_y, bbox.max_y],
    )
    utm_bbox = BBox(
        min_x=float(min(xs)),
        min_y=float(min(ys)),
        max_x=float(max(xs)),
        max_y=float(max(ys)),
    )
    layout = SheetLayout(
        spec=spec,
        utm_epsg=epsg,
        utm_zone=zone,
        utm_hemisphere=hemi,
        gzd=gzd,
        center_easting=float(cx),
        center_northing=float(cy),
        page_width_mm=210.0,
        page_height_mm=297.0,
        margins_mm=FrameMarginsMM(),
        frame_x_mm=12.0,
        frame_y_mm=14.0,
        frame_width_mm=186.0,
        frame_height_mm=235.0,
        utm_bbox=utm_bbox,
        utm_fetch_bbox=utm_bbox,
        wgs84_bbox=bbox,
        wgs84_fetch_bbox=bbox,
    )
    src = AutoDEMSource(cache_dir=cache_dir)
    dem_path = src.fetch_dem(layout, work_dir)
    attr = str(getattr(src, "resolved_attribution", None) or src.attribution)
    return (dem_path, attr)


def build_offline_basemap_mbtiles(
    bbox: BBox,
    output_mbtiles: Path | str,
    *,
    source_dem: Path | str | None = None,
    min_zoom: int = DEFAULT_BASEMAP_MIN_ZOOM,
    max_zoom: int = DEFAULT_BASEMAP_MAX_ZOOM,
    name: str = "GridForge Offline Terrain Basemap",
    attribution: str | None = None,
    tile_format: Literal["png", "jpg"] = "jpg",
    jpeg_quality: int = 82,
    cache_dir: Path | None = None,
) -> Path:
    """Build an atomic hypsometric + hillshade overview ``.mbtiles`` archive (< 300 MB RAM)."""
    if not 0 <= min_zoom <= max_zoom <= 16:
        raise ValueError(
            f"Basemap zoom range must satisfy 0 <= min_zoom <= max_zoom <= 16, "
            f"got {min_zoom}..{max_zoom}."
        )
    if not 1 <= jpeg_quality <= 100:
        raise ValueError(f"jpeg_quality must be in 1..100, got {jpeg_quality}.")
    if bbox.width <= 0.0 or bbox.height <= 0.0:
        raise ValueError(f"Basemap bbox must have positive width & height, got {bbox}.")

    caller_out = Path(output_mbtiles)
    resolved_out = caller_out.resolve()
    resolved_out.parent.mkdir(parents=True, exist_ok=True)

    with (
        staged_archive(
            resolved_out,
            sidecar_suffixes=("-journal", "-wal", "-shm", ".aux.xml"),
        ) as staged,
        tempfile.TemporaryDirectory(prefix="gridforge_basemap_") as tmp_dir_str,
    ):
        tmp_dir = Path(tmp_dir_str)

        if source_dem is not None:
            dem_src_path = Path(source_dem).resolve()
            if not dem_src_path.is_file():
                raise FileNotFoundError(f"Source DEM file not found: '{dem_src_path}'")
            resolved_attr = attribution or "GridForge Offline Terrain Basemap (DEM Hillshade)"
        else:
            dem_src_path, fetched_attr = _fetch_online_dem_for_bbox(bbox, tmp_dir, cache_dir)
            resolved_attr = attribution or fetched_attr

        min_x, min_y, max_x, max_y = _bbox_wgs84_to_3857(bbox)
        eff_max_zoom, res_m = _choose_effective_zoom(
            max_x - min_x,
            max_y - min_y,
            min_zoom=min_zoom,
            max_zoom=max_zoom,
        )
        eff_min_zoom = min(min_zoom, eff_max_zoom)

        dem_3857 = tmp_dir / "dem_3857.tif"
        _run_cmd(
            [
                "gdalwarp",
                "-overwrite",
                "-t_srs",
                "EPSG:3857",
                "-te",
                f"{min_x:.3f}",
                f"{min_y:.3f}",
                f"{max_x:.3f}",
                f"{max_y:.3f}",
                "-tr",
                f"{res_m:.6f}",
                f"{res_m:.6f}",
                "-r",
                "bilinear",
                "-dstnodata",
                "-9999",
                "-co",
                "COMPRESS=DEFLATE",
                "-co",
                "TILED=YES",
                str(dem_src_path),
                str(dem_3857),
            ],
            step="gdalwarp EPSG:3857 DEM for offline basemap",
        )

        ramp_txt = tmp_dir / "hypsometric_ramp.txt"
        ramp_txt.write_text(_HYPSOMETRIC_COLOR_RAMP_TXT, encoding="utf-8")

        color_tif = tmp_dir / "color_relief.tif"
        _run_cmd(
            [
                "gdaldem",
                "color-relief",
                str(dem_3857),
                str(ramp_txt),
                str(color_tif),
                "-co",
                "COMPRESS=LZW",
                "-co",
                "TILED=YES",
            ],
            step="gdaldem color-relief for offline basemap",
        )

        hillshade_tif = tmp_dir / "hillshade.tif"
        _run_cmd(
            [
                "gdaldem",
                "hillshade",
                str(dem_3857),
                str(hillshade_tif),
                "-multidirectional",
                "-z",
                "1.6",
                "-compute_edges",
                "-co",
                "COMPRESS=LZW",
                "-co",
                "TILED=YES",
            ],
            step="gdaldem hillshade for offline basemap",
        )

        blended_tif = _blend_relief_and_hillshade_chunked(
            color_tif, hillshade_tif, tmp_dir / "blended_3857.tif"
        )

        gdal_tile_fmt = "PNG" if tile_format == "png" else "JPEG"
        description = (
            f"GridForge air-gapped hypsometric terrain basemap (z{eff_min_zoom}–z{eff_max_zoom})"
        )
        translate_cmd = [
            "gdal_translate",
            "-of",
            "MBTILES",
            "-co",
            f"TILE_FORMAT={gdal_tile_fmt}",
            "-co",
            "ZOOM_LEVEL_STRATEGY=AUTO",
            "-co",
            "RESAMPLING=AVERAGE",
            "-co",
            f"NAME={name}",
            "-co",
            f"DESCRIPTION={description}",
            "-co",
            "TYPE=basemap",
        ]
        if tile_format == "jpg":
            translate_cmd.extend(["-co", f"QUALITY={jpeg_quality}"])
        translate_cmd.extend([str(blended_tif), str(staged)])
        _run_cmd(translate_cmd, step="gdal_translate offline basemap MBTiles")

        with closing(sqlite3.connect(staged)) as conn:
            row = conn.execute("SELECT MAX(zoom_level) FROM tiles").fetchone()
            actual_max_zoom = int(row[0]) if row and row[0] is not None else eff_max_zoom

        # Clamp any higher zoom if GDAL rounded above eff_max_zoom, and build down to eff_min_zoom.
        overview_levels = max(0, actual_max_zoom - eff_min_zoom)
        if overview_levels > 0:
            factors = [str(1 << k) for k in range(1, overview_levels + 1)]
            _run_cmd(
                ["gdaladdo", "-r", "average", str(staged), *factors],
                step="gdaladdo offline basemap overviews",
            )

        _fill_missing_overview_zooms(
            staged,
            target_min_zoom=eff_min_zoom,
            tile_format=tile_format,
            jpeg_quality=jpeg_quality,
        )

        # Normalize MBTiles 1.3 metadata so MapLibre and Studio BasemapStore consume cleanly.
        bounds_str = (
            f"{bbox.min_x:.6f},{bbox.min_y:.6f},{bbox.max_x:.6f},{bbox.max_y:.6f}"
        )
        c_lon = (bbox.min_x + bbox.max_x) / 2.0
        c_lat = (bbox.min_y + bbox.max_y) / 2.0

        with closing(sqlite3.connect(staged)) as conn:
            stats = conn.execute(
                "SELECT COUNT(*), MIN(zoom_level), MAX(zoom_level) FROM tiles"
            ).fetchone()
            if stats is None or not stats[0]:
                raise RuntimeError(f"Offline basemap '{resolved_out}' contains zero tiles.")
            final_min_z = int(stats[1])
            final_max_z = int(stats[2])
            center_z = max(final_min_z, min(final_max_z, (final_min_z + final_max_z) // 2))
            center_str = f"{c_lon:.6f},{c_lat:.6f},{center_z}"
            conn.executemany(
                "INSERT OR REPLACE INTO metadata (name, value) VALUES (?, ?)",
                [
                    ("name", name),
                    ("format", tile_format),
                    ("bounds", bounds_str),
                    ("center", center_str),
                    ("minzoom", str(final_min_z)),
                    ("maxzoom", str(final_max_z)),
                    ("type", "basemap"),
                    ("description", description),
                    ("attribution", resolved_attr),
                ],
            )
            conn.commit()

        return caller_out


def build_basemap_for_region(
    region_or_bbox: str,
    output_mbtiles: Path | str,
    *,
    source_dem: Path | str | None = None,
    min_zoom: int = DEFAULT_BASEMAP_MIN_ZOOM,
    max_zoom: int = DEFAULT_BASEMAP_MAX_ZOOM,
    name: str | None = None,
    attribution: str | None = None,
    tile_format: Literal["png", "jpg"] = "jpg",
    jpeg_quality: int = 82,
    cache_dir: Path | None = None,
) -> Path:
    """Resolve a catalog region ID or ``w,s,e,n`` bbox and build an offline terrain basemap."""
    preset = resolve_region(region_or_bbox)
    return build_offline_basemap_mbtiles(
        preset.bbox_wgs84,
        output_mbtiles,
        source_dem=source_dem,
        min_zoom=min_zoom,
        max_zoom=max_zoom,
        name=name or f"{preset.name} Offline Basemap",
        attribution=attribution,
        tile_format=tile_format,
        jpeg_quality=jpeg_quality,
        cache_dir=cache_dir,
    )
