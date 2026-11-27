"""User-supplied GeoJSON, GPX, and CSV overlay vector source.

Web-uploaded formats (``.geojson``, ``.json``, ``.gpx``, ``.csv``) are parsed
purely in Python and written into ``overlays.gpkg`` inside a single OGR
transaction, preventing GDAL/OGR driver auto-detection attacks (such as
``OGR_VRT`` local file / SQLite exfiltration or ``/vsicurl/`` SSRF).
CLI-only formats (``.gpkg``, ``.kml``) enforce explicit driver allowlists
(``allowed_drivers`` / ``-if``) with network VSI access disabled.
"""

from __future__ import annotations

import csv
import json
import logging
import math
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Final, Sequence

from shapely.geometry import (
    LineString,
    MultiLineString,
    MultiPoint,
    MultiPolygon,
    Point,
    Polygon,
    box as shapely_box,
    shape as shapely_shape,
)
from shapely.ops import transform as shapely_transform

from gridforge.config import SheetLayout
from gridforge.geo.crs import get_wgs84_to_utm_transformer
from gridforge.subprocess_env import build_safe_subprocess_env

logger = logging.getLogger(__name__)

SUPPORTED_OVERLAY_EXTENSIONS: Final[tuple[str, ...]] = (
    ".geojson",
    ".json",
    ".gpx",
    ".csv",
    ".gpkg",
    ".kml",
)

_CLI_EXTENSION_DRIVERS: Final[dict[str, str]] = {
    ".gpkg": "GPKG",
    ".kml": "KML",
}

_GEOM_BUCKETS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("overlay_points", ("POINT", "MULTIPOINT")),
    ("overlay_lines", ("LINESTRING", "MULTILINESTRING")),
    ("overlay_polygons", ("POLYGON", "MULTIPOLYGON")),
)

_LON_CANDIDATES: Final[set[str]] = {"lon", "lng", "long", "longitude", "x", "easting"}
_LAT_CANDIDATES: Final[set[str]] = {"lat", "latitude", "y", "northing"}
_NAME_CANDIDATES: Final[tuple[str, ...]] = ("name", "title", "label", "id", "desc", "description")

_MAX_GEOJSON_DEPTH: Final[int] = 12
_MAX_FEATURES_PER_FILE: Final[int] = 50_000


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


def _extract_feature_name(props: dict[str, Any] | None) -> str:
    if not isinstance(props, dict):
        return ""
    lowered = {str(k).strip().lower(): v for k, v in props.items()}
    for cand in _NAME_CANDIDATES:
        val = lowered.get(cand)
        if val is not None:
            s = str(val).strip()
            if s:
                return s[:120]
    return ""


def _parse_csv_features(
    csv_path: Path,
) -> tuple[list[tuple[Any, str]], list[tuple[Any, str]], list[tuple[Any, str]]]:
    """Safely parse point features from a CSV file in pure Python."""
    lon_col, lat_col = _detect_csv_lat_lon_columns(csv_path)
    points: list[tuple[Any, str]] = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        raw_header = next(reader, None)
        if not raw_header:
            return points, [], []
        header = [c.strip() for c in raw_header]
        lon_idx = header.index(lon_col)
        lat_idx = header.index(lat_col)
        name_idx: int | None = None
        for cand in _NAME_CANDIDATES:
            for idx, col in enumerate(header):
                if col.lower() == cand and idx not in (lon_idx, lat_idx):
                    name_idx = idx
                    break
            if name_idx is not None:
                break

        for row in reader:
            if len(points) >= _MAX_FEATURES_PER_FILE:
                break
            if len(row) <= max(lon_idx, lat_idx):
                continue
            try:
                lon = float(row[lon_idx].strip())
                lat = float(row[lat_idx].strip())
            except ValueError:
                continue
            if not (math.isfinite(lon) and math.isfinite(lat)):
                continue
            if not (-180.0 <= lon <= 180.0 and -85.0 <= lat <= 85.0):
                continue
            name = row[name_idx].strip()[:120] if (name_idx is not None and name_idx < len(row)) else ""
            points.append((Point(lon, lat), name))
    return points, [], []


def _strip_xml_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower() if "}" in tag else tag.lower()


