"""Australian state-level CC BY backcountry trail, fire-trail, hut, and trig overlays (All 8 States & Auto Router)."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

import httpx

from gridforge.config import BBox, SheetLayout
from gridforge.sources.base import OverlaySource
from gridforge.sources.overlays_au import (
    USER_AGENT,
    _BaseAUOverlaySource,
    _bbox_hash,
    _clip_and_reproject_local_gpkg,
    _prop_ci,
    _query_arcgis_geojson,
    _round_enclosing_bbox,
    _to_float_or_none,
    _write_projected_gpkg_layers,
)

logger = logging.getLogger(__name__)

# Fast-path geographic bounds for all 8 Australian states & territories (WGS84 lon/lat)
NSW_COVERAGE_BBOX: Final[BBox] = BBox(min_x=140.5, min_y=-37.8, max_x=154.0, max_y=-28.0)
VIC_COVERAGE_BBOX: Final[BBox] = BBox(min_x=140.5, min_y=-39.4, max_x=150.2, max_y=-33.8)
TAS_COVERAGE_BBOX: Final[BBox] = BBox(min_x=143.5, min_y=-43.8, max_x=148.6, max_y=-39.5)
QLD_COVERAGE_BBOX: Final[BBox] = BBox(min_x=137.8, min_y=-29.3, max_x=154.0, max_y=-10.5)
WA_COVERAGE_BBOX: Final[BBox] = BBox(min_x=112.9, min_y=-35.2, max_x=129.0, max_y=-13.6)
SA_COVERAGE_BBOX: Final[BBox] = BBox(min_x=129.0, min_y=-38.1, max_x=141.0, max_y=-26.0)
NT_COVERAGE_BBOX: Final[BBox] = BBox(min_x=129.0, min_y=-26.0, max_x=138.0, max_y=-10.9)
ACT_COVERAGE_BBOX: Final[BBox] = BBox(min_x=148.70, min_y=-35.95, max_x=149.45, max_y=-35.10)
ACT_STRICT_BBOX: Final[BBox] = BBox(min_x=148.72, min_y=-35.93, max_x=149.42, max_y=-35.12)

# NSW Spatial Services CC BY 4.0 ArcGIS REST endpoints
DEFAULT_NSW_TRANSPORT_BASE_URL: Final[str] = (
    "https://portal.spatial.nsw.gov.au/server/rest/services/NSW_Transport_Theme/MapServer"
)
DEFAULT_NSW_TRIG_URL: Final[str] = (
    "https://portal.spatial.nsw.gov.au/server/rest/services/SurveyMarkGDA2020/MapServer/0/query"
)
DEFAULT_NSW_POI_URL: Final[str] = (
    "https://maps.six.nsw.gov.au/arcgis/rest/services/public/NSW_POI/MapServer/0/query"
)
DEFAULT_NSW_HUTS_URL: Final[str] = (
    "https://portal.spatial.nsw.gov.au/server/rest/services/NSW_Features_of_Interest_Category/MapServer/3/query"
)

# Victoria DataVic / DEECA / DTP Vicmap OGC WFS 2.0.0 GeoJSON endpoint
DEFAULT_VIC_WFS_URL: Final[str] = "https://opendata.maps.vic.gov.au/geoserver/wfs"

# Tasmania LISTdata (NRE Tas) CC BY 3.0 AU ArcGIS REST endpoints
DEFAULT_TAS_OPENDATA_BASE_URL: Final[str] = (
    "https://services.thelist.tas.gov.au/arcgis/rest/services/Public/OpenDataWFS/MapServer"
)
DEFAULT_TAS_TOPO_BASE_URL: Final[str] = (
    "https://services.thelist.tas.gov.au/arcgis/rest/services/Public/TopographyAndRelief/MapServer"
)

# Queensland QSpatial (DESI / Dept. of Natural Resources & Mines) CC BY 4.0 ArcGIS REST endpoints
DEFAULT_QLD_PARKS_BASE_URL: Final[str] = (
    "https://spatial-gis.information.qld.gov.au/arcgis/rest/services/Environment/ParksTerrestrialProtectedAreas/MapServer"
)
DEFAULT_QLD_ROADS_URL: Final[str] = (
    "https://spatial-gis.information.qld.gov.au/arcgis/rest/services/Transportation/RoadsAndTracks/MapServer/10/query"
)
DEFAULT_QLD_TOURIST_URL: Final[str] = (
    "https://spatial-gis.information.qld.gov.au/arcgis/rest/services/Structure/PhysicalInfrastructure/MapServer/60/query"
)
DEFAULT_QLD_SURVEY_URL: Final[str] = (
    "https://spatial-gis.information.qld.gov.au/arcgis/rest/services/Basemaps/FoundationData/MapServer/8/query"
)

# ACT ACTmapi / EPSDD CC BY 4.0 ArcGIS Online FeatureServer host
DEFAULT_ACT_BASE_URL: Final[str] = (
    "https://services1.arcgis.com/E5n4f1VY84i0xSjy/arcgis/rest/services"
)

_HUT_WORD_RE: Final[re.Pattern[str]] = re.compile(r"\b(hut|huts|bivouac|shelter|refuge)\b", re.IGNORECASE)
_NUMERIC_PREFIX_RE: Final[re.Pattern[str]] = re.compile(r"[-+]?\d+(?:\.\d+)?")

_NSW_SURFACE_MAP: Final[dict[str, str]] = {
    "1": "sealed",
    "2": "unsealed",
    "3": "natural",
    "4": "unsealed",
}

_VIC_SEAL_MAP: Final[dict[str, str]] = {
    "1": "sealed",
    "2": "unsealed",
    "3": "natural",
}

_PLACEHOLDER_NAMES: Final[frozenset[str]] = frozenset(
    {"unnamed", "unknown", "none", "null", "n/a", "unnamed track", "unnamed road"}
)

_ACT_GENERIC_PARK_LOCATIONS: Final[frozenset[str]] = frozenset(
    {
        "namadgi national park",
        "tidbinbilla nature reserve",
        "tidbinbilla special purpose reserve",
        "canberra nature park",
        "murrumbidgee river corridor",
    }
)


# ---------------------------------------------------------------------------
# Shared public helpers reused across all 8 Australian state overlay sources
# ---------------------------------------------------------------------------


def clean_feature_name(raw: Any) -> str:
    """Strip whitespace and normalize placeholder labels (e.g. ``'Unnamed'``) to ``''``."""
    text = re.sub(r"\s+", " ", str(raw or "")).strip()
    return "" if text.lower() in _PLACEHOLDER_NAMES else text


def bbox_intersects(a: BBox, b: BBox) -> bool:
    """Return True when two WGS84 bounding boxes overlap."""
    return (
        a.max_x >= b.min_x
        and a.min_x <= b.max_x
        and a.max_y >= b.min_y
        and a.min_y <= b.max_y
    )


def _bbox_strictly_within(inner: BBox, outer: BBox) -> bool:
    return (
        inner.min_x >= outer.min_x
        and inner.max_x <= outer.max_x
        and inner.min_y >= outer.min_y
        and inner.max_y <= outer.max_y
    )


def is_in_nsw_coverage(bbox: BBox) -> bool:
    """Return True if ``bbox`` (WGS84) intersects NSW / ACT coverage."""
    return bbox_intersects(bbox, NSW_COVERAGE_BBOX)


def is_in_vic_coverage(bbox: BBox) -> bool:
    """Return True if ``bbox`` (WGS84) intersects Victoria coverage."""
    if not bbox_intersects(bbox, VIC_COVERAGE_BBOX):
        return False
    # Exclude NSW/ACT northeast of the Murray River / Forest Hill border (zero VIC land)
    if bbox.min_x > 148.0 and bbox.min_y > -35.9:
        return False
    return True


def is_in_tas_coverage(bbox: BBox) -> bool:
    """Return True if ``bbox`` (WGS84) intersects Tasmania coverage."""
    return bbox_intersects(bbox, TAS_COVERAGE_BBOX)


def is_in_qld_coverage(bbox: BBox) -> bool:
    """Return True if ``bbox`` (WGS84) intersects Queensland coverage."""
    return bbox_intersects(bbox, QLD_COVERAGE_BBOX)


def is_in_act_coverage(bbox: BBox) -> bool:
    """Return True if ``bbox`` (WGS84) intersects Australian Capital Territory coverage."""
    return bbox_intersects(bbox, ACT_COVERAGE_BBOX)


def is_in_wa_coverage(bbox: BBox) -> bool:
    """Return True if ``bbox`` (WGS84) intersects Western Australia coverage."""
    return bbox_intersects(bbox, WA_COVERAGE_BBOX)


def is_in_sa_coverage(bbox: BBox) -> bool:
    """Return True if ``bbox`` (WGS84) intersects South Australia coverage."""
    return bbox_intersects(bbox, SA_COVERAGE_BBOX)


def is_in_nt_coverage(bbox: BBox) -> bool:
    """Return True if ``bbox`` (WGS84) intersects Northern Territory coverage."""
    return bbox_intersects(bbox, NT_COVERAGE_BBOX)


def parse_elev_m(value: Any) -> float | None:
    """Parse float meters from numeric or string elevation fields like ``'2229.470'`` or ``'986.7 m'``."""
    direct = _to_float_or_none(value)
    if direct is not None:
        return direct
    if isinstance(value, str):
        match = _NUMERIC_PREFIX_RE.search(value)
        if match:
            return _to_float_or_none(match.group(0))
    return None


def dedupe_pois(
    rows: list[tuple[dict[str, Any], dict[str, Any]]],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Drop duplicate POIs sharing the same ``(poi_type, name)`` and ~100 m location.

    Prefers records that carry a numeric ``elev_m`` when collapsing duplicates.
    """
    ordered = sorted(rows, key=lambda item: 0 if item[1].get("elev_m") is not None else 1)
    deduped: list[tuple[dict[str, Any], dict[str, Any]]] = []
    seen: set[tuple[str, str, int, int]] = set()
    for geom, attrs in ordered:
        coords = geom.get("coordinates") if isinstance(geom, dict) else None
        lon_bucket = 0
        lat_bucket = 0
        if isinstance(coords, (list, tuple)) and len(coords) >= 2:
            # Handle Polygon rings or Point coordinates gracefully
            pt = coords
            while isinstance(pt, (list, tuple)) and pt and isinstance(pt[0], (list, tuple)):
                pt = pt[0]
            if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                try:
                    lon_bucket = int(round(float(pt[0]) * 1000.0))
                    lat_bucket = int(round(float(pt[1]) * 1000.0))
                except (TypeError, ValueError):
                    pass
        key = (
            str(attrs.get("poi_type") or "").lower(),
            str(attrs.get("name") or "").strip().lower(),
            lon_bucket,
            lat_bucket,
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append((geom, attrs))
    return deduped


def _redact_url_secrets(text: str) -> str:
    """Strip inline WFS/API key tokens (e.g. ``;key=...`` or ``key=...``) before logging."""
    redacted = re.sub(r"(;key=)[^/&\s;]+", r"\1REDACTED", text, flags=re.IGNORECASE)
    redacted = re.sub(r"([?&](?:api_?)?key=)[^&\s]+", r"\1REDACTED", redacted, flags=re.IGNORECASE)
    return redacted


def query_wfs_geojson(
    wfs_url: str,
    type_name: str,
    bbox: BBox,
    *,
    cache_path: Path,
    cql_filter: str | None = None,
    geom_column: str = "geom",
    use_bbox_param: bool = False,
    max_features: int = 2500,
    timeout_s: float = 25.0,
    retries: int = 2,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    """Query an OGC WFS 2.0.0 GeoJSON endpoint with disk cache and graceful fallback.

    Uses GeoServer ``CQL_FILTER=BBOX(geom,...)`` by default (traditional lon/lat order)
    or standard OGC WFS 2.0 ``bbox=minx,miny,maxx,maxy,urn:ogc:def:crs:OGC:1.3:CRS84``
    when ``use_bbox_param=True``.
    """
    if cache_path.is_file() and cache_path.stat().st_size > 0:
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(cached, list):
                return cached
        except json.JSONDecodeError:
            logger.warning("Ignoring corrupt cached WFS overlay JSON: %s", cache_path)

    params: dict[str, str] = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeNames": type_name,
        "outputFormat": "application/json",
        "srsName": "EPSG:4326",
        "count": str(max_features),
    }
    if use_bbox_param and not cql_filter:
        params["bbox"] = (
            f"{bbox.min_x:.6f},{bbox.min_y:.6f},{bbox.max_x:.6f},{bbox.max_y:.6f},"
            "urn:ogc:def:crs:OGC:1.3:CRS84"
        )
    else:
        bbox_cql = (
            f"BBOX({geom_column},{bbox.min_x:.6f},{bbox.min_y:.6f},"
            f"{bbox.max_x:.6f},{bbox.max_y:.6f},'EPSG:4326')"
        )
        params["CQL_FILTER"] = f"{bbox_cql} AND ({cql_filter})" if cql_filter else bbox_cql

    owns_client = client is None
    http = (
        client
        if client is not None
        else httpx.Client(
            timeout=httpx.Timeout(timeout_s, connect=min(8.0, timeout_s)),
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        )
    )

    last_error: str | None = None
    try:
        for attempt in range(max(1, retries)):
            try:
                resp = http.get(wfs_url, params=params, headers={"User-Agent": USER_AGENT})
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                last_error = f"{_redact_url_secrets(wfs_url)} ({type_name}, try={attempt + 1}): {_redact_url_secrets(str(exc))}"
                continue

            if not isinstance(data, dict) or not isinstance(data.get("features"), list):
                last_error = f"{_redact_url_secrets(wfs_url)} ({type_name}): unexpected WFS payload"
                continue

            features = [
                f
                for f in data["features"]
                if isinstance(f, dict) and f.get("geometry") is not None
            ]
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(features), encoding="utf-8")
            tmp.replace(cache_path)
            return features
    finally:
        if owns_client:
            http.close()

    if last_error:
        logger.warning("AU WFS overlay query unavailable (%s); skipping layer cleanly", last_error)
    return []


