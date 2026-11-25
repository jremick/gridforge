"""Memory-safe air-gapped offline regional data pack catalog and builder."""

from gridforge.packs.builder import (
    DEA_SRTM_1S_URL,
    DEFAULT_AU_PBF_URL,
    build_offline_pack,
    discover_packs,
    load_pack_manifest,
)
from gridforge.packs.catalog import (
    AUSTRALIA_REGIONS,
    RegionPreset,
    compute_utm_zones_for_bbox,
    list_regions,
    resolve_region,
)

__all__ = [
    "AUSTRALIA_REGIONS",
    "DEA_SRTM_1S_URL",
    "DEFAULT_AU_PBF_URL",
    "RegionPreset",
    "build_offline_pack",
    "compute_utm_zones_for_bbox",
    "discover_packs",
    "list_regions",
    "load_pack_manifest",
    "resolve_region",
]
