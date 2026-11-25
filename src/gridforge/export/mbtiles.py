"""ATAK-compatible MBTiles 1.3 raster tile archive exporter."""

from __future__ import annotations

import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path

from osgeo import gdal
from pyproj import Transformer

from gridforge.config import (
    ColorMode,
    ExportFormat,
    ExportResult,
    JobWorkspace,
    MBTilesFormat,
)
from gridforge.export.raster import render_collarless_geotiff


def _run_cmd(cmd: list[str], *, step: str) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"{step} failed (exit={proc.returncode}).\n"
            f"Command: {' '.join(cmd)}\n"
            f"STDERR:\n{proc.stderr[-2500:]}\n"
            f"STDOUT:\n{proc.stdout[-1500:]}"
        )


def _raster_info_3857(tif_path: Path) -> tuple[int, int, tuple[float, float, float, float]]:
    """Return (width_px, height_px, (min_lon, min_lat, max_lon, max_lat)) for an EPSG:3857 GeoTIFF."""
    gdal.UseExceptions()
    ds = gdal.Open(str(tif_path))
    if ds is None:
        raise RuntimeError(f"Failed to open warped GeoTIFF '{tif_path}'.")
    try:
        w = ds.RasterXSize
        h = ds.RasterYSize
        gt = ds.GetGeoTransform()
    finally:
        ds = None

    min_x = gt[0]
    max_y = gt[3]
    max_x = min_x + w * gt[1]
    min_y = max_y + h * gt[5]

    to_4326 = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
    lons, lats = to_4326.transform(
        [min_x, max_x, min_x, max_x],
        [min_y, min_y, max_y, max_y],
    )
    bounds = (
        max(-180.0, float(min(lons))),
        max(-85.051129, float(min(lats))),
        min(180.0, float(max(lons))),
        min(85.051129, float(max(lats))),
    )
    return (w, h, bounds)


def _compute_overview_factors(width_px: int, height_px: int, max_zoom: int) -> list[str]:
    """Compute power-of-two gdaladdo overview factors down to ~1-tile overview."""
    factors: list[str] = []
    factor = 2
    peak = max(width_px, height_px)
    while len(factors) < 6 and (max_zoom - len(factors) - 1) >= 0:
        if peak // factor < 16 and factors:
            break
        factors.append(str(factor))
        factor *= 2
    if not factors and max_zoom > 0:
        factors.append("2")
    return factors


def _paper_bg_rgb(color_mode: ColorMode) -> tuple[int, int, int]:
    return (250, 249, 245) if color_mode == ColorMode.COLOR else (255, 255, 255)


