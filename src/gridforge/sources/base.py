"""Data source protocol interfaces (on-demand cloud-native and air-gapped local)."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from gridforge.config import SheetLayout


@runtime_checkable
class ElevationSource(Protocol):
    """Protocol for pluggable elevation (DEM) data sources."""

    name: str
    attribution: str

    def fetch_dem(self, layout: SheetLayout, work_dir: Path) -> Path:
        """Fetch or clip a DEM covering ``layout.wgs84_fetch_bbox`` / ``layout.utm_fetch_bbox``."""
        ...


@runtime_checkable
class VectorSource(Protocol):
    """Protocol for pluggable OpenStreetMap vector data sources."""

    name: str
    attribution: str

    def fetch_osm(self, layout: SheetLayout, work_dir: Path) -> Path:
        """Fetch or extract OSM data (XML or PBF) covering ``layout.wgs84_fetch_bbox``."""
        ...


@runtime_checkable
class OverlaySource(Protocol):
    """Protocol for optional user or thematic overlay data sources."""

    name: str
    attribution: str

    def fetch_overlay(self, layout: SheetLayout, work_dir: Path) -> Path | None:
        """Prepare an optional overlay GeoPackage projected to ``EPSG:{layout.utm_epsg}``."""
        ...
