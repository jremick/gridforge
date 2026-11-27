"""OsmAnd / RMaps BigPlanet SQLite tile archive (.sqlitedb) exporter."""

from __future__ import annotations

import sqlite3
import tempfile
import time
from contextlib import closing
from pathlib import Path

from gridforge.config import (
    ExportFormat,
    ExportResult,
    JobWorkspace,
    MBTilesFormat,
)
from gridforge.export._atomic import staged_archive
from gridforge.export.mbtiles import export_mbtiles


def _write_osmand_from_mbtiles(
    src_mbtiles: Path,
    dst_sqlitedb: Path,
    *,
    sheet_name: str,
    description: str,
    attribution: str,
) -> tuple[int, int, int, int]:
    """Stream MBTiles TMS tiles into an OsmAnd BigPlanet ``.sqlitedb`` (``z = 17 - zoom``).

    Returns:
        (tile_count, min_osm_zoom, max_osm_zoom, max_tile_bytes)
    """
    with (
        closing(sqlite3.connect(src_mbtiles)) as src_conn,
        closing(sqlite3.connect(dst_sqlitedb)) as dst_conn,
    ):
        dst_conn.executescript(
            """
            CREATE TABLE tiles (
                x int,
                y int,
                z int,
                s int,
                image blob,
                PRIMARY KEY (x, y, z, s)
            );
            CREATE INDEX IND ON tiles (x, y, z, s);
            CREATE TABLE info (
                minzoom int,
                maxzoom int,
                tilenumbering text,
                timecolumn text,
                ellipsoidal int,
                url text,
                rule text,
                name text,
                description text,
                attribution text
            );
            """
        )

        tile_count = 0
        min_osm_zoom: int | None = None
        max_osm_zoom: int | None = None
        max_tile_bytes = 0

        cursor = src_conn.execute(
            "SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles"
        )
        batch: list[tuple[int, int, int, int, bytes]] = []
        for osm_zoom_raw, tile_col_raw, tms_row_raw, blob in cursor:
            osm_zoom = int(osm_zoom_raw)
            xyz_x = int(tile_col_raw)
            # Flip MBTiles TMS Y (bottom-up) to standard XYZ Y (top-down) for OsmAnd.
            xyz_y = (1 << osm_zoom) - 1 - int(tms_row_raw)
            # BigPlanet inverted zoom convention required by OsmAnd .sqlitedb.
            bp_z = 17 - osm_zoom
            tile_bytes = bytes(blob)

            batch.append((xyz_x, xyz_y, bp_z, 0, tile_bytes))
            tile_count += 1
            if min_osm_zoom is None or osm_zoom < min_osm_zoom:
                min_osm_zoom = osm_zoom
            if max_osm_zoom is None or osm_zoom > max_osm_zoom:
                max_osm_zoom = osm_zoom
            if len(tile_bytes) > max_tile_bytes:
                max_tile_bytes = len(tile_bytes)

            if len(batch) >= 256:
                dst_conn.executemany(
                    "INSERT OR REPLACE INTO tiles (x, y, z, s, image) VALUES (?, ?, ?, ?, ?)",
                    batch,
                )
                batch.clear()

        if batch:
            dst_conn.executemany(
                "INSERT OR REPLACE INTO tiles (x, y, z, s, image) VALUES (?, ?, ?, ?, ?)",
                batch,
            )

        if tile_count == 0 or min_osm_zoom is None or max_osm_zoom is None:
            raise RuntimeError(f"OsmAnd SQLiteDB archive '{dst_sqlitedb}' contains zero tiles.")

        # In BigPlanet schema, info.minzoom = MIN(z) = 17 - max_osm_zoom
        # and info.maxzoom = MAX(z) = 17 - min_osm_zoom.
        bp_min_z = 17 - max_osm_zoom
        bp_max_z = 17 - min_osm_zoom
        dst_conn.execute(
            "INSERT INTO info "
            "(minzoom, maxzoom, tilenumbering, timecolumn, ellipsoidal, url, rule, name, description, attribution) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                bp_min_z,
                bp_max_z,
                "BigPlanet",
                "no",
                0,
                "",
                "",
                sheet_name,
                description,
                attribution,
            ),
        )
        dst_conn.commit()

    return (tile_count, min_osm_zoom, max_osm_zoom, max_tile_bytes)


def export_osmand_sqlitedb(
    workspace: JobWorkspace,
    output_sqlitedb: Path,
    *,
    tile_format: MBTilesFormat = MBTilesFormat.PNG,
    jpeg_quality: int = 85,
    dpi: int = 300,
    include_grid: bool = True,
    collarless_tif: Path | None = None,
    mbtiles_path: Path | None = None,
) -> ExportResult:
    """Export the exact sheet map body to an atomic OsmAnd BigPlanet ``.sqlitedb`` archive."""
    t0 = time.perf_counter()

    if not 1 <= jpeg_quality <= 100:
        raise ValueError(f"jpeg_quality must be in 1..100, got {jpeg_quality}.")

    caller_out = Path(output_sqlitedb)
    resolved_out = caller_out.resolve()
    resolved_out.parent.mkdir(parents=True, exist_ok=True)

    layout = workspace.layout
    spec = layout.spec
    sheet_name = spec.title or f"GridForge {layout.gzd} {workspace.mgrs_meta.center_mgrs_10m}"
    description = (
        f"GridForge 1:{spec.scale:,} {spec.color_mode.value.upper()} OsmAnd offline tile map "
        f"({layout.gzd}, EPSG:{layout.utm_epsg}, {workspace.vertical_datum})"
    )
    attribution = (
        " | ".join(workspace.attributions)
        if workspace.attributions
        else "Map data © OpenStreetMap contributors (ODbL)"
    )

    with (
        staged_archive(
            resolved_out,
            sidecar_suffixes=("-journal", "-wal", "-shm", ".aux.xml"),
        ) as staged,
        tempfile.TemporaryDirectory(prefix="gridforge_osmand_") as tmp_dir_str,
    ):
        if mbtiles_path is not None and Path(mbtiles_path).is_file():
            src_mbtiles = Path(mbtiles_path).resolve()
        else:
            tmp_dir = Path(tmp_dir_str)
            src_mbtiles = tmp_dir / "intermediate.mbtiles"
            export_mbtiles(
                workspace,
                src_mbtiles,
                tile_format=tile_format,
                jpeg_quality=jpeg_quality,
                dpi=dpi,
                include_grid=include_grid,
                collarless_tif=collarless_tif,
            )

        tile_count, min_zoom, max_zoom, max_tile_bytes = _write_osmand_from_mbtiles(
            src_mbtiles,
            staged,
            sheet_name=sheet_name,
            description=description,
            attribution=attribution,
        )

        elapsed = round(time.perf_counter() - t0, 3)
        return ExportResult(
            format=ExportFormat.OSMAND,
            output_path=caller_out,
            file_size_bytes=staged.stat().st_size,
            tile_count=tile_count,
            min_zoom=min_zoom,
            max_zoom=max_zoom,
            max_tile_px=(256, 256),
            max_tile_bytes=max_tile_bytes,
            elapsed_seconds=elapsed,
        )
