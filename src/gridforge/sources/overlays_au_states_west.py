"""Australian western & central state CC BY 4.0 trail, fire-trail, hut, and trig overlays (WA, SA, NT).

All endpoints strictly use commercial-friendly CC BY 4.0 public open data (zero auth):
  - ``WATrailsSource`` (``au-wa-trails``): DBCA AGOL Trails & RATIS Sites/Facilities + Landgate SLIP
    Tracks, DBCA Fire Towers (DBCA-061), and Geodetic Survey Marks (LGATE-076).
    (Intentionally omits DBCA-027 CC BY-NC 4.0.)
  - ``SATrailsSource`` (``au-sa-trails``): DEW Recreation Trails ZIP streamed via GDAL ``/vsizip/``
    with OGR spatial filter (< 20 MB RSS), DEW Fire Access Tracks, GA Topo250K 4WD tracks,
    cached DEW Parks Features & Facilities JSON, and Location SA Survey Marks (MAD_WMS_PRD/0).
  - ``NTTrailsSource`` (``au-nt-trails``): NTG Road Centrelines (4WD/unformed/pastoral), GA Topo250K
    tracks, NTLIS GeoServer WFS 2.0 Place Names & Survey Control Marks (AHD).

Outputs the exact unified 2-layer UTM GeoPackage schema shared across all 8 Australian states:
  - ``au_state_trails`` (MultiLineString: ``name``, ``trail_class``, ``surface``, ``state``)
  - ``au_state_pois``   (Point: ``name``, ``poi_type``, ``elev_m``, ``state``)
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
import zipfile
import zlib
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any, Final, TypeVar

import httpx

from gridforge.config import BBox, SheetLayout
from gridforge.sources._http import bounded_response_chunks, read_bounded_file, stream_provider_get
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

# Fast-path geographic coverage bounding boxes (WGS84 lon/lat)
WA_COVERAGE_BBOX: Final[BBox] = BBox(min_x=112.9, min_y=-35.2, max_x=129.0, max_y=-13.6)
SA_COVERAGE_BBOX: Final[BBox] = BBox(min_x=129.0, min_y=-38.1, max_x=141.0, max_y=-26.0)
NT_COVERAGE_BBOX: Final[BBox] = BBox(min_x=129.0, min_y=-26.0, max_x=138.0, max_y=-10.9)

# --- Western Australia CC BY 4.0 endpoints (DBCA AGOL + Landgate SLIP Public Services) ---
DEFAULT_WA_TRACKS_URL: Final[str] = (
    "https://services3.arcgis.com/lSP3CnkTXxMmSpdI/arcgis/rest/services/"
    "Tracks_and_Trails_hosted/FeatureServer/0/query"
)
DEFAULT_WA_SLIP_ROADS_URL: Final[str] = (
    "https://public-services.slip.wa.gov.au/public/rest/services/"
    "SLIP_Public_Services/Transport/MapServer/20/query"
)
DEFAULT_WA_RATIS_SITES_URL: Final[str] = (
    "https://services3.arcgis.com/lSP3CnkTXxMmSpdI/arcgis/rest/services/"
    "RATIS_Sites/FeatureServer/0/query"
)
DEFAULT_WA_RATIS_FACILITIES_URL: Final[str] = (
    "https://services3.arcgis.com/lSP3CnkTXxMmSpdI/arcgis/rest/services/"
    "RATIS_Facilities/FeatureServer/0/query"
)
DEFAULT_WA_FIRE_TOWERS_URL: Final[str] = (
    "https://public-services.slip.wa.gov.au/public/rest/services/"
    "SLIP_Public_Services/Environment/MapServer/56/query"
)
DEFAULT_WA_GEODETIC_URL: Final[str] = (
    "https://public-services.slip.wa.gov.au/public/rest/services/"
    "SLIP_Public_Services/Property_and_Planning/MapServer/1/query"
)

# --- South Australia CC BY 4.0 endpoints (DEW / DHUD / Location SA + GA Topo250K) ---
DEFAULT_SA_TRAILS_ZIP_URL: Final[str] = (
    "https://www.waterconnect.sa.gov.au/Content/Downloads/DEWNR/TOPO_RecreationTrails_geojson.zip"
)
DEFAULT_SA_TRAILS_ZIP_INNER: Final[str] = "TOPO_RecreationTrails_GDA2020.geojson"
_MAX_SA_TRAILS_ZIP_BYTES = 32 * 1024 * 1024
_MAX_SA_TRAILS_MEMBER_BYTES = 64 * 1024 * 1024
_MAX_SA_TRAILS_ZIP_ENTRIES = 128
_MAX_SA_PARKS_BYTES = 8 * 1024 * 1024
DEFAULT_SA_FIRE_TRACKS_URL: Final[str] = (
    "https://services-ap1.arcgis.com/G7iVcvOu3F7YCq11/arcgis/rest/services/"
    "Fire_Management_Fire_Access_Tracks/FeatureServer/1/query"
)
DEFAULT_SA_PARKS_JSON_URL: Final[str] = (
    "https://data.sa.gov.au/data/dataset/919a3d26-e150-4282-8c30-2e090287b9b3/"
    "resource/4aec8622-c685-4484-bb83-18cda3f30959/download/parks-data.json"
)
DEFAULT_SA_SURVEY_MARKS_URL: Final[str] = (
    "https://location.sa.gov.au/server5/rest/services/MAD_WMS_PRD/MapServer/0/query"
)

# --- Northern Territory CC BY 4.0 endpoints (NTG DEPWS / NTLIS WFS 2.0 + GA Topo250K) ---
DEFAULT_NT_ROADS_URL: Final[str] = (
    "https://gis-d.nt.gov.au/arcgis/rest/services/ROAD_CENTRELINES/FeatureServer/0/query"
)
DEFAULT_NT_WFS_URL: Final[str] = "https://ogc.ntlis.nt.gov.au/gs/ows"

# --- Shared Geoscience Australia CC BY 4.0 Topo250K Roads (Outback 4WD/Bush Tracks) ---
DEFAULT_GA_TOPO250K_ROADS_URL: Final[str] = (
    "https://services.ga.gov.au/gis/rest/services/Topographic_Base_Map/MapServer/434/query"
)

_HUT_WORD_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(hut|huts|bivouac|shelter|refuge|cabin)\b", re.IGNORECASE
)
_NUMERIC_PREFIX_RE: Final[re.Pattern[str]] = re.compile(r"[-+]?\d+(?:\.\d+)?")
_PLACEHOLDER_NAMES: Final[frozenset[str]] = frozenset(
    {"unnamed", "unknown", "none", "null", "n/a", "pastoral road"}
)


def _bbox_intersects(a: BBox, b: BBox) -> bool:
    return (
        a.max_x >= b.min_x
        and a.min_x <= b.max_x
        and a.max_y >= b.min_y
        and a.min_y <= b.max_y
    )


def is_in_wa_coverage(bbox: BBox) -> bool:
    """Return True if ``bbox`` (WGS84) intersects Western Australia coverage."""
    return _bbox_intersects(bbox, WA_COVERAGE_BBOX)


def is_in_sa_coverage(bbox: BBox) -> bool:
    """Return True if ``bbox`` (WGS84) intersects South Australia coverage."""
    return _bbox_intersects(bbox, SA_COVERAGE_BBOX)


def is_in_nt_coverage(bbox: BBox) -> bool:
    """Return True if ``bbox`` (WGS84) intersects Northern Territory coverage."""
    return _bbox_intersects(bbox, NT_COVERAGE_BBOX)


def _clean_name(raw: Any, *, strip_leading_star: bool = False) -> str:
    text = str(raw or "").strip()
    if strip_leading_star:
        text = text.lstrip("*").strip()
    return "" if text.lower() in _PLACEHOLDER_NAMES else text


def _parse_elev(value: Any) -> float | None:
    direct = _to_float_or_none(value)
    if direct is not None:
        return direct
    if isinstance(value, str):
        match = _NUMERIC_PREFIX_RE.search(value)
        if match:
            return _to_float_or_none(match.group(0))
    return None


def _dedupe_pois(
    rows: list[tuple[dict[str, Any], dict[str, Any]]],
    *,
    grid_scale: float = 1000.0,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Drop duplicate POIs sharing the same ``(poi_type, name)`` within ~100 m (or grid_scale)."""
    deduped: list[tuple[dict[str, Any], dict[str, Any]]] = []
    seen: set[tuple[str, str, int, int]] = set()
    for geom, attrs in rows:
        coords = geom.get("coordinates") if isinstance(geom, dict) else None
        lon_b = 0
        lat_b = 0
        if isinstance(coords, (list, tuple)) and len(coords) >= 2:
            try:
                lon_b = int(round(float(coords[0]) * grid_scale))
                lat_b = int(round(float(coords[1]) * grid_scale))
            except (TypeError, ValueError):
                pass
        key = (
            str(attrs.get("poi_type") or "").lower(),
            str(attrs.get("name") or "").strip().lower(),
            lon_b,
            lat_b,
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append((geom, attrs))
    return deduped


def _write_unified_state_gpkg(
    out_gpkg: Path,
    layout: SheetLayout,
    trail_rows: list[tuple[dict[str, Any], dict[str, Any]]],
    poi_rows: list[tuple[dict[str, Any], dict[str, Any]]],
) -> Path | None:
    """Write the unified 2-layer ``au_state_trails`` + ``au_state_pois`` UTM GeoPackage."""
    if not trail_rows and not poi_rows:
        return None

    # Reuse canonical writer from overlays_au_states lazily (avoids circular imports).
    try:
        from gridforge.sources import overlays_au_states as _east

        writer = getattr(_east, "write_state_trails_gpkg", None) or getattr(
            _east, "_write_state_trails_gpkg", None
        )
        if callable(writer):
            return writer(out_gpkg, layout, trail_rows, poi_rows)
    except Exception:
        pass

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
            _dedupe_pois(poi_rows),
        ),
    ]
    return _write_projected_gpkg_layers(out_gpkg, layout.utm_epsg, layout.utm_fetch_bbox, layer_specs)


