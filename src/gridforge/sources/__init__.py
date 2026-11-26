"""Pluggable geospatial data source protocols and implementations for GridForge."""

from __future__ import annotations

from gridforge.sources.base import ElevationSource, OverlaySource, VectorSource
from gridforge.sources.dem_au_elvis import AUElvisDEMSource, is_in_australia_coverage
from gridforge.sources.dem_copernicus import CopernicusGLO30Source
from gridforge.sources.dem_local import LocalDEMSource
from gridforge.sources.dem_nz_linz import NZLinzDEMSource, is_in_nz_coverage
from gridforge.sources.dem_usgs3dep import AutoDEMSource, USGS3DEPSource, is_in_us_3dep_coverage
from gridforge.sources.osm_overpass import OverpassOSMSource
from gridforge.sources.osm_pbf import LocalPBFSource
from gridforge.sources.overlay_user import UserOverlaySource
from gridforge.sources.overlays_au import (
    CAPADParksSource,
    GAAviationSource,
    GAHydroSource,
    GAInfraSource,
    build_au_overlay_sources,
)
from gridforge.sources.overlays_au_states import (
    ACTTrailsSource,
    AutoStateTrailsSource,
    NSWTrailsSource,
    QLDTrailsSource,
    TASTrailsSource,
    VICTrailsSource,
    is_in_act_coverage,
    is_in_nsw_coverage,
    is_in_nt_coverage,
    is_in_qld_coverage,
    is_in_sa_coverage,
    is_in_tas_coverage,
    is_in_vic_coverage,
    is_in_wa_coverage,
    resolve_intersecting_states,
)
from gridforge.sources.overlays_au_states_west import (
    NTTrailsSource,
    SATrailsSource,
    WATrailsSource,
)
from gridforge.sources.overlays_nz import (
    NZDocTrailsSource,
    build_nz_overlay_sources,
    is_in_nz_overlay_coverage,
)
from gridforge.sources.overlays_us import (
    FAATowersSource,
    FEMAFloodSource,
    NHDHydroSource,
    NOAAENCSource,
    build_us_overlay_sources,
)

__all__ = [
    "ACTTrailsSource",
    "AUElvisDEMSource",
    "AutoDEMSource",
    "AutoStateTrailsSource",
    "CAPADParksSource",
    "CopernicusGLO30Source",
    "ElevationSource",
    "FAATowersSource",
    "FEMAFloodSource",
    "GAAviationSource",
    "GAHydroSource",
    "GAInfraSource",
    "LocalDEMSource",
    "LocalPBFSource",
    "NHDHydroSource",
    "NOAAENCSource",
    "NSWTrailsSource",
    "NTTrailsSource",
    "NZDocTrailsSource",
    "NZLinzDEMSource",
    "OverpassOSMSource",
    "OverlaySource",
    "QLDTrailsSource",
    "SATrailsSource",
    "TASTrailsSource",
    "USGS3DEPSource",
    "UserOverlaySource",
    "VICTrailsSource",
    "VectorSource",
    "WATrailsSource",
    "build_au_overlay_sources",
    "build_nz_overlay_sources",
    "build_us_overlay_sources",
    "is_in_act_coverage",
    "is_in_australia_coverage",
    "is_in_nsw_coverage",
    "is_in_nt_coverage",
    "is_in_nz_coverage",
    "is_in_nz_overlay_coverage",
    "is_in_qld_coverage",
    "is_in_sa_coverage",
    "is_in_tas_coverage",
    "is_in_us_3dep_coverage",
    "is_in_vic_coverage",
    "is_in_wa_coverage",
    "resolve_intersecting_states",
]
