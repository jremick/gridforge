"""Fast terrain-masked RF / Line-of-Sight viewshed preview helper for GridForge Studio API.

Runs purely with GDAL + PyProj + Shapely (zero QGIS / zero PyQt5 imports) at fast ~30 m
resolution so interactive MapLibre clicks return in ~1-2 s under 150 MB RAM.
"""

from __future__ import annotations

import inspect
import logging
import math
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from osgeo import gdal
from pydantic import BaseModel
from pyproj import Transformer
import shapely
from shapely.geometry import shape as shapely_shape

from gridforge.config import (
    BBox,
    DemSourceKind,
    FrameMarginsMM,
    RFLinkSpec,
    SheetLayout,
    SheetSpec,
    ViewshedObserver,
)
from gridforge.geo.crs import MAX_UTM_LAT, MIN_UTM_LAT, resolve_utm_zone
from gridforge.geo.strip_atlas import _format_mgrs_10m, _sample_dem_point_bilinear
from gridforge.geo.tactical import RulerMetricsResponse, compute_ruler_metrics
from gridforge.packs.builder import load_pack_manifest
from gridforge.pipeline import terrain as terrain_mod
from gridforge.sources.dem_au_elvis import AUElvisDEMSource
from gridforge.sources.dem_copernicus import CopernicusGLO30Source
from gridforge.sources.dem_local import LocalDEMSource
from gridforge.sources.dem_usgs3dep import AutoDEMSource, USGS3DEPSource
from gridforge.web.schemas import (
    RFLinkProfileRequest,
    RFLinkProfileResponse,
    RulerMetricsRequest,
    ViewshedPreviewRequest,
    ViewshedPreviewResponse,
)

logger = logging.getLogger(__name__)

_DEM_NODATA: float = -9999.0


def _build_range_ring_geojson(
    ox: float,
    oy: float,
    observer: ViewshedObserver,
    utm_epsg: int,
    *,
    num_vertices: int = 72,
) -> dict[str, Any]:
    """Build a smooth WGS84 GeoJSON circle polygon of radius ``observer.max_distance_m``."""
    to_wgs84 = Transformer.from_crs(f"EPSG:{utm_epsg}", "EPSG:4326", always_xy=True)
    r = float(observer.max_distance_m)
    xs: list[float] = []
    ys: list[float] = []
    for idx in range(num_vertices):
        ang = 2.0 * math.pi * idx / num_vertices
        xs.append(ox + r * math.cos(ang))
        ys.append(oy + r * math.sin(ang))

    lons, lats = to_wgs84.transform(xs, ys)
    ring = [[round(float(lo), 6), round(float(la), 6)] for lo, la in zip(lons, lats)]
    ring.append(ring[0])
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "label": observer.label,
                    "max_distance_m": observer.max_distance_m,
                    "observer_height_m": observer.observer_height_m,
                    "target_height_m": observer.target_height_m,
                    "utm_epsg": utm_epsg,
                },
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [ring],
                },
            }
        ],
    }


def _build_viewshed_fetch_layout(
    observer: ViewshedObserver,
    ox: float,
    oy: float,
    utm_zone: int,
    utm_hemi: str,
    utm_epsg: int,
    gzd: str,
) -> SheetLayout:
    """Build a synthetic ``SheetLayout`` spanning ``observer.max_distance_m`` for DEM fetching."""
    r = float(observer.max_distance_m)
    pad = max(300.0, 0.08 * r)
    utm_bbox = BBox(min_x=ox - r, min_y=oy - r, max_x=ox + r, max_y=oy + r)
    utm_fetch_bbox = BBox(
        min_x=ox - r - pad,
        min_y=oy - r - pad,
        max_x=ox + r + pad,
        max_y=oy + r + pad,
    )

    to_wgs84 = Transformer.from_crs(f"EPSG:{utm_epsg}", "EPSG:4326", always_xy=True)

    def _to_wgs84_bbox(b: BBox) -> BBox:
        mx = 0.5 * (b.min_x + b.max_x)
        my = 0.5 * (b.min_y + b.max_y)
        xs = [b.min_x, mx, b.max_x, b.max_x, b.max_x, mx, b.min_x, b.min_x]
        ys = [b.min_y, b.min_y, b.min_y, my, b.max_y, b.max_y, b.max_y, my]
        lons, lats = to_wgs84.transform(xs, ys)
        return BBox(
            min_x=max(-180.0, float(min(lons))),
            min_y=max(MIN_UTM_LAT, float(min(lats))),
            max_x=min(180.0, float(max(lons))),
            max_y=min(MAX_UTM_LAT, float(max(lats))),
        )

    spec = SheetSpec(
        center_lat=observer.lat,
        center_lon=observer.lon,
        scale=25000,
    )
    return SheetLayout(
        spec=spec,
        utm_epsg=utm_epsg,
        utm_zone=utm_zone,
        utm_hemisphere="N" if utm_hemi == "N" else "S",
        gzd=gzd,
        center_easting=ox,
        center_northing=oy,
        page_width_mm=210.0,
        page_height_mm=297.0,
        margins_mm=FrameMarginsMM(),
        frame_x_mm=12.0,
        frame_y_mm=14.0,
        frame_width_mm=186.0,
        frame_height_mm=235.0,
        utm_bbox=utm_bbox,
        utm_fetch_bbox=utm_fetch_bbox,
        wgs84_bbox=_to_wgs84_bbox(utm_bbox),
        wgs84_fetch_bbox=_to_wgs84_bbox(utm_fetch_bbox),
    )