def write_state_trails_gpkg(
    out_gpkg: Path,
    layout: SheetLayout,
    trail_rows: list[tuple[dict[str, Any], dict[str, Any]]],
    poi_rows: list[tuple[dict[str, Any], dict[str, Any]]],
) -> Path | None:
    """Write the unified 2-layer ``au_state_trails`` + ``au_state_pois`` UTM GeoPackage."""
    if not trail_rows and not poi_rows:
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
            dedupe_pois(poi_rows),
        ),
    ]
    return _write_projected_gpkg_layers(out_gpkg, layout.utm_epsg, layout.utm_fetch_bbox, layer_specs)


# Public re-exports for sibling state overlay modules (e.g. overlays_au_states_west.py)
BaseAUOverlaySource = _BaseAUOverlaySource
query_arcgis_geojson = _query_arcgis_geojson
round_enclosing_bbox = _round_enclosing_bbox
bbox_hash = _bbox_hash
prop_ci = _prop_ci
clip_and_reproject_local_gpkg = _clip_and_reproject_local_gpkg

# Backward-compatible private aliases
_clean_feature_name = clean_feature_name
_bboxes_intersect = bbox_intersects
_parse_elevation_m = parse_elev_m
_dedupe_poi_rows = dedupe_pois
_query_wfs_geojson = query_wfs_geojson
_write_state_trails_gpkg = write_state_trails_gpkg


# ---------------------------------------------------------------------------
# Classification helpers for NSW, VIC, TAS, QLD, and ACT
# ---------------------------------------------------------------------------


def _compose_nsw_road_name(props: Mapping[str, Any]) -> str:
    full = clean_feature_name(_prop_ci(props, "roadname", "firetrailname", "trailname", "name"))
    if full:
        return full
    base = clean_feature_name(_prop_ci(props, "roadnamebase"))
    rtype = clean_feature_name(_prop_ci(props, "roadnametype"))
    if base and rtype:
        if base.lower().endswith(rtype.lower()):
            return base
        return f"{base} {rtype}"
    if base:
        return base
    brimsid = _prop_ci(props, "brimsid")
    if brimsid is not None:
        return f"Fire Trail {str(brimsid).strip()}"
    return ""


def _normalize_nsw_surface(value: Any, *, default: str = "unsealed") -> str:
    if value is None:
        return default
    raw = str(value).strip().lower()
    if not raw or raw in ("0", "unknown", "none", "null"):
        return default
    return _NSW_SURFACE_MAP.get(raw, raw)


def _classify_nsw_poi(props: Mapping[str, Any]) -> str | None:
    poitype = str(_prop_ci(props, "poitype", "featuretype", "type") or "").strip().lower()
    label = str(_prop_ci(props, "poilabel", "poiname", "generalname", "name") or "").strip()
    if any(k in poitype for k in ("hut", "shelter", "cabin", "refuge", "bivouac")):
        return "hut"
    if "camp" in poitype:
        return "campground"
    if "lookout" in poitype or "viewpoint" in poitype:
        return "lookout"
    if "picnic" in poitype or "rest area" in poitype:
        return "picnic"
    if _HUT_WORD_RE.search(label):
        return "hut"
    return None


def _classify_vic_road_trail(props: Mapping[str, Any], name: str) -> str:
    lower_name = name.lower()
    ftype = str(_prop_ci(props, "feature_type_code") or "").strip().lower()
    if (
        "fire trail" in lower_name
        or "firetrail" in lower_name
        or "fireline" in lower_name
        or "fire access" in lower_name
        or "fire" in ftype
    ):
        return "fire_trail"
    if "management" in lower_name:
        return "management_trail"

    raw_class = _prop_ci(props, "class_code")
    try:
        class_code = int(raw_class) if raw_class is not None else -1
    except (TypeError, ValueError):
        class_code = -1

    if class_code in (8, 9) or ftype == "trail":
        return "walking_track"
    if class_code == 6:
        return "management_trail"
    return "4wd_track"


def _classify_vic_recweb_track(props: Mapping[str, Any], name: str) -> str:
    lower_name = name.lower()
    access = str(_prop_ci(props, "access_dsc", "trk_class", "track_type") or "").strip().lower()
    if "fire" in lower_name or "fire" in access:
        return "fire_trail"
    if "management" in lower_name or "management" in access:
        return "management_trail"
    if any(k in access for k in ("4wd", "vehicle", "motor", "drive")):
        return "4wd_track"
    return "walking_track"


