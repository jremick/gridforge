"""Australian state-level CC BY 4.0 backcountry trail, fire-trail, hut, and trig overlays (NSW & VIC)."""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

import httpx

from gridforge.config import BBox, SheetLayout
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

# Fast-path geographic bounds for NSW/ACT and Victoria (WGS84 lon/lat)
NSW_COVERAGE_BBOX: Final[BBox] = BBox(min_x=140.5, min_y=-37.8, max_x=154.0, max_y=-28.0)
VIC_COVERAGE_BBOX: Final[BBox] = BBox(min_x=140.5, min_y=-39.4, max_x=150.2, max_y=-33.8)

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

_PLACEHOLDER_NAMES: Final[frozenset[str]] = frozenset({"unnamed", "unknown", "none", "null", "n/a"})


def _clean_feature_name(raw: Any) -> str:
    text = str(raw or "").strip()
    return "" if text.lower() in _PLACEHOLDER_NAMES else text


def _bboxes_intersect(a: BBox, b: BBox) -> bool:
    return (
        a.max_x >= b.min_x
        and a.min_x <= b.max_x
        and a.max_y >= b.min_y
        and a.min_y <= b.max_y
    )


def is_in_nsw_coverage(bbox: BBox) -> bool:
    """Return True if ``bbox`` (WGS84) intersects NSW / ACT coverage."""
    return _bboxes_intersect(bbox, NSW_COVERAGE_BBOX)


def is_in_vic_coverage(bbox: BBox) -> bool:
    """Return True if ``bbox`` (WGS84) intersects Victoria coverage."""
    return _bboxes_intersect(bbox, VIC_COVERAGE_BBOX)


def _parse_elevation_m(value: Any) -> float | None:
    """Parse float meters from numeric or string elevation fields like ``'2229.470'`` or ``'986.7 m'``."""
    direct = _to_float_or_none(value)
    if direct is not None:
        return direct
    if isinstance(value, str):
        match = _NUMERIC_PREFIX_RE.search(value)
        if match:
            return _to_float_or_none(match.group(0))
    return None


def _compose_nsw_road_name(props: Mapping[str, Any]) -> str:
    full = _clean_feature_name(_prop_ci(props, "roadname", "firetrailname", "trailname", "name"))
    if full:
        return full
    base = _clean_feature_name(_prop_ci(props, "roadnamebase"))
    rtype = _clean_feature_name(_prop_ci(props, "roadnametype"))
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
    if "fire trail" in lower_name or "firetrail" in lower_name or "fireline" in lower_name or "fire access" in lower_name or "fire" in ftype:
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