def _resolve_preview_elevation_source(
    dem_source: DemSourceKind,
    *,
    pack_id: str | None = None,
    packs_dir: Path | None = None,
    pack_dir_finder: Any | None = None,
) -> Any:
    if pack_id and packs_dir is not None:
        pack_dir = pack_dir_finder(pack_id) if pack_dir_finder is not None else (packs_dir / pack_id)
        if pack_dir is not None and Path(pack_dir).is_dir():
            manifest = load_pack_manifest(Path(pack_dir))
            if manifest.dem_path:
                dem_file = (Path(pack_dir) / manifest.dem_path).resolve()
                if dem_file.is_file():
                    return LocalDEMSource(dem_file, attribution=manifest.dem_attribution)

    if dem_source == DemSourceKind.USGS3DEP:
        return USGS3DEPSource()
    if dem_source == DemSourceKind.AU_ELVIS:
        return AUElvisDEMSource()
    if dem_source == DemSourceKind.GLO30:
        return CopernicusGLO30Source()
    if dem_source == DemSourceKind.NZ_LINZ:
        try:
            from gridforge.sources.dem_nz_linz import NZLinzDEMSource  # type: ignore[import-not-found]

            return NZLinzDEMSource()
        except Exception:
            logger.debug("NZLinzDEMSource unavailable; falling back to AutoDEMSource")
            return AutoDEMSource()
    if dem_source == DemSourceKind.CA_HRDEM:
        try:
            from gridforge.sources import dem_ca_hrdem  # type: ignore[import-not-found]

            cls = getattr(dem_ca_hrdem, "CanadaHRDEMSource", None) or getattr(
                dem_ca_hrdem, "CAHrdemDEMSource", None
            )
            if cls is not None:
                return cls()
        except Exception:
            logger.debug("CanadaHRDEMSource unavailable; falling back to AutoDEMSource")
        return AutoDEMSource()
    return AutoDEMSource()


def _warp_dem_fast_utm(
    raw_dem_path: Path,
    utm_fetch_bbox: BBox,
    utm_epsg: int,
    max_distance_m: float,
    output_tif: Path,
) -> Path:
    """Warp raw DEM to local UTM at fast preview resolution (>= 30 m, <= ~750 px/side)."""
    res_m = max(30.0, round(float(max_distance_m) / 350.0, 1))
    cmd = [
        "gdalwarp",
        "-overwrite",
        "-t_srs",
        f"EPSG:{utm_epsg}",
        "-te",
        f"{utm_fetch_bbox.min_x:.3f}",
        f"{utm_fetch_bbox.min_y:.3f}",
        f"{utm_fetch_bbox.max_x:.3f}",
        f"{utm_fetch_bbox.max_y:.3f}",
        "-tr",
        f"{res_m:g}",
        f"{res_m:g}",
        "-r",
        "bilinear",
        "-dstnodata",
        str(_DEM_NODATA),
        "-ot",
        "Float32",
        "-co",
        "COMPRESS=DEFLATE",
        str(raw_dem_path),
        str(output_tif),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"gdalwarp viewshed preview DEM failed: {proc.stderr.strip()}")
    return output_tif