_CacheValue = TypeVar("_CacheValue")


def _stream_download_cached(
    url: str,
    dest: Path,
    *,
    max_bytes: int,
    validate: Callable[[Path], _CacheValue],
    timeout_s: float,
    client: httpx.Client | None = None,
) -> tuple[Path, _CacheValue] | None:
    """Validate the existing cache or publish one complete, bounded provider response."""
    if dest.is_file():
        try:
            if not 0 < dest.stat().st_size <= max_bytes:
                raise ValueError("Cached overlay exceeds the byte budget or is empty")
            return dest, validate(dest)
        except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, zlib.error):
            logger.warning("Ignoring invalid cached SA overlay: %s", dest)

    dest.parent.mkdir(parents=True, exist_ok=True)
    owns_client = client is None
    http = (
        client
        if client is not None
        else httpx.Client(
            timeout=httpx.Timeout(timeout_s, connect=min(15.0, timeout_s)),
            headers={"User-Agent": USER_AGENT},
        )
    )
    try:
        with tempfile.NamedTemporaryFile(dir=dest.parent, suffix=".part", delete=False) as pending:
            pending_path = Path(pending.name)
            try:
                deadline = time.monotonic() + timeout_s
                with stream_provider_get(
                    http, url, params=None, user_agent=USER_AGENT, deadline=deadline
                ) as resp:
                    resp.raise_for_status()
                    for chunk in bounded_response_chunks(resp, max_bytes=max_bytes, deadline=deadline):
                        pending.write(chunk)
                pending.close()
                value = validate(pending_path)
                pending_path.replace(dest)
                return dest, value
            finally:
                pending_path.unlink(missing_ok=True)
    except (OSError, httpx.HTTPError, ValueError, RuntimeError, zipfile.BadZipFile, zlib.error) as exc:
        logger.warning("Failed streaming download %s (%s)", url, exc)
        return None
    finally:
        if owns_client:
            http.close()