def export_mbtiles(
    workspace: JobWorkspace,
    output_mbtiles: Path,
    *,
    tile_format: MBTilesFormat = MBTilesFormat.PNG,
    jpeg_quality: int = 85,
    dpi: int = 300,
    include_grid: bool = True,
    collarless_tif: Path | None = None,
) -> ExportResult:
    """Export the exact sheet map body to an ATAK-ready MBTiles 1.3 SQLite archive."""
    t0 = time.perf_counter()

    if not 1 <= jpeg_quality <= 100:
        raise ValueError(f"jpeg_quality must be in 1..100, got {jpeg_quality}.")

    caller_out = Path(output_mbtiles)
    resolved_out = caller_out.resolve()
    resolved_out.parent.mkdir(parents=True, exist_ok=True)

    # Remove stale MBTiles archive and sidecars before creating fresh output.
    for sidecar in (
        resolved_out,
        Path(f"{resolved_out}-journal"),
        Path(f"{resolved_out}-wal"),
        Path(f"{resolved_out}-shm"),
        Path(f"{resolved_out}.aux.xml"),
    ):
        if sidecar.exists():
            sidecar.unlink()

    layout = workspace.layout
    spec = layout.spec

    with tempfile.TemporaryDirectory(prefix="gridforge_mbtiles_") as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)

        if collarless_tif is not None and Path(collarless_tif).is_file():
            src_tif = Path(collarless_tif).resolve()
        else:
            src_tif = render_collarless_geotiff(
                workspace,
                tmp_dir / "collarless_utm.tif",
                dpi=dpi,
                include_grid=include_grid,
            ).resolve()

        warped_tif = tmp_dir / "warped_3857.tif"
        warp_cmd = [
            "gdalwarp",
            "-overwrite",
            "-t_srs",
            "EPSG:3857",
            "-r",
            "lanczos",
            "-co",
            "TILED=YES",
            "-co",
            "COMPRESS=LZW",
        ]

        if tile_format == MBTilesFormat.PNG:
            # Transparent outside the rotated UTM map footprint for crisp ATAK overlays.
            warp_cmd.append("-dstalpha")
            warp_input = src_tif
        else:
            # JPEG has no alpha; composite rotated wedges onto matching paper background.
            gdal.UseExceptions()
            ds_chk = gdal.Open(str(src_tif))
            band_count = ds_chk.RasterCount if ds_chk is not None else 3
            ds_chk = None

            if band_count > 3:
                rgb_only = tmp_dir / "src_rgb.tif"
                _run_cmd(
                    [
                        "gdal_translate",
                        "-b",
                        "1",
                        "-b",
                        "2",
                        "-b",
                        "3",
                        "-co",
                        "COMPRESS=LZW",
                        str(src_tif),
                        str(rgb_only),
                    ],
                    step="gdal_translate RGB strip for JPEG MBTiles",
                )
                warp_input = rgb_only
            else:
                warp_input = src_tif

            bg_r, bg_g, bg_b = _paper_bg_rgb(spec.color_mode)
            warp_cmd.extend(["-wo", f"INIT_DEST={bg_r},{bg_g},{bg_b}"])

        warp_cmd.extend([str(warp_input), str(warped_tif)])
        _run_cmd(warp_cmd, step="gdalwarp EPSG:3857 for MBTiles")

        w_3857, h_3857, bounds_wgs84 = _raster_info_3857(warped_tif)

        gdal_tile_fmt = "PNG" if tile_format == MBTilesFormat.PNG else "JPEG"
        sheet_name = spec.title or f"GridForge {layout.gzd} {workspace.mgrs_meta.center_mgrs_10m}"
        description = (
            f"GridForge 1:{spec.scale:,} {spec.color_mode.value.upper()} topographic map "
            f"({layout.gzd}, EPSG:{layout.utm_epsg}, {workspace.vertical_datum})"
        )

        translate_cmd = [
            "gdal_translate",
            "-of",
            "MBTILES",
            "-co",
            f"TILE_FORMAT={gdal_tile_fmt}",
            "-co",
            "ZOOM_LEVEL_STRATEGY=UPPER",
            "-co",
            "RESAMPLING=AVERAGE",
            "-co",
            f"NAME={sheet_name}",
            "-co",
            f"DESCRIPTION={description}",
            "-co",
            "TYPE=basemap",
        ]
        if tile_format == MBTilesFormat.JPG:
            translate_cmd.extend(["-co", f"QUALITY={jpeg_quality}"])

        translate_cmd.extend([str(warped_tif), str(resolved_out)])
        _run_cmd(translate_cmd, step="gdal_translate MBTILES")

        # Inspect initial max zoom to compute clean multi-zoom overview pyramid.
        with sqlite3.connect(str(resolved_out)) as conn:
            row = conn.execute("SELECT MAX(zoom_level) FROM tiles").fetchone()
            base_max_zoom = int(row[0]) if row and row[0] is not None else 0

        overview_factors = _compute_overview_factors(w_3857, h_3857, base_max_zoom)
        if overview_factors:
            _run_cmd(
                ["gdaladdo", "-r", "average", str(resolved_out), *overview_factors],
                step="gdaladdo MBTiles overviews",
            )

        # Populate/normalize full MBTiles 1.3 metadata and query exact tile stats.
        attribution = (
            " | ".join(workspace.attributions)
            if workspace.attributions
            else "Map data © OpenStreetMap contributors (ODbL)"
        )
        fmt_meta = "png" if tile_format == MBTilesFormat.PNG else "jpg"
        bounds_str = (
            f"{bounds_wgs84[0]:.6f},{bounds_wgs84[1]:.6f},"
            f"{bounds_wgs84[2]:.6f},{bounds_wgs84[3]:.6f}"
        )

        with sqlite3.connect(str(resolved_out)) as conn:
            stats = conn.execute(
                "SELECT COUNT(*), MIN(zoom_level), MAX(zoom_level), MAX(LENGTH(tile_data)) "
                "FROM tiles"
            ).fetchone()
            if stats is None or not stats[0]:
                raise RuntimeError(f"MBTiles archive '{resolved_out}' contains zero tiles.")

            tile_count = int(stats[0])
            min_zoom = int(stats[1])
            max_zoom = int(stats[2])
            max_tile_bytes = int(stats[3])

            center_str = f"{spec.center_lon:.6f},{spec.center_lat:.6f},{max_zoom}"
            metadata_rows = [
                ("name", sheet_name),
                ("format", fmt_meta),
                ("bounds", bounds_str),
                ("center", center_str),
                ("minzoom", str(min_zoom)),
                ("maxzoom", str(max_zoom)),
                ("type", "basemap"),
                ("description", description),
                ("attribution", attribution),
            ]
            conn.executemany(
                "INSERT OR REPLACE INTO metadata (name, value) VALUES (?, ?)",
                metadata_rows,
            )
            conn.commit()

    elapsed = round(time.perf_counter() - t0, 3)
    return ExportResult(
        format=ExportFormat.MBTILES,
        output_path=caller_out,
        file_size_bytes=resolved_out.stat().st_size,
        tile_count=tile_count,
        min_zoom=min_zoom,
        max_zoom=max_zoom,
        max_tile_px=(256, 256),
        max_tile_bytes=max_tile_bytes,
        elapsed_seconds=elapsed,
    )
