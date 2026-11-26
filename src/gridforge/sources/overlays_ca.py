"""Canada OGL-Canada / OGL-BC trail, backcountry POI, and CPCAD protected-area overlays."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

import httpx

from gridforge.config import BBox, CAOverlayKind, SheetLayout
from gridforge.sources.base import OverlaySource
from gridforge.sources.dem_ca_hrdem import is_in_canada_coverage
from gridforge.sources.overlays_au import (
    _BaseAUOverlaySource,
    _bbox_hash,
    _clip_and_reproject_local_gpkg,
    _prop_ci,
    _query_arcgis_geojson,
    _round_enclosing_bbox,
)
from gridforge.sources.overlays_us import _write_projected_gpkg_layers
from gridforge.sources.overlays_au_states import (
    bbox_intersects,
    clean_feature_name,
)

logger = logging.getLogger(__name__)

# Parks Canada Open Data ArcGIS Online org (OGL-Canada, zero-auth)
DEFAULT_PARKS_CANADA_BASE_URL: Final[str] = (
    "https://services2.arcgis.com/wCOMu5IS7YdSyPNx/arcgis/rest/services"
)

# Environment and Climate Change Canada (ECCC) CPCAD nationwide protected areas (OGL-Canada, zero-auth)
DEFAULT_CPCAD_URL: Final[str] = (
    "https://maps-cartes.ec.gc.ca/arcgis/rest/services/CWS_SCF/CPCAD/MapServer/0/query"
)

# DataBC Recreation Lines WFS (OGL-BC, zero-auth; queried only when bbox intersects British Columbia)
DEFAULT_DATABC_WFS_URL: Final[str] = "https://openmaps.gov.bc.ca/geo/pub/wfs"
BC_PROVINCE_BBOX: Final[BBox] = BBox(min_x=-139.1, min_y=48.2, max_x=-114.0, max_y=60.1)


def _query_databc_wfs_geojson(
    wfs_url: str,
    type_name: str,
    bbox: BBox,
    *,
    cache_path: Path,
    timeout_s: float = 25.0,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    """Query DataBC WFS 2.0 using standard EPSG:4326 axis-ordered ``bbox`` with disk caching."""
    import json

    if cache_path.is_file() and cache_path.stat().st_size > 0:
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(cached, list):
                return cached
        except json.JSONDecodeError:
            logger.warning("Ignoring corrupt cached DataBC WFS JSON: %s", cache_path)

    owns_client = client is None
    http = (
        client
        if client is not None
        else httpx.Client(
            timeout=httpx.Timeout(timeout_s, connect=min(8.0, timeout_s)),
            follow_redirects=True,
            headers={"User-Agent": "gridforge/1.4 (+https://github.com/jremick/gridforge)"},
        )
    )
    params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeNames": type_name,
        "srsName": "EPSG:4326",
        "bbox": f"{bbox.min_y:.6f},{bbox.min_x:.6f},{bbox.max_y:.6f},{bbox.max_x:.6f},urn:ogc:def:crs:EPSG::4326",
        "outputFormat": "application/json",
        "count": "2000",
    }
    try:
        resp = http.get(wfs_url, params=params)
        resp.raise_for_status()
        data = resp.json()
        raw_feats = data.get("features") or []
        feats = [f for f in raw_feats if isinstance(f, dict) and f.get("geometry") is not None]
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(feats), encoding="utf-8")
        tmp.replace(cache_path)
        return feats
    except Exception as exc:
        logger.warning("DataBC WFS query unavailable (%s); skipping layer cleanly", exc)
        return []
    finally:
        if owns_client:
            http.close()


def is_in_ca_overlay_coverage(bbox: BBox) -> bool:
    """Return ``True`` if ``bbox`` (WGS84) intersects Canadian overlay bounds."""
    return is_in_canada_coverage(bbox)


def _classify_ca_poi_type(raw_label: str, raw_descr: str = "", default_type: str = "lookout") -> str:
    text = f"{raw_label} {raw_descr}".lower()
    if any(k in text for k in ("hut", "cabin", "shelter", "warden", "refuge", "abri", "chalet", "bivouac")):
        return "hut"
    if any(k in text for k in ("camp", "tent", "rv ", "oTentik".lower(), "backcountry site")):
        return "campground"
    if any(k in text for k in ("picnic", "day use", "day-use", "pique-nique")):
        return "picnic"
    if any(k in text for k in ("water", "spring", "well", "tap", "eau")):
        return "water"
    if any(k in text for k in ("lookout", "viewpoint", "belvedere", "summit", "peak", "gondola", "overlook")):
        return "lookout"
    if any(k in text for k in ("trailhead", "parking", "visitor", "info", "départ")):
        return "trailhead"
    return default_type


def _classify_cpcad_designation(name: str, iucn_cat: Any) -> tuple[str, str]:
    low = name.lower()
    if "national park" in low or "parc national" in low:
        return ("Parks Canada", "National Park")
    if "wildland" in low or "wilderness" in low or str(iucn_cat) in ("1", "1a", "1b", "Ia", "Ib"):
        return ("Provincial / Federal", "Wilderness Area")
    if "provincial park" in low or "parc provincial" in low or "parc national du québec" in low:
        return ("Provincial Parks", "Provincial Park")
    if "ecological reserve" in low or "conservancy" in low or "wildlife" in low:
        return ("Conservation", "Protected Area")
    return ("Public / Protected", "Protected Area")


class CACanVecTrailsSource(_BaseAUOverlaySource):
    """Parks Canada trails/POIs, DataBC provincial backcountry trails, and ECCC CPCAD protected areas.

    Writes the unified 3-layer UTM GeoPackage schema (``au_state_trails``, ``au_state_pois``, and
    ``public_lands``, all ``state='CA'``) so Color, B&W, Night-Red, and Alpine themes render Canadian
    backcountry sheets cleanly with zero style duplication.
    """

    name: str = "ca-canvec-trails"
    overlay_key: str = "ca-canvec-trails"
    attribution: str = (
        "Canada Trails & Protected Areas: Parks Canada, ECCC CPCAD & DataBC "
        "(Open Government Licence – Canada / BC)"
    )

    def __init__(
        self,
        *,
        parks_canada_base_url: str | None = None,
        cpcad_url: str | None = None,
        databc_wfs_url: str | None = None,
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
        self.cache_dir = base_cache / "ca_overlays"
        self.parks_canada_base_url = (
            parks_canada_base_url
            or os.environ.get("GRIDFORGE_CA_PARKS_BASE_URL", DEFAULT_PARKS_CANADA_BASE_URL)
        ).rstrip("/")
        self.cpcad_url = (
            cpcad_url or os.environ.get("GRIDFORGE_CA_CPCAD_URL", DEFAULT_CPCAD_URL)
        )
        self.databc_wfs_url = (
            databc_wfs_url or os.environ.get("GRIDFORGE_CA_DATABC_WFS_URL", DEFAULT_DATABC_WFS_URL)
        )

    def fetch_overlay(self, layout: SheetLayout, work_dir: Path) -> Path | None:
        work_dir.mkdir(parents=True, exist_ok=True)
        out_gpkg = work_dir / "ca_canvec_trails.gpkg"
        if self.local_gpkg is not None:
            return _clip_and_reproject_local_gpkg(
                self.local_gpkg,
                layout,
                out_gpkg,
                ("au_state_trails", "au_state_pois", "public_lands"),
            )

        if not is_in_ca_overlay_coverage(layout.wgs84_fetch_bbox):
            logger.debug("Sheet bbox outside Canada coverage; skipping ca-canvec-trails")
            return None

        query_bbox = _round_enclosing_bbox(layout.wgs84_fetch_bbox)
        bhash = _bbox_hash(query_bbox)

        # 1. Parks Canada nationwide official trails (OGL-Canada)
        pc_trails = _query_arcgis_geojson(
            [f"{self.parks_canada_base_url}/vw_Trails_Sentiers_APCA_V2_FGP/FeatureServer/0/query"],
            query_bbox,
            cache_path=self.cache_dir / f"ca_pc_trails_{bhash}.json",
            out_fields="Label_e_20k_5k,Label_e_5k_less,Noms_Alt_Names,Official_Officiel",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )

        # 2. DataBC Provincial Recreation Trails WFS (OGL-BC, BC only)
        bc_trails: list[dict[str, Any]] = []
        if bbox_intersects(query_bbox, BC_PROVINCE_BBOX):
            bc_trails = _query_databc_wfs_geojson(
                self.databc_wfs_url,
                "pub:WHSE_FOREST_TENURE.FTEN_RECREATION_LINES_SVW",
                query_bbox,
                cache_path=self.cache_dir / f"ca_bc_trails_{bhash}.json",
                timeout_s=self.timeout_s,
                client=self._injected_client,
            )

        # 3. Parks Canada POIs, Facilities & Backcountry Accommodations
        pc_pois = _query_arcgis_geojson(
            [f"{self.parks_canada_base_url}/Interest_Point_Interet_APCA_OpenOuvert/FeatureServer/0/query"],
            query_bbox,
            cache_path=self.cache_dir / f"ca_pc_pois_{bhash}.json",
            out_fields="Name_e,Nom_f,Descr_e,Principal_type",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        pc_facs = _query_arcgis_geojson(
            [f"{self.parks_canada_base_url}/Facilities_Installations_Point_OpenData/FeatureServer/0/query"],
            query_bbox,
            cache_path=self.cache_dir / f"ca_pc_facs_{bhash}.json",
            out_fields="Name_e,Nom_f,Label_e,Facility_Type_Installation",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        pc_accom = _query_arcgis_geojson(
            [f"{self.parks_canada_base_url}/Accommodation_Hebergement_APCA_OpenOuvert/FeatureServer/0/query"],
            query_bbox,
            cache_path=self.cache_dir / f"ca_pc_accom_{bhash}.json",
            out_fields="*",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )

        # 4. ECCC CPCAD Nationwide Protected Areas (simplified for fast rendering)
        cpcad_feats = _query_arcgis_geojson(
            [self.cpcad_url],
            query_bbox,
            cache_path=self.cache_dir / f"ca_cpcad_{bhash}.json",
            out_fields="NAME_E,NAME_F,IUCN_CAT,O_AREA_HA",
            extra_params={"maxAllowableOffset": "0.0005"},
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )

        trail_rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for feat in pc_trails:
            props = feat.get("properties") or {}
            name = clean_feature_name(
                _prop_ci(props, "Label_e_20k_5k", "Label_e_5k_less", "Noms_Alt_Names", "Etiquette_f_20k_5k")
            )
            trail_rows.append(
                (
                    feat["geometry"],
                    {
                        "name": name,
                        "trail_class": "walking_track",
                        "surface": "",
                        "state": "CA",
                    },
                )
            )

        for feat in bc_trails:
            props = feat.get("properties") or {}
            status = str(_prop_ci(props, "LIFE_CYCLE_STATUS_CODE") or "ACTIVE").upper()
            if status == "RETIRED":
                continue
            name = clean_feature_name(_prop_ci(props, "PROJECT_NAME", "MAP_LABEL", "FOREST_FILE_ID"))
            ptype = str(_prop_ci(props, "PROJECT_TYPE") or "").lower()
            tclass = "4wd_track" if ("4wd" in ptype or "road" in ptype) else "walking_track"
            trail_rows.append(
                (
                    feat["geometry"],
                    {
                        "name": name,
                        "trail_class": tclass,
                        "surface": "",
                        "state": "CA",
                    },
                )
            )

        poi_rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
        seen_poi_names: set[str] = set()
        for source_feats, default_type in (
            (pc_accom, "hut"),
            (pc_facs, "trailhead"),
            (pc_pois, "lookout"),
        ):
            for feat in source_feats:
                props = feat.get("properties") or {}
                name = clean_feature_name(_prop_ci(props, "Name_e", "Nom_f", "Label_e"))
                if not name:
                    continue
                dedupe_key = name.lower()
                if dedupe_key in seen_poi_names:
                    continue
                seen_poi_names.add(dedupe_key)
                label_hint = str(_prop_ci(props, "Label_e", "Name_e") or "")
                descr_hint = str(_prop_ci(props, "Descr_e") or "")
                poi_rows.append(
                    (
                        feat["geometry"],
                        {
                            "name": name,
                            "poi_type": _classify_ca_poi_type(label_hint, descr_hint, default_type=default_type),
                            "elev_m": None,
                            "state": "CA",
                        },
                    )
                )

        land_rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
        seen_lands: set[str] = set()
        for feat in cpcad_feats:
            props = feat.get("properties") or {}
            name = clean_feature_name(_prop_ci(props, "NAME_E", "NAME_F"))
            if not name:
                continue
            manager, designation = _classify_cpcad_designation(name, _prop_ci(props, "IUCN_CAT"))
            lkey = f"{name.lower()}|{designation.lower()}"
            if lkey in seen_lands:
                continue
            seen_lands.add(lkey)
            land_rows.append(
                (
                    feat["geometry"],
                    {
                        "name": name,
                        "manager": manager,
                        "designation": designation,
                        "state": "CA",
                    },
                )
            )

        if not trail_rows and not poi_rows and not land_rows:
            return None

        from osgeo import ogr  # type: ignore[import-untyped]

        layer_specs = [
            (
                "au_state_trails",
                ogr.wkbMultiLineString,
                (
                    ("name", ogr.OFTString),
                    ("trail_class", ogr.OFTString),
                    ("surface", ogr.OFTString),
                    ("state", ogr.OFTString),
                ),
                trail_rows,
            ),
            (
                "au_state_pois",
                ogr.wkbPoint,
                (
                    ("name", ogr.OFTString),
                    ("poi_type", ogr.OFTString),
                    ("elev_m", ogr.OFTReal),
                    ("state", ogr.OFTString),
                ),
                poi_rows,
            ),
            (
                "public_lands",
                ogr.wkbMultiPolygon,
                (
                    ("name", ogr.OFTString),
                    ("manager", ogr.OFTString),
                    ("designation", ogr.OFTString),
                    ("state", ogr.OFTString),
                ),
                land_rows,
            ),
        ]
        return _write_projected_gpkg_layers(
            out_gpkg,
            layout.utm_epsg,
            layout.utm_fetch_bbox,
            layer_specs,
        )


def build_ca_overlay_sources(
    kinds: Sequence[CAOverlayKind | str],
    local_overrides: Mapping[str | CAOverlayKind, Path | str] | None = None,
    cache_dir: Path | str | None = None,
) -> dict[str, OverlaySource]:
    """Instantiate requested Canada OGL-Canada overlay sources keyed by ``CAOverlayKind.value``."""
    normalized_overrides: dict[str, Path] = {}
    if local_overrides:
        for raw_key, raw_path in local_overrides.items():
            key_str = (
                raw_key.value
                if isinstance(raw_key, CAOverlayKind)
                else str(raw_key).lower().replace("_", "-")
            )
            normalized_overrides[key_str] = Path(raw_path)

    registry: dict[CAOverlayKind, type[_BaseAUOverlaySource]] = {
        CAOverlayKind.CA_CANVEC_TRAILS: CACanVecTrailsSource,
    }

    sources: dict[str, OverlaySource] = {}
    for raw_kind in kinds:
        kind = (
            raw_kind
            if isinstance(raw_kind, CAOverlayKind)
            else CAOverlayKind(str(raw_kind).lower().replace("_", "-"))
        )
        if kind.value in sources:
            continue
        cls = registry[kind]
        sources[kind.value] = cls(
            cache_dir=cache_dir,
            local_gpkg=normalized_overrides.get(kind.value),
        )
    return sources
