"""Shared OSM XML / PBF normalization into a canonical UTM GeoPackage."""

from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
from pathlib import Path
from typing import Final

from gridforge.config import SheetLayout
from gridforge.subprocess_env import build_safe_subprocess_env

logger = logging.getLogger(__name__)

_GPKG_ATTR_INDEXES: Final[tuple[tuple[str, str], ...]] = (
    ("multipolygons", "building"),
    ("multipolygons", "natural"),
    ("multipolygons", "landuse"),
    ("multipolygons", "waterway"),
    ("multipolygons", "amenity"),
    ("multipolygons", "aeroway"),
    ("lines", "highway"),
    ("lines", "waterway"),
    ("lines", "railway"),
    ("points", "natural"),
    ("points", "place"),
    ("points", "amenity"),
)

CORE_OSM_LAYERS: Final[tuple[str, ...]] = (
    "points",
    "lines",
    "multilinestrings",
    "multipolygons",
)

# Matches config/osmconf.ini (including Phase 2 columns and literal "tower:type") so
# downstream PyQGIS rule expressions never fail on sparse/empty layers.
_CORE_LAYER_SCHEMAS: Final[dict[str, tuple[str, tuple[str, ...]]]] = {
    "points": (
        "MULTIPOINT",
        (
            "osm_id",
            "name",
            "highway",
            "ref",
            "exit_to",
            "junction",
            "railway",
            "aeroway",
            "natural",
            "ele",
            "amenity",
            "building",
            "religion",
            "emergency",
            "shop",
            "historic",
            "place",
            "power",
            "man_made",
            "tower:type",
            "communication",
            "tourism",
            "waterway",
            "barrier",
            "ford",
            "other_tags",
        ),
    ),
    "lines": (
        "MULTILINESTRING",
        (
            "osm_id",
            "name",
            "highway",
            "ref",
            "surface",
            "tracktype",
            "railway",
            "aeroway",
            "waterway",
            "intermittent",
            "power",
            "voltage",
            "man_made",
            "substance",
            "location",
            "bridge",
            "tunnel",
            "layer",
            "oneway",
            "natural",
            "barrier",
            "ford",
            "z_order",
            "other_tags",
        ),
    ),
    "multilinestrings": (
        "MULTILINESTRING",
        (
            "osm_id",
            "name",
            "type",
            "highway",
            "ref",
            "waterway",
            "railway",
            "route",
            "other_tags",
        ),
    ),
    "multipolygons": (
        "MULTIPOLYGON",
        (
            "osm_id",
            "osm_way_id",
            "name",
            "type",
            "natural",
            "landuse",
            "leaf_type",
            "wood",
            "leaf_cycle",
            "water",
            "waterway",
            "building",
            "amenity",
            "religion",
            "emergency",
            "leisure",
            "aeroway",
            "power",
            "man_made",
            "boundary",
            "other_tags",
        ),
    ),
}


def find_osmconf_path(osmconf_path: Path | None = None) -> Path:
    """Locate ``config/osmconf.ini`` via explicit path, env override, or package/repo roots."""
    if osmconf_path is not None:
        resolved = Path(osmconf_path).expanduser().resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"Explicit osmconf.ini not found: {resolved}")
        return resolved

    env_override = os.environ.get("GRIDFORGE_OSMCONF")
    candidates: list[Path] = []
    if env_override:
        candidates.append(Path(env_override).expanduser())

    # src/gridforge/pipeline/normalize.py -> parents[3] is repo root
    candidates.extend(
        [
            Path(__file__).resolve().parents[3] / "config" / "osmconf.ini",
            Path.cwd() / "config" / "osmconf.ini",
            Path("/app/config/osmconf.ini"),
        ]
    )

    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()

    searched = ", ".join(str(c) for c in candidates)
    raise FileNotFoundError(f"Could not locate config/osmconf.ini (searched: {searched})")


