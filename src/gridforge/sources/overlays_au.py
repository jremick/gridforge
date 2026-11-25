"""Australia-wide national CC BY 4.0 thematic overlays (GA Hydrology, DCCEEW CAPAD, GA Infra, GA Aviation)."""

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

from gridforge.config import AUOverlayKind, BBox, SheetLayout
from gridforge.sources.base import OverlaySource

logger = logging.getLogger(__name__)

USER_AGENT: Final[str] = "gridforge/0.2 (+https://github.com/gridforge/gridforge)"

DEFAULT_AU_HYDRO_BASE_URL: Final[str] = (
    "https://services.ga.gov.au/gis/rest/services/Surface_Hydrology/MapServer"
)
DEFAULT_AU_PARKS_URL: Final[str] = (
    "https://gis.environment.gov.au/gispubmap/rest/services/ogc_services/CAPAD/FeatureServer/0/query"
)
DEFAULT_AU_ELEC_BASE_URL: Final[str] = (
    "https://services.ga.gov.au/gis/rest/services/Electricity_Infrastructure/MapServer"
)
DEFAULT_AU_PIPE_BASE_URL: Final[str] = (
    "https://services.ga.gov.au/gis/rest/services/Oil_Gas_Pipelines/MapServer"
)
DEFAULT_AU_TOPO_BASE_URL: Final[str] = (
    "https://services.ga.gov.au/gis/rest/services/Topographic_Base_Map/MapServer"
)

# ~5 m simplification offset keeps massive Outback National Park / IPA polygons < 500 KB
CAPAD_MAX_ALLOWABLE_OFFSET_DEG: Final[str] = "0.00005"


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