def _dedupe_poi_rows(
    rows: list[tuple[dict[str, Any], dict[str, Any]]],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Drop duplicate POIs sharing the same ``(poi_type, name)`` and ~100 m location."""
    deduped: list[tuple[dict[str, Any], dict[str, Any]]] = []
    seen: set[tuple[str, str, int, int]] = set()
    for geom, attrs in rows:
        coords = geom.get("coordinates") if isinstance(geom, dict) else None
        lon_bucket = 0
        lat_bucket = 0
        if isinstance(coords, (list, tuple)) and len(coords) >= 2:
            try:
                lon_bucket = int(round(float(coords[0]) * 1000.0))
                lat_bucket = int(round(float(coords[1]) * 1000.0))
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


def _query_wfs_geojson(
    wfs_url: str,
    type_name: str,
    bbox: BBox,
    *,
    cache_path: Path,
    cql_filter: str | None = None,
    geom_column: str = "geom",
    max_features: int = 2500,
    timeout_s: float = 25.0,
    retries: int = 2,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    """Query an OGC WFS 2.0.0 GeoJSON endpoint using EPSG:4326 CQL BBOX filtering with disk cache."""
    if cache_path.is_file() and cache_path.stat().st_size > 0:
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(cached, list):
                return cached
        except json.JSONDecodeError:
            logger.warning("Ignoring corrupt cached WFS overlay JSON: %s", cache_path)

    bbox_cql = (
        f"BBOX({geom_column},{bbox.min_x:.6f},{bbox.min_y:.6f},"
        f"{bbox.max_x:.6f},{bbox.max_y:.6f},'EPSG:4326')"
    )
    full_cql = f"{bbox_cql} AND ({cql_filter})" if cql_filter else bbox_cql

    params: dict[str, str] = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeNames": type_name,
        "outputFormat": "application/json",
        "srsName": "EPSG:4326",
        "count": str(max_features),
        "CQL_FILTER": full_cql,
    }

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
                last_error = f"{wfs_url} ({type_name}, try={attempt + 1}): {exc}"
                continue

            if not isinstance(data, dict) or not isinstance(data.get("features"), list):
                last_error = f"{wfs_url} ({type_name}): unexpected WFS payload"
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


def _write_state_trails_gpkg(
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
            _dedupe_poi_rows(poi_rows),
        ),
    ]
    return _write_projected_gpkg_layers(out_gpkg, layout.utm_epsg, layout.utm_fetch_bbox, layer_specs)


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
            trigname = str(_prop_ci(props, "trigname", "name") or "").strip()
            if not trigname:
                marknum = _prop_ci(props, "marknumber")
                trigname = f"TS {marknum}" if marknum is not None else "Trig Station"
            poi_rows.append(
                (
                    feat["geometry"],
                    {
                        "name": trigname,
                        "poi_type": "trig",
                        "elev_m": _parse_elevation_m(
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
            name = str(_prop_ci(props, "poilabel", "poiname", "generalname", "name") or "").strip()
            poi_rows.append(
                (
                    feat["geometry"],
                    {
                        "name": name,
                        "poi_type": poi_type,
                        "elev_m": _parse_elevation_m(_prop_ci(props, "elev_m", "elevation")),
                        "state": "NSW",
                    },
                )
            )

        for feat in hut_feats:
            props = feat.get("properties") or {}
            name = str(_prop_ci(props, "generalname", "name", "poilabel") or "").strip()
            if not name:
                continue
            poi_rows.append(
                (
                    feat["geometry"],
                    {
                        "name": name,
                        "poi_type": "hut",
                        "elev_m": _parse_elevation_m(_prop_ci(props, "elev_m", "elevation")),
                        "state": "NSW",
                    },
                )
            )

        return _write_state_trails_gpkg(out_gpkg, layout, trail_rows, poi_rows)


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

        road_feats = _query_wfs_geojson(
            self.wfs_url,
            "open-data-platform:tr_road",
            query_bbox,
            cache_path=self.cache_dir / f"au_vic_tr_road_{bhash}.json",
            cql_filter="class_code IN (7,8,9) OR (class_code=6 AND road_seal='2')",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        rec_track_feats = _query_wfs_geojson(
            self.wfs_url,
            "open-data-platform:recweb_tracks",
            query_bbox,
            cache_path=self.cache_dir / f"au_vic_recweb_tracks_{bhash}.json",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        foi_feats = _query_wfs_geojson(
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
        rec_hut_feats = _query_wfs_geojson(
            self.wfs_url,
            "open-data-platform:recweb_hut",
            query_bbox,
            cache_path=self.cache_dir / f"au_vic_recweb_hut_{bhash}.json",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        rec_site_feats = _query_wfs_geojson(
            self.wfs_url,
            "open-data-platform:recweb_site",
            query_bbox,
            cache_path=self.cache_dir / f"au_vic_recweb_site_{bhash}.json",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        geomark_feats = _query_wfs_geojson(
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
            name = _clean_feature_name(_prop_ci(props, "ezi_road_name_label", "road_name", "name"))
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
            name = _clean_feature_name(_prop_ci(props, "name", "track_name"))
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
            name = str(_prop_ci(props, "name_label", "name") or "").strip()
            poi_rows.append(
                (
                    feat["geometry"],
                    {
                        "name": name,
                        "poi_type": poi_type,
                        "elev_m": _parse_elevation_m(
                            _prop_ci(props, "elev_m", "elevation", "ahd_height")
                        ),
                        "state": "VIC",
                    },
                )
            )

        for feat in rec_hut_feats:
            props = feat.get("properties") or {}
            name = str(_prop_ci(props, "name", "hut_name", "site_name") or "").strip()
            poi_rows.append(
                (
                    feat["geometry"],
                    {
                        "name": name or "Hut",
                        "poi_type": "hut",
                        "elev_m": _parse_elevation_m(_prop_ci(props, "elev_m", "elevation")),
                        "state": "VIC",
                    },
                )
            )

        for feat in rec_site_feats:
            props = feat.get("properties") or {}
            poi_type = _classify_vic_poi(props, default_type="campground")
            if poi_type is None:
                continue
            name = str(_prop_ci(props, "name", "site_name", "name_label") or "").strip()
            poi_rows.append(
                (
                    feat["geometry"],
                    {
                        "name": name,
                        "poi_type": poi_type,
                        "elev_m": _parse_elevation_m(_prop_ci(props, "elev_m", "elevation")),
                        "state": "VIC",
                    },
                )
            )

        for feat in geomark_feats:
            props = feat.get("properties") or {}
            poi_type = _classify_vic_poi(props)
            if poi_type is None:
                continue
            name = str(_prop_ci(props, "name_label", "name") or "").strip()
            poi_rows.append(
                (
                    feat["geometry"],
                    {
                        "name": name,
                        "poi_type": poi_type,
                        "elev_m": _parse_elevation_m(
                            _prop_ci(props, "elev_m", "elevation", "height", "ahd_height")
                        ),
                        "state": "VIC",
                    },
                )
            )

        return _write_state_trails_gpkg(out_gpkg, layout, trail_rows, poi_rows)
