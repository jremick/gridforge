"""Backward-compatible re-export of the 29-region offline pack catalog."""

from __future__ import annotations

from gridforge.packs.regions import (
    ALL_REGIONS,
    AUSTRALIA_REGIONS,
    DEFAULT_AU_PBF_URL,
    DEFAULT_NZ_PBF_URL,
    REGION_GROUPS,
    PackDemTier,
    RegionCategory,
    RegionCountry,
    RegionGroup,
    RegionPreset,
    compute_utm_zones_for_bbox,
    list_regions,
    resolve_region,
)

__all__ = [
    "ALL_REGIONS",
    "AUSTRALIA_REGIONS",
    "DEFAULT_AU_PBF_URL",
    "DEFAULT_NZ_PBF_URL",
    "REGION_GROUPS",
    "PackDemTier",
    "RegionCategory",
    "RegionCountry",
    "RegionGroup",
    "RegionPreset",
    "compute_utm_zones_for_bbox",
    "list_regions",
    "resolve_region",
]