def _esri_feature_to_geojson(feat: dict[str, Any]) -> dict[str, Any] | None:
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
    retries: int = 2,
    page_size: int = 1000,
    max_pages: int = 5,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    """Query an Australian ArcGIS REST service by WGS84 bbox with disk cache, retries, and fallback."""
    if cache_path.is_file() and cache_path.stat().st_size > 0:
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(cached, list):
                return cached
        except json.JSONDecodeError:
            logger.warning("Ignoring corrupt cached AU overlay JSON: %s", cache_path)

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
                for attempt in range(max(1, retries)):
                    collected: list[dict[str, Any]] = []
                    offset = 0
                    succeeded = True
                    for _page in range(max_pages):
                        params: dict[str, str] = {
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
                            last_error = f"{url} (f={fmt}, try={attempt + 1}): {exc}"
                            succeeded = False
                            break

                        if not isinstance(data, dict) or "error" in data:
                            last_error = (
                                f"{url} (f={fmt}): ArcGIS error "
                                f"{data.get('error') if isinstance(data, dict) else type(data).__name__}"
                            )
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
        logger.warning("AU ArcGIS overlay query unavailable (%s); skipping layer cleanly", last_error)
    return []


def _clip_and_reproject_local_gpkg(
    local_gpkg: Path,
    layout: SheetLayout,
    out_gpkg: Path,
    expected_layers: Sequence[str],
) -> Path | None:
    """Reproject & clip an air-gapped local AU overlay GeoPackage to ``EPSG:{layout.utm_epsg}``."""
    from osgeo import ogr  # type: ignore[import-untyped]

    src_path = local_gpkg.expanduser().resolve()
    if not src_path.is_file():
        raise FileNotFoundError(f"Local AU overlay GeoPackage not found: {src_path}")

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
            f"ogr2ogr failed for local AU overlay {src_path} (exit {proc.returncode}): {proc.stderr.strip()}"
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
        return out_gpkg if total > 0 else None
    finally:
        ds = None


def _coerce_geometry_type(geom: Any, target_type: int) -> Any:
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


def _write_projected_gpkg_layers(
    out_gpkg: Path,
    utm_epsg: int,
    clip_bbox: BBox,
    layer_specs: Sequence[tuple[str, int, Sequence[tuple[str, int]], list[tuple[dict[str, Any], dict[str, Any]]]]],
) -> Path | None:
    """Project GeoJSON features from EPSG:4326 -> ``EPSG:{utm_epsg}``, clip, flatten 2D, and write GPKG."""
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
        raise RuntimeError(f"Failed to create AU overlay GeoPackage at {out_gpkg}")

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
                if not geom.Intersects(clip_poly):
                    continue
                if geom_type != ogr.wkbPoint:
                    clipped = geom.Intersection(clip_poly)
                    if clipped is None or clipped.IsEmpty():
                        continue
                    geom = clipped

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


class _BaseAUOverlaySource:
    """Shared cache/timeout/air-gapped override scaffolding for Australian national overlays."""

    name: str = "au-overlay"
    attribution: str = ""
    overlay_key: str = "au-overlay"

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
        self.cache_dir: Path = base_cache / "au_overlays"
        self.local_gpkg: Path | None = Path(local_gpkg) if local_gpkg is not None else None
        self.timeout_s = timeout_s
        self._injected_client = client


class GAHydroSource(_BaseAUOverlaySource):
    """Geoscience Australia National Surface Hydrology (bores, waterholes, watercourses, lakes)."""

    name: str = "au-ga-hydro"
    overlay_key: str = "au-hydro"
    attribution: str = (
        "Surface Hydrology: © Commonwealth of Australia (Geoscience Australia) "
        "& AusHydro Contributors (CC BY 4.0)"
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
            base_url or os.environ.get("GRIDFORGE_AU_HYDRO_URL", DEFAULT_AU_HYDRO_BASE_URL)
        ).rstrip("/")

    def fetch_overlay(self, layout: SheetLayout, work_dir: Path) -> Path | None:
        work_dir.mkdir(parents=True, exist_ok=True)
        out_gpkg = work_dir / "au_hydro.gpkg"
        if self.local_gpkg is not None:
            return _clip_and_reproject_local_gpkg(
                self.local_gpkg,
                layout,
                out_gpkg,
                ("au_hydro_points", "au_hydro_lines", "au_hydro_polys"),
            )

        query_bbox = _round_enclosing_bbox(layout.wgs84_fetch_bbox)
        bhash = _bbox_hash(query_bbox)

        pt_feats = _query_arcgis_geojson(
            [f"{self.base_url}/1/query"],
            query_bbox,
            cache_path=self.cache_dir / f"au_hydro_pts_{bhash}.json",
            out_fields="name,featuretype,type,perenniality",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        line_feats = _query_arcgis_geojson(
            [f"{self.base_url}/3/query"],
            query_bbox,
            cache_path=self.cache_dir / f"au_hydro_lines_{bhash}.json",
            where="featuretype <> 'Connector'",
            out_fields="name,featuretype,perenniality,hierarchy",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        poly_feats = _query_arcgis_geojson(
            [f"{self.base_url}/5/query"],
            query_bbox,
            cache_path=self.cache_dir / f"au_hydro_polys_{bhash}.json",
            out_fields="name,featuretype,perenniality",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )

        if not pt_feats and not line_feats and not poly_feats:
            return None

        from osgeo import ogr  # type: ignore[import-untyped]

        pt_rows = [
            (
                f["geometry"],
                {
                    "name": str(_prop_ci(f.get("properties") or {}, "name") or "").strip(),
                    "featuretype": str(_prop_ci(f.get("properties") or {}, "featuretype") or "").strip(),
                    "type": str(_prop_ci(f.get("properties") or {}, "type") or "").strip(),
                    "perenniality": str(_prop_ci(f.get("properties") or {}, "perenniality") or "").strip(),
                },
            )
            for f in pt_feats
        ]
        line_rows = [
            (
                f["geometry"],
                {
                    "name": str(_prop_ci(f.get("properties") or {}, "name") or "").strip(),
                    "featuretype": str(_prop_ci(f.get("properties") or {}, "featuretype") or "").strip(),
                    "perenniality": str(_prop_ci(f.get("properties") or {}, "perenniality") or "").strip(),
                    "hierarchy": str(_prop_ci(f.get("properties") or {}, "hierarchy") or "").strip(),
                },
            )
            for f in line_feats
        ]
        poly_rows = [
            (
                f["geometry"],
                {
                    "name": str(_prop_ci(f.get("properties") or {}, "name") or "").strip(),
                    "featuretype": str(_prop_ci(f.get("properties") or {}, "featuretype") or "").strip(),
                    "perenniality": str(_prop_ci(f.get("properties") or {}, "perenniality") or "").strip(),
                },
            )
            for f in poly_feats
        ]

        layer_specs = [
            (
                "au_hydro_points",
                ogr.wkbPoint,
                (
                    ("name", ogr.OFTString),
                    ("featuretype", ogr.OFTString),
                    ("type", ogr.OFTString),
                    ("perenniality", ogr.OFTString),
                ),
                pt_rows,
            ),
            (
                "au_hydro_lines",
                ogr.wkbMultiLineString,
                (
                    ("name", ogr.OFTString),
                    ("featuretype", ogr.OFTString),
                    ("perenniality", ogr.OFTString),
                    ("hierarchy", ogr.OFTString),
                ),
                line_rows,
            ),
            (
                "au_hydro_polys",
                ogr.wkbMultiPolygon,
                (
                    ("name", ogr.OFTString),
                    ("featuretype", ogr.OFTString),
                    ("perenniality", ogr.OFTString),
                ),
                poly_rows,
            ),
        ]
        return _write_projected_gpkg_layers(out_gpkg, layout.utm_epsg, layout.utm_fetch_bbox, layer_specs)


class CAPADParksSource(_BaseAUOverlaySource):
    """DCCEEW Collaborative Australian Protected Areas Database (CAPAD National Parks & IPAs)."""

    name: str = "au-dcceew-capad"
    overlay_key: str = "au-parks"
    attribution: str = "Protected Areas (CAPAD): © Commonwealth of Australia (DCCEEW) 2025 (CC BY 4.0)"

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
        self.endpoint = endpoint or os.environ.get("GRIDFORGE_AU_PARKS_URL", DEFAULT_AU_PARKS_URL)

    def fetch_overlay(self, layout: SheetLayout, work_dir: Path) -> Path | None:
        work_dir.mkdir(parents=True, exist_ok=True)
        out_gpkg = work_dir / "au_parks.gpkg"
        if self.local_gpkg is not None:
            return _clip_and_reproject_local_gpkg(
                self.local_gpkg,
                layout,
                out_gpkg,
                ("au_protected_areas",),
            )

        query_bbox = _round_enclosing_bbox(layout.wgs84_fetch_bbox)
        cache_path = self.cache_dir / f"au_parks_{_bbox_hash(query_bbox)}.json"
        features = _query_arcgis_geojson(
            [self.endpoint],
            query_bbox,
            cache_path=cache_path,
            out_fields="NAME,TYPE,TYPE_ABBR,IUCN,STATE",
            extra_params={"maxAllowableOffset": CAPAD_MAX_ALLOWABLE_OFFSET_DEG},
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        if not features:
            return None

        from osgeo import ogr  # type: ignore[import-untyped]

        rows = [
            (
                f["geometry"],
                {
                    "name": str(_prop_ci(f.get("properties") or {}, "NAME", "name") or "").strip(),
                    "park_type": str(_prop_ci(f.get("properties") or {}, "TYPE", "park_type") or "").strip(),
                    "type_abbr": str(_prop_ci(f.get("properties") or {}, "TYPE_ABBR", "type_abbr") or "").strip(),
                    "iucn": str(_prop_ci(f.get("properties") or {}, "IUCN", "iucn") or "").strip(),
                    "state": str(_prop_ci(f.get("properties") or {}, "STATE", "state") or "").strip(),
                },
            )
            for f in features
        ]
        fields = (
            ("name", ogr.OFTString),
            ("park_type", ogr.OFTString),
            ("type_abbr", ogr.OFTString),
            ("iucn", ogr.OFTString),
            ("state", ogr.OFTString),
        )
        return _write_projected_gpkg_layers(
            out_gpkg,
            layout.utm_epsg,
            layout.utm_fetch_bbox,
            [("au_protected_areas", ogr.wkbMultiPolygon, fields, rows)],
        )


class GAInfraSource(_BaseAUOverlaySource):
    """Geoscience Australia Electricity Transmission Lines, Substations, and Oil/Gas Pipelines."""

    name: str = "au-ga-infra"
    overlay_key: str = "au-infra"
    attribution: str = "Energy Infrastructure: © Commonwealth of Australia (Geoscience Australia) 2026 (CC BY 4.0)"

    def __init__(
        self,
        *,
        elec_base_url: str | None = None,
        pipe_base_url: str | None = None,
        cache_dir: Path | str | None = None,
        local_gpkg: Path | str | None = None,
        timeout_s: float = 20.0,
        client: httpx.Client | None = None,
    ) -> None:
        super().__init__(
            cache_dir=cache_dir,
            local_gpkg=local_gpkg,
            timeout_s=timeout_s,
            client=client,
        )
        self.elec_base_url = (
            elec_base_url or os.environ.get("GRIDFORGE_AU_ELEC_URL", DEFAULT_AU_ELEC_BASE_URL)
        ).rstrip("/")
        self.pipe_base_url = (
            pipe_base_url or os.environ.get("GRIDFORGE_AU_PIPE_URL", DEFAULT_AU_PIPE_BASE_URL)
        ).rstrip("/")

    def fetch_overlay(self, layout: SheetLayout, work_dir: Path) -> Path | None:
        work_dir.mkdir(parents=True, exist_ok=True)
        out_gpkg = work_dir / "au_infra.gpkg"
        if self.local_gpkg is not None:
            return _clip_and_reproject_local_gpkg(
                self.local_gpkg,
                layout,
                out_gpkg,
                ("au_power_lines", "au_substations", "au_pipelines"),
            )

        query_bbox = _round_enclosing_bbox(layout.wgs84_fetch_bbox)
        bhash = _bbox_hash(query_bbox)

        power_feats = _query_arcgis_geojson(
            [f"{self.elec_base_url}/2/query"],
            query_bbox,
            cache_path=self.cache_dir / f"au_power_lines_{bhash}.json",
            out_fields="FEATURE_NAME,CAPACITY_KV,STATUS,STATE",
            timeout_s=self.timeout_s,
            retries=2,
            client=self._injected_client,
        )
        sub_feats = _query_arcgis_geojson(
            [f"{self.elec_base_url}/0/query"],
            query_bbox,
            cache_path=self.cache_dir / f"au_substations_{bhash}.json",
            out_fields="FEATURE_NAME,VOLTAGE_KV,STATUS,STATE",
            timeout_s=self.timeout_s,
            retries=2,
            client=self._injected_client,
        )
        oil_feats = _query_arcgis_geojson(
            [f"{self.pipe_base_url}/0/query"],
            query_bbox,
            cache_path=self.cache_dir / f"au_pipe_oil_{bhash}.json",
            out_fields="feature_name,feature_type,status,operator",
            timeout_s=self.timeout_s,
            retries=2,
            client=self._injected_client,
        )
        gas_feats = _query_arcgis_geojson(
            [f"{self.pipe_base_url}/1/query"],
            query_bbox,
            cache_path=self.cache_dir / f"au_pipe_gas_{bhash}.json",
            out_fields="feature_name,feature_type,status,operator",
            timeout_s=self.timeout_s,
            retries=2,
            client=self._injected_client,
        )

        if not power_feats and not sub_feats and not oil_feats and not gas_feats:
            return None

        from osgeo import ogr  # type: ignore[import-untyped]

        power_rows = [
            (
                f["geometry"],
                {
                    "name": str(_prop_ci(f.get("properties") or {}, "FEATURE_NAME", "name") or "").strip(),
                    "capacity_kv": _to_float_or_none(_prop_ci(f.get("properties") or {}, "CAPACITY_KV", "capacity_kv")),
                    "status": str(_prop_ci(f.get("properties") or {}, "STATUS", "status") or "").strip(),
                    "state": str(_prop_ci(f.get("properties") or {}, "STATE", "state") or "").strip(),
                },
            )
            for f in power_feats
        ]
        sub_rows = [
            (
                f["geometry"],
                {
                    "name": str(_prop_ci(f.get("properties") or {}, "FEATURE_NAME", "name") or "").strip(),
                    "voltage_kv": _to_float_or_none(_prop_ci(f.get("properties") or {}, "VOLTAGE_KV", "voltage_kv")),
                    "status": str(_prop_ci(f.get("properties") or {}, "STATUS", "status") or "").strip(),
                    "state": str(_prop_ci(f.get("properties") or {}, "STATE", "state") or "").strip(),
                },
            )
            for f in sub_feats
        ]
        pipe_rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for default_kind, feats in (("oil", oil_feats), ("gas", gas_feats)):
            for f in feats:
                props = f.get("properties") or {}
                ptype = str(_prop_ci(props, "pipeline_type", "feature_type") or default_kind).strip()
                pipe_rows.append(
                    (
                        f["geometry"],
                        {
                            "name": str(_prop_ci(props, "feature_name", "name") or "").strip(),
                            "pipeline_type": ptype,
                            "status": str(_prop_ci(props, "status") or "").strip(),
                            "operator": str(_prop_ci(props, "operator") or "").strip(),
                        },
                    )
                )

        layer_specs = [
            (
                "au_power_lines",
                ogr.wkbMultiLineString,
                (
                    ("name", ogr.OFTString),
                    ("capacity_kv", ogr.OFTReal),
                    ("status", ogr.OFTString),
                    ("state", ogr.OFTString),
                ),
                power_rows,
            ),
            (
                "au_substations",
                ogr.wkbPoint,
                (
                    ("name", ogr.OFTString),
                    ("voltage_kv", ogr.OFTReal),
                    ("status", ogr.OFTString),
                    ("state", ogr.OFTString),
                ),
                sub_rows,
            ),
            (
                "au_pipelines",
                ogr.wkbMultiLineString,
                (
                    ("name", ogr.OFTString),
                    ("pipeline_type", ogr.OFTString),
                    ("status", ogr.OFTString),
                    ("operator", ogr.OFTString),
                ),
                pipe_rows,
            ),
        ]
        return _write_projected_gpkg_layers(out_gpkg, layout.utm_epsg, layout.utm_fetch_bbox, layer_specs)


class GAAviationSource(_BaseAUOverlaySource):
    """Geoscience Australia National Airfields, Runways, and Outback Homesteads."""

    name: str = "au-ga-aviation"
    overlay_key: str = "au-aviation"
    attribution: str = "Topographic & Aviation Data: © Commonwealth of Australia (Geoscience Australia) (CC BY 4.0)"

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
            base_url or os.environ.get("GRIDFORGE_AU_TOPO_URL", DEFAULT_AU_TOPO_BASE_URL)
        ).rstrip("/")

    def fetch_overlay(self, layout: SheetLayout, work_dir: Path) -> Path | None:
        work_dir.mkdir(parents=True, exist_ok=True)
        out_gpkg = work_dir / "au_aviation.gpkg"
        if self.local_gpkg is not None:
            return _clip_and_reproject_local_gpkg(
                self.local_gpkg,
                layout,
                out_gpkg,
                ("au_airfields", "au_runways", "au_homesteads"),
            )

        query_bbox = _round_enclosing_bbox(layout.wgs84_fetch_bbox)
        bhash = _bbox_hash(query_bbox)

        airfield_feats = _query_arcgis_geojson(
            [f"{self.base_url}/419/query"],
            query_bbox,
            cache_path=self.cache_dir / f"au_airfields_{bhash}.json",
            out_fields="name,featuretype,type",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        runway_feats = _query_arcgis_geojson(
            [f"{self.base_url}/420/query"],
            query_bbox,
            cache_path=self.cache_dir / f"au_runways_{bhash}.json",
            out_fields="featuretype,type",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )
        homestead_feats = _query_arcgis_geojson(
            [f"{self.base_url}/237/query"],
            query_bbox,
            cache_path=self.cache_dir / f"au_homesteads_{bhash}.json",
            out_fields="name,featuretype",
            timeout_s=self.timeout_s,
            client=self._injected_client,
        )

        if not airfield_feats and not runway_feats and not homestead_feats:
            return None

        from osgeo import ogr  # type: ignore[import-untyped]

        airfield_rows = [
            (
                f["geometry"],
                {
                    "name": str(_prop_ci(f.get("properties") or {}, "name") or "").strip(),
                    "featuretype": str(_prop_ci(f.get("properties") or {}, "featuretype") or "").strip(),
                    "type": str(_prop_ci(f.get("properties") or {}, "type") or "").strip(),
                },
            )
            for f in airfield_feats
        ]
        runway_rows = [
            (
                f["geometry"],
                {
                    "featuretype": str(_prop_ci(f.get("properties") or {}, "featuretype") or "").strip(),
                    "type": str(_prop_ci(f.get("properties") or {}, "type") or "").strip(),
                },
            )
            for f in runway_feats
        ]
        homestead_rows = [
            (
                f["geometry"],
                {
                    "name": str(_prop_ci(f.get("properties") or {}, "name") or "").strip(),
                    "featuretype": str(_prop_ci(f.get("properties") or {}, "featuretype") or "").strip(),
                },
            )
            for f in homestead_feats
        ]

        layer_specs = [
            (
                "au_airfields",
                ogr.wkbPoint,
                (
                    ("name", ogr.OFTString),
                    ("featuretype", ogr.OFTString),
                    ("type", ogr.OFTString),
                ),
                airfield_rows,
            ),
            (
                "au_runways",
                ogr.wkbMultiLineString,
                (
                    ("featuretype", ogr.OFTString),
                    ("type", ogr.OFTString),
                ),
                runway_rows,
            ),
            (
                "au_homesteads",
                ogr.wkbPoint,
                (
                    ("name", ogr.OFTString),
                    ("featuretype", ogr.OFTString),
                ),
                homestead_rows,
            ),
        ]
        return _write_projected_gpkg_layers(out_gpkg, layout.utm_epsg, layout.utm_fetch_bbox, layer_specs)


def build_au_overlay_sources(
    kinds: Sequence[AUOverlayKind | str],
    local_overrides: Mapping[str | AUOverlayKind, Path | str] | None = None,
    cache_dir: Path | str | None = None,
) -> dict[str, OverlaySource]:
    """Instantiate requested Australia CC BY 4.0 overlay sources keyed by ``AUOverlayKind.value``."""
    normalized_overrides: dict[str, Path] = {}
    if local_overrides:
        for raw_key, raw_path in local_overrides.items():
            key_str = (
                raw_key.value
                if isinstance(raw_key, AUOverlayKind)
                else str(raw_key).lower().replace("_", "-")
            )
            normalized_overrides[key_str] = Path(raw_path)

    from gridforge.sources.overlays_au_states import NSWTrailsSource, VICTrailsSource

    registry: dict[AUOverlayKind, type[_BaseAUOverlaySource]] = {
        AUOverlayKind.AU_HYDRO: GAHydroSource,
        AUOverlayKind.AU_PARKS: CAPADParksSource,
        AUOverlayKind.AU_INFRA: GAInfraSource,
        AUOverlayKind.AU_AVIATION: GAAviationSource,
        AUOverlayKind.AU_NSW_TRAILS: NSWTrailsSource,
        AUOverlayKind.AU_VIC_TRAILS: VICTrailsSource,
    }

    sources: dict[str, OverlaySource] = {}
    for raw_kind in kinds:
        kind = (
            raw_kind
            if isinstance(raw_kind, AUOverlayKind)
            else AUOverlayKind(str(raw_kind).lower().replace("_", "-"))
        )
        if kind.value in sources:
            continue
        cls = registry[kind]
        sources[kind.value] = cls(
            cache_dir=cache_dir,
            local_gpkg=normalized_overrides.get(kind.value),
        )
    return sources