def _sa_parks_data(path: Path) -> list[dict[str, Any]]:
    parks = json.loads(read_bounded_file(path, _MAX_SA_PARKS_BYTES))
    if not isinstance(parks, list):
        raise ValueError("SA parks data must be a JSON list")
    return [entry for entry in parks if isinstance(entry, dict)]


def _sa_trails_zip_member(path: Path) -> str:
    """Check the layer name, archive resources, and integrity before opening it with GDAL."""
    if not 0 < path.stat().st_size <= _MAX_SA_TRAILS_ZIP_BYTES:
        raise ValueError("SA trails archive exceeds the byte budget or is empty")

    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        if len(members) > _MAX_SA_TRAILS_ZIP_ENTRIES:
            raise ValueError("SA trails archive exceeds the entry budget")
        selected = next(
            (entry for entry in members if entry.filename == DEFAULT_SA_TRAILS_ZIP_INNER),
            None,
        )
        if selected is None:
            selected = next(
                (entry for entry in members if entry.filename.lower().endswith((".geojson", ".json"))),
                None,
            )
        if selected is None:
            raise ValueError("SA trails archive contains no GeoJSON layer")

        name = selected.filename
        member = PurePosixPath(name)
        if (
            member.is_absolute()
            or ".." in member.parts
            or "\\" in name
            or ":" in name
            or str(member) != name
            or sum(entry.filename == name for entry in members) != 1
        ):
            raise ValueError("SA trails archive has an invalid GeoJSON layer name")

        if (
            not 0 < selected.file_size <= _MAX_SA_TRAILS_MEMBER_BYTES
            or selected.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
            or selected.flag_bits & 1
        ):
            raise ValueError("SA trails archive layer exceeds the extraction budget or uses an unsupported format")

        # Reading the selected member to EOF lets zipfile verify its CRC while
        # holding only one small chunk in memory.
        with archive.open(selected) as source:
            prefix = source.read(4096).removeprefix(b"\xef\xbb\xbf").lstrip()
            if not prefix.startswith(b"{"):
                raise ValueError("SA trails archive layer is not a GeoJSON object")
            while source.read(64 * 1024):
                pass
        return name