def _parse_gpx_features(
    gpx_path: Path,
) -> tuple[list[tuple[Any, str]], list[tuple[Any, str]], list[tuple[Any, str]]]:
    """Safely parse GPX waypoints, tracks, and routes in pure Python."""
    raw = gpx_path.read_bytes()
    if b"<!ENTITY" in raw.upper() or b"<!DOCTYPE" in raw.upper():
        raise ValueError(f"GPX overlay {gpx_path.name} contains disallowed XML DTD/ENTITY declarations.")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError(f"Invalid GPX XML in {gpx_path.name}: {exc}") from exc

    points: list[tuple[Any, str]] = []
    lines: list[tuple[Any, str]] = []

    def _child_text(elem: ET.Element, target: str) -> str:
        for ch in elem:
            if _strip_xml_tag(ch.tag) == target and ch.text:
                return ch.text.strip()[:120]
        return ""

    def _pt_coords(elem: ET.Element) -> tuple[float, float] | None:
        try:
            lat = float(elem.attrib["lat"])
            lon = float(elem.attrib["lon"])
        except (KeyError, ValueError):
            return None
        if not (math.isfinite(lat) and math.isfinite(lon)):
            return None
        if not (-85.0 <= lat <= 85.0 and -180.0 <= lon <= 180.0):
            return None
        return (lon, lat)

    for elem in root:
        tag = _strip_xml_tag(elem.tag)
        if tag == "wpt" and len(points) < _MAX_FEATURES_PER_FILE:
            xy = _pt_coords(elem)
            if xy is not None:
                points.append((Point(xy[0], xy[1]), _child_text(elem, "name")))
        elif tag == "trk" and len(lines) < _MAX_FEATURES_PER_FILE:
            trk_name = _child_text(elem, "name")
            seg_lines: list[LineString] = []
            for ch in elem:
                if _strip_xml_tag(ch.tag) == "trkseg":
                    coords = [xy for pt in ch if _strip_xml_tag(pt.tag) == "trkpt" and (xy := _pt_coords(pt)) is not None]
                    if len(coords) >= 2:
                        seg_lines.append(LineString(coords))
            if len(seg_lines) == 1:
                lines.append((seg_lines[0], trk_name))
            elif len(seg_lines) > 1:
                lines.append((MultiLineString(seg_lines), trk_name))
        elif tag == "rte" and len(lines) < _MAX_FEATURES_PER_FILE:
            rte_name = _child_text(elem, "name")
            coords = [xy for pt in elem if _strip_xml_tag(pt.tag) == "rtept" and (xy := _pt_coords(pt)) is not None]
            if len(coords) >= 2:
                lines.append((LineString(coords), rte_name))

    return points, lines, []


def _bucket_geometry(
    geom: Any,
    name: str,
    points: list[tuple[Any, str]],
    lines: list[tuple[Any, str]],
    polygons: list[tuple[Any, str]],
    *,
    depth: int = 0,
) -> None:
    if geom is None or geom.is_empty or depth > _MAX_GEOJSON_DEPTH:
        return
    gtype = geom.geom_type
    if gtype in ("Point", "MultiPoint"):
        if len(points) < _MAX_FEATURES_PER_FILE:
            points.append((geom, name))
    elif gtype in ("LineString", "MultiLineString"):
        if len(lines) < _MAX_FEATURES_PER_FILE:
            lines.append((geom, name))
    elif gtype in ("Polygon", "MultiPolygon"):
        if len(polygons) < _MAX_FEATURES_PER_FILE:
            polygons.append((geom, name))
    elif gtype == "GeometryCollection":
        for sub in geom.geoms:
            _bucket_geometry(sub, name, points, lines, polygons, depth=depth + 1)


def _parse_geojson_features(
    geojson_path: Path,
) -> tuple[list[tuple[Any, str]], list[tuple[Any, str]], list[tuple[Any, str]]]:
    """Safely parse GeoJSON features in pure Python (never touching GDAL drivers)."""
    raw_text = geojson_path.read_text(encoding="utf-8-sig")
    try:
        obj = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid GeoJSON in {geojson_path.name}: {exc}") from exc
    if not isinstance(obj, dict):
        raise ValueError(f"GeoJSON root in {geojson_path.name} must be a JSON object.")

    points: list[tuple[Any, str]] = []
    lines: list[tuple[Any, str]] = []
    polygons: list[tuple[Any, str]] = []

    def _visit_obj(node: dict[str, Any], depth: int = 0) -> None:
        if depth > _MAX_GEOJSON_DEPTH:
            raise ValueError(f"GeoJSON nesting depth exceeded ({_MAX_GEOJSON_DEPTH}).")
        ntype = node.get("type")
        if ntype == "FeatureCollection":
            feats = node.get("features")
            if not isinstance(feats, list):
                return
            for item in feats[:_MAX_FEATURES_PER_FILE]:
                if isinstance(item, dict):
                    _visit_obj(item, depth + 1)
        elif ntype == "Feature":
            geom_dict = node.get("geometry")
            if isinstance(geom_dict, dict):
                name = _extract_feature_name(node.get("properties"))
                try:
                    geom = shapely_shape(geom_dict)
                except (ValueError, TypeError, AttributeError):
                    return
                _bucket_geometry(geom, name, points, lines, polygons, depth=depth + 1)
        elif ntype in {
            "Point",
            "MultiPoint",
            "LineString",
            "MultiLineString",
            "Polygon",
            "MultiPolygon",
            "GeometryCollection",
        }:
            try:
                geom = shapely_shape(node)
            except (ValueError, TypeError, AttributeError):
                return
            _bucket_geometry(geom, "", points, lines, polygons, depth=depth + 1)
        else:
            raise ValueError(f"Unsupported GeoJSON object type '{ntype}' in {geojson_path.name}.")

    _visit_obj(obj)
    return points, lines, polygons


