"""Memory-safe air-gapped offline regional data pack catalog, basemap, and builder."""

from gridforge.packs.basemap_builder import (
    DEFAULT_BASEMAP_MAX_ZOOM,
    DEFAULT_BASEMAP_MIN_ZOOM,
    build_basemap_for_region,
    build_offline_basemap_mbtiles,
)
from gridforge.packs.builder import (
    DEA_SRTM_1S_URL,
    DEFAULT_AU_PBF_URL,
    build_offline_pack,
    discover_packs,
    load_pack_manifest,
    resolve_pack_directory,
)
from gridforge.packs.regions import (
    ALL_REGIONS,
    AUSTRALIA_REGIONS,
    DEFAULT_NZ_PBF_URL,
    REGION_GROUPS,
    RegionPreset,
    compute_utm_zones_for_bbox,
    list_regions,
    resolve_region,
)

__all__ = [
    "ALL_REGIONS",
    "AUSTRALIA_REGIONS",
    "DEA_SRTM_1S_URL",
    "DEFAULT_AU_PBF_URL",
    "DEFAULT_BASEMAP_MAX_ZOOM",
    "DEFAULT_BASEMAP_MIN_ZOOM",
    "DEFAULT_NZ_PBF_URL",
    "REGION_GROUPS",
    "RegionPreset",
    "build_basemap_for_region",
    "build_offline_basemap_mbtiles",
    "build_offline_pack",
    "compute_utm_zones_for_bbox",
    "discover_packs",
    "list_regions",
    "load_pack_manifest",
    "resolve_pack_directory",
    "resolve_region",
]