def _sample_observer_elev(dem_utm_tif: Path, ox: float, oy: float) -> float:
    gdal.UseExceptions()
    ds = gdal.Open(str(dem_utm_tif), gdal.GA_ReadOnly)
    if ds is None:
        return 0.0
    try:
        band = ds.GetRasterBand(1)
        nodata = band.GetNoDataValue()
        gt = ds.GetGeoTransform()
        inv_gt = gdal.InvGeoTransform(gt)
        val = _sample_dem_point_bilinear(ds, band, inv_gt, nodata, ox, oy)
        if val is not None and math.isfinite(val):
            return float(val)
        try:
            _, _, mean_val, _ = band.ComputeStatistics(False)
            if math.isfinite(mean_val):
                return float(mean_val)
        except Exception:
            pass
        return 0.0
    finally:
        ds = None


def _compute_geojson_visible_area_km2(
    viewshed_geojson: dict[str, Any],
    utm_epsg: int,
) -> float:
    """Compute total visible coverage area in km² from a WGS84 viewshed FeatureCollection."""
    # Prefer precomputed metric if provided by compute_viewshed_geojson.
    top_props = viewshed_geojson.get("properties")
    if isinstance(top_props, dict) and isinstance(top_props.get("visible_area_km2"), (int, float)):
        return max(0.0, float(top_props["visible_area_km2"]))
    if isinstance(viewshed_geojson.get("visible_area_km2"), (int, float)):
        return max(0.0, float(viewshed_geojson["visible_area_km2"]))

    features = viewshed_geojson.get("features")
    if not isinstance(features, list) or not features:
        return 0.0

    to_utm = Transformer.from_crs("EPSG:4326", f"EPSG:{utm_epsg}", always_xy=True)

    import numpy as np

    def _proj_coords(coords: Any) -> Any:
        xs, ys = to_utm.transform(coords[:, 0], coords[:, 1])
        return np.column_stack((xs, ys))

    total_m2 = 0.0
    for feat in features:
        if not isinstance(feat, dict):
            continue
        geom_dict = feat.get("geometry")
        if not isinstance(geom_dict, dict):
            continue
        try:
            geom = shapely_shape(geom_dict)
            if geom.is_empty:
                continue
            geom_utm = shapely.transform(geom, _proj_coords)
            total_m2 += max(0.0, float(geom_utm.area))
        except Exception:
            continue

    return total_m2 / 1_000_000.0


def compute_viewshed_preview(
    req: ViewshedPreviewRequest,
    *,
    packs_dir: Path | None = None,
    pack_dir_finder: Any | None = None,
    elevation_source: Any | None = None,
) -> ViewshedPreviewResponse:
    """Fetch/warp DEM at fast ~30 m resolution and compute live terrain-masked RF/LOS GeoJSON."""
    obs = req.observer
    utm_zone, utm_hemi, utm_epsg, gzd = resolve_utm_zone(obs.lat, obs.lon)
    to_utm = Transformer.from_crs("EPSG:4326", f"EPSG:{utm_epsg}", always_xy=True)
    ox, oy = to_utm.transform(obs.lon, obs.lat)
    ox_f, oy_f = float(ox), float(oy)

    observer_mgrs_10m = _format_mgrs_10m(obs.lat, obs.lon)
    range_ring_geojson = _build_range_ring_geojson(ox_f, oy_f, obs, utm_epsg)
    fetch_layout = _build_viewshed_fetch_layout(
        obs, ox_f, oy_f, utm_zone, utm_hemi, utm_epsg, gzd
    )

    elev_src = (
        elevation_source
        if elevation_source is not None
        else _resolve_preview_elevation_source(
            req.dem_source,
            pack_id=req.pack_id,
            packs_dir=packs_dir,
            pack_dir_finder=pack_dir_finder,
        )
    )

    compute_fn = getattr(terrain_mod, "compute_viewshed_geojson", None)
    if compute_fn is None:
        raise RuntimeError("terrain.compute_viewshed_geojson() is not available in this build.")

    with tempfile.TemporaryDirectory(prefix="gf_vs_preview_") as tmp_s:
        tmp_dir = Path(tmp_s)
        raw_dem = elev_src.fetch_dem(fetch_layout, tmp_dir)
        dem_utm_tif = _warp_dem_fast_utm(
            Path(raw_dem),
            fetch_layout.utm_fetch_bbox,
            utm_epsg,
            obs.max_distance_m,
            tmp_dir / "dem_preview_utm.tif",
        )
        observer_elev_m = _sample_observer_elev(dem_utm_tif, ox_f, oy_f)
        viewshed_geojson = compute_fn(dem_utm_tif, obs, utm_epsg, tmp_dir)

    if not isinstance(viewshed_geojson, dict) or viewshed_geojson.get("type") != "FeatureCollection":
        viewshed_geojson = {"type": "FeatureCollection", "features": []}

    visible_area_km2 = _compute_geojson_visible_area_km2(viewshed_geojson, utm_epsg)

    return ViewshedPreviewResponse(
        observer_mgrs_10m=observer_mgrs_10m,
        observer_elev_m=round(observer_elev_m, 1),
        utm_epsg=utm_epsg,
        max_distance_m=obs.max_distance_m,
        visible_area_km2=round(visible_area_km2, 3),
        viewshed_geojson=viewshed_geojson,
        range_ring_geojson=range_ring_geojson,
    )