def _promote_to_multi(geom: Any) -> Any:
    gtype = geom.geom_type
    if gtype == "Point":
        return MultiPoint([geom])
    if gtype == "LineString":
        return MultiLineString([geom])
    if gtype == "Polygon":
        return MultiPolygon([geom])
    return geom


def _write_projected_features_gpkg(
    out_gpkg: Path,
    layout: SheetLayout,
    *,
    points: list[tuple[Any, str]],
    lines: list[tuple[Any, str]],
    polygons: list[tuple[Any, str]],
) -> None:
    """Reproject, clip, and append parsed overlay geometries into ``overlays.gpkg`` in one transaction."""
    from osgeo import ogr, osr  # type: ignore[import-untyped]  # system python3-gdal ships untyped osgeo bindings

    if not (points or lines or polygons):
        return

    transformer = get_wgs84_to_utm_transformer(layout.utm_epsg)
    b = layout.utm_fetch_bbox
    clip_box = shapely_box(b.min_x, b.min_y, b.max_x, b.max_y)

    def _project_and_clip(
        items: list[tuple[Any, str]],
        expected_types: tuple[str, ...],
    ) -> list[tuple[bytes, str]]:
        out: list[tuple[bytes, str]] = []
        for geom_wgs84, name in items:
            try:
                utm_geom = shapely_transform(transformer.transform, geom_wgs84)
                if not utm_geom.is_valid:
                    utm_geom = utm_geom.buffer(0) if utm_geom.geom_type in ("Polygon", "MultiPolygon") else utm_geom
                clipped = utm_geom.intersection(clip_box)
            except Exception:
                continue
            if clipped.is_empty:
                continue
            if clipped.geom_type == "GeometryCollection":
                parts = [g for g in clipped.geoms if g.geom_type in expected_types and not g.is_empty]
                for p in parts:
                    out.append((_promote_to_multi(p).wkb, name))
            elif clipped.geom_type in expected_types:
                out.append((_promote_to_multi(clipped).wkb, name))
        return out

    proj_points = _project_and_clip(points, ("Point", "MultiPoint"))
    proj_lines = _project_and_clip(lines, ("LineString", "MultiLineString"))
    proj_polygons = _project_and_clip(polygons, ("Polygon", "MultiPolygon"))

    if not (proj_points or proj_lines or proj_polygons):
        return

    ogr.UseExceptions()
    osr.UseExceptions()
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(layout.utm_epsg)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

    drv = ogr.GetDriverByName("GPKG")
    if out_gpkg.exists():
        ds = drv.Open(str(out_gpkg), 1)
    else:
        ds = drv.CreateDataSource(str(out_gpkg))
    if ds is None:
        raise RuntimeError(f"Failed to open/create GeoPackage: {out_gpkg}")

    layer_specs = (
        ("overlay_points", ogr.wkbMultiPoint, proj_points),
        ("overlay_lines", ogr.wkbMultiLineString, proj_lines),
        ("overlay_polygons", ogr.wkbMultiPolygon, proj_polygons),
    )

    try:
        ds.StartTransaction()
        try:
            for layer_name, geom_type, rows in layer_specs:
                if not rows:
                    continue
                layer = ds.GetLayerByName(layer_name)
                if layer is None:
                    layer = ds.CreateLayer(layer_name, srs=srs, geom_type=geom_type)
                    layer.CreateField(ogr.FieldDefn("name", ogr.OFTString))
                defn = layer.GetLayerDefn()
                has_name = defn.GetFieldIndex("name") >= 0
                for wkb_bytes, name in rows:
                    feat = ogr.Feature(defn)
                    ogr_geom = ogr.CreateGeometryFromWkb(wkb_bytes)
                    if ogr_geom is None:
                        continue
                    feat.SetGeometry(ogr_geom)
                    if has_name:
                        feat.SetField("name", name)
                    layer.CreateFeature(feat)
            ds.CommitTransaction()
        except Exception:
            ds.RollbackTransaction()
            raise
    finally:
        ds = None


