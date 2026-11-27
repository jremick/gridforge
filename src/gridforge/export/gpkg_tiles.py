"""OGC GeoPackage raster tile pyramid (.gpkg) exporter for ATAK, QGIS, and field GIS."""

from __future__ import annotations

import re
import sqlite3
import subprocess
import tempfile
import time
from contextlib import closing
from pathlib import Path

from osgeo import gdal

from gridforge.config import (
    ColorMode,
    ExportFormat,
    ExportResult,
    JobWorkspace,
    MBTilesFormat,
)
from gridforge.export._atomic import staged_archive
from gridforge.export.mbtiles import (
    _compute_overview_factors,
    _paper_bg_rgb,
    _raster_info_3857,
    prepare_warped_3857,
)
from gridforge.export.raster import render_collarless_geotiff
from gridforge.subprocess_env import build_safe_subprocess_env

_VALID_TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


def _run_cmd(cmd: list[str], *, step: str, timeout: int = 300) -> None:
    proc = subprocess.run(
        cmd,
        env=build_safe_subprocess_env(allow_network=False),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"{step} failed (exit={proc.returncode}).\n"
            f"Command: {' '.join(cmd)}\n"
            f"STDERR:\n{proc.stderr[-2500:]}\n"
            f"STDOUT:\n{proc.stdout[-1500:]}"
        )


def export_gpkg_tiles(
    workspace: JobWorkspace,
    output_gpkg: Path,
    *,
    tile_format: MBTilesFormat = MBTilesFormat.PNG,
    jpeg_quality: int = 85,
    dpi: int = 300,
    include_grid: bool = True,
    collarless_tif: Path | None = None,
    warped_3857_tif: Path | None = None,
    table_name: str = "gridforge_tiles",
) -> ExportResult:
    """Export the exact sheet map body to an atomic OGC GeoPackage raster tiles archive."""
    t0 = time.perf_counter()

    if not 1 <= jpeg_quality <= 100:
        raise ValueError(f"jpeg_quality must be in 1..100, got {jpeg_quality}.")
    if not _VALID_TABLE_RE.match(table_name):
        raise ValueError(f"Invalid GeoPackage raster table name '{table_name}'.")

    caller_out = Path(output_gpkg)
    resolved_out = caller_out.resolve()
    resolved_out.parent.mkdir(parents=True, exist_ok=True)

    layout = workspace.layout
    spec = layout.spec

    with (
        staged_archive(
            resolved_out,
            sidecar_suffixes=("-journal", "-wal", "-shm", ".aux.xml"),
        ) as staged,
        tempfile.TemporaryDirectory(prefix="gridforge_gpkg_") as tmp_dir_str,
    ):
        tmp_dir = Path(tmp_dir_str)

        if warped_3857_tif is not None and Path(warped_3857_tif).is_file():
            warped_tif = Path(warped_3857_tif).resolve()
        else:
            if collarless_tif is not None and Path(collarless_tif).is_file():
                src_tif = Path(collarless_tif).resolve()
            else:
                src_tif = render_collarless_geotiff(
                    workspace,
                    tmp_dir / "collarless_utm.tif",
                    dpi=dpi,
                    include_grid=include_grid,
                ).resolve()

            warped_tif = prepare_warped_3857(
                src_tif,
                tmp_dir / "warped_3857.tif",
                tile_format=tile_format,
                color_mode=spec.color_mode,
                tmp_dir=tmp_dir,
                step_label="gdalwarp EPSG:3857 for GPKG tiles",
            )

        w_3857, h_3857, _ = _raster_info_3857(warped_tif)

        gdal_tile_fmt = "PNG" if tile_format == MBTilesFormat.PNG else "JPEG"
        sheet_name = spec.title or f"GridForge {layout.gzd} {workspace.mgrs_meta.center_mgrs_10m}"
        description = (
            f"GridForge 1:{spec.scale:,} {spec.color_mode.value.upper()} OGC GeoPackage raster tiles "
            f"({layout.gzd}, EPSG:{layout.utm_epsg}, {workspace.vertical_datum})"
        )

        translate_cmd = [
            "gdal_translate",
            "-of",
            "GPKG",
            "-co",
            f"RASTER_TABLE={table_name}",
            "-co",
            "TILING_SCHEME=GoogleMapsCompatible",
            "-co",
            f"TILE_FORMAT={gdal_tile_fmt}",
            "-co",
            "ZOOM_LEVEL_STRATEGY=UPPER",
            "-co",
            "RESAMPLING=AVERAGE",
            "-co",
            f"RASTER_IDENTIFIER={sheet_name}",
            "-co",
            f"RASTER_DESCRIPTION={description}",
        ]
        if tile_format == MBTilesFormat.JPG:
            translate_cmd.extend(["-co", f"QUALITY={jpeg_quality}"])

        translate_cmd.extend([str(warped_tif), str(staged)])
        _run_cmd(translate_cmd, step="gdal_translate GPKG raster tiles")

        with closing(sqlite3.connect(staged)) as conn:
            row = conn.execute(f"SELECT MAX(zoom_level) FROM {table_name}").fetchone()
            base_max_zoom = int(row[0]) if row and row[0] is not None else 0

        overview_factors = _compute_overview_factors(w_3857, h_3857, base_max_zoom)
        if overview_factors:
            _run_cmd(
                ["gdaladdo", "-r", "average", str(staged), *overview_factors],
                step="gdaladdo GPKG tile overviews",
            )

        with closing(sqlite3.connect(staged)) as conn:
            stats = conn.execute(
                f"SELECT COUNT(*), MIN(zoom_level), MAX(zoom_level), MAX(LENGTH(tile_data)) "
                f"FROM {table_name}"
            ).fetchone()
            if stats is None or not stats[0]:
                raise RuntimeError(f"GeoPackage raster archive '{resolved_out}' contains zero tiles.")

            tile_count = int(stats[0])
            min_zoom = int(stats[1])
            max_zoom = int(stats[2])
            max_tile_bytes = int(stats[3])

        elapsed = round(time.perf_counter() - t0, 3)
        return ExportResult(
            format=ExportFormat.GPKG,
            output_path=caller_out,
            file_size_bytes=staged.stat().st_size,
            tile_count=tile_count,
            min_zoom=min_zoom,
            max_zoom=max_zoom,
            max_tile_px=(256, 256),
            max_tile_bytes=max_tile_bytes,
            elapsed_seconds=elapsed,
        )
