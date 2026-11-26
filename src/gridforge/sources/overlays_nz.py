"""New Zealand DOC & LINZ CC BY 4.0 backcountry trail, hut, campsite, and peak/trig overlays."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

import httpx

from gridforge.config import BBox, NZOverlayKind, SheetLayout
from gridforge.sources.base import OverlaySource
from gridforge.sources.overlays_au import (
    _BaseAUOverlaySource,
    _bbox_hash,
    _clip_and_reproject_local_gpkg,
    _prop_ci,
    _query_arcgis_geojson,
    _round_enclosing_bbox,
)
from gridforge.sources.overlays_au_states import (
    bbox_intersects,
    clean_feature_name,
    parse_elev_m,
    query_wfs_geojson,
    write_state_trails_gpkg,
)

logger = logging.getLogger(__name__)

NZ_OVERLAY_COVERAGE_BBOX: Final[BBox] = BBox(min_x=166.0, min_y=-47.5, max_x=179.0, max_y=-34.0)

# New Zealand Department of Conservation (DOC) official CC BY 4.0 ArcGIS FeatureServers
DEFAULT_DOC_BASE_URL: Final[str] = (
    "https://services1.arcgis.com/3JjYDyG3oajxU6HO/arcgis/rest/services"
)

# LINZ official CC BY 4.0 AGOL NZ Gazetteer Peaks/Maunga (zero-auth)
DEFAULT_LINZ_MAUNGA_URL: Final[str] = (
    "https://services.arcgis.com/xdsHIIxuCWByZiCB/arcgis/rest/services/NZ_Maunga/FeatureServer/0/query"
)


def is_in_nz_overlay_coverage(bbox: BBox) -> bool:
    """Return True if ``bbox`` (WGS84) intersects New Zealand DOC / LINZ overlay bounds."""
    return bbox_intersects(bbox, NZ_OVERLAY_COVERAGE_BBOX)


class NZDocTrailsSource(_BaseAUOverlaySource):
    """DOC Great Walks, Tramping Tracks, Backcountry Huts, Campsites & LINZ Peaks/Trigs (CC BY 4.0).

    Writes the exact unified 2-layer UTM GeoPackage schema (``au_state_trails`` +
    ``au_state_pois`` with ``state='NZ'``) so Color & B&W cartographic styles render
    New Zealand backcountry features seamlessly with zero style duplication.
    """

    name: str = "nz-doc-trails"
    overlay_key: str = "nz-doc-trails"
    attribution: str = (
        "NZ Tracks, Huts & Peaks: © Department of Conservation Te Papa Atawhai "
        "& Toitū Te Whenua LINZ (CC BY 4.0)"
    )

    def __init__(
        self,
        *,
        doc_base_url: str | None = None,
        linz_maunga_url: str | None = None,
        linz_api_key: str | None = None,
        cache_dir: Path | str | None = None,
        local_gpkg: Path | str | None = None,
        timeout_s: float = 25.0,
        client: httpx.Client | None = None,
    ) -> None:
        super().__init__(
            cache_dir=cache_dir,
            local_gpkg=local_gpkg,
            timeout_s=timeout_s,
            client=client,
        )
        base_cache = Path(
            cache_dir
            if cache_dir is not None
            else os.environ.get("GRIDFORGE_CACHE_DIR", "/tmp/gridforge-cache")
        )
        self.cache_dir = base_cache / "nz_overlays"
        self.doc_base_url = (
            doc_base_url or os.environ.get("GRIDFORGE_NZ_DOC_BASE_URL", DEFAULT_DOC_BASE_URL)
        ).rstrip("/")
        self.linz_maunga_url = (
            linz_maunga_url or os.environ.get("GRIDFORGE_NZ_LINZ_MAUNGA_URL", DEFAULT_LINZ_MAUNGA_URL)
        )
        self.linz_api_key = linz_api_key or os.environ.get("LINZ_API_KEY")

    def fetch_overlay(self, layout: SheetLayout, work_dir: Path) -> Path | None:
        work_dir.mkdir(parents=True, exist_ok=True)
        out_gpkg = work_dir / "nz_doc_trails.gpkg"
        if self.local_gpkg is not None:
            return _clip_and_reproject_local_gpkg(
                self.local_gpkg,
                layout,
                out_gpkg,
                ("au_state_trails", "au_state_pois"),
            )

        if not is_in_nz_overlay_coverage(layout.wgs84_fetch_bbox):
            logger.debug("Sheet bbox outside New Zealand coverage; skipping nz-doc-trails")
            return None

        query_bbox = _round_enclosing_bbox(layout.wgs84_fetch_bbox)
        bhash = _bbox_hash(query_bbox)

        track_feats = _query_arcgis_geojson(
            [f"{self.doc_base_url}/DOC_Tracks_Open_Data/FeatureServer/0/query"],
            query_bbox,
            cache_path=self.cache_dir / f"nz_doc_tracks_{bhash}.json",
            out_fields="TechObjectName,ObjectType,SubObjectType",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        road_feats = _query_arcgis_geojson(
            [f"{self.doc_base_url}/DOC_Roads_Open_Data/FeatureServer/0/query"],
            query_bbox,
            cache_path=self.cache_dir / f"nz_doc_roads_{bhash}.json",
            out_fields="TechObjectName,ObjectType,SubObjectType",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        hut_feats = _query_arcgis_geojson(
            [f"{self.doc_base_url}/DOC_Huts_Open_Data/FeatureServer/0/query"],
            query_bbox,
            cache_path=self.cache_dir / f"nz_doc_huts_{bhash}.json",
            out_fields="TechObjectName,ObjectType,SubObjectType",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        camp_feats = _query_arcgis_geojson(
            [f"{self.doc_base_url}/DOC_Campsites_Open_Data/FeatureServer/0/query"],
            query_bbox,
            cache_path=self.cache_dir / f"nz_doc_campsites_{bhash}.json",
            out_fields="TechObjectName,ObjectType,SubObjectType",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        maunga_feats = _query_arcgis_geojson(
            [self.linz_maunga_url],
            query_bbox,
            cache_path=self.cache_dir / f"nz_linz_maunga_{bhash}.json",
            out_fields="name,feat_type,height,desc_code",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )

        # Optional live LINZ Data Service WFS Geodetic Marks when user supplies LINZ_API_KEY
        lds_trig_feats: list[dict[str, Any]] = []
        if self.linz_api_key:
            lds_wfs_url = f"https://data.linz.govt.nz/services;key={self.linz_api_key}/wfs"
            lds_trig_feats = query_wfs_geojson(
                lds_wfs_url,
                "layer-50787",
                query_bbox,
                cache_path=self.cache_dir / f"nz_linz_geodetic_{bhash}.json",
                use_bbox_param=True,
                timeout_s=self.timeout_s,
                client=self._injected_client,
            )

        trail_rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for feat in track_feats:
            props = feat.get("properties") or {}
            name = clean_feature_name(_prop_ci(props, "TechObjectName", "name"))
            trail_rows.append(
                (
                    feat["geometry"],
                    {
                        "name": name,
                        "trail_class": "walking_track",
                        "surface": "natural",
                        "state": "NZ",
                    },
                )
            )

        for feat in road_feats:
            props = feat.get("properties") or {}
            name = clean_feature_name(_prop_ci(props, "TechObjectName", "name"))
            subtype = str(_prop_ci(props, "SubObjectType") or "").strip().lower()
            lower_name = name.lower()
            if "fire" in lower_name:
                trail_class = "fire_trail"
            elif "4wd" in subtype or "4wd" in lower_name:
                trail_class = "4wd_track"
            else:
                trail_class = "management_trail"
            trail_rows.append(
                (
                    feat["geometry"],
                    {
                        "name": name,
                        "trail_class": trail_class,
                        "surface": "unsealed",
                        "state": "NZ",
                    },
                )
            )

        poi_rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for feat in hut_feats:
            props = feat.get("properties") or {}
            name = clean_feature_name(_prop_ci(props, "TechObjectName", "name"))
            poi_rows.append(
                (
                    feat["geometry"],
                    {
                        "name": name or "DOC Hut",
                        "poi_type": "hut",
                        "elev_m": None,
                        "state": "NZ",
                    },
                )
            )

        for feat in camp_feats:
            props = feat.get("properties") or {}
            name = clean_feature_name(_prop_ci(props, "TechObjectName", "name"))
            poi_rows.append(
                (
                    feat["geometry"],
                    {
                        "name": name or "DOC Campsite",
                        "poi_type": "campground",
                        "elev_m": None,
                        "state": "NZ",
                    },
                )
            )

        for feat in lds_trig_feats:
            props = feat.get("properties") or {}
            name = clean_feature_name(_prop_ci(props, "current_mark_name", "geodetic_code", "name"))
            if not name:
                continue
            poi_rows.append(
                (
                    feat["geometry"],
                    {
                        "name": name,
                        "poi_type": "trig",
                        "elev_m": parse_elev_m(_prop_ci(props, "ellipsoidal_height", "height", "elev_m")),
                        "state": "NZ",
                    },
                )
            )

        for feat in maunga_feats:
            props = feat.get("properties") or {}
            name = clean_feature_name(_prop_ci(props, "name"))
            if not name:
                continue
            poi_rows.append(
                (
                    feat["geometry"],
                    {
                        "name": name,
                        "poi_type": "trig",
                        "elev_m": parse_elev_m(_prop_ci(props, "elevation", "height", "elev_m")),
                        "state": "NZ",
                    },
                )
            )

        return write_state_trails_gpkg(out_gpkg, layout, trail_rows, poi_rows)


def build_nz_overlay_sources(
    kinds: Sequence[NZOverlayKind | str],
    local_overrides: Mapping[str | NZOverlayKind, Path | str] | None = None,
    cache_dir: Path | str | None = None,
) -> dict[str, OverlaySource]:
    """Instantiate requested New Zealand CC BY 4.0 overlay sources keyed by ``NZOverlayKind.value``."""
    normalized_overrides: dict[str, Path] = {}
    if local_overrides:
        for raw_key, raw_path in local_overrides.items():
            key_str = (
                raw_key.value
                if isinstance(raw_key, NZOverlayKind)
                else str(raw_key).lower().replace("_", "-")
            )
            normalized_overrides[key_str] = Path(raw_path)

    registry: dict[NZOverlayKind, type[_BaseAUOverlaySource]] = {
        NZOverlayKind.NZ_DOC_TRAILS: NZDocTrailsSource,
    }

    sources: dict[str, OverlaySource] = {}
    for raw_kind in kinds:
        kind = (
            raw_kind
            if isinstance(raw_kind, NZOverlayKind)
            else NZOverlayKind(str(raw_kind).lower().replace("_", "-"))
        )
        if kind.value in sources:
            continue
        cls = registry[kind]
        sources[kind.value] = cls(
            cache_dir=cache_dir,
            local_gpkg=normalized_overrides.get(kind.value),
        )
    return sources
