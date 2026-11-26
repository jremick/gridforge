"""Optional US federal public-domain thematic overlays (FEMA NFHL, USGS NHD, FAA DOF, NOAA ENC)."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

import httpx

from gridforge.config import BBox, SheetLayout, USOverlayKind
from gridforge.sources.base import OverlaySource

logger = logging.getLogger(__name__)

USER_AGENT: Final[str] = "gridforge/0.2 (+https://github.com/gridforge/gridforge)"

DEFAULT_FEMA_ENDPOINTS: Final[tuple[str, ...]] = (
    "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28/query",
    "https://services.arcgis.com/P3ePLMYs2RVChkJx/arcgis/rest/services/"
    "USA_Flood_Hazard_Reduced_Set_gdb/FeatureServer/0/query",
)
DEFAULT_NHD_ENDPOINT: Final[str] = (
    "https://hydro.nationalmap.gov/arcgis/rest/services/nhd/MapServer/6/query"
)
DEFAULT_FAA_ENDPOINT: Final[str] = (
    "https://services6.arcgis.com/ssFJjBXIUyZDrSYZ/arcgis/rest/services/"
    "Digital_Obstacle_File/FeatureServer/0/query"
)
DEFAULT_ENC_BASE_URL: Final[str] = (
    "https://encdirect.noaa.gov/arcgis/rest/services/encdirect/enc_harbour/MapServer"
)
DEFAULT_ENC_DEPTH_LAYER_ID: Final[int] = 104
DEFAULT_ENC_NAVAID_LAYERS: Final[tuple[tuple[int, str], ...]] = (
    (1, "beacon"),
    (6, "buoy"),
    (11, "light"),
)
DEFAULT_USFS_TRAILS_ENDPOINT: Final[str] = (
    "https://apps.fs.usda.gov/arcx/rest/services/EDW/EDW_TrailNFSPublish_01/MapServer/0/query"
)
DEFAULT_USFS_REC_ENDPOINT: Final[str] = (
    "https://apps.fs.usda.gov/arcx/rest/services/EDW/EDW_RecreationOpportunities_01/MapServer/0/query"
)
DEFAULT_NPS_TRAILS_ENDPOINT: Final[str] = (
    "https://mapservices.nps.gov/arcgis/rest/services/NationalDatasets/NPS_Public_Trails/FeatureServer/0/query"
)
DEFAULT_NPS_POIS_ENDPOINT: Final[str] = (
    "https://mapservices.nps.gov/arcgis/rest/services/NationalDatasets/NPS_Public_POIs_Geographic/FeatureServer/0/query"
)
DEFAULT_PADUS_FEE_ENDPOINT: Final[str] = (
    "https://services.arcgis.com/v01gqwM5QqNysAAi/arcgis/rest/services/"
    "Federal_Fee_Managers_Authoritative_PADUS/FeatureServer/0/query"
)
DEFAULT_USFS_WILDERNESS_ENDPOINT: Final[str] = (
    "https://apps.fs.usda.gov/arcx/rest/services/EDW/EDW_Wilderness_01/MapServer/0/query"
)
DEFAULT_PADUS_ACCESS_ENDPOINT: Final[str] = (
    "https://services.arcgis.com/v01gqwM5QqNysAAi/arcgis/rest/services/"
    "PADUS_Public_Access/FeatureServer/0/query"
)


def _round_enclosing_bbox(bbox: BBox, step_deg: float = 0.001) -> BBox:
    inv = 1.0 / step_deg
    return BBox(
        min_x=math.floor(bbox.min_x * inv) / inv,
        min_y=math.floor(bbox.min_y * inv) / inv,
        max_x=math.ceil(bbox.max_x * inv) / inv,
        max_y=math.ceil(bbox.max_y * inv) / inv,
    )


def _bbox_hash(bbox: BBox) -> str:
    payload = f"{bbox.min_x:.3f},{bbox.min_y:.3f},{bbox.max_x:.3f},{bbox.max_y:.3f}"
    return hashlib.sha256(payload.encode("ascii")).hexdigest()[:24]


def _prop_ci(props: Mapping[str, Any], *keys: str) -> Any:
    """Case-insensitive lookup returning the first non-empty matching property value."""
    lower_map = {str(k).lower(): v for k, v in props.items()}
    for key in keys:
        val = lower_map.get(key.lower())
        if val is not None and str(val).strip() != "":
            return val
    return None


def _to_float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


def _to_int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def classify_nhd_fcode(fcode: int | None) -> str:
    """Map USGS NHD flowline ``fcode`` to canonical ``flow_type`` category."""
    if fcode == 46003:
        return "intermittent"
    if fcode == 46006:
        return "perennial"
    if fcode == 46007:
        return "ephemeral"
    return "canal"


def _esri_feature_to_geojson(feat: dict[str, Any]) -> dict[str, Any] | None:
    """Convert an EsriJSON feature dict to GeoJSON if an endpoint fell back from ``f=geojson``."""
    if feat.get("type") == "Feature" and "geometry" in feat:
        return feat
    geom = feat.get("geometry")
    if not isinstance(geom, dict):
        return None
    props = feat.get("attributes") or feat.get("properties") or {}
    if "x" in geom and "y" in geom:
        gj_geom: dict[str, Any] = {"type": "Point", "coordinates": [geom["x"], geom["y"]]}
    elif "paths" in geom:
        paths = geom["paths"]
        gj_geom = (
            {"type": "LineString", "coordinates": paths[0]}
            if len(paths) == 1
            else {"type": "MultiLineString", "coordinates": paths}
        )
    elif "rings" in geom:
        gj_geom = {"type": "Polygon", "coordinates": geom["rings"]}
    else:
        return None
    return {"type": "Feature", "geometry": gj_geom, "properties": props}


def _query_arcgis_geojson(
    endpoints: Sequence[str],
    bbox: BBox,
    *,
    cache_path: Path,
    where: str = "1=1",
    out_fields: str = "*",
    extra_params: Mapping[str, str] | None = None,
    timeout_s: float = 25.0,
    page_size: int = 1000,
    max_pages: int = 5,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    """Query an ArcGIS REST MapServer/FeatureServer layer by WGS84 bbox with disk caching."""
    if cache_path.is_file() and cache_path.stat().st_size > 0:
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(cached, list):
                return cached
        except json.JSONDecodeError:
            logger.warning("Ignoring corrupt cached US overlay JSON: %s", cache_path)

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
        for url in endpoints:
            if not url:
                continue
            for fmt in ("geojson", "json"):
                collected: list[dict[str, Any]] = []
                offset = 0
                succeeded = True
                for _page in range(max_pages):
                    params = {
                        "where": where,
                        "geometry": f"{bbox.min_x:.6f},{bbox.min_y:.6f},{bbox.max_x:.6f},{bbox.max_y:.6f}",
                        "geometryType": "esriGeometryEnvelope",
                        "inSR": "4326",
                        "spatialRel": "esriSpatialRelIntersects",
                        "outFields": out_fields,
                        "outSR": "4326",
                        "f": fmt,
                        "resultRecordCount": str(page_size),
                        "resultOffset": str(offset),
                    }
                    if extra_params:
                        params.update(extra_params)
                    try:
                        resp = http.get(url, params=params, headers={"User-Agent": USER_AGENT})
                        resp.raise_for_status()
                        data = resp.json()
                    except Exception as exc:
                        last_error = f"{url} (f={fmt}): {exc}"
                        succeeded = False
                        break

                    if not isinstance(data, dict) or "error" in data:
                        last_error = f"{url} (f={fmt}): ArcGIS error {data.get('error') if isinstance(data, dict) else type(data).__name__}"
                        succeeded = False
                        break

                    raw_features = data.get("features") or []
                    for feat in raw_features:
                        if isinstance(feat, dict):
                            gj = _esri_feature_to_geojson(feat)
                            if gj is not None and gj.get("geometry") is not None:
                                collected.append(gj)

                    exceeded = bool(
                        data.get("exceededTransferLimit")
                        or (data.get("properties") or {}).get("exceededTransferLimit")
                    )
                    if not exceeded or not raw_features:
                        break
                    offset += len(raw_features)

                if succeeded:
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    tmp = cache_path.with_suffix(".json.tmp")
                    tmp.write_text(json.dumps(collected), encoding="utf-8")
                    tmp.replace(cache_path)
                    return collected
    finally:
        if owns_client:
            http.close()

    if last_error:
        logger.warning("US ArcGIS overlay query unavailable (%s); skipping overlay cleanly", last_error)
    return []


def _clip_and_reproject_local_gpkg(
    local_gpkg: Path,
    layout: SheetLayout,
    out_gpkg: Path,
    expected_layers: Sequence[str],
) -> Path | None:
    """Reproject & clip an air-gapped local overlay GeoPackage to ``EPSG:{layout.utm_epsg}``."""
    from osgeo import ogr  # type: ignore[import-untyped]

    src_path = local_gpkg.expanduser().resolve()
    if not src_path.is_file():
        raise FileNotFoundError(f"Local US overlay GeoPackage not found: {src_path}")

    out_gpkg.parent.mkdir(parents=True, exist_ok=True)
    if out_gpkg.exists():
        out_gpkg.unlink()

    b = layout.utm_fetch_bbox
    cmd = [
        "ogr2ogr",
        "-f",
        "GPKG",
        "-t_srs",
        f"EPSG:{layout.utm_epsg}",
        "-clipdst",
        f"{b.min_x}",
        f"{b.min_y}",
        f"{b.max_x}",
        f"{b.max_y}",
        "-nlt",
        "PROMOTE_TO_MULTI",
        "-overwrite",
        str(out_gpkg),
        str(src_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"ogr2ogr failed for local US overlay {src_path} (exit {proc.returncode}): {proc.stderr.strip()}"
        )

    ogr.UseExceptions()
    ds = ogr.Open(str(out_gpkg))
    if ds is None:
        return None
    try:
        total = 0
        available = {ds.GetLayerByIndex(i).GetName() for i in range(ds.GetLayerCount())}
        target_layers = [ln for ln in expected_layers if ln in available] or list(available)
        for ln in target_layers:
            lyr = ds.GetLayerByName(ln)
            if lyr is not None:
                total += lyr.GetFeatureCount()
        if total <= 0:
            return None
        return out_gpkg
    finally:
        ds = None


def _write_projected_gpkg_layers(
    out_gpkg: Path,
    utm_epsg: int,
    clip_bbox: BBox,
    layer_specs: Sequence[tuple[str, int, Sequence[tuple[str, int]], list[tuple[dict[str, Any], dict[str, Any]]]]],
) -> Path | None:
    """Project GeoJSON features from EPSG:4326 -> ``EPSG:{utm_epsg}``, clip, and write a GeoPackage."""
    from osgeo import ogr, osr  # type: ignore[import-untyped]

    ogr.UseExceptions()
    src_srs = osr.SpatialReference()
    src_srs.ImportFromEPSG(4326)
    src_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

    dst_srs = osr.SpatialReference()
    dst_srs.ImportFromEPSG(utm_epsg)
    dst_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

    transform = osr.CoordinateTransformation(src_srs, dst_srs)

    clip_ring = ogr.Geometry(ogr.wkbLinearRing)
    clip_ring.AddPoint(clip_bbox.min_x, clip_bbox.min_y)
    clip_ring.AddPoint(clip_bbox.max_x, clip_bbox.min_y)
    clip_ring.AddPoint(clip_bbox.max_x, clip_bbox.max_y)
    clip_ring.AddPoint(clip_bbox.min_x, clip_bbox.max_y)
    clip_ring.AddPoint(clip_bbox.min_x, clip_bbox.min_y)
    clip_poly = ogr.Geometry(ogr.wkbPolygon)
    clip_poly.AddGeometry(clip_ring)

    out_gpkg.parent.mkdir(parents=True, exist_ok=True)
    if out_gpkg.exists():
        out_gpkg.unlink()

    driver = ogr.GetDriverByName("GPKG")
    ds = driver.CreateDataSource(str(out_gpkg))
    if ds is None:
        raise RuntimeError(f"Failed to create overlay GeoPackage at {out_gpkg}")

    total_written = 0
    try:
        for layer_name, geom_type, fields, rows in layer_specs:
            layer = ds.CreateLayer(layer_name, srs=dst_srs, geom_type=geom_type)
            for fname, ftype in fields:
                layer.CreateField(ogr.FieldDefn(fname, ftype))
            defn = layer.GetLayerDefn()

            for gj_geom, attrs in rows:
                geom = ogr.CreateGeometryFromJson(json.dumps(gj_geom))
                if geom is None or geom.IsEmpty():
                    continue
                geom.FlattenTo2D()
                if geom.Transform(transform) != 0:
                    continue
                if geom_type in (ogr.wkbPolygon, ogr.wkbMultiPolygon):
                    try:
                        if not geom.IsValid():
                            repaired = geom.MakeValid()
                            if repaired is not None and not repaired.IsEmpty():
                                geom = repaired
                    except RuntimeError:
                        try:
                            repaired = geom.Buffer(0.0)
                            if repaired is not None and not repaired.IsEmpty():
                                geom = repaired
                        except RuntimeError:
                            continue
                try:
                    if not geom.Intersects(clip_poly):
                        continue
                    if geom_type != ogr.wkbPoint:
                        clipped = geom.Intersection(clip_poly)
                        if clipped is None or clipped.IsEmpty():
                            continue
                        geom = clipped
                except RuntimeError:
                    repaired = None
                    try:
                        repaired = geom.MakeValid()
                    except RuntimeError:
                        pass
                    if repaired is None or repaired.IsEmpty():
                        try:
                            repaired = geom.Buffer(0.0)
                        except RuntimeError:
                            continue
                    if repaired is None or repaired.IsEmpty():
                        continue
                    try:
                        if not repaired.Intersects(clip_poly):
                            continue
                        geom = repaired.Intersection(clip_poly) if geom_type != ogr.wkbPoint else repaired
                    except RuntimeError:
                        continue
                    if geom is None or geom.IsEmpty():
                        continue

                geom = _coerce_geometry_type(geom, geom_type)
                if geom is None or geom.IsEmpty():
                    continue
                geom.FlattenTo2D()

                feat = ogr.Feature(defn)
                feat.SetGeometry(geom)
                for fname, _ftype in fields:
                    val = attrs.get(fname)
                    if val is not None:
                        feat.SetField(fname, val)
                layer.CreateFeature(feat)
                feat = None
                total_written += 1
    finally:
        ds = None

    if total_written == 0:
        out_gpkg.unlink(missing_ok=True)
        return None
    return out_gpkg


def _coerce_geometry_type(geom: Any, target_type: int) -> Any:
    """Coerce clipped geometries (including GeometryCollections) to match strict GPKG layer type."""
    from osgeo import ogr  # type: ignore[import-untyped]

    gtype = ogr.GT_Flatten(geom.GetGeometryType())

    if target_type == ogr.wkbPoint:
        if gtype == ogr.wkbPoint:
            return geom
        if gtype == ogr.wkbMultiPoint and geom.GetGeometryCount() > 0:
            return geom.GetGeometryRef(0).Clone()
        return geom.Centroid() if not geom.IsEmpty() else None

    if target_type == ogr.wkbMultiLineString:
        if gtype == ogr.wkbMultiLineString:
            return geom
        if gtype == ogr.wkbLineString:
            return ogr.ForceToMultiLineString(geom)
        if gtype == ogr.wkbGeometryCollection:
            multi = ogr.Geometry(ogr.wkbMultiLineString)
            for idx in range(geom.GetGeometryCount()):
                sub = geom.GetGeometryRef(idx)
                st = ogr.GT_Flatten(sub.GetGeometryType())
                if st == ogr.wkbLineString:
                    multi.AddGeometry(sub)
                elif st == ogr.wkbMultiLineString:
                    for j in range(sub.GetGeometryCount()):
                        multi.AddGeometry(sub.GetGeometryRef(j))
            return multi if multi.GetGeometryCount() > 0 else None
        return None

    if target_type == ogr.wkbMultiPolygon:
        if gtype == ogr.wkbMultiPolygon:
            return geom
        if gtype == ogr.wkbPolygon:
            return ogr.ForceToMultiPolygon(geom)
        if gtype == ogr.wkbGeometryCollection:
            multi = ogr.Geometry(ogr.wkbMultiPolygon)
            for idx in range(geom.GetGeometryCount()):
                sub = geom.GetGeometryRef(idx)
                st = ogr.GT_Flatten(sub.GetGeometryType())
                if st == ogr.wkbPolygon:
                    multi.AddGeometry(sub)
                elif st == ogr.wkbMultiPolygon:
                    for j in range(sub.GetGeometryCount()):
                        multi.AddGeometry(sub.GetGeometryRef(j))
            return multi if multi.GetGeometryCount() > 0 else None
        return None

    return geom


class _BaseUSOverlaySource:
    """Shared cache/timeout/air-gapped override scaffolding for US public-domain overlays."""

    name: str = "us-overlay"
    attribution: str = ""
    overlay_key: str = "overlay"

    def __init__(
        self,
        *,
        cache_dir: Path | str | None = None,
        local_gpkg: Path | str | None = None,
        timeout_s: float = 25.0,
        client: httpx.Client | None = None,
    ) -> None:
        base_cache = Path(
            cache_dir
            if cache_dir is not None
            else os.environ.get("GRIDFORGE_CACHE_DIR", "/tmp/gridforge-cache")
        )
        self.cache_dir: Path = base_cache / "us_overlays"
        self.local_gpkg: Path | None = Path(local_gpkg) if local_gpkg is not None else None
        self.timeout_s = timeout_s
        self._injected_client = client


class FEMAFloodSource(_BaseUSOverlaySource):
    """FEMA National Flood Hazard Layer (NFHL ``S_Fld_Haz_Ar``) overlay source."""

    name: str = "us-fema-nfhl"
    overlay_key: str = "fema"
    attribution: str = "Flood zones: FEMA National Flood Hazard Layer (Public Domain)"

    def __init__(
        self,
        *,
        endpoints: Sequence[str] | None = None,
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
        if endpoints is not None:
            self.endpoints = list(endpoints)
        else:
            env_url = os.environ.get("GRIDFORGE_FEMA_URL", "").strip()
            raw = [env_url, *DEFAULT_FEMA_ENDPOINTS] if env_url else list(DEFAULT_FEMA_ENDPOINTS)
            self.endpoints = list(dict.fromkeys(u for u in raw if u))

    def fetch_overlay(self, layout: SheetLayout, work_dir: Path) -> Path | None:
        work_dir.mkdir(parents=True, exist_ok=True)
        out_gpkg = work_dir / "fema.gpkg"
        if self.local_gpkg is not None:
            return _clip_and_reproject_local_gpkg(self.local_gpkg, layout, out_gpkg, ("fema_flood",))

        query_bbox = _round_enclosing_bbox(layout.wgs84_fetch_bbox)
        cache_path = self.cache_dir / f"fema_{_bbox_hash(query_bbox)}.json"
        features = _query_arcgis_geojson(
            self.endpoints,
            query_bbox,
            cache_path=cache_path,
            out_fields="FLD_ZONE,ZONE_SUBTY,SFHA_TF",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        if not features:
            return None

        from osgeo import ogr  # type: ignore[import-untyped]

        rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for feat in features:
            props = feat.get("properties") or {}
            fld_zone = str(_prop_ci(props, "FLD_ZONE", "fld_zone") or "")
            zone_subty = str(_prop_ci(props, "ZONE_SUBTY", "zone_subty") or "")
            sfha_raw = _prop_ci(props, "SFHA_TF", "sfha_tf")
            if sfha_raw is not None:
                sfha_tf = str(sfha_raw).strip().upper()[:1]
            else:
                sfha_tf = "T" if fld_zone.upper().startswith(("A", "V")) else "F"
            rows.append(
                (
                    feat["geometry"],
                    {"fld_zone": fld_zone, "zone_subty": zone_subty, "sfha_tf": sfha_tf},
                )
            )

        fields = (
            ("fld_zone", ogr.OFTString),
            ("zone_subty", ogr.OFTString),
            ("sfha_tf", ogr.OFTString),
        )
        return _write_projected_gpkg_layers(
            out_gpkg,
            layout.utm_epsg,
            layout.utm_fetch_bbox,
            [("fema_flood", ogr.wkbMultiPolygon, fields, rows)],
        )


class NHDHydroSource(_BaseUSOverlaySource):
    """USGS National Hydrography Dataset (NHD Flowlines) overlay source."""

    name: str = "us-usgs-nhd"
    overlay_key: str = "nhd"
    attribution: str = "Hydrography: USGS National Hydrography Dataset (Public Domain)"

    def __init__(
        self,
        *,
        endpoint: str | None = None,
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
        self.endpoint = endpoint or os.environ.get("GRIDFORGE_NHD_URL", DEFAULT_NHD_ENDPOINT)

    def fetch_overlay(self, layout: SheetLayout, work_dir: Path) -> Path | None:
        work_dir.mkdir(parents=True, exist_ok=True)
        out_gpkg = work_dir / "nhd.gpkg"
        if self.local_gpkg is not None:
            return _clip_and_reproject_local_gpkg(self.local_gpkg, layout, out_gpkg, ("nhd_flowlines",))

        query_bbox = _round_enclosing_bbox(layout.wgs84_fetch_bbox)
        cache_path = self.cache_dir / f"nhd_{_bbox_hash(query_bbox)}.json"
        features = _query_arcgis_geojson(
            [self.endpoint],
            query_bbox,
            cache_path=cache_path,
            where="fcode <> 55800",
            out_fields="gnis_name,fcode,ftype",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        if not features:
            return None

        from osgeo import ogr  # type: ignore[import-untyped]

        rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for feat in features:
            props = feat.get("properties") or {}
            gnis_name = _prop_ci(props, "gnis_name", "GNIS_NAME", "name")
            fcode = _to_int_or_none(_prop_ci(props, "fcode", "FCODE"))
            flow_type = classify_nhd_fcode(fcode)
            rows.append(
                (
                    feat["geometry"],
                    {
                        "gnis_name": str(gnis_name) if gnis_name is not None else "",
                        "fcode": fcode,
                        "flow_type": flow_type,
                    },
                )
            )

        fields = (
            ("gnis_name", ogr.OFTString),
            ("fcode", ogr.OFTInteger),
            ("flow_type", ogr.OFTString),
        )
        return _write_projected_gpkg_layers(
            out_gpkg,
            layout.utm_epsg,
            layout.utm_fetch_bbox,
            [("nhd_flowlines", ogr.wkbMultiLineString, fields, rows)],
        )


class FAATowersSource(_BaseUSOverlaySource):
    """FAA Digital Obstacle File (DOF) vertical obstacle towers overlay source."""

    name: str = "us-faa-towers"
    overlay_key: str = "towers"
    attribution: str = "Obstacle towers: FAA Digital Obstacle File (Public Domain)"

    def __init__(
        self,
        *,
        endpoint: str | None = None,
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
        self.endpoint = endpoint or os.environ.get("GRIDFORGE_FAA_URL", DEFAULT_FAA_ENDPOINT)

    def fetch_overlay(self, layout: SheetLayout, work_dir: Path) -> Path | None:
        work_dir.mkdir(parents=True, exist_ok=True)
        out_gpkg = work_dir / "towers.gpkg"
        if self.local_gpkg is not None:
            return _clip_and_reproject_local_gpkg(self.local_gpkg, layout, out_gpkg, ("us_towers",))

        query_bbox = _round_enclosing_bbox(layout.wgs84_fetch_bbox)
        cache_path = self.cache_dir / f"towers_{_bbox_hash(query_bbox)}.json"
        features = _query_arcgis_geojson(
            [self.endpoint],
            query_bbox,
            cache_path=cache_path,
            out_fields="OAS_Number,Type_Code,AGL,AMSL,Lighting,City,State",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        if not features:
            return None

        from osgeo import ogr  # type: ignore[import-untyped]

        rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for feat in features:
            props = feat.get("properties") or {}
            obstacle_type = str(
                _prop_ci(props, "Type_Code", "obstacle_type", "TYPE_CODE", "structure_type") or "TOWER"
            ).strip()
            oas_num = _prop_ci(props, "name", "OAS_Number", "oas_number", "ASR_Number")
            name = str(oas_num).strip() if oas_num is not None else obstacle_type
            agl_ft = _to_float_or_none(_prop_ci(props, "AGL", "agl_ft", "height_agl"))
            amsl_ft = _to_float_or_none(_prop_ci(props, "AMSL", "amsl_ft", "height_amsl"))
            lighting = str(_prop_ci(props, "Lighting", "lighting") or "").strip()
            source_tag = str(_prop_ci(props, "source") or "FAA-DOF").strip()
            rows.append(
                (
                    feat["geometry"],
                    {
                        "source": source_tag,
                        "name": name,
                        "obstacle_type": obstacle_type,
                        "agl_ft": agl_ft,
                        "amsl_ft": amsl_ft,
                        "lighting": lighting,
                    },
                )
            )

        fields = (
            ("source", ogr.OFTString),
            ("name", ogr.OFTString),
            ("obstacle_type", ogr.OFTString),
            ("agl_ft", ogr.OFTReal),
            ("amsl_ft", ogr.OFTReal),
            ("lighting", ogr.OFTString),
        )
        return _write_projected_gpkg_layers(
            out_gpkg,
            layout.utm_epsg,
            layout.utm_fetch_bbox,
            [("us_towers", ogr.wkbPoint, fields, rows)],
        )


class NOAAENCSource(_BaseUSOverlaySource):
    """NOAA ENC Direct to GIS nautical depth contours and aids to navigation overlay source."""

    name: str = "us-noaa-enc"
    overlay_key: str = "enc"
    attribution: str = "Nautical overlays: NOAA ENC (Public Domain)"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        depth_layer_id: int = DEFAULT_ENC_DEPTH_LAYER_ID,
        navaid_layers: Sequence[tuple[int, str]] = DEFAULT_ENC_NAVAID_LAYERS,
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
        self.base_url = (base_url or os.environ.get("GRIDFORGE_ENC_URL", DEFAULT_ENC_BASE_URL)).rstrip("/")
        self.depth_layer_id = depth_layer_id
        self.navaid_layers = list(navaid_layers)

    def fetch_overlay(self, layout: SheetLayout, work_dir: Path) -> Path | None:
        work_dir.mkdir(parents=True, exist_ok=True)
        out_gpkg = work_dir / "enc.gpkg"
        if self.local_gpkg is not None:
            return _clip_and_reproject_local_gpkg(
                self.local_gpkg,
                layout,
                out_gpkg,
                ("enc_depth_contours", "enc_navaids"),
            )

        query_bbox = _round_enclosing_bbox(layout.wgs84_fetch_bbox)
        bhash = _bbox_hash(query_bbox)

        depth_url = f"{self.base_url}/{self.depth_layer_id}/query"
        depth_features = _query_arcgis_geojson(
            [depth_url],
            query_bbox,
            cache_path=self.cache_dir / f"enc_depth_{bhash}.json",
            out_fields="VALDCO",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )

        navaid_rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for layer_id, navaid_kind in self.navaid_layers:
            nav_url = f"{self.base_url}/{layer_id}/query"
            feats = _query_arcgis_geojson(
                [nav_url],
                query_bbox,
                cache_path=self.cache_dir / f"enc_nav_{layer_id}_{bhash}.json",
                out_fields="OBJNAM,COLOUR",
                timeout_s=self.timeout_s,
                client=self._injected_client,
            )
            for feat in feats:
                props = feat.get("properties") or {}
                objnam = str(_prop_ci(props, "OBJNAM", "objnam", "name") or "").strip()
                colour = str(_prop_ci(props, "COLOUR", "colour", "color") or "").strip()
                ntype = str(_prop_ci(props, "navaid_type") or navaid_kind).strip()
                navaid_rows.append(
                    (
                        feat["geometry"],
                        {"objnam": objnam, "navaid_type": ntype, "colour": colour},
                    )
                )

        depth_rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for feat in depth_features:
            props = feat.get("properties") or {}
            valdco = _to_float_or_none(_prop_ci(props, "VALDCO", "valdco"))
            depth_rows.append((feat["geometry"], {"valdco": valdco}))

        if not depth_rows and not navaid_rows:
            return None

        from osgeo import ogr  # type: ignore[import-untyped]

        layer_specs = [
            (
                "enc_depth_contours",
                ogr.wkbMultiLineString,
                (("valdco", ogr.OFTReal),),
                depth_rows,
            ),
            (
                "enc_navaids",
                ogr.wkbPoint,
                (
                    ("objnam", ogr.OFTString),
                    ("navaid_type", ogr.OFTString),
                    ("colour", ogr.OFTString),
                ),
                navaid_rows,
            ),
        ]
        return _write_projected_gpkg_layers(
            out_gpkg,
            layout.utm_epsg,
            layout.utm_fetch_bbox,
            layer_specs,
        )


def _classify_us_trail_class(raw_type: str, raw_use: str = "", raw_class: str = "") -> str:
    text = f"{raw_type} {raw_use} {raw_class}".lower()
    if any(k in text for k in ("4wd", "ohv", "motor", "atv", "jeep")):
        return "4wd_track"
    if any(k in text for k in ("bicycle", "bike", "mtb", "cycling")) and "hiker" not in text:
        return "mtb_track"
    if any(k in text for k in ("horse", "pack", "saddle", "equestrian")) and "hiker" not in text:
        return "bridle_track"
    return "walking_track"


def _classify_us_poi_type(raw_type: str, raw_name: str = "") -> str:
    text = f"{raw_type} {raw_name}".lower()
    if any(k in text for k in ("hut", "cabin", "shelter", "lookout tower", "lodge")):
        return "hut"
    if any(k in text for k in ("camp", "rv ", "tent", "backcountry site")):
        return "campground"
    if any(k in text for k in ("picnic", "day use", "day-use")):
        return "picnic"
    if any(k in text for k in ("water", "spring", "fountain", "well")):
        return "water"
    if any(k in text for k in ("overlook", "lookout", "view", "vista", "summit", "peak")):
        return "lookout"
    if any(k in text for k in ("trailhead", "parking", "visitor", "ranger", "information", "recreation area")):
        return "trailhead"
    return "lookout"


def _normalize_padus_manager(raw_mgr: str) -> str:
    m = raw_mgr.strip()
    low = m.lower()
    if low in ("usfs", "fs") or "forest service" in low:
        return "USFS"
    if low == "nps" or "national park" in low:
        return "NPS"
    if low == "blm" or "land management" in low:
        return "BLM"
    if low in ("fws", "usfws") or "fish and wildlife" in low:
        return "FWS"
    if low == "dod" or "defense" in low:
        return "DOD"
    if "state" in low:
        return "State"
    return m or "Federal"


def _normalize_padus_designation(raw_des: str, raw_name: str = "") -> str:
    text = f"{raw_des} {raw_name}".lower()
    if "wilderness" in text:
        return "Wilderness Area"
    if "national park" in text or raw_des.strip().upper() == "NP":
        return "National Park"
    if "national forest" in text or raw_des.strip().upper() == "NF":
        return "National Forest"
    if "national monument" in text or raw_des.strip().upper() == "NM":
        return "National Monument"
    if "wildlife refuge" in text or raw_des.strip().upper() == "NWR":
        return "Wildlife Refuge"
    if "blm" in text or "land management" in text:
        return "BLM Public Land"
    if "state park" in text:
        return "State Park"
    return raw_des.strip() or "Public Land"


class USTrailsSource(_BaseUSOverlaySource):
    """USFS National Forest & NPS Public Trails and Recreation POIs (Public Domain).

    Writes the unified 2-layer UTM GeoPackage schema (``au_state_trails`` + ``au_state_pois``,
    ``state='US'``) shared with Australian, New Zealand, and Canadian trail overlays.
    """

    name: str = "us-trails"
    overlay_key: str = "us-trails"
    attribution: str = (
        "US Trails & Recreation: U.S. Forest Service (USFS) & National Park Service (NPS) (Public Domain)"
    )

    def __init__(
        self,
        *,
        usfs_trails_url: str | None = None,
        usfs_rec_url: str | None = None,
        nps_trails_url: str | None = None,
        nps_pois_url: str | None = None,
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
        self.usfs_trails_url = usfs_trails_url or os.environ.get(
            "GRIDFORGE_USFS_TRAILS_URL", DEFAULT_USFS_TRAILS_ENDPOINT
        )
        self.usfs_rec_url = usfs_rec_url or os.environ.get(
            "GRIDFORGE_USFS_REC_URL", DEFAULT_USFS_REC_ENDPOINT
        )
        self.nps_trails_url = nps_trails_url or os.environ.get(
            "GRIDFORGE_NPS_TRAILS_URL", DEFAULT_NPS_TRAILS_ENDPOINT
        )
        self.nps_pois_url = nps_pois_url or os.environ.get(
            "GRIDFORGE_NPS_POIS_URL", DEFAULT_NPS_POIS_ENDPOINT
        )

    def fetch_overlay(self, layout: SheetLayout, work_dir: Path) -> Path | None:
        from gridforge.sources.dem_usgs3dep import is_in_us_3dep_coverage
        from gridforge.sources.overlays_au_states import clean_feature_name, write_state_trails_gpkg

        work_dir.mkdir(parents=True, exist_ok=True)
        out_gpkg = work_dir / "us_trails.gpkg"
        if self.local_gpkg is not None:
            return _clip_and_reproject_local_gpkg(
                self.local_gpkg,
                layout,
                out_gpkg,
                ("au_state_trails", "au_state_pois"),
            )

        if not is_in_us_3dep_coverage(layout.wgs84_fetch_bbox):
            logger.debug("Sheet bbox outside US coverage; skipping us-trails")
            return None

        query_bbox = _round_enclosing_bbox(layout.wgs84_fetch_bbox)
        bhash = _bbox_hash(query_bbox)

        usfs_trails = _query_arcgis_geojson(
            [self.usfs_trails_url],
            query_bbox,
            cache_path=self.cache_dir / f"usfs_trails_{bhash}.json",
            out_fields="trail_name,trail_type,trail_class,national_trail_designation",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        nps_trails = _query_arcgis_geojson(
            [self.nps_trails_url],
            query_bbox,
            cache_path=self.cache_dir / f"nps_trails_{bhash}.json",
            out_fields="TRLNAME,MAPLABEL,TRLSURFACE,TRLCLASS,TRLUSE,TRLTYPE",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        usfs_rec = _query_arcgis_geojson(
            [self.usfs_rec_url],
            query_bbox,
            cache_path=self.cache_dir / f"usfs_rec_{bhash}.json",
            out_fields="recareaname,markeractivity,markeractivitygroup,forestname",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        nps_pois = _query_arcgis_geojson(
            [self.nps_pois_url],
            query_bbox,
            cache_path=self.cache_dir / f"nps_pois_{bhash}.json",
            out_fields="POINAME,MAPLABEL,POITYPE,UNITNAME",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )

        trail_rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for feat in usfs_trails:
            props = feat.get("properties") or {}
            name = clean_feature_name(_prop_ci(props, "trail_name", "name"))
            if name and name.isupper() and len(name) > 3:
                name = name.title()
            tclass = _classify_us_trail_class(
                str(_prop_ci(props, "trail_type") or ""),
                raw_class=str(_prop_ci(props, "trail_class") or ""),
            )
            trail_rows.append(
                (
                    feat["geometry"],
                    {"name": name, "trail_class": tclass, "surface": "", "state": "US"},
                )
            )

        for feat in nps_trails:
            props = feat.get("properties") or {}
            name = clean_feature_name(_prop_ci(props, "MAPLABEL", "TRLNAME", "name"))
            tclass = _classify_us_trail_class(
                str(_prop_ci(props, "TRLTYPE") or ""),
                raw_use=str(_prop_ci(props, "TRLUSE") or ""),
                raw_class=str(_prop_ci(props, "TRLCLASS") or ""),
            )
            surface = str(_prop_ci(props, "TRLSURFACE") or "").strip()
            trail_rows.append(
                (
                    feat["geometry"],
                    {"name": name, "trail_class": tclass, "surface": surface, "state": "US"},
                )
            )

        poi_rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
        seen_pois: set[str] = set()
        for feat in usfs_rec:
            props = feat.get("properties") or {}
            name = clean_feature_name(_prop_ci(props, "recareaname", "name"))
            if not name or name.lower() in seen_pois:
                continue
            seen_pois.add(name.lower())
            hint = f"{_prop_ci(props, 'markeractivity') or ''} {_prop_ci(props, 'markeractivitygroup') or ''}"
            poi_rows.append(
                (
                    feat["geometry"],
                    {
                        "name": name,
                        "poi_type": _classify_us_poi_type(hint, name),
                        "elev_m": None,
                        "state": "US",
                    },
                )
            )

        for feat in nps_pois:
            props = feat.get("properties") or {}
            name = clean_feature_name(_prop_ci(props, "MAPLABEL", "POINAME", "name"))
            if not name or name.lower() in seen_pois:
                continue
            seen_pois.add(name.lower())
            ptype = str(_prop_ci(props, "POITYPE") or "")
            poi_rows.append(
                (
                    feat["geometry"],
                    {
                        "name": name,
                        "poi_type": _classify_us_poi_type(ptype, name),
                        "elev_m": None,
                        "state": "US",
                    },
                )
            )

        return write_state_trails_gpkg(out_gpkg, layout, trail_rows, poi_rows)


class USPublicLandsSource(_BaseUSOverlaySource):
    """USGS PAD-US & USFS Wilderness public land polygons (Public Domain).

    Writes a single UTM polygon layer ``public_lands`` (``name``, ``manager``, ``designation``,
    ``state='US'``) into ``work_dir / "us_public_lands.gpkg"``.
    """

    name: str = "us-public-lands"
    overlay_key: str = "us-public-lands"
    attribution: str = "US Public Lands & Wilderness: USGS PAD-US & U.S. Forest Service (Public Domain)"

    def __init__(
        self,
        *,
        padus_fee_url: str | None = None,
        usfs_wilderness_url: str | None = None,
        padus_access_url: str | None = None,
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
        self.padus_fee_url = padus_fee_url or os.environ.get(
            "GRIDFORGE_PADUS_FEE_URL", DEFAULT_PADUS_FEE_ENDPOINT
        )
        self.usfs_wilderness_url = usfs_wilderness_url or os.environ.get(
            "GRIDFORGE_USFS_WILDERNESS_URL", DEFAULT_USFS_WILDERNESS_ENDPOINT
        )
        self.padus_access_url = padus_access_url or os.environ.get(
            "GRIDFORGE_PADUS_ACCESS_URL", DEFAULT_PADUS_ACCESS_ENDPOINT
        )

    def fetch_overlay(self, layout: SheetLayout, work_dir: Path) -> Path | None:
        from gridforge.sources.dem_usgs3dep import is_in_us_3dep_coverage
        from gridforge.sources.overlays_au_states import clean_feature_name

        work_dir.mkdir(parents=True, exist_ok=True)
        out_gpkg = work_dir / "us_public_lands.gpkg"
        if self.local_gpkg is not None:
            return _clip_and_reproject_local_gpkg(
                self.local_gpkg,
                layout,
                out_gpkg,
                ("public_lands",),
            )

        if not is_in_us_3dep_coverage(layout.wgs84_fetch_bbox):
            logger.debug("Sheet bbox outside US coverage; skipping us-public-lands")
            return None

        query_bbox = _round_enclosing_bbox(layout.wgs84_fetch_bbox)
        bhash = _bbox_hash(query_bbox)

        fee_feats = _query_arcgis_geojson(
            [self.padus_fee_url],
            query_bbox,
            cache_path=self.cache_dir / f"us_padus_fee_{bhash}.json",
            out_fields="Unit_Nm,Mang_Name,Loc_Ds,Des_Tp,Category",
            extra_params={"maxAllowableOffset": "0.0004"},
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        wilderness_feats = _query_arcgis_geojson(
            [self.usfs_wilderness_url],
            query_bbox,
            cache_path=self.cache_dir / f"us_usfs_wilderness_{bhash}.json",
            out_fields="wildernessname",
            extra_params={"maxAllowableOffset": "0.0004"},
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        access_feats = _query_arcgis_geojson(
            [self.padus_access_url],
            query_bbox,
            cache_path=self.cache_dir / f"us_padus_access_{bhash}.json",
            where="Category='Designation' OR MngTp_Reclass IN ('Federal','State')",
            out_fields="Unit_Nm,MngNm_Desc,DesTp_Desc,Category",
            extra_params={"maxAllowableOffset": "0.0004"},
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )

        rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
        seen: set[str] = set()

        for feat in wilderness_feats:
            props = feat.get("properties") or {}
            name = clean_feature_name(_prop_ci(props, "wildernessname", "name"))
            if not name:
                continue
            key = f"{name.lower()}|wilderness area"
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                (
                    feat["geometry"],
                    {
                        "name": name,
                        "manager": "USFS",
                        "designation": "Wilderness Area",
                        "state": "US",
                    },
                )
            )

        for feat in fee_feats:
            props = feat.get("properties") or {}
            name = clean_feature_name(_prop_ci(props, "Unit_Nm", "name"))
            if not name:
                continue
            manager = _normalize_padus_manager(str(_prop_ci(props, "Mang_Name", "Loc_Mang") or ""))
            designation = _normalize_padus_designation(
                str(_prop_ci(props, "Loc_Ds", "Des_Tp") or ""), name
            )
            key = f"{name.lower()}|{designation.lower()}"
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                (
                    feat["geometry"],
                    {
                        "name": name,
                        "manager": manager,
                        "designation": designation,
                        "state": "US",
                    },
                )
            )

        for feat in access_feats:
            props = feat.get("properties") or {}
            name = clean_feature_name(_prop_ci(props, "Unit_Nm", "name"))
            if not name:
                continue
            manager = _normalize_padus_manager(str(_prop_ci(props, "MngNm_Desc", "MngTp_Desc") or ""))
            designation = _normalize_padus_designation(str(_prop_ci(props, "DesTp_Desc") or ""), name)
            key = f"{name.lower()}|{designation.lower()}"
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                (
                    feat["geometry"],
                    {
                        "name": name,
                        "manager": manager,
                        "designation": designation,
                        "state": "US",
                    },
                )
            )

        if not rows:
            return None

        from osgeo import ogr  # type: ignore[import-untyped]

        layer_specs = [
            (
                "public_lands",
                ogr.wkbMultiPolygon,
                (
                    ("name", ogr.OFTString),
                    ("manager", ogr.OFTString),
                    ("designation", ogr.OFTString),
                    ("state", ogr.OFTString),
                ),
                rows,
            ),
        ]
        return _write_projected_gpkg_layers(
            out_gpkg,
            layout.utm_epsg,
            layout.utm_fetch_bbox,
            layer_specs,
        )


def build_us_overlay_sources(
    kinds: Sequence[USOverlayKind | str],
    local_overrides: Mapping[str | USOverlayKind, Path | str] | None = None,
    cache_dir: Path | str | None = None,
) -> dict[str, OverlaySource]:
    """Instantiate requested US public-domain overlay sources keyed by ``USOverlayKind.value``."""
    normalized_overrides: dict[str, Path] = {}
    if local_overrides:
        for raw_key, raw_path in local_overrides.items():
            key_str = (
                raw_key.value
                if isinstance(raw_key, USOverlayKind)
                else str(raw_key).lower().replace("_", "-")
            )
            normalized_overrides[key_str] = Path(raw_path)

    registry: dict[USOverlayKind, type[_BaseUSOverlaySource]] = {
        USOverlayKind.FEMA: FEMAFloodSource,
        USOverlayKind.NHD: NHDHydroSource,
        USOverlayKind.TOWERS: FAATowersSource,
        USOverlayKind.ENC: NOAAENCSource,
        USOverlayKind.US_TRAILS: USTrailsSource,
        USOverlayKind.US_PUBLIC_LANDS: USPublicLandsSource,
    }

    sources: dict[str, OverlaySource] = {}
    for raw_kind in kinds:
        kind = (
            raw_kind
            if isinstance(raw_kind, USOverlayKind)
            else USOverlayKind(str(raw_kind).lower().replace("_", "-"))
        )
        if kind.value in sources:
            continue
        cls = registry[kind]
        sources[kind.value] = cls(
            cache_dir=cache_dir,
            local_gpkg=normalized_overrides.get(kind.value),
        )
    return sources