def _run_ogr2ogr(cmd: list[str], context: str, *, timeout: int = 120) -> None:
    env = build_safe_subprocess_env(allow_network=False)
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"{context} failed (exit {proc.returncode}):\n"
            f"cmd: {' '.join(cmd)}\n"
            f"stderr: {proc.stderr.strip()}"
        )


def _list_ogr_layers_with_counts(
    src_path: Path,
    *,
    allowed_drivers: list[str] | None = None,
) -> list[tuple[str, int]]:
    """Return ``(layer_name, feature_count)`` pairs for an OGR vector dataset."""
    from osgeo import gdal  # type: ignore[import-untyped]  # system python3-gdal ships untyped osgeo bindings

    gdal.UseExceptions()
    ds = gdal.OpenEx(
        str(src_path),
        gdal.OF_VECTOR | gdal.OF_READONLY,
        allowed_drivers=allowed_drivers,
    )
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


def _count_matching_geometries(
    src_path: Path,
    layer_name: str,
    geom_types: tuple[str, ...],
    *,
    allowed_drivers: list[str] | None = None,
) -> int:
    """Count features in ``layer_name`` matching any OGR geometry type in ``geom_types``."""
    from osgeo import gdal  # type: ignore[import-untyped]  # system python3-gdal ships untyped osgeo bindings

    gdal.UseExceptions()
    ds = gdal.OpenEx(
        str(src_path),
        gdal.OF_VECTOR | gdal.OF_READONLY,
        allowed_drivers=allowed_drivers,
    )
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
    return any(
        count > 0
        for _, count in _list_ogr_layers_with_counts(gpkg_path, allowed_drivers=["GPKG"])
    )


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

    def _base_ogr_cmd(self, layout: SheetLayout, out_gpkg: Path, *, input_driver: str) -> list[str]:
        b = layout.utm_fetch_bbox
        cmd = [
            "ogr2ogr",
            "-if",
            input_driver,
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

    def _ingest_cli_vector(
        self,
        src_path: Path,
        layout: SheetLayout,
        out_gpkg: Path,
        *,
        driver_name: str,
    ) -> None:
        allowed = [driver_name]
        layers = _list_ogr_layers_with_counts(src_path, allowed_drivers=allowed)
        for layer_name, total_count in layers:
            if total_count <= 0:
                continue
            quoted = layer_name.replace('"', '""')
            for target_layer, geom_types in _GEOM_BUCKETS:
                matched = _count_matching_geometries(
                    src_path, layer_name, geom_types, allowed_drivers=allowed
                )
                if matched <= 0:
                    continue
                in_clause = ", ".join(f"'{g}'" for g in geom_types)
                sql = f'SELECT * FROM "{quoted}" WHERE OGR_GEOMETRY IN ({in_clause})'
                cmd = self._base_ogr_cmd(layout, out_gpkg, input_driver=driver_name)
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
                pts, lns, polys = _parse_csv_features(src_path)
                _write_projected_features_gpkg(out_gpkg, layout, points=pts, lines=lns, polygons=polys)
            elif suffix == ".gpx":
                pts, lns, polys = _parse_gpx_features(src_path)
                _write_projected_features_gpkg(out_gpkg, layout, points=pts, lines=lns, polygons=polys)
            elif suffix in (".geojson", ".json"):
                pts, lns, polys = _parse_geojson_features(src_path)
                _write_projected_features_gpkg(out_gpkg, layout, points=pts, lines=lns, polygons=polys)
            else:
                self._ingest_cli_vector(
                    src_path,
                    layout,
                    out_gpkg,
                    driver_name=_CLI_EXTENSION_DRIVERS[suffix],
                )

        if not _gpkg_has_features(out_gpkg):
            logger.info("No user overlay features intersect sheet fetch bbox; skipping overlays.gpkg")
            if out_gpkg.exists():
                out_gpkg.unlink()
            return None

        return out_gpkg