def _read_sa_trails_from_vsizip(
    zip_path: Path,
    bbox: BBox,
    inner_name: str,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Stream-filter a validated SA Recreation Trails layer directly from its ZIP."""
    from osgeo import gdal  # type: ignore[import-untyped]  # system python3-gdal ships untyped osgeo bindings

    resolved = zip_path.resolve()
    vsi_path = f"/vsizip/{resolved}/{inner_name}"
    gdal.UseExceptions()
    try:
        ds = gdal.OpenEx(vsi_path, gdal.OF_VECTOR, allowed_drivers=["GeoJSON"])
    except RuntimeError as exc:
        logger.warning("GDAL rejected SA trail layer %s (%s)", zip_path, exc)
        return []
    if ds is None:
        return []

    rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
    try:
        layer = ds.GetLayer(0)
        if layer is None:
            return []
        layer.SetSpatialFilterRect(bbox.min_x, bbox.min_y, bbox.max_x, bbox.max_y)
        for feat in layer:
            geom = feat.GetGeometryRef()
            if geom is None or geom.IsEmpty():
                continue
            props = feat.items()
            status = str(_prop_ci(props, "TRAILSTATU", "trailstatus") or "").strip().lower()
            if status in ("closed", "proposed", "retired"):
                continue

            name = _clean_name(_prop_ci(props, "TRAILNAME", "trailname"))
            trail_type = str(_prop_ci(props, "TRAILTYPE", "trailtype") or "").strip().lower()
            lower_name = name.lower()
            if "fire" in lower_name:
                trail_class = "fire_trail"
            elif "4wd" in lower_name or "4wd" in trail_type:
                trail_class = "4wd_track"
            else:
                trail_class = "walking_track"

            surface = "natural" if "walk" in trail_type or trail_class == "walking_track" else "unsealed"
            gj_geom = json.loads(geom.ExportToJson())
            rows.append(
                (
                    gj_geom,
                    {
                        "name": name,
                        "trail_class": trail_class,
                        "surface": surface,
                        "state": "SA",
                    },
                )
            )
    finally:
        ds = None

    return rows


# ---------------------------------------------------------------------------
# 1. Western Australia (au-wa-trails)
# ---------------------------------------------------------------------------


def _classify_wa_ratis_site(props: Mapping[str, Any]) -> tuple[str | None, str]:
    status = str(_prop_ci(props, "RTS_STATUS") or "OPEN").strip().upper()
    if status and status != "OPEN":
        return (None, "")
    func = str(_prop_ci(props, "RTS_PRIMARY_FUNCTION") or "").strip().lower()
    if func == "management site only":
        return (None, "")

    name = _clean_name(_prop_ci(props, "RTS_NAME", "name"))
    if not name:
        return (None, "")
    low = name.lower()

    if "camp" in func or "camp" in low or "retreat" in low or "accommodation" in func:
        return ("campground", name)
    if _HUT_WORD_RE.search(name):
        return ("hut", name)
    if "picnic" in low or "day use" in low or "car park" in low or "trail head" in low or "trailhead" in low:
        return ("picnic", name)
    if any(k in low for k in ("lookout", "falls", "gorge", "summit", "bluff", "ledge", "peak")):
        return ("lookout", name)
    # Skip road / trail-section administrative markers that aren't true visitor POIs
    if any(k in low for k in ("section ", " road", " drive", "entry station", "water tank")):
        return (None, "")
    if "day use" in func:
        return ("picnic", name)
    return (None, "")


class WATrailsSource(_BaseAUOverlaySource):
    """Western Australia DBCA Trails, RATIS Sites/Facilities, SLIP Tracks, Fire Towers & Trigs (CC BY 4.0)."""

    name: str = "au-wa-trails"
    overlay_key: str = "au-wa-trails"
    attribution: str = (
        "Western Australia Trails, Parks & Geodetic Marks: "
        "© State of Western Australia (DBCA & Landgate SLIP) (CC BY 4.0)"
    )

    def __init__(
        self,
        *,
        tracks_url: str | None = None,
        slip_roads_url: str | None = None,
        ratis_sites_url: str | None = None,
        ratis_facilities_url: str | None = None,
        fire_towers_url: str | None = None,
        geodetic_url: str | None = None,
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
        self.tracks_url = tracks_url or os.environ.get("GRIDFORGE_AU_WA_TRACKS_URL", DEFAULT_WA_TRACKS_URL)
        self.slip_roads_url = slip_roads_url or os.environ.get(
            "GRIDFORGE_AU_WA_SLIP_ROADS_URL", DEFAULT_WA_SLIP_ROADS_URL
        )
        self.ratis_sites_url = ratis_sites_url or os.environ.get(
            "GRIDFORGE_AU_WA_RATIS_SITES_URL", DEFAULT_WA_RATIS_SITES_URL
        )
        self.ratis_facilities_url = ratis_facilities_url or os.environ.get(
            "GRIDFORGE_AU_WA_RATIS_FACILITIES_URL", DEFAULT_WA_RATIS_FACILITIES_URL
        )
        self.fire_towers_url = fire_towers_url or os.environ.get(
            "GRIDFORGE_AU_WA_FIRE_TOWERS_URL", DEFAULT_WA_FIRE_TOWERS_URL
        )
        self.geodetic_url = geodetic_url or os.environ.get(
            "GRIDFORGE_AU_WA_GEODETIC_URL", DEFAULT_WA_GEODETIC_URL
        )

    def fetch_overlay(self, layout: SheetLayout, work_dir: Path) -> Path | None:
        work_dir.mkdir(parents=True, exist_ok=True)
        out_gpkg = work_dir / "au_wa_trails.gpkg"
        if self.local_gpkg is not None:
            return _clip_and_reproject_local_gpkg(
                self.local_gpkg,
                layout,
                out_gpkg,
                ("au_state_trails", "au_state_pois"),
            )

        if not is_in_wa_coverage(layout.wgs84_fetch_bbox):
            logger.debug("Sheet bbox outside Western Australia coverage; skipping au-wa-trails")
            return None

        query_bbox = _round_enclosing_bbox(layout.wgs84_fetch_bbox)
        bhash = _bbox_hash(query_bbox)

        # 1. DBCA AGOL Trails (Bibbulmun, Cape to Cape, Munda Biddi, Walk/Bike/Bridle Trails)
        dbca_tracks = _query_arcgis_geojson(
            [self.tracks_url],
            query_bbox,
            cache_path=self.cache_dir / f"au_wa_dbca_tracks_{bhash}.json",
            out_fields="TYPE",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        # 2. Landgate SLIP Unsealed / 4WD / Fire Tracks (LGATE-195 layer 20)
        slip_tracks = _query_arcgis_geojson(
            [self.slip_roads_url],
            query_bbox,
            cache_path=self.cache_dir / f"au_wa_slip_tracks_{bhash}.json",
            where="mapclassification = 'Track' OR fcsubtype = 'Track'",
            out_fields="road_name,fcsubtype,mapclassification,roadsurface",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        # 3. DBCA RATIS Visitor Sites (Campgrounds, Lookouts, Picnic Areas)
        ratis_sites = _query_arcgis_geojson(
            [self.ratis_sites_url],
            query_bbox,
            cache_path=self.cache_dir / f"au_wa_ratis_sites_{bhash}.json",
            out_fields="RTS_NAME,RTS_PRIMARY_FUNCTION,RTS_STATUS",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        # 4. DBCA RATIS Facilities (Camp Shelters & Lookouts/Decks)
        ratis_facs = _query_arcgis_geojson(
            [self.ratis_facilities_url],
            query_bbox,
            cache_path=self.cache_dir / f"au_wa_ratis_facs_{bhash}.json",
            where="RTT_SUB_TYPE_DESC IN ('Camp Shelter','Lookout/Deck')",
            out_fields="RTT_FAC_TYPE,RTT_SUB_TYPE_DESC,RTT_ASSET_DESC,RTT_ASSET_STATUS",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        # 5. DBCA Fire Towers (DBCA-061 -> Lookouts)
        fire_towers = _query_arcgis_geojson(
            [self.fire_towers_url],
            query_bbox,
            cache_path=self.cache_dir / f"au_wa_fire_towers_{bhash}.json",
            out_fields="ftw_name,ftw_hist_name,ftw_type,ftw_status",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        # 6. Landgate SLIP Geodetic Survey Marks (LGATE-076 — S0/S1/SD primary, Outback B-mark fallback)
        geodetic_marks = _query_arcgis_geojson(
            [self.geodetic_url],
            query_bbox,
            cache_path=self.cache_dir / f"au_wa_geodetic_primary_{bhash}.json",
            where=(
                "reduced_level IS NOT NULL AND "
                "(render_value LIKE 'S0%' OR render_value LIKE 'S1%' OR render_value LIKE 'SD%')"
            ),
            out_fields=(
                "geodetic_point_name,stamped_name_on_point,point_type,render_value,"
                "filter_value,reduced_level,vert_datum,latest_status_description"
            ),
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        if not geodetic_marks:
            geodetic_marks = _query_arcgis_geojson(
                [self.geodetic_url],
                query_bbox,
                cache_path=self.cache_dir / f"au_wa_geodetic_outback_{bhash}.json",
                where="reduced_level IS NOT NULL AND filter_value = 2",
                out_fields=(
                    "geodetic_point_name,stamped_name_on_point,point_type,render_value,"
                    "filter_value,reduced_level,vert_datum,latest_status_description"
                ),
                page_size=50,
                max_pages=1,
                timeout_s=self.timeout_s,
                client=self._injected_client,
            )

        trail_rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for feat in dbca_tracks:
            props = feat.get("properties") or {}
            name = _clean_name(_prop_ci(props, "TYPE", "name"))
            low = name.lower()
            if "fire" in low:
                tclass = "fire_trail"
            elif "management" in low:
                tclass = "management_trail"
            else:
                tclass = "walking_track"
            trail_rows.append(
                (
                    feat["geometry"],
                    {
                        "name": name,
                        "trail_class": tclass,
                        "surface": "natural" if tclass == "walking_track" else "unsealed",
                        "state": "WA",
                    },
                )
            )

        for feat in slip_tracks:
            props = feat.get("properties") or {}
            name = _clean_name(_prop_ci(props, "road_name", "name"))
            low = name.lower()
            if "fire" in low:
                tclass = "fire_trail"
            elif "management" in low:
                tclass = "management_trail"
            else:
                tclass = "4wd_track"
            raw_surf = str(_prop_ci(props, "roadsurface") or "").strip().lower()
            surface = raw_surf if raw_surf and raw_surf not in ("unknown", "none") else "unsealed"
            trail_rows.append(
                (
                    feat["geometry"],
                    {
                        "name": name,
                        "trail_class": tclass,
                        "surface": surface,
                        "state": "WA",
                    },
                )
            )

        poi_rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for feat in ratis_sites:
            props = feat.get("properties") or {}
            poi_type, label = _classify_wa_ratis_site(props)
            if poi_type is None:
                continue
            poi_rows.append(
                (
                    feat["geometry"],
                    {"name": label, "poi_type": poi_type, "elev_m": None, "state": "WA"},
                )
            )

        for feat in ratis_facs:
            props = feat.get("properties") or {}
            asset_status = str(_prop_ci(props, "RTT_ASSET_STATUS") or "").strip().lower()
            if any(bad in asset_status for bad in ("closed", "removed", "decommissioned")):
                continue
            subtype = str(_prop_ci(props, "RTT_SUB_TYPE_DESC") or "").strip().lower()
            desc = _clean_name(_prop_ci(props, "RTT_ASSET_DESC"))
            if "camp shelter" in subtype:
                poi_rows.append(
                    (
                        feat["geometry"],
                        {
                            "name": desc[:48] if desc else "Camp Shelter",
                            "poi_type": "hut",
                            "elev_m": None,
                            "state": "WA",
                        },
                    )
                )
            elif "lookout" in subtype:
                poi_rows.append(
                    (
                        feat["geometry"],
                        {
                            "name": desc[:48] if desc else "Lookout",
                            "poi_type": "lookout",
                            "elev_m": None,
                            "state": "WA",
                        },
                    )
                )

        for feat in fire_towers:
            props = feat.get("properties") or {}
            status = str(_prop_ci(props, "ftw_status") or "").strip().lower()
            if "removed" in status or "demolished" in status:
                continue
            name = (
                _clean_name(_prop_ci(props, "ftw_name"))
                or _clean_name(_prop_ci(props, "ftw_hist_name"))
                or "Fire Tower"
            )
            poi_rows.append(
                (
                    feat["geometry"],
                    {"name": name, "poi_type": "lookout", "elev_m": None, "state": "WA"},
                )
            )

        for feat in geodetic_marks:
            props = feat.get("properties") or {}
            status = str(_prop_ci(props, "latest_status_description") or "").strip().upper()
            if any(bad in status for bad in ("DESTROYED", "NOT FOUND", "REMOVED")):
                continue
            elev_m = _parse_elev(_prop_ci(props, "reduced_level", "elev_m"))
            if elev_m is None:
                continue
            name = (
                _clean_name(_prop_ci(props, "geodetic_point_name"))
                or _clean_name(_prop_ci(props, "stamped_name_on_point"))
                or "Geodetic Mark"
            )
            poi_rows.append(
                (
                    feat["geometry"],
                    {"name": name, "poi_type": "trig", "elev_m": elev_m, "state": "WA"},
                )
            )

        return _write_unified_state_gpkg(out_gpkg, layout, trail_rows, poi_rows)


# ---------------------------------------------------------------------------
# 2. South Australia (au-sa-trails)
# ---------------------------------------------------------------------------


def _to_int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


class SATrailsSource(_BaseAUOverlaySource):
    """South Australia DEW Recreation Trails (/vsizip/), Fire Access Tracks, Parks & Survey Marks (CC BY 4.0)."""

    name: str = "au-sa-trails"
    overlay_key: str = "au-sa-trails"
    attribution: str = (
        "South Australia Trails, Parks & Survey Marks: "
        "© Government of South Australia (DEW / DHUD) (CC BY 4.0) "
        "& Geoscience Australia (CC BY 4.0)"
    )

    def __init__(
        self,
        *,
        trails_zip_url: str | None = None,
        trails_zip_path: Path | str | None = None,
        fire_tracks_url: str | None = None,
        ga_tracks_url: str | None = None,
        parks_json_url: str | None = None,
        survey_marks_url: str | None = None,
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
        self.trails_zip_url = trails_zip_url or os.environ.get(
            "GRIDFORGE_AU_SA_TRAILS_ZIP_URL", DEFAULT_SA_TRAILS_ZIP_URL
        )
        self.trails_zip_path = Path(trails_zip_path) if trails_zip_path is not None else None
        self.fire_tracks_url = fire_tracks_url or os.environ.get(
            "GRIDFORGE_AU_SA_FIRE_TRACKS_URL", DEFAULT_SA_FIRE_TRACKS_URL
        )
        self.ga_tracks_url = ga_tracks_url or os.environ.get(
            "GRIDFORGE_AU_GA_TOPO_ROADS_URL", DEFAULT_GA_TOPO250K_ROADS_URL
        )
        self.parks_json_url = parks_json_url or os.environ.get(
            "GRIDFORGE_AU_SA_PARKS_JSON_URL", DEFAULT_SA_PARKS_JSON_URL
        )
        self.survey_marks_url = survey_marks_url or os.environ.get(
            "GRIDFORGE_AU_SA_SURVEY_MARKS_URL", DEFAULT_SA_SURVEY_MARKS_URL
        )

    def _load_sa_parks_in_bbox(self, bbox: BBox) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        parks_cache = self.cache_dir / "sa_parks_data.json"
        cached = _stream_download_cached(
            self.parks_json_url,
            parks_cache,
            max_bytes=_MAX_SA_PARKS_BYTES,
            validate=_sa_parks_data,
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        if cached is None:
            return []

        _, parks = cached
        rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for entry in parks:
            lat = _to_float_or_none(entry.get("lat"))
            lon = _to_float_or_none(entry.get("lng") if "lng" in entry else entry.get("lon"))
            if lat is None or lon is None:
                continue
            if not (bbox.min_x <= lon <= bbox.max_x and bbox.min_y <= lat <= bbox.max_y):
                continue
            name = _clean_name(entry.get("name"))
            if not name:
                continue

            camping = entry.get("campingAndAccommodation") or {}
            has_camp = isinstance(camping, dict) and any(bool(v) for v in camping.values())
            low = name.lower()
            if has_camp:
                poi_type = "campground"
            elif any(k in low for k in ("summit", "lookout", "falls", "gorge", "gully", "cliff")):
                poi_type = "lookout"
            else:
                poi_type = "picnic"

            rows.append(
                (
                    {"type": "Point", "coordinates": [lon, lat]},
                    {"name": name, "poi_type": poi_type, "elev_m": None, "state": "SA"},
                )
            )
        return rows

    def fetch_overlay(self, layout: SheetLayout, work_dir: Path) -> Path | None:
        work_dir.mkdir(parents=True, exist_ok=True)
        out_gpkg = work_dir / "au_sa_trails.gpkg"
        if self.local_gpkg is not None:
            return _clip_and_reproject_local_gpkg(
                self.local_gpkg,
                layout,
                out_gpkg,
                ("au_state_trails", "au_state_pois"),
            )

        if not is_in_sa_coverage(layout.wgs84_fetch_bbox):
            logger.debug("Sheet bbox outside South Australia coverage; skipping au-sa-trails")
            return None

        query_bbox = _round_enclosing_bbox(layout.wgs84_fetch_bbox)
        bhash = _bbox_hash(query_bbox)

        trail_rows: list[tuple[dict[str, Any], dict[str, Any]]] = []

        # 1. DEW Recreation Trails ZIP -> low-RAM GDAL /vsizip/ spatial filter
        if self.trails_zip_path is None:
            trail_archive = _stream_download_cached(
                self.trails_zip_url,
                self.cache_dir / "sa_recreation_trails.zip",
                max_bytes=_MAX_SA_TRAILS_ZIP_BYTES,
                validate=_sa_trails_zip_member,
                timeout_s=max(45.0, self.timeout_s),
                client=self._injected_client,
            )
        else:
            try:
                trail_archive = (self.trails_zip_path, _sa_trails_zip_member(self.trails_zip_path))
            except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, zlib.error) as exc:
                logger.warning("Ignoring invalid local SA trails ZIP %s (%s)", self.trails_zip_path, exc)
                trail_archive = None
        if trail_archive is not None:
            zip_path, inner_name = trail_archive
            trail_rows.extend(_read_sa_trails_from_vsizip(zip_path, query_bbox, inner_name))

        # 2. DEW SA Fire Access Tracks (Official DEW AGOL FeatureServer/1)
        fire_tracks = _query_arcgis_geojson(
            [self.fire_tracks_url],
            query_bbox,
            cache_path=self.cache_dir / f"au_sa_fire_tracks_{bhash}.json",
            out_fields="TRACK_NAME,RESERVE_NAME,ASSET_NAME,PROP_FA_CLASS,ACTIVITY",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        for feat in fire_tracks:
            props = feat.get("properties") or {}
            name = (
                _clean_name(_prop_ci(props, "TRACK_NAME"))
                or _clean_name(_prop_ci(props, "RESERVE_NAME"))
                or _clean_name(_prop_ci(props, "ASSET_NAME"))
            )
            trail_rows.append(
                (
                    feat["geometry"],
                    {
                        "name": name,
                        "trail_class": "fire_trail",
                        "surface": "unsealed",
                        "state": "SA",
                    },
                )
            )

        # 3. GA Topo250K Outback 4WD/Bush Tracks (MapServer/434)
        ga_tracks = _query_arcgis_geojson(
            [self.ga_tracks_url],
            query_bbox,
            cache_path=self.cache_dir / f"au_sa_ga_tracks_{bhash}.json",
            where="class IN ('Track','Vehicular Track','Foot Track')",
            out_fields="name,class,formation",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        for feat in ga_tracks:
            props = feat.get("properties") or {}
            name = _clean_name(_prop_ci(props, "name"))
            cls = str(_prop_ci(props, "class") or "").strip().lower()
            formation = str(_prop_ci(props, "formation") or "").strip().lower()
            tclass = "walking_track" if "foot" in cls else "4wd_track"
            trail_rows.append(
                (
                    feat["geometry"],
                    {
                        "name": name,
                        "trail_class": tclass,
                        "surface": formation if formation and formation != "unknown" else "unsealed",
                        "state": "SA",
                    },
                )
            )

        # 4. Cached DEW SA Parks Features & Facilities JSON (campgrounds & visitor POIs)
        poi_rows: list[tuple[dict[str, Any], dict[str, Any]]] = self._load_sa_parks_in_bbox(
            query_bbox
        )

        # 5. Location SA Survey Marks (MAD_WMS_PRD/0): ALWAYS query where="1=1" & filter in Python!
        #    (Server-side LIKE causes unindexed full-table scan timeout on MAD_WMS_PRD.)
        survey_marks = _query_arcgis_geojson(
            [self.survey_marks_url],
            query_bbox,
            cache_path=self.cache_dir / f"au_sa_survey_marks_{bhash}.json",
            where="1=1",
            out_fields="MARK_NO,MARKTYPE,SPECATTRIBUTES,H_ORDER,V_ORDER,V_DATUM,HEIGHT,GONE",
            page_size=300,
            max_pages=1,
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        primary_marks: list[tuple[dict[str, Any], dict[str, Any]]] = []
        fallback_pillar_marks: list[tuple[dict[str, Any], dict[str, Any]]] = []
        fallback_any_ahd: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for feat in survey_marks:
            props = feat.get("properties") or {}
            if str(_prop_ci(props, "GONE") or "").strip().upper() == "Y":
                continue
            elev_m = _parse_elev(_prop_ci(props, "HEIGHT", "elev_m"))
            if elev_m is None:
                continue
            v_datum = str(_prop_ci(props, "V_DATUM") or "").strip().upper()
            if v_datum and v_datum not in ("A", "AHD", "AHD71"):
                continue

            h_order = _to_int_or_none(_prop_ci(props, "H_ORDER"))
            v_order = _to_int_or_none(_prop_ci(props, "V_ORDER"))
            marktype = str(_prop_ci(props, "MARKTYPE") or "").strip().upper()
            spec_attr = str(_prop_ci(props, "SPECATTRIBUTES") or "").strip().upper()
            mark_no = _clean_name(_prop_ci(props, "MARK_NO"))
            label = f"SM {mark_no}" if mark_no else "Survey Mark"

            is_high_order = (
                (h_order is not None and h_order <= 2)
                or (v_order is not None and v_order <= 2)
                or marktype in ("TS", "TRIG", "PLR", "BCN", "CRN")
                or any(k in spec_attr for k in ("TRIG", "PILLAR", "BEACON"))
            )
            row = (
                feat["geometry"],
                {"name": label, "poi_type": "trig", "elev_m": elev_m, "state": "SA"},
            )
            if is_high_order:
                primary_marks.append(row)
            elif marktype in ("PSMP", "PSM", "BM", "MP") or (v_order is not None and v_order <= 3):
                fallback_pillar_marks.append(row)
            else:
                fallback_any_ahd.append(row)

        selected_marks = primary_marks or fallback_pillar_marks or fallback_any_ahd
        # Thin dense survey marks (~500 m spatial bucket) so labels remain crisp
        if len(selected_marks) > 12:
            thinned: list[tuple[dict[str, Any], dict[str, Any]]] = []
            seen_cells: set[tuple[int, int]] = set()
            for geom, attrs in selected_marks:
                coords = geom.get("coordinates") if isinstance(geom, dict) else None
                cell = (0, 0)
                if isinstance(coords, (list, tuple)) and len(coords) >= 2:
                    cell = (int(round(float(coords[0]) * 200.0)), int(round(float(coords[1]) * 200.0)))
                if cell in seen_cells:
                    continue
                seen_cells.add(cell)
                thinned.append((geom, attrs))
            selected_marks = thinned
        poi_rows.extend(selected_marks)

        return _write_unified_state_gpkg(out_gpkg, layout, trail_rows, poi_rows)


# ---------------------------------------------------------------------------
# 3. Northern Territory (au-nt-trails)
# ---------------------------------------------------------------------------

_NT_PLACENAME_POI_MAP: Final[dict[str, str]] = {
    "lookout": "lookout",
    "cliff": "lookout",
    "bluff": "lookout",
    "gorge": "lookout",
    "waterfall": "lookout",
    "hut": "hut",
    "shelter": "hut",
    "ranger station": "hut",
    "campground": "campground",
    "camping area": "campground",
    "camping ground": "campground",
    "picnic area": "picnic",
    "rest area": "picnic",
}


class NTTrailsSource(_BaseAUOverlaySource):
    """Northern Territory NTG 4WD/Pastoral Tracks, GA Topo250K Tracks & NTLIS WFS POIs/Trigs (CC BY 4.0)."""

    name: str = "au-nt-trails"
    overlay_key: str = "au-nt-trails"
    attribution: str = (
        "Northern Territory Tracks, Place Names & Survey Marks: "
        "© Northern Territory of Australia (NTLIS / DEPWS) (CC BY 4.0) "
        "& Geoscience Australia (CC BY 4.0)"
    )

    def __init__(
        self,
        *,
        roads_url: str | None = None,
        ga_tracks_url: str | None = None,
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
        self.roads_url = roads_url or os.environ.get("GRIDFORGE_AU_NT_ROADS_URL", DEFAULT_NT_ROADS_URL)
        self.ga_tracks_url = ga_tracks_url or os.environ.get(
            "GRIDFORGE_AU_GA_TOPO_ROADS_URL", DEFAULT_GA_TOPO250K_ROADS_URL
        )
        self.wfs_url = wfs_url or os.environ.get("GRIDFORGE_AU_NT_WFS_URL", DEFAULT_NT_WFS_URL)

    def fetch_overlay(self, layout: SheetLayout, work_dir: Path) -> Path | None:
        work_dir.mkdir(parents=True, exist_ok=True)
        out_gpkg = work_dir / "au_nt_trails.gpkg"
        if self.local_gpkg is not None:
            return _clip_and_reproject_local_gpkg(
                self.local_gpkg,
                layout,
                out_gpkg,
                ("au_state_trails", "au_state_pois"),
            )

        if not is_in_nt_coverage(layout.wgs84_fetch_bbox):
            logger.debug("Sheet bbox outside Northern Territory coverage; skipping au-nt-trails")
            return None

        query_bbox = _round_enclosing_bbox(layout.wgs84_fetch_bbox)
        bhash = _bbox_hash(query_bbox)

        # 1. NTG Road Centrelines (4WD, Unformed, Pastoral tracks)
        nt_roads = _query_arcgis_geojson(
            [self.roads_url],
            query_bbox,
            cache_path=self.cache_dir / f"au_nt_roads_{bhash}.json",
            where="SURFACE_TYPE IN ('4WD','UNFORMED') OR CATEGORY LIKE 'PASTORAL%'",
            out_fields="ROAD_LABEL,ROAD_NAME,ROAD_TYPE,SURFACE_TYPE,CATEGORY",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        # 2. GA Topo250K Outback Tracks supplement
        ga_tracks = _query_arcgis_geojson(
            [self.ga_tracks_url],
            query_bbox,
            cache_path=self.cache_dir / f"au_nt_ga_tracks_{bhash}.json",
            where="class IN ('Track','Vehicular Track','Foot Track')",
            out_fields="name,class,formation",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        # 3. NTLIS GeoServer WFS 2.0 Place Names (Lookouts, Gorges, Waterfalls, Ranger Stations, etc.)
        from gridforge.sources.overlays_au_states import query_wfs_geojson

        placenames = query_wfs_geojson(
            self.wfs_url,
            "ntlis:PLACENAMES",
            query_bbox,
            cache_path=self.cache_dir / f"au_nt_placenames_{bhash}.json",
            max_features=1000,
            use_bbox_param=True,
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        # 4. NTLIS GeoServer WFS 2.0 Survey Marks (AHD control marks)
        survey_marks = query_wfs_geojson(
            self.wfs_url,
            "survey:SURVEY_MARKS",
            query_bbox,
            cache_path=self.cache_dir / f"au_nt_survey_marks_{bhash}.json",
            max_features=1000,
            use_bbox_param=True,
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )

        trail_rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for feat in nt_roads:
            props = feat.get("properties") or {}
            surf_raw = str(_prop_ci(props, "SURFACE_TYPE") or "").strip().upper()
            if surf_raw == "SEALED":
                continue
            name = (
                _clean_name(_prop_ci(props, "ROAD_LABEL"), strip_leading_star=True)
                or _clean_name(_prop_ci(props, "ROAD_NAME"), strip_leading_star=True)
            )
            low = name.lower()
            if "fire" in low:
                tclass = "fire_trail"
            elif surf_raw in ("4WD", "UNFORMED"):
                tclass = "4wd_track"
            else:
                tclass = "management_trail"
            surface = "natural" if surf_raw == "UNFORMED" else "unsealed"
            trail_rows.append(
                (
                    feat["geometry"],
                    {
                        "name": name,
                        "trail_class": tclass,
                        "surface": surface,
                        "state": "NT",
                    },
                )
            )

        for feat in ga_tracks:
            props = feat.get("properties") or {}
            name = _clean_name(_prop_ci(props, "name"))
            cls = str(_prop_ci(props, "class") or "").strip().lower()
            formation = str(_prop_ci(props, "formation") or "").strip().lower()
            tclass = "walking_track" if "foot" in cls else "4wd_track"
            trail_rows.append(
                (
                    feat["geometry"],
                    {
                        "name": name,
                        "trail_class": tclass,
                        "surface": formation if formation and formation != "unknown" else "unsealed",
                        "state": "NT",
                    },
                )
            )

        poi_rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for feat in placenames:
            props = feat.get("properties") or {}
            name = _clean_name(_prop_ci(props, "NAME", "name"))
            if not name:
                continue
            desig = str(_prop_ci(props, "TYPE_DESIGNATION") or "").strip().lower()
            poi_type = _NT_PLACENAME_POI_MAP.get(desig)
            if poi_type is None:
                low = name.lower()
                if _HUT_WORD_RE.search(name):
                    poi_type = "hut"
                elif "camp" in low:
                    poi_type = "campground"
                elif "lookout" in low:
                    poi_type = "lookout"
                elif "picnic" in low:
                    poi_type = "picnic"
            if poi_type is None:
                continue
            poi_rows.append(
                (
                    feat["geometry"],
                    {"name": name, "poi_type": poi_type, "elev_m": None, "state": "NT"},
                )
            )

        primary_nt_marks: list[tuple[dict[str, Any], dict[str, Any]]] = []
        fallback_nt_marks: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for feat in survey_marks:
            props = feat.get("properties") or {}
            mark_type = str(_prop_ci(props, "MARKTYPE") or "").strip().upper()
            elev_m = _parse_elev(_prop_ci(props, "ADJ_RL"))
            if elev_m is None or elev_m <= 0.0:
                elev_m = _parse_elev(_prop_ci(props, "QUASI_AHD"))
            if elev_m is None or elev_m <= 0.0:
                continue
            name = _clean_name(_prop_ci(props, "PRIMARYNAME", "name")) or "Survey Mark"
            row = (
                feat["geometry"],
                {"name": name, "poi_type": "trig", "elev_m": elev_m, "state": "NT"},
            )
            if mark_type in ("CM", "CRM", "TRIG", "PM"):
                primary_nt_marks.append(row)
            else:
                fallback_nt_marks.append(row)

        poi_rows.extend(primary_nt_marks if primary_nt_marks else fallback_nt_marks)
        return _write_unified_state_gpkg(out_gpkg, layout, trail_rows, poi_rows)