def _build_extent_fetch_layout(
    lat_pts: list[float],
    lon_pts: list[float],
    *,
    pad_m: float = 1000.0,
) -> tuple[SheetLayout, int]:
    """Build a synthetic ``SheetLayout`` spanning ``(lat_pts, lon_pts)`` in midpoint UTM."""
    mid_lat = 0.5 * (min(lat_pts) + max(lat_pts))
    mid_lon = 0.5 * (min(lon_pts) + max(lon_pts))
    utm_zone, utm_hemi, utm_epsg, gzd = resolve_utm_zone(mid_lat, mid_lon)

    to_utm = Transformer.from_crs("EPSG:4326", f"EPSG:{utm_epsg}", always_xy=True)
    to_wgs84 = Transformer.from_crs(f"EPSG:{utm_epsg}", "EPSG:4326", always_xy=True)
    xs, ys = to_utm.transform(lon_pts, lat_pts)
    min_x, max_x = float(min(xs)), float(max(xs))
    min_y, max_y = float(min(ys)), float(max(ys))
    cx = 0.5 * (min_x + max_x)
    cy = 0.5 * (min_y + max_y)
    span = max(max_x - min_x, max_y - min_y, 500.0)
    eff_pad = max(pad_m, 0.12 * span)

    utm_bbox = BBox(
        min_x=min_x - 0.5 * eff_pad,
        min_y=min_y - 0.5 * eff_pad,
        max_x=max_x + 0.5 * eff_pad,
        max_y=max_y + 0.5 * eff_pad,
    )
    utm_fetch_bbox = BBox(
        min_x=min_x - eff_pad,
        min_y=min_y - eff_pad,
        max_x=max_x + eff_pad,
        max_y=max_y + eff_pad,
    )

    def _to_wgs84_bbox(b: BBox) -> BBox:
        mx = 0.5 * (b.min_x + b.max_x)
        my = 0.5 * (b.min_y + b.max_y)
        bxs = [b.min_x, mx, b.max_x, b.max_x, b.max_x, mx, b.min_x, b.min_x]
        bys = [b.min_y, b.min_y, b.min_y, my, b.max_y, b.max_y, b.max_y, my]
        blons, blats = to_wgs84.transform(bxs, bys)
        return BBox(
            min_x=max(-180.0, float(min(blons))),
            min_y=max(MIN_UTM_LAT, float(min(blats))),
            max_x=min(180.0, float(max(blons))),
            max_y=min(MAX_UTM_LAT, float(max(blats))),
        )

    layout = SheetLayout(
        spec=SheetSpec(center_lat=mid_lat, center_lon=mid_lon, scale=25000),
        utm_epsg=utm_epsg,
        utm_zone=utm_zone,
        utm_hemisphere="N" if utm_hemi == "N" else "S",
        gzd=gzd,
        center_easting=cx,
        center_northing=cy,
        page_width_mm=210.0,
        page_height_mm=297.0,
        margins_mm=FrameMarginsMM(),
        frame_x_mm=12.0,
        frame_y_mm=14.0,
        frame_width_mm=186.0,
        frame_height_mm=235.0,
        utm_bbox=utm_bbox,
        utm_fetch_bbox=utm_fetch_bbox,
        wgs84_bbox=_to_wgs84_bbox(utm_bbox),
        wgs84_fetch_bbox=_to_wgs84_bbox(utm_fetch_bbox),
    )
    return (layout, utm_epsg)


