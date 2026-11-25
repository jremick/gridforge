"""Data normalization and GDAL terrain processing pipeline for GridForge."""

from __future__ import annotations

from gridforge.pipeline.normalize import (
    CORE_OSM_LAYERS,
    find_osmconf_path,
    normalize_osm_to_gpkg,
)
from gridforge.pipeline.terrain import (
    TerrainResult,
    build_terrain_products,
    choose_contour_interval,
)

__all__ = [
    "CORE_OSM_LAYERS",
    "TerrainResult",
    "build_terrain_products",
    "choose_contour_interval",
    "find_osmconf_path",
    "normalize_osm_to_gpkg",
]
