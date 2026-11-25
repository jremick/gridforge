"""Pluggable geospatial data source protocols and implementations for GridForge."""

from __future__ import annotations

from gridforge.sources.base import ElevationSource, OverlaySource, VectorSource
from gridforge.sources.dem_copernicus import CopernicusGLO30Source
from gridforge.sources.dem_local import LocalDEMSource
from gridforge.sources.dem_usgs3dep import AutoDEMSource, USGS3DEPSource
from gridforge.sources.osm_overpass import OverpassOSMSource
from gridforge.sources.osm_pbf import LocalPBFSource
from gridforge.sources.overlay_user import UserOverlaySource
from gridforge.sources.overlays_us import (
    FAATowersSource,
    FEMAFloodSource,
    NHDHydroSource,
    NOAAENCSource,
    build_us_overlay_sources,
)

__all__ = [
    "AutoDEMSource",
    "CopernicusGLO30Source",
    "ElevationSource",
    "FAATowersSource",
    "FEMAFloodSource",
    "LocalDEMSource",
    "LocalPBFSource",
    "NHDHydroSource",
    "NOAAENCSource",
    "OverpassOSMSource",
    "OverlaySource",
    "USGS3DEPSource",
    "UserOverlaySource",
    "VectorSource",
    "build_us_overlay_sources",
]
