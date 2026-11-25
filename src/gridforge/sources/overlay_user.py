"""User-supplied GeoJSON, GPX, and CSV overlay vector source."""

from __future__ import annotations

import csv
import logging
import subprocess
from pathlib import Path
from typing import Final, Sequence

from gridforge.config import SheetLayout

logger = logging.getLogger(__name__)

SUPPORTED_OVERLAY_EXTENSIONS: Final[tuple[str, ...]] = (
    ".geojson",
    ".json",
    ".gpx",
    ".csv",
    ".gpkg",
    ".kml",
)

_GEOM_BUCKETS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("overlay_points", ("POINT", "MULTIPOINT")),
    ("overlay_lines", ("LINESTRING", "MULTILINESTRING")),
    ("overlay_polygons", ("POLYGON", "MULTIPOLYGON")),
)

_GPX_LAYER_MAP: Final[tuple[tuple[str, str], ...]] = (
    ("waypoints", "overlay_points"),
    ("tracks", "overlay_lines"),
    ("routes", "overlay_lines"),
)

_LON_CANDIDATES: Final[set[str]] = {"lon", "lng", "long", "longitude", "x", "easting"}
_LAT_CANDIDATES: Final[set[str]] = {"lat", "latitude", "y", "northing"}


def _detect_csv_lat_lon_columns(csv_path: Path) -> tuple[str, str]:
    """Inspect a CSV header row and return ``(lon_col, lat_col)``."""
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
    if not header:
        raise ValueError(f"CSV overlay file is empty: {csv_path}")

    lon_col: str | None = None
    lat_col: str | None = None
    for col in header:
        cleaned = col.strip()
        lower = cleaned.lower()
        if lon_col is None and (lower in _LON_CANDIDATES or lower.startswith(("lon", "lng"))):
            lon_col = cleaned
        if lat_col is None and (lower in _LAT_CANDIDATES or lower.startswith("lat")):
            lat_col = cleaned

    if lon_col is None or lat_col is None:
        raise ValueError(
            f"CSV overlay {csv_path} must contain longitude and latitude columns "
            f"(found headers: {header})"
        )
    return lon_col, lat_col


def _run_ogr2ogr(cmd: list[str], context: str) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"{context} failed (exit {proc.returncode}):\n"
            f"cmd: {' '.join(cmd)}\n"
            f"stderr: {proc.stderr.strip()}"
        )


def _list_ogr_layers_with_counts(src_path: Path) -> list[tuple[str, int]]:
    """Return ``(layer_name, feature_count)`` pairs for an OGR vector dataset."""
    from osgeo import ogr  # type: ignore[import-untyped]

    ogr.UseExceptions()
    ds = ogr.Open(str(src_path))
    if ds is None:
        raise RuntimeError(f"GDAL/OGR could not open overlay file: {src_path}")
    try:
        result: list[tuple[str, int]] = []
        for idx in range(ds.GetLayerCount()):
            layer = ds.GetLayerByIndex(idx)
            result.append((layer.GetName(), layer.GetFeatureCount()))
        return result
    finally:
        ds = None


def _count_matching_geometries(src_path: Path, layer_name: str, geom_types: tuple[str, ...]) -> int:
    """Count features in ``layer_name`` matching any OGR geometry type in ``geom_types``."""
    from osgeo import ogr  # type: ignore[import-untyped]

    ogr.UseExceptions()
    ds = ogr.Open(str(src_path))
    if ds is None:
        return 0
    try:
        quoted = layer_name.replace('"', '""')
        in_clause = ", ".join(f"'{g}'" for g in geom_types)
        sql = f'SELECT COUNT(*) FROM "{quoted}" WHERE OGR_GEOMETRY IN ({in_clause})'
        res_layer = ds.ExecuteSQL(sql, dialect="OGRSQL")
        if res_layer is None:
            return 0
        try:
            feat = res_layer.GetNextFeature()
            return int(feat.GetField(0)) if feat is not None else 0
        finally:
            ds.ReleaseResultSet(res_layer)
    finally:
        ds = None


def _gpkg_has_features(gpkg_path: Path) -> bool:
    if not gpkg_path.is_file():
        return False
    return any(count > 0 for _, count in _list_ogr_layers_with_counts(gpkg_path))