def _ensure_core_layers_and_columns(gpkg_path: Path, utm_epsg: int) -> None:
    """Guarantee all 4 core OSM layers and expected cartographic columns exist in ``gpkg_path``."""
    from osgeo import ogr, osr  # type: ignore[import-untyped]

    ogr.UseExceptions()
    geom_type_map = {
        "MULTIPOINT": ogr.wkbMultiPoint,
        "MULTILINESTRING": ogr.wkbMultiLineString,
        "MULTIPOLYGON": ogr.wkbMultiPolygon,
    }

    srs = osr.SpatialReference()
    srs.ImportFromEPSG(utm_epsg)

    ds = ogr.Open(str(gpkg_path), 1)
    if ds is None:
        driver = ogr.GetDriverByName("GPKG")
        ds = driver.CreateDataSource(str(gpkg_path))
    if ds is None:
        raise RuntimeError(f"Failed to open or create GeoPackage at {gpkg_path}")

    try:
        for layer_name, (geom_name, expected_fields) in _CORE_LAYER_SCHEMAS.items():
            layer = ds.GetLayerByName(layer_name)
            if layer is None:
                logger.debug("Creating missing empty OSM layer '%s' in %s", layer_name, gpkg_path)
                layer = ds.CreateLayer(
                    layer_name,
                    srs=srs,
                    geom_type=geom_type_map[geom_name],
                )
                if layer is None:
                    raise RuntimeError(f"Failed to create layer '{layer_name}' in {gpkg_path}")

            defn = layer.GetLayerDefn()
            existing = {defn.GetFieldDefn(i).GetName() for i in range(defn.GetFieldCount())}
            for field_name in expected_fields:
                if field_name not in existing:
                    field_type = ogr.OFTInteger if field_name == "z_order" else ogr.OFTString
                    layer.CreateField(ogr.FieldDefn(field_name, field_type))
    finally:
        ds = None


def _create_gpkg_attribute_indexes(gpkg_path: Path) -> None:
    """Create lightweight SQLite attribute indexes accelerating PyQGIS subset filtering."""
    conn = sqlite3.connect(str(gpkg_path))
    try:
        conn.execute("PRAGMA synchronous=OFF;")
        for table, col in _GPKG_ATTR_INDEXES:
            cols = {
                str(r[1])
                for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
            }
            if col in cols:
                conn.execute(
                    f'CREATE INDEX IF NOT EXISTS "idx_{table}_{col}" ON "{table}" ("{col}")'
                )
        conn.commit()
    except sqlite3.Error as exc:
        logger.debug("Skipping optional GeoPackage attribute indexes for %s: %s", gpkg_path, exc)
    finally:
        conn.close()


def normalize_osm_to_gpkg(
    osm_path: Path,
    layout: SheetLayout,
    output_gpkg: Path,
    osmconf_path: Path | None = None,
) -> Path:
    """Normalize an Overpass ``.osm`` XML or offline ``.osm.pbf`` into a canonical UTM GeoPackage.

    Both online Overpass and air-gapped PBF inputs pass through this exact function,
    guaranteeing identical layer names (``points``, ``lines``, ``multilinestrings``,
    ``multipolygons``), identical columns from ``config/osmconf.ini``, and identical
    ``EPSG:{layout.utm_epsg}`` projection clipped to ``layout.utm_fetch_bbox``.
    """
    src_path = Path(osm_path).expanduser().resolve()
    if not src_path.is_file():
        raise FileNotFoundError(f"OSM input file does not exist: {src_path}")

    osmconf = find_osmconf_path(osmconf_path)
    out_path = Path(output_gpkg)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()

    b = layout.utm_fetch_bbox
    cmd = [
        "ogr2ogr",
        "-f",
        "GPKG",
        "-gt",
        "65536",
        "-t_srs",
        f"EPSG:{layout.utm_epsg}",
        "-clipdst",
        f"{b.min_x}",
        f"{b.min_y}",
        f"{b.max_x}",
        f"{b.max_y}",
        "-nlt",
        "PROMOTE_TO_MULTI",
        "-makevalid",
        "-overwrite",
        str(out_path),
        str(src_path),
        *CORE_OSM_LAYERS,
    ]

    env = build_safe_subprocess_env(
        extra={
            "OSM_CONFIG_FILE": str(osmconf),
            "OSM_USE_CUSTOM_INDEXING": "NO",
            "OGR_SQLITE_SYNCHRONOUS": "OFF",
        },
        allow_network=False,
    )

    logger.info("Normalizing OSM %s -> %s (EPSG:%d)", src_path.name, out_path, layout.utm_epsg)
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=300, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"ogr2ogr OSM normalization failed (exit {proc.returncode}):\n"
            f"cmd: {' '.join(cmd)}\n"
            f"stderr: {proc.stderr.strip()}"
        )

    _ensure_core_layers_and_columns(out_path, layout.utm_epsg)
    _create_gpkg_attribute_indexes(out_path)
    return out_path