def compute_rf_link_preview(
    req: RFLinkProfileRequest,
    *,
    packs_dir: Path | None = None,
    pack_dir_finder: Any | None = None,
    elevation_source: Any | None = None,
) -> RFLinkProfileResponse:
    """Fetch/warp DEM along an A↔B RF link and compute curved-Earth Fresnel clearance profile."""
    link = RFLinkSpec(
        from_lat=req.from_lat,
        from_lon=req.from_lon,
        to_lat=req.to_lat,
        to_lon=req.to_lon,
        from_height_m=req.from_height_m,
        to_height_m=req.to_height_m,
        freq_mhz=req.freq_mhz,
        k_factor=req.k_factor,
        from_label=req.from_label,
        to_label=req.to_label,
    )

    compute_fn = getattr(terrain_mod, "compute_rf_link_profile", None)
    if compute_fn is None:
        raise RuntimeError("terrain.compute_rf_link_profile() is not available in this build.")

    fetch_layout, utm_epsg = _build_extent_fetch_layout(
        [link.from_lat, link.to_lat],
        [link.from_lon, link.to_lon],
        pad_m=800.0,
    )
    elev_src = (
        elevation_source
        if elevation_source is not None
        else _resolve_preview_elevation_source(
            req.dem_source,
            pack_id=req.pack_id,
            packs_dir=packs_dir,
            pack_dir_finder=pack_dir_finder,
        )
    )

    span_m = max(fetch_layout.utm_fetch_bbox.width, fetch_layout.utm_fetch_bbox.height)
    with tempfile.TemporaryDirectory(prefix="gf_rflink_preview_") as tmp_s:
        tmp_dir = Path(tmp_s)
        raw_dem = elev_src.fetch_dem(fetch_layout, tmp_dir)
        dem_utm_tif = _warp_dem_fast_utm(
            Path(raw_dem),
            fetch_layout.utm_fetch_bbox,
            utm_epsg,
            max(1000.0, 0.5 * span_m),
            tmp_dir / "dem_rflink_utm.tif",
        )
        sig = inspect.signature(compute_fn)
        call_kwargs: dict[str, Any] = {}
        if "date_iso" in sig.parameters and req.date_iso is not None:
            call_kwargs["date_iso"] = req.date_iso
        raw_result = compute_fn(dem_utm_tif, link, utm_epsg, **call_kwargs)

    if isinstance(raw_result, BaseModel):
        result_dict = raw_result.model_dump()
    elif isinstance(raw_result, dict):
        result_dict = dict(raw_result)
    else:
        raise RuntimeError(f"Unexpected compute_rf_link_profile result type: {type(raw_result)!r}")

    result_dict.setdefault("from_label", link.from_label)
    result_dict.setdefault("to_label", link.to_label)
    result_dict.setdefault("from_height_m", link.from_height_m)
    result_dict.setdefault("to_height_m", link.to_height_m)
    return RFLinkProfileResponse.model_validate(result_dict)


def compute_ruler_preview(
    req: RulerMetricsRequest,
    *,
    packs_dir: Path | None = None,
    pack_dir_finder: Any | None = None,
    elevation_source: Any | None = None,
) -> RulerMetricsResponse:
    """Compute geodesic distance, True/Grid/Mag bearings, mils, and optional DEM grade."""
    pts = [(float(p.lat), float(p.lon)) for p in req.points]

    # Sample elevation when an offline pack DEM, explicit elevation_source, or dem_source is supplied.
    wants_dem = bool(elevation_source is not None or req.pack_id is not None or req.dem_source is not None)
    if not wants_dem:
        return compute_ruler_metrics(pts, date_iso=req.date_iso)

    try:
        elev_src = (
            elevation_source
            if elevation_source is not None
            else _resolve_preview_elevation_source(
                req.dem_source or DemSourceKind.AUTO,
                pack_id=req.pack_id,
                packs_dir=packs_dir,
                pack_dir_finder=pack_dir_finder,
            )
        )
        fetch_layout, _ = _build_extent_fetch_layout(
            [p[0] for p in pts],
            [p[1] for p in pts],
            pad_m=500.0,
        )
        with tempfile.TemporaryDirectory(prefix="gf_ruler_") as tmp_s:
            raw_dem = elev_src.fetch_dem(fetch_layout, Path(tmp_s))
            return compute_ruler_metrics(pts, dem_path=Path(raw_dem), date_iso=req.date_iso)
    except Exception as exc:
        logger.debug("Ruler elevation lookup skipped (%s); returning pure geodesic metrics", exc)
        return compute_ruler_metrics(pts, date_iso=req.date_iso)