def _classify_vic_poi(props: Mapping[str, Any], *, default_type: str | None = None) -> str | None:
    subtype = str(
        _prop_ci(props, "feature_subtype", "feature_type", "site_type", "site_class", "type") or ""
    ).strip().lower()
    name = str(_prop_ci(props, "name_label", "name", "hut_name", "site_name") or "").strip()
    if any(k in subtype for k in ("hut", "shelter", "cabin", "refuge", "bivouac")):
        return "hut"
    if "survey" in subtype or "trig" in subtype:
        return "trig"
    if "camp" in subtype:
        return "campground"
    if "lookout" in subtype or "viewpoint" in subtype:
        return "lookout"
    if "picnic" in subtype or "day visitor" in subtype or "visitor area" in subtype:
        return "picnic"
    if _HUT_WORD_RE.search(name):
        return "hut"
    return default_type


def _classify_tas_segment(props: Mapping[str, Any], name: str) -> tuple[str, str]:
    lower_name = name.lower()
    tseg = str(_prop_ci(props, "TSEG_FEAT") or "").strip().lower()
    ttype = str(_prop_ci(props, "TRANS_TYPE") or "").strip().lower()
    tclass = str(_prop_ci(props, "TRAN_CLASS") or "").strip().lower()
    surf_raw = str(_prop_ci(props, "SURFACE_TY") or "").strip().lower()

    if "fire" in tseg or "fire trail" in lower_name or "firetrail" in lower_name:
        trail_class = "fire_trail"
    elif (
        ttype == "track"
        or "as2156" in tclass
        or "walking" in tclass
        or "shared use" in tclass
        or "bike" in tclass
    ):
        trail_class = "walking_track"
    elif "access road" in tclass or "management" in lower_name:
        trail_class = "management_trail"
    else:
        trail_class = "4wd_track"

    if "sealed" in surf_raw and "unsealed" not in surf_raw:
        surface = "sealed"
    elif "boardwalk" in surf_raw:
        surface = "boardwalk"
    elif "unsealed" in surf_raw or "4wd" in surf_raw:
        surface = "unsealed"
    else:
        surface = "natural" if trail_class == "walking_track" else "unsealed"

    return trail_class, surface


def _classify_qld_qpws_trail(
    props: Mapping[str, Any],
    name: str,
    *,
    is_walking_layer: bool = False,
) -> tuple[str | None, str]:
    subcat = str(_prop_ci(props, "subcat") or "").strip().lower()
    assettype = str(_prop_ci(props, "assettype") or "").strip().lower()
    if subcat in ("access structures", "track structures") or "bollard" in assettype or "barrier" in assettype:
        return None, ""

    lower_name = name.lower()
    comments = str(_prop_ci(props, "comments") or "").strip().lower()
    fireline = str(_prop_ci(props, "fireline") or "").strip().lower()
    access = str(_prop_ci(props, "access_") or "").strip().lower()
    traffic = str(_prop_ci(props, "traffic") or "").strip().lower()
    material = str(_prop_ci(props, "material") or "").strip().lower()

    if (
        (fireline and fireline not in ("not used for fire", "no", "none", "unclassified"))
        or "firebreak" in lower_name
        or "fire trail" in lower_name
        or "fireline" in lower_name
        or "firebreak" in comments
    ):
        trail_class = "fire_trail"
    elif is_walking_layer or "tracks and trails" in subcat or "walking" in traffic or "walking" in assettype:
        trail_class = "walking_track"
    elif "management" in access or "management" in lower_name:
        trail_class = "management_trail"
    elif "4wd" in traffic or subcat == "roads":
        trail_class = "4wd_track"
    else:
        trail_class = "walking_track"

    if "sealed" in material or "paved" in material or "bitumen" in material:
        surface = "sealed"
    elif "natural" in material:
        surface = "natural"
    elif "gravel" in material or "unsealed" in material or "formed" in material:
        surface = "unsealed"
    else:
        surface = "natural" if trail_class == "walking_track" else "unsealed"

    return trail_class, surface


# ---------------------------------------------------------------------------
# State Overlay Source Implementations (NSW, VIC, TAS, QLD, ACT)
# ---------------------------------------------------------------------------