class UserOverlaySource:
    """Overlay source converting user GeoJSON, GPX, or CSV files into projected ``overlays.gpkg``."""

    name: str = "user-overlay"
    attribution: str = ""

    def __init__(
        self,
        paths: Path | str | Sequence[Path | str],
        attribution: str = "",
        overwrite: bool = False,
    ) -> None:
        if isinstance(paths, (str, Path)):
            self.paths: list[Path] = [Path(paths)]
        else:
            self.paths = [Path(p) for p in paths]
        self.attribution = attribution
        self.overwrite = overwrite

    def _base_ogr_cmd(self, layout: SheetLayout, out_gpkg: Path) -> list[str]:
        b = layout.utm_fetch_bbox
        cmd = [
            "ogr2ogr",
            "-f",
            "GPKG",
            "-t_srs",
            f"EPSG:{layout.utm_epsg}",
            "-clipdst",
            f"{b.min_x}",
            f"{b.min_y}",
            f"{b.max_x}",
            f"{b.max_y}",
            "-nlt",
            "PROMOTE_TO_MULTI",
        ]
        if out_gpkg.exists():
            cmd.extend(["-update", "-append", "-addfields"])
        return cmd

    def _ingest_csv(self, src_path: Path, layout: SheetLayout, out_gpkg: Path) -> None:
        lon_col, lat_col = _detect_csv_lat_lon_columns(src_path)
        cmd = self._base_ogr_cmd(layout, out_gpkg)
        cmd.extend(
            [
                "-s_srs",
                "EPSG:4326",
                "-oo",
                f"X_POSSIBLE_NAMES={lon_col}",
                "-oo",
                f"Y_POSSIBLE_NAMES={lat_col}",
                "-oo",
                "KEEP_GEOM_COLUMNS=NO",
                "-nln",
                "overlay_points",
                str(out_gpkg),
                str(src_path),
            ]
        )
        _run_ogr2ogr(cmd, context=f"ogr2ogr CSV overlay ({src_path.name})")

    def _ingest_gpx(self, src_path: Path, layout: SheetLayout, out_gpkg: Path) -> None:
        counts = dict(_list_ogr_layers_with_counts(src_path))
        for gpx_layer, target_layer in _GPX_LAYER_MAP:
            if counts.get(gpx_layer, 0) <= 0:
                continue
            cmd = self._base_ogr_cmd(layout, out_gpkg)
            cmd.extend(
                [
                    "-s_srs",
                    "EPSG:4326",
                    "-nln",
                    target_layer,
                    str(out_gpkg),
                    str(src_path),
                    gpx_layer,
                ]
            )
            _run_ogr2ogr(cmd, context=f"ogr2ogr GPX overlay ({src_path.name}:{gpx_layer})")

    def _ingest_generic_vector(self, src_path: Path, layout: SheetLayout, out_gpkg: Path) -> None:
        layers = _list_ogr_layers_with_counts(src_path)
        for layer_name, total_count in layers:
            if total_count <= 0:
                continue
            quoted = layer_name.replace('"', '""')
            for target_layer, geom_types in _GEOM_BUCKETS:
                matched = _count_matching_geometries(src_path, layer_name, geom_types)
                if matched <= 0:
                    continue
                in_clause = ", ".join(f"'{g}'" for g in geom_types)
                sql = f'SELECT * FROM "{quoted}" WHERE OGR_GEOMETRY IN ({in_clause})'
                cmd = self._base_ogr_cmd(layout, out_gpkg)
                cmd.extend(
                    [
                        "-dialect",
                        "OGRSQL",
                        "-sql",
                        sql,
                        "-nln",
                        target_layer,
                        str(out_gpkg),
                        str(src_path),
                    ]
                )
                _run_ogr2ogr(
                    cmd,
                    context=f"ogr2ogr vector overlay ({src_path.name}:{layer_name}->{target_layer})",
                )

    def fetch_overlay(self, layout: SheetLayout, work_dir: Path) -> Path | None:
        """Convert configured overlay files into projected ``work_dir / 'overlays.gpkg'``."""
        if not self.paths:
            return None

        work_dir.mkdir(parents=True, exist_ok=True)
        out_gpkg = work_dir / "overlays.gpkg"
        if self.overwrite and out_gpkg.exists():
            out_gpkg.unlink()

        for raw_path in self.paths:
            src_path = raw_path.expanduser().resolve()
            if not src_path.is_file():
                raise FileNotFoundError(f"User overlay file does not exist: {src_path}")
            suffix = src_path.suffix.lower()
            if suffix not in SUPPORTED_OVERLAY_EXTENSIONS:
                raise ValueError(
                    f"Unsupported overlay format '{suffix}' for {src_path}. "
                    f"Supported: {', '.join(SUPPORTED_OVERLAY_EXTENSIONS)}"
                )

            logger.info("Ingesting user overlay %s into %s", src_path, out_gpkg)
            if suffix == ".csv":
                self._ingest_csv(src_path, layout, out_gpkg)
            elif suffix == ".gpx":
                self._ingest_gpx(src_path, layout, out_gpkg)
            else:
                self._ingest_generic_vector(src_path, layout, out_gpkg)

        if not _gpkg_has_features(out_gpkg):
            logger.info("No user overlay features intersected sheet footprint %s", layout.utm_fetch_bbox)
            return None

        return out_gpkg