class NSWTrailsSource(_BaseAUOverlaySource):
    """NSW Spatial Services Fire Trails, 4WD/Walking Tracks, Huts, POIs & Trig Stations (CC BY 4.0)."""

    name: str = "au-nsw-trails"
    overlay_key: str = "au-nsw-trails"
    attribution: str = (
        "NSW Trails, Facilities & Trig Stations: "
        "© State of New South Wales (Spatial Services, DCS) (CC BY 4.0)"
    )

    def __init__(
        self,
        *,
        transport_base_url: str | None = None,
        trig_url: str | None = None,
        poi_url: str | None = None,
        huts_url: str | None = None,
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
        self.transport_base_url = (
            transport_base_url
            or os.environ.get("GRIDFORGE_AU_NSW_TRANSPORT_URL", DEFAULT_NSW_TRANSPORT_BASE_URL)
        ).rstrip("/")
        self.trig_url = trig_url or os.environ.get("GRIDFORGE_AU_NSW_TRIG_URL", DEFAULT_NSW_TRIG_URL)
        self.poi_url = poi_url or os.environ.get("GRIDFORGE_AU_NSW_POI_URL", DEFAULT_NSW_POI_URL)
        self.huts_url = huts_url or os.environ.get("GRIDFORGE_AU_NSW_HUTS_URL", DEFAULT_NSW_HUTS_URL)

    def fetch_overlay(self, layout: SheetLayout, work_dir: Path) -> Path | None:
        work_dir.mkdir(parents=True, exist_ok=True)
        out_gpkg = work_dir / "au_nsw_trails.gpkg"
        if self.local_gpkg is not None:
            return _clip_and_reproject_local_gpkg(
                self.local_gpkg,
                layout,
                out_gpkg,
                ("au_state_trails", "au_state_pois"),
            )

        if not is_in_nsw_coverage(layout.wgs84_fetch_bbox):
            logger.debug("Sheet bbox outside NSW/ACT coverage; skipping au-nsw-trails")
            return None

        query_bbox = _round_enclosing_bbox(layout.wgs84_fetch_bbox)
        bhash = _bbox_hash(query_bbox)

        fire_feats = _query_arcgis_geojson(
            [f"{self.transport_base_url}/9/query"],
            query_bbox,
            cache_path=self.cache_dir / f"au_nsw_firetrails_{bhash}.json",
            out_fields="*",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        track_feats = _query_arcgis_geojson(
            [f"{self.transport_base_url}/5/query"],
            query_bbox,
            cache_path=self.cache_dir / f"au_nsw_tracks_{bhash}.json",
            where="classsubtype IN (5, 6)",
            out_fields="classsubtype,roadnamebase,roadnametype,functionhierarchy,surface,operationalstatus",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        trig_feats = _query_arcgis_geojson(
            [self.trig_url],
            query_bbox,
            cache_path=self.cache_dir / f"au_nsw_trigs_{bhash}.json",
            where="marktype='TS'",
            out_fields="marktype,marknumber,trigname,trigtype,ahdheight_label,markstatus,monumenttype",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        poi_feats = _query_arcgis_geojson(
            [self.poi_url],
            query_bbox,
            cache_path=self.cache_dir / f"au_nsw_pois_{bhash}.json",
            where="1=1",
            out_fields="*",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        hut_feats = _query_arcgis_geojson(
            [self.huts_url],
            query_bbox,
            cache_path=self.cache_dir / f"au_nsw_huts_{bhash}.json",
            where="UPPER(generalname) LIKE '%HUT%'",
            out_fields="*",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )

        trail_rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for feat in fire_feats:
            props = feat.get("properties") or {}
            trail_rows.append(
                (
                    feat["geometry"],
                    {
                        "name": _compose_nsw_road_name(props),
                        "trail_class": "fire_trail",
                        "surface": _normalize_nsw_surface(_prop_ci(props, "surface", "surfacetype")),
                        "state": "NSW",
                    },
                )
            )

        for feat in track_feats:
            props = feat.get("properties") or {}
            name = _compose_nsw_road_name(props)
            lower_name = name.lower()
            raw_subtype = _prop_ci(props, "classsubtype")
            try:
                subtype_int = int(raw_subtype) if raw_subtype is not None else 5
            except (TypeError, ValueError):
                subtype_int = 6 if "path" in str(raw_subtype).lower() else 5

            if "fire trail" in lower_name or "firetrail" in lower_name or lower_name.endswith(" ft"):
                trail_class = "fire_trail"
            elif "management trail" in lower_name or "management track" in lower_name:
                trail_class = "management_trail"
            elif subtype_int == 6:
                trail_class = "walking_track"
            else:
                trail_class = "4wd_track"

            default_surface = "natural" if trail_class == "walking_track" else "unsealed"
            trail_rows.append(
                (
                    feat["geometry"],
                    {
                        "name": name,
                        "trail_class": trail_class,
                        "surface": _normalize_nsw_surface(_prop_ci(props, "surface"), default=default_surface),
                        "state": "NSW",
                    },
                )
            )

        poi_rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for feat in trig_feats:
            props = feat.get("properties") or {}
            trigname = clean_feature_name(_prop_ci(props, "trigname", "name"))
            if not trigname:
                marknum = _prop_ci(props, "marknumber")
                trigname = f"TS {marknum}" if marknum is not None else "Trig Station"
            poi_rows.append(
                (
                    feat["geometry"],
                    {
                        "name": trigname,
                        "poi_type": "trig",
                        "elev_m": parse_elev_m(
                            _prop_ci(props, "ahdheight_label", "ahdheight", "elev_m")
                        ),
                        "state": "NSW",
                    },
                )
            )

        for feat in poi_feats:
            props = feat.get("properties") or {}
            poi_type = _classify_nsw_poi(props)
            if poi_type is None:
                continue
            name = clean_feature_name(_prop_ci(props, "poilabel", "poiname", "generalname", "name"))
            poi_rows.append(
                (
                    feat["geometry"],
                    {
                        "name": name,
                        "poi_type": poi_type,
                        "elev_m": parse_elev_m(_prop_ci(props, "elev_m", "elevation")),
                        "state": "NSW",
                    },
                )
            )

        for feat in hut_feats:
            props = feat.get("properties") or {}
            name = clean_feature_name(_prop_ci(props, "generalname", "name", "poilabel"))
            if not name:
                continue
            poi_rows.append(
                (
                    feat["geometry"],
                    {
                        "name": name,
                        "poi_type": "hut",
                        "elev_m": parse_elev_m(_prop_ci(props, "elev_m", "elevation")),
                        "state": "NSW",
                    },
                )
            )

        return write_state_trails_gpkg(out_gpkg, layout, trail_rows, poi_rows)


class VICTrailsSource(_BaseAUOverlaySource):
    """Victoria DataVic / DEECA / Vicmap Fire Tracks, Walking Trails, Huts & Survey Marks (CC BY 4.0)."""

    name: str = "au-vic-trails"
    overlay_key: str = "au-vic-trails"
    attribution: str = (
        "Victoria Trails, Huts & Survey Marks: "
        "© State of Victoria (DEECA / DTP Vicmap) (CC BY 4.0)"
    )

    def __init__(
        self,
        *,
        wfs_url: str | None = None,
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
        self.wfs_url = wfs_url or os.environ.get("GRIDFORGE_AU_VIC_WFS_URL", DEFAULT_VIC_WFS_URL)

    def fetch_overlay(self, layout: SheetLayout, work_dir: Path) -> Path | None:
        work_dir.mkdir(parents=True, exist_ok=True)
        out_gpkg = work_dir / "au_vic_trails.gpkg"
        if self.local_gpkg is not None:
            return _clip_and_reproject_local_gpkg(
                self.local_gpkg,
                layout,
                out_gpkg,
                ("au_state_trails", "au_state_pois"),
            )

        if not is_in_vic_coverage(layout.wgs84_fetch_bbox):
            logger.debug("Sheet bbox outside Victoria coverage; skipping au-vic-trails")
            return None

        query_bbox = _round_enclosing_bbox(layout.wgs84_fetch_bbox)
        bhash = _bbox_hash(query_bbox)

        road_feats = query_wfs_geojson(
            self.wfs_url,
            "open-data-platform:tr_road",
            query_bbox,
            cache_path=self.cache_dir / f"au_vic_tr_road_{bhash}.json",
            cql_filter="class_code IN (7,8,9) OR (class_code=6 AND road_seal='2')",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        rec_track_feats = query_wfs_geojson(
            self.wfs_url,
            "open-data-platform:recweb_tracks",
            query_bbox,
            cache_path=self.cache_dir / f"au_vic_recweb_tracks_{bhash}.json",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        foi_feats = query_wfs_geojson(
            self.wfs_url,
            "open-data-platform:foi_point",
            query_bbox,
            cache_path=self.cache_dir / f"au_vic_foi_point_{bhash}.json",
            cql_filter=(
                "feature_subtype IN ('hut','camp ground','camping ground',"
                "'picnic site','day visitor area','lookout','trailhead','survey monument')"
            ),
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        rec_hut_feats = query_wfs_geojson(
            self.wfs_url,
            "open-data-platform:recweb_hut",
            query_bbox,
            cache_path=self.cache_dir / f"au_vic_recweb_hut_{bhash}.json",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        rec_site_feats = query_wfs_geojson(
            self.wfs_url,
            "open-data-platform:recweb_site",
            query_bbox,
            cache_path=self.cache_dir / f"au_vic_recweb_site_{bhash}.json",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        geomark_feats = query_wfs_geojson(
            self.wfs_url,
            "open-data-platform:geomark_point",
            query_bbox,
            cache_path=self.cache_dir / f"au_vic_geomark_point_{bhash}.json",
            cql_filter="feature_subtype IN ('survey monument','hut')",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )

        trail_rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for feat in road_feats:
            props = feat.get("properties") or {}
            name = clean_feature_name(_prop_ci(props, "ezi_road_name_label", "road_name", "name"))
            trail_class = _classify_vic_road_trail(props, name)
            raw_seal = str(_prop_ci(props, "road_seal") or "").strip().lower()
            default_surf = "natural" if trail_class == "walking_track" else "unsealed"
            surface = _VIC_SEAL_MAP.get(raw_seal, default_surf if raw_seal in ("", "0", "unknown") else raw_seal)
            trail_rows.append(
                (
                    feat["geometry"],
                    {
                        "name": name,
                        "trail_class": trail_class,
                        "surface": surface,
                        "state": "VIC",
                    },
                )
            )

        for feat in rec_track_feats:
            props = feat.get("properties") or {}
            name = clean_feature_name(_prop_ci(props, "name", "track_name"))
            trail_class = _classify_vic_recweb_track(props, name)
            surface = "natural" if trail_class == "walking_track" else "unsealed"
            trail_rows.append(
                (
                    feat["geometry"],
                    {
                        "name": name,
                        "trail_class": trail_class,
                        "surface": surface,
                        "state": "VIC",
                    },
                )
            )

        poi_rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for feat in foi_feats:
            props = feat.get("properties") or {}
            poi_type = _classify_vic_poi(props)
            if poi_type is None:
                continue
            name = clean_feature_name(_prop_ci(props, "name_label", "name"))
            poi_rows.append(
                (
                    feat["geometry"],
                    {
                        "name": name,
                        "poi_type": poi_type,
                        "elev_m": parse_elev_m(
                            _prop_ci(props, "elev_m", "elevation", "ahd_height")
                        ),
                        "state": "VIC",
                    },
                )
            )

        for feat in rec_hut_feats:
            props = feat.get("properties") or {}
            name = clean_feature_name(_prop_ci(props, "name", "hut_name", "site_name"))
            poi_rows.append(
                (
                    feat["geometry"],
                    {
                        "name": name or "Hut",
                        "poi_type": "hut",
                        "elev_m": parse_elev_m(_prop_ci(props, "elev_m", "elevation")),
                        "state": "VIC",
                    },
                )
            )

        for feat in rec_site_feats:
            props = feat.get("properties") or {}
            poi_type = _classify_vic_poi(props, default_type="campground")
            if poi_type is None:
                continue
            name = clean_feature_name(_prop_ci(props, "name", "site_name", "name_label"))
            poi_rows.append(
                (
                    feat["geometry"],
                    {
                        "name": name,
                        "poi_type": poi_type,
                        "elev_m": parse_elev_m(_prop_ci(props, "elev_m", "elevation")),
                        "state": "VIC",
                    },
                )
            )

        for feat in geomark_feats:
            props = feat.get("properties") or {}
            poi_type = _classify_vic_poi(props)
            if poi_type is None:
                continue
            name = clean_feature_name(_prop_ci(props, "name_label", "name"))
            poi_rows.append(
                (
                    feat["geometry"],
                    {
                        "name": name,
                        "poi_type": poi_type,
                        "elev_m": parse_elev_m(
                            _prop_ci(props, "elev_m", "elevation", "height", "ahd_height")
                        ),
                        "state": "VIC",
                    },
                )
            )

        return write_state_trails_gpkg(out_gpkg, layout, trail_rows, poi_rows)


class TASTrailsSource(_BaseAUOverlaySource):
    """Tasmania LISTdata Fire Trails, PWS Walking Tracks, Walkers Huts & State Trigs (CC BY 3.0 AU)."""

    name: str = "au-tas-trails"
    overlay_key: str = "au-tas-trails"
    attribution: str = (
        "Tasmania Trails, Huts & Survey Control: "
        "© State of Tasmania (the LIST, NRE Tas) (CC BY 3.0 AU)"
    )

    def __init__(
        self,
        *,
        opendata_base_url: str | None = None,
        topo_base_url: str | None = None,
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
        self.opendata_base_url = (
            opendata_base_url
            or os.environ.get("GRIDFORGE_AU_TAS_OPENDATA_URL", DEFAULT_TAS_OPENDATA_BASE_URL)
        ).rstrip("/")
        self.topo_base_url = (
            topo_base_url
            or os.environ.get("GRIDFORGE_AU_TAS_TOPO_URL", DEFAULT_TAS_TOPO_BASE_URL)
        ).rstrip("/")

    def fetch_overlay(self, layout: SheetLayout, work_dir: Path) -> Path | None:
        work_dir.mkdir(parents=True, exist_ok=True)
        out_gpkg = work_dir / "au_tas_trails.gpkg"
        if self.local_gpkg is not None:
            return _clip_and_reproject_local_gpkg(
                self.local_gpkg,
                layout,
                out_gpkg,
                ("au_state_trails", "au_state_pois"),
            )

        if not is_in_tas_coverage(layout.wgs84_fetch_bbox):
            logger.debug("Sheet bbox outside Tasmania coverage; skipping au-tas-trails")
            return None

        query_bbox = _round_enclosing_bbox(layout.wgs84_fetch_bbox)
        bhash = _bbox_hash(query_bbox)

        seg_feats = _query_arcgis_geojson(
            [f"{self.opendata_base_url}/42/query"],
            query_bbox,
            cache_path=self.cache_dir / f"au_tas_segments_{bhash}.json",
            where=(
                "TRANS_TYPE = 'Track' OR TSEG_FEAT = 'Fire Trail' "
                "OR TRAN_CLASS IN ('Vehicular Track', 'Walking') "
                "OR TRAN_CLASS LIKE 'AS2156%' OR SURFACE_TY = '4WD required'"
            ),
            out_fields="TRANS_TYPE,TSEG_FEAT,TRAN_CLASS,SURFACE_TY,PRI_NAME,SEC_NAME",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        hut_feats = _query_arcgis_geojson(
            [f"{self.opendata_base_url}/12/query"],
            query_bbox,
            cache_path=self.cache_dir / f"au_tas_huts_{bhash}.json",
            where="BUILD_TY = 'Hut' OR BLD_PUR IN ('Walkers', 'Shelter')",
            out_fields="BUILD_TY,BLD_PUR,BUILD_NAME",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        nom_feats = _query_arcgis_geojson(
            [f"{self.opendata_base_url}/34/query"],
            query_bbox,
            cache_path=self.cache_dir / f"au_tas_nomenclature_{bhash}.json",
            where="FEAT_TYPE IN ('Camping Ground', 'Lookout', 'Picnic Area')",
            out_fields="FEAT_TYPE,FEAT_NAME",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        infra_feats = _query_arcgis_geojson(
            [f"{self.topo_base_url}/69/query"],
            query_bbox,
            cache_path=self.cache_dir / f"au_tas_infra_pts_{bhash}.json",
            where="INFTY2_TXT IN ('Camping', 'Lookout')",
            out_fields="INFTY2_TXT,NAME,DESCRIPT",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        picnic_feats = _query_arcgis_geojson(
            [f"{self.opendata_base_url}/36/query"],
            query_bbox,
            cache_path=self.cache_dir / f"au_tas_picnic_{bhash}.json",
            where="COM_TYPE2 = 'Picnic Area'",
            out_fields="COM_TYPE2,NAME",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        trig_feats = _query_arcgis_geojson(
            [f"{self.opendata_base_url}/40/query"],
            query_bbox,
            cache_path=self.cache_dir / f"au_tas_trigs_{bhash}.json",
            where="PACK_ID LIKE 'ST%'",
            out_fields="PACK_ID,SCS_NAME,HEIGHT,HGT_DATUM,ORDER_SYMB",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )

        trail_rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for feat in seg_feats:
            props = feat.get("properties") or {}
            name = clean_feature_name(_prop_ci(props, "PRI_NAME", "SEC_NAME", "name"))
            trail_class, surface = _classify_tas_segment(props, name)
            trail_rows.append(
                (
                    feat["geometry"],
                    {
                        "name": name,
                        "trail_class": trail_class,
                        "surface": surface,
                        "state": "TAS",
                    },
                )
            )

        poi_rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for feat in hut_feats:
            props = feat.get("properties") or {}
            name = clean_feature_name(_prop_ci(props, "BUILD_NAME", "name"))
            poi_rows.append(
                (
                    feat["geometry"],
                    {
                        "name": name,
                        "poi_type": "hut",
                        "elev_m": None,
                        "state": "TAS",
                    },
                )
            )

        for feat in nom_feats:
            props = feat.get("properties") or {}
            ftype = str(_prop_ci(props, "FEAT_TYPE") or "").strip().lower()
            if "camp" in ftype:
                poi_type = "campground"
            elif "lookout" in ftype:
                poi_type = "lookout"
            elif "picnic" in ftype:
                poi_type = "picnic"
            else:
                continue
            poi_rows.append(
                (
                    feat["geometry"],
                    {
                        "name": clean_feature_name(_prop_ci(props, "FEAT_NAME", "name")),
                        "poi_type": poi_type,
                        "elev_m": None,
                        "state": "TAS",
                    },
                )
            )

        for feat in infra_feats:
            props = feat.get("properties") or {}
            itype = str(_prop_ci(props, "INFTY2_TXT") or "").strip().lower()
            poi_type = "campground" if "camp" in itype else "lookout" if "lookout" in itype else ""
            if not poi_type:
                continue
            poi_rows.append(
                (
                    feat["geometry"],
                    {
                        "name": clean_feature_name(_prop_ci(props, "NAME", "DESCRIPT")),
                        "poi_type": poi_type,
                        "elev_m": None,
                        "state": "TAS",
                    },
                )
            )

        for feat in picnic_feats:
            props = feat.get("properties") or {}
            poi_rows.append(
                (
                    feat["geometry"],
                    {
                        "name": clean_feature_name(_prop_ci(props, "NAME")),
                        "poi_type": "picnic",
                        "elev_m": None,
                        "state": "TAS",
                    },
                )
            )

        for feat in trig_feats:
            props = feat.get("properties") or {}
            trigname = clean_feature_name(_prop_ci(props, "SCS_NAME", "PACK_ID", "name")) or "Trig Station"
            poi_rows.append(
                (
                    feat["geometry"],
                    {
                        "name": trigname,
                        "poi_type": "trig",
                        "elev_m": parse_elev_m(_prop_ci(props, "HEIGHT", "elev_m")),
                        "state": "TAS",
                    },
                )
            )

        return write_state_trails_gpkg(out_gpkg, layout, trail_rows, poi_rows)


class QLDTrailsSource(_BaseAUOverlaySource):
    """Queensland QSpatial QPWS Walking/Great Walks, Firelines, 4WD Tracks, Campgrounds & Trigs (CC BY 4.0)."""

    name: str = "au-qld-trails"
    overlay_key: str = "au-qld-trails"
    attribution: str = (
        "Queensland Trails, Parks & Survey Control: "
        "© State of Queensland (DESI / Dept. of Natural Resources & Mines) (CC BY 4.0)"
    )

    def __init__(
        self,
        *,
        parks_base_url: str | None = None,
        roads_url: str | None = None,
        tourist_url: str | None = None,
        survey_url: str | None = None,
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
        self.parks_base_url = (
            parks_base_url
            or os.environ.get("GRIDFORGE_AU_QLD_PARKS_URL", DEFAULT_QLD_PARKS_BASE_URL)
        ).rstrip("/")
        self.roads_url = roads_url or os.environ.get("GRIDFORGE_AU_QLD_ROADS_URL", DEFAULT_QLD_ROADS_URL)
        self.tourist_url = tourist_url or os.environ.get("GRIDFORGE_AU_QLD_TOURIST_URL", DEFAULT_QLD_TOURIST_URL)
        self.survey_url = survey_url or os.environ.get("GRIDFORGE_AU_QLD_SURVEY_URL", DEFAULT_QLD_SURVEY_URL)

    def fetch_overlay(self, layout: SheetLayout, work_dir: Path) -> Path | None:
        work_dir.mkdir(parents=True, exist_ok=True)
        out_gpkg = work_dir / "au_qld_trails.gpkg"
        if self.local_gpkg is not None:
            return _clip_and_reproject_local_gpkg(
                self.local_gpkg,
                layout,
                out_gpkg,
                ("au_state_trails", "au_state_pois"),
            )

        if not is_in_qld_coverage(layout.wgs84_fetch_bbox):
            logger.debug("Sheet bbox outside Queensland coverage; skipping au-qld-trails")
            return None

        query_bbox = _round_enclosing_bbox(layout.wgs84_fetch_bbox)
        bhash = _bbox_hash(query_bbox)

        qpws_fields = "objectid,name,visitor_id,subcat,assettype,fireline,traffic,access_,material,comments"
        great_walk_feats = _query_arcgis_geojson(
            [f"{self.parks_base_url}/6/query"],
            query_bbox,
            cache_path=self.cache_dir / f"au_qld_greatwalk_{bhash}.json",
            out_fields=qpws_fields,
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        walking_feats = _query_arcgis_geojson(
            [f"{self.parks_base_url}/5/query"],
            query_bbox,
            cache_path=self.cache_dir / f"au_qld_walking_{bhash}.json",
            out_fields=qpws_fields,
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        park_road_feats = _query_arcgis_geojson(
            [f"{self.parks_base_url}/2/query"],
            query_bbox,
            cache_path=self.cache_dir / f"au_qld_park_roads_{bhash}.json",
            out_fields="objectid,name,subcat,assettype,fireline,traffic,access_,material,comments",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        state_track_feats = _query_arcgis_geojson(
            [self.roads_url],
            query_bbox,
            cache_path=self.cache_dir / f"au_qld_state_tracks_{bhash}.json",
            where="class = 'Track' AND (road_owner IS NULL OR road_owner <> 'QPWS')",
            out_fields="road_name_full,sub_class,trafficability,surface_type,road_owner",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        tourist_feats = _query_arcgis_geojson(
            [self.tourist_url],
            query_bbox,
            cache_path=self.cache_dir / f"au_qld_tourist_{bhash}.json",
            where="feature_type IN ('Camping Ground', 'Lookout', 'Picnic Area', 'Hut', 'Shelter')",
            out_fields="feature_type,name,alternate_name",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        survey_feats = _query_arcgis_geojson(
            [self.survey_url],
            query_bbox,
            cache_path=self.cache_dir / f"au_qld_survey_{bhash}.json",
            where=(
                "ahdfix_de = 'Trig' OR UPPER(mrktype_de) LIKE '%TRIG%' "
                "OR UPPER(mrktype_de) LIKE '%BEACON%' OR UPPER(mrktype_de) LIKE '%PILLAR%'"
            ),
            out_fields="mrk_id,alt1_nm,locality_de,mrktype_de,ahdfix_de,ahdheight,mrkcnd_de",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        if not survey_feats:
            survey_feats = _query_arcgis_geojson(
                [self.survey_url],
                query_bbox,
                cache_path=self.cache_dir / f"au_qld_survey_named_{bhash}.json",
                where="alt1_nm IS NOT NULL AND ahdheight IS NOT NULL AND mrkcnd_de = 'Good'",
                out_fields="mrk_id,alt1_nm,locality_de,mrktype_de,ahdfix_de,ahdheight,mrkcnd_de",
                page_size=25,
                max_pages=1,
                timeout_s=self.timeout_s,
                client=self._injected_client,
            )

        # Dedupe QPWS features across layers /6, /5, /2 by objectid while preserving fire_trail priority
        trail_rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
        qpws_by_oid: dict[Any, int] = {}

        def _add_qpws(feats: list[dict[str, Any]], *, is_walking_layer: bool) -> None:
            for feat in feats:
                props = feat.get("properties") or {}
                name = clean_feature_name(_prop_ci(props, "name", "visitor_id"))
                trail_class, surface = _classify_qld_qpws_trail(
                    props, name, is_walking_layer=is_walking_layer
                )
                if trail_class is None:
                    continue
                oid = _prop_ci(props, "objectid")
                row = (
                    feat["geometry"],
                    {
                        "name": name,
                        "trail_class": trail_class,
                        "surface": surface,
                        "state": "QLD",
                    },
                )
                if oid is not None and oid in qpws_by_oid:
                    existing_idx = qpws_by_oid[oid]
                    existing_attrs = trail_rows[existing_idx][1]
                    if trail_class == "fire_trail":
                        existing_attrs["trail_class"] = "fire_trail"
                    if not existing_attrs["name"] and name:
                        existing_attrs["name"] = name
                    continue
                if oid is not None:
                    qpws_by_oid[oid] = len(trail_rows)
                trail_rows.append(row)

        _add_qpws(great_walk_feats, is_walking_layer=True)
        _add_qpws(walking_feats, is_walking_layer=True)
        _add_qpws(park_road_feats, is_walking_layer=False)

        for feat in state_track_feats:
            props = feat.get("properties") or {}
            name = clean_feature_name(_prop_ci(props, "road_name_full", "name"))
            if name.lower() == "track":
                name = ""
            lower_name = name.lower()
            sub_class = str(_prop_ci(props, "sub_class") or "").strip().lower()
            traff = str(_prop_ci(props, "trafficability") or "").strip().lower()
            surf_raw = str(_prop_ci(props, "surface_type") or "").strip().lower()

            if "firebreak" in lower_name or "fire trail" in lower_name or "fireline" in lower_name:
                trail_class = "fire_trail"
            elif "non vehicular" in sub_class or "pedestrian" in traff or "bicycle" in traff:
                trail_class = "walking_track"
            elif "management" in lower_name:
                trail_class = "management_trail"
            else:
                trail_class = "4wd_track"

            if "sealed" in surf_raw and "unsealed" not in surf_raw:
                surface = "sealed"
            elif "boardwalk" in surf_raw:
                surface = "boardwalk"
            elif "unsealed" in surf_raw:
                surface = "unsealed"
            else:
                surface = "natural" if trail_class == "walking_track" else "unsealed"

            trail_rows.append(
                (
                    feat["geometry"],
                    {
                        "name": name,
                        "trail_class": trail_class,
                        "surface": surface,
                        "state": "QLD",
                    },
                )
            )

        poi_rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for feat in tourist_feats:
            props = feat.get("properties") or {}
            ftype = str(_prop_ci(props, "feature_type") or "").strip().lower()
            if "camp" in ftype:
                poi_type = "campground"
            elif "lookout" in ftype:
                poi_type = "lookout"
            elif "picnic" in ftype:
                poi_type = "picnic"
            elif "hut" in ftype or "shelter" in ftype:
                poi_type = "hut"
            else:
                continue
            poi_rows.append(
                (
                    feat["geometry"],
                    {
                        "name": clean_feature_name(_prop_ci(props, "name", "alternate_name")),
                        "poi_type": poi_type,
                        "elev_m": None,
                        "state": "QLD",
                    },
                )
            )

        for feat in survey_feats:
            props = feat.get("properties") or {}
            mrk_id = _prop_ci(props, "mrk_id")
            name = clean_feature_name(_prop_ci(props, "alt1_nm", "locality_de"))
            if not name:
                name = f"PM {mrk_id}" if mrk_id is not None else "Trig Station"
            poi_rows.append(
                (
                    feat["geometry"],
                    {
                        "name": name,
                        "poi_type": "trig",
                        "elev_m": parse_elev_m(_prop_ci(props, "ahdheight", "elev_m")),
                        "state": "QLD",
                    },
                )
            )

        return write_state_trails_gpkg(out_gpkg, layout, trail_rows, poi_rows)


class ACTTrailsSource(_BaseAUOverlaySource):
    """ACTmapi / EPSDD Fire & Management Trails, Namadgi Alpine Huts, Campgrounds & Trigs (CC BY 4.0)."""

    name: str = "au-act-trails"
    overlay_key: str = "au-act-trails"
    attribution: str = (
        "ACT Trails, Huts & Survey Marks: "
        "© Australian Capital Territory (ACTmapi / EPSDD) (CC BY 4.0)"
    )

    def __init__(
        self,
        *,
        base_url: str | None = None,
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
        self.base_url = (
            base_url or os.environ.get("GRIDFORGE_AU_ACT_BASE_URL", DEFAULT_ACT_BASE_URL)
        ).rstrip("/")

    def fetch_overlay(self, layout: SheetLayout, work_dir: Path) -> Path | None:
        work_dir.mkdir(parents=True, exist_ok=True)
        out_gpkg = work_dir / "au_act_trails.gpkg"
        if self.local_gpkg is not None:
            return _clip_and_reproject_local_gpkg(
                self.local_gpkg,
                layout,
                out_gpkg,
                ("au_state_trails", "au_state_pois"),
            )

        if not is_in_act_coverage(layout.wgs84_fetch_bbox):
            logger.debug("Sheet bbox outside ACT coverage; skipping au-act-trails")
            return None

        query_bbox = _round_enclosing_bbox(layout.wgs84_fetch_bbox)
        bhash = _bbox_hash(query_bbox)

        track_feats = _query_arcgis_geojson(
            [f"{self.base_url}/ACTGOV_TRANSPORT_TRACK/FeatureServer/0/query"],
            query_bbox,
            cache_path=self.cache_dir / f"au_act_tracks_{bhash}.json",
            out_fields="ROAD_NAME,DISTRICT_NAME",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        equestrian_feats = _query_arcgis_geojson(
            [f"{self.base_url}/ACT_Equestrian_Routes/FeatureServer/0/query"],
            query_bbox,
            cache_path=self.cache_dir / f"au_act_equestrian_{bhash}.json",
            out_fields="BICENTENNIAL_NATIONAL_TRAIL,ROUTE_STATUS",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        rural_road_feats = _query_arcgis_geojson(
            [f"{self.base_url}/ACTGOV_ROAD_CENTRELINES/FeatureServer/0/query"],
            query_bbox,
            cache_path=self.cache_dir / f"au_act_rural_roads_{bhash}.json",
            where="HIERARCHY = 'RURAL SPECIAL PURPOSE'",
            out_fields="ROAD_NAME,HIERARCHY",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        heritage_hut_feats = _query_arcgis_geojson(
            [f"{self.base_url}/ACTGOV_Heritage_Register/FeatureServer/1/query"],
            query_bbox,
            cache_path=self.cache_dir / f"au_act_heritage_huts_{bhash}.json",
            where="(UPPER(SITE_NAME) LIKE '%HUT%' OR UPPER(NAME) LIKE '%HUT%') AND DataRestrictions = 'No'",
            out_fields="SITE_NAME,NAME,DataRestrictions",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        feat_name_feats = _query_arcgis_geojson(
            [f"{self.base_url}/ACTGOV_FEATURE_NAME/FeatureServer/0/query"],
            query_bbox,
            cache_path=self.cache_dir / f"au_act_feature_names_{bhash}.json",
            where=(
                "TYPE IN ('LOOKOUT','CAMPGROUND','PICNIC AREA') "
                "OR UPPER(DISPLAY_NAME) LIKE '%HUT%' OR UPPER(DISPLAY_NAME) LIKE '%COTTAGE%'"
            ),
            out_fields="TYPE,DISPLAY_NAME,FEATURE_NAME",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        bbq_feats = _query_arcgis_geojson(
            [f"{self.base_url}/ACTGOV_Barbecue_Assets/FeatureServer/1/query"],
            query_bbox,
            cache_path=self.cache_dir / f"au_act_bbq_{bhash}.json",
            out_fields="ASSET_NAME,LOCATION",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        shelter_feats = _query_arcgis_geojson(
            [f"{self.base_url}/ACTGOV_Shelter_Assets/FeatureServer/1/query"],
            query_bbox,
            cache_path=self.cache_dir / f"au_act_shelters_{bhash}.json",
            out_fields="ASSET_NAME,LOCATION",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        trig_feats = _query_arcgis_geojson(
            [f"{self.base_url}/ACTGOV_SURVEY_CONTROL_MARK_CURRENT_View/FeatureServer/3/query"],
            query_bbox,
            cache_path=self.cache_dir / f"au_act_trigs_{bhash}.json",
            where=(
                "UPPER(ALIAS) LIKE 'TS%' OR UPPER(MARK_CONSTRUCTION) LIKE '%PILLAR%' "
                "OR (BEACON_CONSTRUCTION IS NOT NULL AND BEACON_CONSTRUCTION <> 'NONE') "
                "OR (MARK_TYPE = 'MC' AND REDUCED_LEVEL IS NOT NULL)"
            ),
            out_fields="MARK_TYPE,MARK_NAME_FULL,ALIAS,REDUCED_LEVEL,MARK_CONSTRUCTION,BEACON_CONSTRUCTION",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )

        trail_rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for feat in track_feats:
            props = feat.get("properties") or {}
            name = clean_feature_name(_prop_ci(props, "ROAD_NAME", "name"))
            lower_name = name.lower()
            if "fire" in lower_name:
                trail_class = "fire_trail"
            elif "trail" in lower_name or "management" in lower_name:
                trail_class = "management_trail"
            else:
                trail_class = "4wd_track"
            trail_rows.append(
                (
                    feat["geometry"],
                    {
                        "name": name,
                        "trail_class": trail_class,
                        "surface": "unsealed",
                        "state": "ACT",
                    },
                )
            )

        for feat in equestrian_feats:
            props = feat.get("properties") or {}
            is_bnt = str(_prop_ci(props, "BICENTENNIAL_NATIONAL_TRAIL") or "").strip().upper() == "YES"
            trail_rows.append(
                (
                    feat["geometry"],
                    {
                        "name": "Bicentennial National Trail" if is_bnt else "",
                        "trail_class": "walking_track" if is_bnt else "management_trail",
                        "surface": "natural",
                        "state": "ACT",
                    },
                )
            )

        for feat in rural_road_feats:
            props = feat.get("properties") or {}
            name = clean_feature_name(_prop_ci(props, "ROAD_NAME", "name"))
            trail_class = "fire_trail" if "fire" in name.lower() else "management_trail"
            trail_rows.append(
                (
                    feat["geometry"],
                    {
                        "name": name,
                        "trail_class": trail_class,
                        "surface": "unsealed",
                        "state": "ACT",
                    },
                )
            )

        poi_rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
        # Heritage Register huts (Polygon geometry -> Centroid in _write_projected_gpkg_layers)
        for feat in heritage_hut_feats:
            props = feat.get("properties") or {}
            name = clean_feature_name(_prop_ci(props, "SITE_NAME", "NAME"))
            if not name:
                continue
            poi_rows.append(
                (
                    feat["geometry"],
                    {
                        "name": name,
                        "poi_type": "hut",
                        "elev_m": None,
                        "state": "ACT",
                    },
                )
            )

        for feat in feat_name_feats:
            props = feat.get("properties") or {}
            ftype = str(_prop_ci(props, "TYPE") or "").strip().lower()
            name = clean_feature_name(_prop_ci(props, "DISPLAY_NAME", "FEATURE_NAME"))
            if "lookout" in ftype:
                poi_type = "lookout"
            elif "camp" in ftype:
                poi_type = "campground"
            elif "picnic" in ftype:
                poi_type = "picnic"
            elif _HUT_WORD_RE.search(name) or "cottage" in name.lower():
                poi_type = "hut"
            else:
                continue
            poi_rows.append(
                (
                    feat["geometry"],
                    {
                        "name": name,
                        "poi_type": poi_type,
                        "elev_m": None,
                        "state": "ACT",
                    },
                )
            )

        # Deduplicate ACT PCS campgrounds & picnic areas by normalized LOCATION / ASSET_NAME
        seen_act_locations: set[tuple[str, str]] = set()
        park_prefix_re = re.compile(
            r"^(?:namadgi national park|tidbinbilla (?:nature|special purpose) reserve)\s*,?\s+",
            re.IGNORECASE,
        )

        def _norm_act_loc(raw_name: str) -> str:
            cleaned = park_prefix_re.sub("", raw_name).strip()
            cleaned = re.sub(r"\s*-\s*shelter.*$", "", cleaned, flags=re.IGNORECASE).strip()
            cleaned = re.sub(r"\s+\d+$", "", cleaned).strip()
            return cleaned or raw_name

        for feat in bbq_feats:
            props = feat.get("properties") or {}
            loc = clean_feature_name(_prop_ci(props, "LOCATION"))
            asset_name = clean_feature_name(_prop_ci(props, "ASSET_NAME"))
            candidate = asset_name if loc.lower() in _ACT_GENERIC_PARK_LOCATIONS else (loc or asset_name)
            lower_cand = candidate.lower()
            if not candidate or lower_cand in _ACT_GENERIC_PARK_LOCATIONS:
                continue
            if "camp" in lower_cand:
                poi_type = "campground"
            elif "picnic" in lower_cand:
                poi_type = "picnic"
            else:
                continue
            norm_label = _norm_act_loc(candidate)
            key = (poi_type, norm_label.lower())
            if key in seen_act_locations:
                continue
            seen_act_locations.add(key)
            poi_rows.append(
                (
                    feat["geometry"],
                    {
                        "name": norm_label,
                        "poi_type": poi_type,
                        "elev_m": None,
                        "state": "ACT",
                    },
                )
            )

        for feat in shelter_feats:
            props = feat.get("properties") or {}
            loc = clean_feature_name(_prop_ci(props, "LOCATION"))
            asset_name = clean_feature_name(_prop_ci(props, "ASSET_NAME"))
            combined = f"{asset_name} {loc}".lower()
            if "camp" in combined:
                norm_label = _norm_act_loc(asset_name if "camp" in asset_name.lower() else loc)
                key = ("campground", norm_label.lower())
                if key in seen_act_locations:
                    continue
                seen_act_locations.add(key)
                poi_rows.append(
                    (
                        feat["geometry"],
                        {"name": norm_label, "poi_type": "campground", "elev_m": None, "state": "ACT"},
                    )
                )
            elif "picnic" in combined:
                norm_label = _norm_act_loc(loc or asset_name)
                key = ("picnic", norm_label.lower())
                if key in seen_act_locations:
                    continue
                seen_act_locations.add(key)
                poi_rows.append(
                    (
                        feat["geometry"],
                        {"name": norm_label, "poi_type": "picnic", "elev_m": None, "state": "ACT"},
                    )
                )
            elif asset_name:
                # Park backcountry fixed-roof shelters -> hut icon (clean label)
                norm_label = _norm_act_loc(asset_name)
                poi_rows.append(
                    (
                        feat["geometry"],
                        {
                            "name": "" if norm_label.lower() == "shelter" else norm_label,
                            "poi_type": "hut",
                            "elev_m": None,
                            "state": "ACT",
                        },
                    )
                )

        for feat in trig_feats:
            props = feat.get("properties") or {}
            name = clean_feature_name(_prop_ci(props, "MARK_NAME_FULL", "ALIAS")) or "Trig Station"
            poi_rows.append(
                (
                    feat["geometry"],
                    {
                        "name": name,
                        "poi_type": "trig",
                        "elev_m": parse_elev_m(_prop_ci(props, "REDUCED_LEVEL", "elev_m")),
                        "state": "ACT",
                    },
                )
            )

        return write_state_trails_gpkg(out_gpkg, layout, trail_rows, poi_rows)


# ---------------------------------------------------------------------------
# Pan-Australia Auto Cross-Border State Trails Router (All 8 States/Territories)
# ---------------------------------------------------------------------------

_STATE_BOUNDS_ORDER: Final[tuple[tuple[str, BBox], ...]] = (
    ("ACT", ACT_COVERAGE_BBOX),
    ("NSW", NSW_COVERAGE_BBOX),
    ("VIC", VIC_COVERAGE_BBOX),
    ("TAS", TAS_COVERAGE_BBOX),
    ("QLD", QLD_COVERAGE_BBOX),
    ("WA", WA_COVERAGE_BBOX),
    ("SA", SA_COVERAGE_BBOX),
    ("NT", NT_COVERAGE_BBOX),
)


def resolve_intersecting_states(bbox: BBox) -> list[str]:
    """Return ordered list of Australian state/territory codes intersecting ``bbox``.

    Sheets strictly inside the ACT resolve to ``['ACT']`` only (avoiding noisy NSW
    overlap); sheets straddling the ACT–NSW border resolve to ``['ACT', 'NSW']``
    for seamless cross-border merging.
    """
    if _bbox_strictly_within(bbox, ACT_STRICT_BBOX):
        return ["ACT"]
    predicates = (
        ("ACT", is_in_act_coverage),
        ("NSW", is_in_nsw_coverage),
        ("VIC", is_in_vic_coverage),
        ("TAS", is_in_tas_coverage),
        ("QLD", is_in_qld_coverage),
        ("WA", is_in_wa_coverage),
        ("SA", is_in_sa_coverage),
        ("NT", is_in_nt_coverage),
    )
    return [code for code, fn in predicates if fn(bbox)]


def _gpkg_has_features(gpkg_path: Path) -> bool:
    from osgeo import ogr  # type: ignore[import-untyped]

    ogr.UseExceptions()
    ds = ogr.Open(str(gpkg_path))
    if ds is None:
        return False
    try:
        for lname in ("au_state_trails", "au_state_pois"):
            lyr = ds.GetLayerByName(lname)
            if lyr is not None and lyr.GetFeatureCount() > 0:
                return True
        return False
    finally:
        ds = None


def _merge_state_trail_gpkgs(
    gpkg_paths: list[Path],
    utm_epsg: int,
    out_gpkg: Path,
) -> Path | None:
    """Merge multiple projected ``au_state_trails`` + ``au_state_pois`` GPKGs with border deduplication."""
    from osgeo import ogr, osr  # type: ignore[import-untyped]

    ogr.UseExceptions()
    dst_srs = osr.SpatialReference()
    dst_srs.ImportFromEPSG(utm_epsg)
    dst_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

    out_gpkg.parent.mkdir(parents=True, exist_ok=True)
    if out_gpkg.exists():
        out_gpkg.unlink()

    driver = ogr.GetDriverByName("GPKG")
    dst_ds = driver.CreateDataSource(str(out_gpkg))
    if dst_ds is None:
        raise RuntimeError(f"Failed to create merged AU state trails GeoPackage at {out_gpkg}")

    trails_lyr = dst_ds.CreateLayer("au_state_trails", srs=dst_srs, geom_type=ogr.wkbMultiLineString)
    for fname in ("name", "trail_class", "surface", "state"):
        trails_lyr.CreateField(ogr.FieldDefn(fname, ogr.OFTString))
    trails_defn = trails_lyr.GetLayerDefn()

    pois_lyr = dst_ds.CreateLayer("au_state_pois", srs=dst_srs, geom_type=ogr.wkbPoint)
    for fname in ("name", "poi_type"):
        pois_lyr.CreateField(ogr.FieldDefn(fname, ogr.OFTString))
    pois_lyr.CreateField(ogr.FieldDefn("elev_m", ogr.OFTReal))
    pois_lyr.CreateField(ogr.FieldDefn("state", ogr.OFTString))
    pois_defn = pois_lyr.GetLayerDefn()

    seen_trails: set[tuple[str, str, int, int, int, int]] = set()
    poi_records: list[tuple[bytes, dict[str, Any], tuple[str, str, int, int]]] = []
    total_written = 0

    try:
        dst_ds.StartTransaction()
        try:
            for src_path in gpkg_paths:
                src_ds = ogr.Open(str(src_path))
                if src_ds is None:
                    continue
                try:
                    src_trails = src_ds.GetLayerByName("au_state_trails")
                    if src_trails is not None:
                        for feat in src_trails:
                            geom = feat.GetGeometryRef()
                            if geom is None or geom.IsEmpty():
                                continue
                            env = geom.GetEnvelope()
                            name = str(feat.GetField("name") or "").strip()
                            tclass = str(feat.GetField("trail_class") or "").strip()
                            tkey = (
                                tclass.lower(),
                                name.lower(),
                                int(round(env[0] / 25.0)),
                                int(round(env[2] / 25.0)),
                                int(round(env[1] / 25.0)),
                                int(round(env[3] / 25.0)),
                            )
                            if name and tkey in seen_trails:
                                continue
                            seen_trails.add(tkey)

                            out_feat = ogr.Feature(trails_defn)
                            out_geom = geom.Clone()
                            out_geom.FlattenTo2D()
                            out_feat.SetGeometry(out_geom)
                            out_feat.SetField("name", name)
                            out_feat.SetField("trail_class", tclass)
                            out_feat.SetField("surface", str(feat.GetField("surface") or "").strip())
                            out_feat.SetField("state", str(feat.GetField("state") or "").strip())
                            trails_lyr.CreateFeature(out_feat)
                            out_feat = None
                            total_written += 1

                    src_pois = src_ds.GetLayerByName("au_state_pois")
                    if src_pois is not None:
                        for feat in src_pois:
                            geom = feat.GetGeometryRef()
                            if geom is None or geom.IsEmpty():
                                continue
                            out_geom = geom.Clone()
                            out_geom.FlattenTo2D()
                            x, y = out_geom.GetX(), out_geom.GetY()
                            name = str(feat.GetField("name") or "").strip()
                            ptype = str(feat.GetField("poi_type") or "").strip()
                            elev = feat.GetField("elev_m")
                            pkey = (
                                ptype.lower(),
                                name.lower(),
                                int(round(x / 100.0)),
                                int(round(y / 100.0)),
                            )
                            poi_records.append(
                                (
                                    bytes(out_geom.ExportToWkb()),
                                    {
                                        "name": name,
                                        "poi_type": ptype,
                                        "elev_m": elev,
                                        "state": str(feat.GetField("state") or "").strip(),
                                    },
                                    pkey,
                                )
                            )
                finally:
                    src_ds = None

            # Prefer POIs with non-None elev_m when deduplicating across state borders
            poi_records.sort(key=lambda r: 0 if r[1]["elev_m"] is not None else 1)
            seen_pois: set[tuple[str, str, int, int]] = set()
            for wkb, attrs, pkey in poi_records:
                if pkey in seen_pois:
                    continue
                seen_pois.add(pkey)
                out_feat = ogr.Feature(pois_defn)
                out_feat.SetGeometry(ogr.CreateGeometryFromWkb(wkb))
                out_feat.SetField("name", attrs["name"])
                out_feat.SetField("poi_type", attrs["poi_type"])
                if attrs["elev_m"] is not None:
                    out_feat.SetField("elev_m", float(attrs["elev_m"]))
                out_feat.SetField("state", attrs["state"])
                pois_lyr.CreateFeature(out_feat)
                out_feat = None
                total_written += 1
            dst_ds.CommitTransaction()
        except Exception:
            dst_ds.RollbackTransaction()
            raise
    finally:
        dst_ds = None

    if total_written == 0:
        out_gpkg.unlink(missing_ok=True)
        return None
    return out_gpkg


class AutoStateTrailsSource(_BaseAUOverlaySource):
    """Auto-detecting cross-border Australian state trail, hut, and trig overlay router (all 8 states)."""

    name: str = "au-state-trails"
    overlay_key: str = "au-state-trails"
    attribution: str = "Australian State Trails, Huts & Survey Control (CC BY)"

    def __init__(
        self,
        *,
        state_sources: Mapping[str, OverlaySource] | None = None,
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
        self._base_cache_dir = cache_dir
        self._injected_state_sources: dict[str, OverlaySource] = (
            {k.upper(): v for k, v in state_sources.items()} if state_sources else {}
        )
        self.resolved_attributions: list[str] = []

    def _get_state_source(self, state_code: str) -> OverlaySource:
        code = state_code.upper()
        if code in self._injected_state_sources:
            return self._injected_state_sources[code]

        kwargs: dict[str, Any] = {
            "cache_dir": self._base_cache_dir,
            "timeout_s": self.timeout_s,
            "client": self._injected_client,
        }
        if code == "NSW":
            return NSWTrailsSource(**kwargs)
        if code == "VIC":
            return VICTrailsSource(**kwargs)
        if code == "TAS":
            return TASTrailsSource(**kwargs)
        if code == "QLD":
            return QLDTrailsSource(**kwargs)
        if code == "ACT":
            return ACTTrailsSource(**kwargs)

        from gridforge.sources.overlays_au_states_west import (
            NTTrailsSource,
            SATrailsSource,
            WATrailsSource,
        )

        if code == "WA":
            return WATrailsSource(**kwargs)
        if code == "SA":
            return SATrailsSource(**kwargs)
        if code == "NT":
            return NTTrailsSource(**kwargs)
        raise ValueError(f"Unsupported Australian state code: {state_code}")

    def fetch_overlay(self, layout: SheetLayout, work_dir: Path) -> Path | None:
        work_dir.mkdir(parents=True, exist_ok=True)
        out_gpkg = work_dir / "au_state_trails.gpkg"
        self.resolved_attributions = []

        if self.local_gpkg is not None:
            return _clip_and_reproject_local_gpkg(
                self.local_gpkg,
                layout,
                out_gpkg,
                ("au_state_trails", "au_state_pois"),
            )

        states = resolve_intersecting_states(layout.wgs84_fetch_bbox)
        if not states:
            logger.debug("Sheet bbox outside Australia; skipping au-state-trails")
            return None

        contributing_gpkgs: list[Path] = []
        contributing_attrs: list[str] = []
        temp_dirs: list[Path] = []

        try:
            for code in states:
                state_work = work_dir / f"_au_state_{code.lower()}"
                temp_dirs.append(state_work)
                src = self._get_state_source(code)
                try:
                    gpkg = src.fetch_overlay(layout, state_work)
                except Exception as exc:
                    logger.warning("AutoStateTrailsSource skipping %s after error: %s", code, exc)
                    continue
                if gpkg is not None and gpkg.is_file() and _gpkg_has_features(gpkg):
                    contributing_gpkgs.append(gpkg)
                    attr = str(getattr(src, "attribution", "") or "").strip()
                    if attr and attr not in contributing_attrs:
                        contributing_attrs.append(attr)

            if not contributing_gpkgs:
                return None

            self.resolved_attributions = contributing_attrs
            if contributing_attrs:
                self.attribution = " | ".join(contributing_attrs)

            if len(contributing_gpkgs) == 1:
                if out_gpkg.exists():
                    out_gpkg.unlink()
                shutil.copy2(contributing_gpkgs[0], out_gpkg)
                return out_gpkg

            return _merge_state_trail_gpkgs(contributing_gpkgs, layout.utm_epsg, out_gpkg)
        finally:
            for tdir in temp_dirs:
                shutil.rmtree(tdir, ignore_errors=True)
