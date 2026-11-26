"""Unit tests for v1.3.0 GDAL RF/LOS Viewshed, AU Tier-0 QLD 2 m LiDAR, and NZ LINZ/DOC sources."""

from __future__ import annotations

import json
import math
import shutil
import sqlite3
import struct
from pathlib import Path
from typing import Any

import httpx
import pytest

from gridforge.config import (
    BBox,
    ColorMode,
    FrameMarginsMM,
    NZOverlayKind,
    Orientation,
    PaperSize,
    SheetLayout,
    SheetSpec,
    Units,
    ViewshedObserver,
)
from gridforge.geo.crs import compute_sheet_layout
from gridforge.pipeline.terrain import (
    VIEWSHED_RGBA_BW,
    VIEWSHED_RGBA_COLOR,
    build_terrain_products,
    build_viewshed_products,
    compute_viewshed_geojson,
)
from gridforge.sources import (
    AutoDEMSource,
    AUElvisDEMSource,
    NZDocTrailsSource,
    NZLinzDEMSource,
    build_nz_overlay_sources,
    is_in_nz_coverage,
    is_in_nz_overlay_coverage,
)
from gridforge.sources.base import ElevationSource, OverlaySource
from gridforge.sources.dem_au_elvis import (
    QLD_DEM_IMAGESERVER_URL,
    QLD_LIDAR_ATTRIBUTION,
    QLD_LIDAR_EXTENTS_URL,
    is_in_qld_lidar_coverage,
    qld_export_size_for_bbox,
)
from gridforge.sources.dem_nz_linz import (
    linz_10k_tiles_for_nztm_bbox,
    topo50_sheets_for_nztm_bbox,
    wgs84_bbox_to_nztm2000,
)

HAS_GDAL = all(
    shutil.which(cmd) is not None
    for cmd in ("gdalwarp", "gdaldem", "gdal_contour", "gdal_viewshed", "ogr2ogr")
)


def _create_ridge_dem(
    path: Path,
    *,
    utm_epsg: int,
    bbox: BBox,
    pixel_m: float = 20.0,
    valley_m: float = 100.0,
    ridge_m: float = 900.0,
) -> tuple[int, int, tuple[float, float, float, float, float, float]]:
    """Create a synthetic UTM Float32 DEM with an East-West 900 m ridge bisecting two 100 m valleys."""
    import numpy as np  # type: ignore[import-not-found]
    from osgeo import gdal, osr  # type: ignore[import-untyped]

    gdal.UseExceptions()
    osr.UseExceptions()

    cols = int(math.ceil(bbox.width / pixel_m))
    rows = int(math.ceil(bbox.height / pixel_m))
    center_y = 0.5 * (bbox.min_y + bbox.max_y)
    mid_r = float(int(math.floor((bbox.max_y - center_y) / pixel_m)))
    half_w = max(8.0, rows * 0.14)

    rr = np.arange(rows, dtype=np.float32)[:, None]
    profile = valley_m + (ridge_m - valley_m) * np.maximum(0.0, 1.0 - np.abs(rr - mid_r) / half_w)
    elev = np.broadcast_to(profile, (rows, cols)).astype(np.float32).copy()

    srs = osr.SpatialReference()
    srs.ImportFromEPSG(utm_epsg)
    gt = (bbox.min_x, pixel_m, 0.0, bbox.max_y, 0.0, -pixel_m)
    ds = gdal.GetDriverByName("GTiff").Create(str(path), cols, rows, 1, gdal.GDT_Float32)
    ds.SetGeoTransform(gt)
    ds.SetProjection(srs.ExportToWkt())
    band = ds.GetRasterBand(1)
    band.SetNoDataValue(-9999.0)
    band.WriteArray(elev)
    band.FlushCache()
    ds = None
    return rows, cols, gt


def _utm_to_wgs84(utm_epsg: int, x: float, y: float) -> tuple[float, float]:
    from pyproj import Transformer

    tx = Transformer.from_crs(f"EPSG:{utm_epsg}", "EPSG:4326", always_xy=True)
    lon, lat = tx.transform(x, y)
    return float(lat), float(lon)


def _read_rgba_at_utm(tif_path: Path, x: float, y: float) -> tuple[int, int, int, int]:
    from osgeo import gdal  # type: ignore[import-untyped]

    gdal.UseExceptions()
    ds = gdal.Open(str(tif_path), gdal.GA_ReadOnly)
    assert ds is not None
    try:
        assert ds.RasterCount == 4
        gt = ds.GetGeoTransform()
        col = max(0, min(ds.RasterXSize - 1, int(math.floor((x - gt[0]) / gt[1]))))
        row = max(0, min(ds.RasterYSize - 1, int(math.floor((y - gt[3]) / gt[5]))))
        return tuple(int(ds.GetRasterBand(b).ReadAsArray(col, row, 1, 1)[0, 0]) for b in (1, 2, 3, 4))  # type: ignore[return-value]
    finally:
        ds = None


def _build_float32_tif_bytes(
    tmp_path: Path,
    name: str,
    bbox: BBox,
    *,
    epsg: int = 4326,
    size: int = 32,
    zero_fraction: float = 0.0,
    base_elev: float = 250.0,
    relief: float = 180.0,
) -> bytes:
    from osgeo import gdal, osr  # type: ignore[import-untyped]

    gdal.UseExceptions()
    out = tmp_path / name
    ds = gdal.GetDriverByName("GTiff").Create(str(out), size, size, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((bbox.min_x, bbox.width / size, 0.0, bbox.max_y, 0.0, -(bbox.height / size)))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(epsg)
    ds.SetProjection(srs.ExportToWkt())
    band = ds.GetRasterBand(1)
    band.SetNoDataValue(-9999.0)

    total = size * size
    n_zero = int(total * zero_fraction)
    vals = [
        0.0 if i < n_zero else base_elev + (i % size) / max(1, size - 1) * relief
        for i in range(total)
    ]
    band.WriteRaster(0, 0, size, size, struct.pack(f"={total}f", *vals))
    band.FlushCache()
    ds.FlushCache()
    ds = None
    return out.read_bytes()


# ---------------------------------------------------------------------------
# 1. GDAL RF / Line-of-Sight Viewshed (terrain.py)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not HAS_GDAL, reason="GDAL with gdal_viewshed required")
def test_viewshed_ridge_shadow_color_bw_multi_and_geojson(tmp_path: Path) -> None:
    layout = compute_sheet_layout(
        SheetSpec(
            center_lat=-36.456,
            center_lon=148.263,
            scale=25000,
            paper=PaperSize.A4,
            orientation=Orientation.PORTRAIT,
            color_mode=ColorMode.COLOR,
            units=Units.METRIC,
        )
    )
    b = layout.utm_fetch_bbox
    dem_path = tmp_path / "ridge_dem.tif"
    _create_ridge_dem(dem_path, utm_epsg=layout.utm_epsg, bbox=b, pixel_m=20.0)

    cx = layout.center_easting
    cy = layout.center_northing
    north_y = cy + 1600.0
    south_y = cy - 1600.0
    far_north_y = cy + 3200.0

    ridge_lat, ridge_lon = _utm_to_wgs84(layout.utm_epsg, cx, cy)
    north_lat, north_lon = _utm_to_wgs84(layout.utm_epsg, cx, north_y)
    south_lat, south_lon = _utm_to_wgs84(layout.utm_epsg, cx, south_y)

    # A. North valley observer (max_distance=4500m) sees North valley but South valley is shadowed by 900m ridge
    obs_north = ViewshedObserver(
        label="Valley North",
        lat=north_lat,
        lon=north_lon,
        observer_height_m=2.0,
        target_height_m=1.5,
        max_distance_m=4500.0,
    )
    vs_tif_n, obs_gpkg_n = build_viewshed_products(
        dem_path,
        [obs_north],
        layout.utm_epsg,
        ColorMode.COLOR,
        tmp_path / "vs_north",
    )
    assert vs_tif_n.is_file()
    assert obs_gpkg_n.is_file()
    assert list((tmp_path / "vs_north").glob("_viewshed_obs_*.tif")) == []

    # Visible near North observer; shadowed in South valley behind the ridge!
    assert _read_rgba_at_utm(vs_tif_n, cx + 200.0, north_y) == VIEWSHED_RGBA_COLOR
    assert _read_rgba_at_utm(vs_tif_n, cx, south_y) == (0, 0, 0, 0)

    # Verify observer & ring layers and schema in viewshed_observers.gpkg
    con = sqlite3.connect(str(obs_gpkg_n))
    try:
        layers = {r[0] for r in con.execute("SELECT table_name FROM gpkg_contents")}
        assert {"viewshed_observers", "viewshed_rings"}.issubset(layers)
        obs_rows = con.execute(
            "SELECT label, observer_height_m, target_height_m, max_distance_m, elev_m FROM viewshed_observers"
        ).fetchall()
        assert len(obs_rows) == 1
        assert obs_rows[0][0] == "Valley North"
        assert obs_rows[0][1] == pytest.approx(2.0)
        assert obs_rows[0][2] == pytest.approx(1.5)
        assert obs_rows[0][3] == pytest.approx(4500.0)
        assert 95.0 <= obs_rows[0][4] <= 125.0
        ring_rows = con.execute("SELECT label, max_distance_m FROM viewshed_rings").fetchall()
        assert len(ring_rows) >= 1
        assert ring_rows[0][0] == "Valley North"
    finally:
        con.close()

    # B. Ridge peak observer (max_distance=2200m) in B&W mode sees BOTH valleys within 2.2 km,
    #    and is transparent beyond 2.2 km (far_north_y is 3.2 km away)
    obs_ridge = ViewshedObserver(
        label="Repeater 1",
        lat=ridge_lat,
        lon=ridge_lon,
        observer_height_m=10.0,
        target_height_m=1.5,
        max_distance_m=2200.0,
    )
    vs_tif_bw, _ = build_viewshed_products(
        dem_path,
        [obs_ridge],
        layout.utm_epsg,
        ColorMode.BW,
        tmp_path / "vs_bw",
    )
    assert _read_rgba_at_utm(vs_tif_bw, cx, north_y) == VIEWSHED_RGBA_BW
    assert _read_rgba_at_utm(vs_tif_bw, cx, south_y) == VIEWSHED_RGBA_BW
    assert _read_rgba_at_utm(vs_tif_bw, cx, far_north_y) == (0, 0, 0, 0)

    # C. Multi-observer union (North valley + South valley -> both valleys visible!)
    obs_south = ViewshedObserver(
        label="Valley South",
        lat=south_lat,
        lon=south_lon,
        observer_height_m=2.0,
        target_height_m=1.5,
        max_distance_m=4500.0,
    )
    vs_tif_union, obs_gpkg_union = build_viewshed_products(
        dem_path,
        [obs_north, obs_south],
        layout.utm_epsg,
        ColorMode.COLOR,
        tmp_path / "vs_union",
    )
    assert _read_rgba_at_utm(vs_tif_union, cx + 200.0, north_y) == VIEWSHED_RGBA_COLOR
    assert _read_rgba_at_utm(vs_tif_union, cx + 200.0, south_y) == VIEWSHED_RGBA_COLOR
    con_u = sqlite3.connect(str(obs_gpkg_union))
    try:
        assert con_u.execute("SELECT COUNT(*) FROM viewshed_observers").fetchone()[0] == 2
    finally:
        con_u.close()

    # D. Polygonized WGS84 GeoJSON preview for POST /api/viewshed-preview
    preview_dir = tmp_path / "vs_preview"
    fc = compute_viewshed_geojson(dem_path, obs_ridge, layout.utm_epsg, preview_dir, preview_res_m=40.0)
    assert fc["type"] == "FeatureCollection"
    assert len(fc["features"]) >= 1
    assert fc["properties"]["visible_area_km2"] > 1.0
    assert fc["properties"]["observer_elev_m"] > 800.0
    first_geom = fc["features"][0]["geometry"]
    assert first_geom["type"] in ("Polygon", "MultiPolygon")
    sample_pt = first_geom["coordinates"][0][0]
    if isinstance(sample_pt[0], list):
        sample_pt = sample_pt[0]
    assert 147.0 < sample_pt[0] < 149.5
    assert -37.5 < sample_pt[1] < -35.5
    assert list(preview_dir.glob("_viewshed_preview_*")) == []

    # E. Out-of-DEM observer degrades gracefully without raising
    obs_far = ViewshedObserver(label="OutOfBounds", lat=-33.0, lon=151.0, max_distance_m=3000.0)
    fc_empty = compute_viewshed_geojson(dem_path, obs_far, layout.utm_epsg, preview_dir)
    assert fc_empty["features"] == []
    assert fc_empty["properties"]["visible_area_km2"] == 0.0

    # F. build_terrain_products wiring (off by default, populated when spec.viewsheds non-empty)
    res_off = build_terrain_products(dem_path, layout, tmp_path / "terrain_off")
    assert res_off.viewshed_tif is None
    assert res_off.viewshed_observers_gpkg is None

    layout_on = compute_sheet_layout(
        SheetSpec(
            center_lat=-36.456,
            center_lon=148.263,
            scale=25000,
            viewsheds=[obs_ridge],
        )
    )
    res_on = build_terrain_products(dem_path, layout_on, tmp_path / "terrain_on")
    assert res_on.viewshed_tif is not None and res_on.viewshed_tif.is_file()
    assert res_on.viewshed_observers_gpkg is not None and res_on.viewshed_observers_gpkg.is_file()


# ---------------------------------------------------------------------------
# 2. AU Tier-0 QLD 2 m LiDAR (dem_au_elvis.py)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not HAS_GDAL, reason="GDAL required for QLD GeoTIFF inspection")
def test_au_elvis_qld_tier0_lidar_and_fallback(tmp_path: Path) -> None:
    lamington = compute_sheet_layout(SheetSpec(center_lat=-28.200, center_lon=153.190, scale=25000))
    assert is_in_qld_lidar_coverage(lamington.wgs84_fetch_bbox)
    assert not is_in_qld_lidar_coverage(BBox(min_x=150.25, min_y=-33.76, max_x=150.36, max_y=-33.68))

    px_w, px_h = qld_export_size_for_bbox(lamington.wgs84_fetch_bbox)
    assert 64 <= px_w <= 2048 and 64 <= px_h <= 2048

    valid_qld_bytes = _build_float32_tif_bytes(
        tmp_path, "qld_valid.tif", lamington.wgs84_fetch_bbox, base_elev=540.0, relief=360.0
    )
    calls: list[str] = []

    def handler_qld_ok(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        if "ElevationProjectExtents" in url:
            return httpx.Response(
                200,
                json={"features": [{"attributes": {"project": "SEQ_2022", "res_value": "1m", "completed": "Yes"}}]},
            )
        if "QldDem/ImageServer" in url:
            return httpx.Response(200, content=valid_qld_bytes, headers={"content-type": "image/tiff"})
        raise AssertionError(f"Unexpected URL: {url}")

    with httpx.Client(transport=httpx.MockTransport(handler_qld_ok)) as client:
        src = AUElvisDEMSource(cache_dir=tmp_path / "cache_qld_ok", client=client)
        out1 = src.fetch_dem(lamington, tmp_path / "run_qld1")
        assert out1.is_file()
        assert src.resolved_tier == "qld-2m-lidar"
        assert src.resolved_vertical_datum == "AHD"
        assert src.resolved_attribution == QLD_LIDAR_ATTRIBUTION
        assert len(calls) == 2

        # Second call hits disk clip cache -> zero additional HTTP calls
        out2 = src.fetch_dem(lamington, tmp_path / "run_qld2")
        assert out2.is_file()
        assert src.resolved_tier == "qld-2m-lidar"
        assert len(calls) == 2

    # Incomplete QLD raster (25% inland 0.0 m abutting steep 400 m ridge) must fall back to GA 5 m WCS!
    corrupt_qld_bytes = _build_float32_tif_bytes(
        tmp_path,
        "qld_fringe.tif",
        lamington.wgs84_fetch_bbox,
        zero_fraction=0.25,
        base_elev=400.0,
        relief=200.0,
    )
    ga_5m_bytes = _build_float32_tif_bytes(
        tmp_path, "ga_5m_fallback.tif", lamington.wgs84_fetch_bbox, epsg=4283, base_elev=540.0, relief=360.0
    )

    def handler_qld_fallback(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "ElevationProjectExtents" in url:
            return httpx.Response(200, json={"features": [{"attributes": {"completed": "Yes"}}]})
        if "QldDem/ImageServer" in url:
            return httpx.Response(200, content=corrupt_qld_bytes, headers={"content-type": "image/tiff"})
        if "DEM_LiDAR_5m_2025" in url:
            return httpx.Response(200, content=ga_5m_bytes, headers={"content-type": "image/tiff"})
        raise AssertionError(f"Unexpected URL: {url}")

    with httpx.Client(transport=httpx.MockTransport(handler_qld_fallback)) as client2:
        src2 = AUElvisDEMSource(cache_dir=tmp_path / "cache_qld_fb", client=client2)
        out_fb = src2.fetch_dem(lamington, tmp_path / "run_qld_fb")
        assert out_fb.is_file()
        assert src2.resolved_tier == "5m-lidar"
        assert "Geoscience Australia" in src2.resolved_attribution


# ---------------------------------------------------------------------------
# 3. NZ LINZ DEM (dem_nz_linz.py) & 4-Way AutoDEMSource (dem_usgs3dep.py)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not HAS_GDAL, reason="GDAL/OSR required for NZTM2000 Topo50 math")
def test_nz_linz_topo50_10k_tile_math_and_coverage() -> None:
    # Coverage bounds check
    tongariro = compute_sheet_layout(SheetSpec(center_lat=-39.133, center_lon=175.642, scale=25000))
    mt_cook = compute_sheet_layout(SheetSpec(center_lat=-43.735, center_lon=170.096, scale=25000))
    milford = compute_sheet_layout(SheetSpec(center_lat=-44.671, center_lon=167.926, scale=25000))

    assert is_in_nz_coverage(tongariro.wgs84_fetch_bbox)
    assert is_in_nz_coverage(mt_cook.wgs84_fetch_bbox)
    assert is_in_nz_coverage(milford.wgs84_fetch_bbox)
    assert not is_in_nz_coverage(BBox(min_x=151.18, min_y=-33.89, max_x=151.25, max_y=-33.82))  # Sydney
    assert not is_in_nz_coverage(BBox(min_x=-105.33, min_y=39.96, max_x=-105.24, max_y=40.05))  # Colorado

    # Exact Topo50 sheets verified against live s3://nz-elevation
    t_nztm = wgs84_bbox_to_nztm2000(tongariro.wgs84_bbox)
    assert "BH35" in topo50_sheets_for_nztm_bbox(*t_nztm)
    t_10k = linz_10k_tiles_for_nztm_bbox(*t_nztm)
    assert any(t.startswith("BH35_10000_") for t in t_10k)

    mc_nztm = wgs84_bbox_to_nztm2000(mt_cook.wgs84_bbox)
    assert "BX15" in topo50_sheets_for_nztm_bbox(*mc_nztm)

    mf_nztm = wgs84_bbox_to_nztm2000(milford.wgs84_bbox)
    assert "CB08" in topo50_sheets_for_nztm_bbox(*mf_nztm)


@pytest.mark.skipif(not HAS_GDAL, reason="GDAL required for NZ LINZ DEM fallback test")
def test_nz_linz_tier_fallback_and_4way_auto_dem_routing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tongariro = compute_sheet_layout(SheetSpec(center_lat=-39.133, center_lon=175.642, scale=25000))
    dummy_tif = tmp_path / "dummy.tif"
    dummy_tif.write_bytes(
        _build_float32_tif_bytes(tmp_path, "nz_mock.tif", tongariro.wgs84_fetch_bbox, base_elev=1200.0, relief=700.0)
    )

    class _FakeCopernicus:
        name = "copernicus-glo30"
        attribution = "Copernicus GLO-30"

        def fetch_dem(self, layout: SheetLayout, work_dir: Path) -> Path:
            out = work_dir / "dem_raw.tif"
            shutil.copy2(dummy_tif, out)
            return out

    # When both 1m and 8m LINZ tiers fail, NZLinzDEMSource falls back to GLO-30 and updates datum to EGM2008!
    nz_src = NZLinzDEMSource(cache_dir=tmp_path / "nz_cache", copernicus_fallback=_FakeCopernicus())  # type: ignore[arg-type]
    assert isinstance(nz_src, ElevationSource)
    monkeypatch.setattr(nz_src, "_fetch_1m_lidar", lambda *a, **kw: False)
    monkeypatch.setattr(nz_src, "_fetch_8m_dem", lambda *a, **kw: False)

    out_glo = nz_src.fetch_dem(tongariro, tmp_path / "nz_fallback_work")
    assert out_glo.is_file()
    assert nz_src.resolved_tier == "glo30-fallback"
    assert nz_src.resolved_source_name == "copernicus-glo30"
    assert nz_src.resolved_vertical_datum == "EGM2008"

    # 4-way AutoDEMSource routing (US -> AU -> NZ -> Global)
    calls: list[str] = []

    class _StubElev:
        def __init__(self, name: str, datum: str, attr: str) -> None:
            self.name = name
            self.vertical_datum = datum
            self.attribution = attr
            self.resolved_source_name = name
            self.resolved_vertical_datum = datum
            self.resolved_attribution = attr

        def fetch_dem(self, layout: SheetLayout, work_dir: Path) -> Path:
            calls.append(self.name)
            work_dir.mkdir(parents=True, exist_ok=True)
            out = work_dir / "dem_raw.tif"
            shutil.copy2(dummy_tif, out)
            return out

    auto = AutoDEMSource(
        cache_dir=tmp_path / "auto_cache",
        usgs_source=_StubElev("usgs-3dep", "NAVD88", "USGS 3DEP"),  # type: ignore[arg-type]
        au_source=_StubElev("au-elvis", "AHD", QLD_LIDAR_ATTRIBUTION),  # type: ignore[arg-type]
        nz_source=_StubElev("nz-linz", "NZVD2016", "LINZ NZVD2016"),  # type: ignore[arg-type]
        copernicus_source=_StubElev("copernicus-glo30", "EGM2008", "Copernicus"),  # type: ignore[arg-type]
    )

    us_layout = compute_sheet_layout(SheetSpec(center_lat=39.99, center_lon=-105.28, scale=25000))
    au_layout = compute_sheet_layout(SheetSpec(center_lat=-28.20, center_lon=153.19, scale=25000))
    eu_layout = compute_sheet_layout(SheetSpec(center_lat=46.55, center_lon=7.98, scale=25000))

    auto.fetch_dem(us_layout, tmp_path / "auto_us")
    assert auto.resolved_source_name == "usgs-3dep" and auto.resolved_vertical_datum == "NAVD88"

    auto.fetch_dem(au_layout, tmp_path / "auto_au")
    assert auto.resolved_source_name == "au-elvis" and auto.resolved_vertical_datum == "AHD"
    assert auto.resolved_attribution == QLD_LIDAR_ATTRIBUTION

    auto.fetch_dem(tongariro, tmp_path / "auto_nz")
    assert auto.resolved_source_name == "nz-linz" and auto.resolved_vertical_datum == "NZVD2016"
    assert auto.resolved_attribution == "LINZ NZVD2016"

    auto.fetch_dem(eu_layout, tmp_path / "auto_eu")
    assert auto.resolved_source_name == "copernicus-glo30" and auto.resolved_vertical_datum == "EGM2008"
    assert calls == ["usgs-3dep", "au-elvis", "nz-linz", "copernicus-glo30"]


# ---------------------------------------------------------------------------
# 4. New Zealand DOC & LINZ Overlays (overlays_nz.py)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not HAS_GDAL, reason="GDAL/OGR required for NZ GeoPackage normalization")
def test_nz_doc_trails_normalization_bounds_and_local_gpkg_override(tmp_path: Path) -> None:
    tongariro = compute_sheet_layout(SheetSpec(center_lat=-39.133, center_lon=175.642, scale=25000))
    katoomba = compute_sheet_layout(SheetSpec(center_lat=-33.715, center_lon=150.312, scale=25000))

    assert is_in_nz_overlay_coverage(tongariro.wgs84_fetch_bbox)
    assert not is_in_nz_overlay_coverage(katoomba.wgs84_fetch_bbox)

    def mock_nz_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "DOC_Tracks_Open_Data" in url:
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {
                                "TechObjectName": "Tongariro Alpine Crossing",
                                "ObjectType": "Track",
                                "SubObjectType": "Great Walk",
                            },
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[175.62, -39.14], [175.64, -39.13], [175.66, -39.12]],
                            },
                        },
                        {
                            "type": "Feature",
                            "properties": {
                                "TechObjectName": "Tama Lakes Track",
                                "ObjectType": "Track",
                                "SubObjectType": "Tramping Track",
                            },
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[175.61, -39.15], [175.63, -39.14]],
                            },
                        },
                    ],
                },
            )
        if "DOC_Roads_Open_Data" in url:
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {
                                "TechObjectName": "Mangatepopo Road",
                                "ObjectType": "Road",
                                "SubObjectType": "4WD Track",
                            },
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[175.59, -39.14], [175.61, -39.14]],
                            },
                        }
                    ],
                },
            )
        if "DOC_Huts_Open_Data" in url:
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {
                                "TechObjectName": "Mangatepopo Hut",
                                "ObjectType": "Hut",
                                "SubObjectType": "Great Walk Hut",
                            },
                            "geometry": {"type": "Point", "coordinates": [175.615, -39.141]},
                        }
                    ],
                },
            )
        if "DOC_Campsites_Open_Data" in url:
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {
                                "TechObjectName": "Mangatepopo Campsite",
                                "ObjectType": "Campsite",
                                "SubObjectType": "Standard",
                            },
                            "geometry": {"type": "Point", "coordinates": [175.618, -39.142]},
                        }
                    ],
                },
            )
        if "NZ_Maunga" in url:
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {"name": "Mount Ngauruhoe", "elevation": 2291},
                            "geometry": {"type": "Point", "coordinates": [175.632, -39.156]},
                        }
                    ],
                },
            )
        if "data.linz.govt.nz" in url:
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {"current_mark_name": "Tongariro Trig", "geodetic_code": "A123", "height": 1967},
                            "geometry": {"type": "Point", "coordinates": [175.642, -39.133]},
                        }
                    ],
                },
            )
        raise AssertionError(f"Unexpected NZ overlay URL: {url}")

    with httpx.Client(transport=httpx.MockTransport(mock_nz_handler)) as client:
        src = NZDocTrailsSource(
            cache_dir=tmp_path / "nz_ov_cache",
            linz_api_key="test-key",
            client=client,
        )
        assert isinstance(src, OverlaySource)

        # Outside NZ returns None cleanly with zero HTTP calls
        assert src.fetch_overlay(katoomba, tmp_path / "au_skip") is None

        gpkg = src.fetch_overlay(tongariro, tmp_path / "nz_run")
        assert gpkg is not None and gpkg.is_file()

        con = sqlite3.connect(str(gpkg))
        try:
            trails = con.execute(
                "SELECT name, trail_class, surface, state FROM au_state_trails ORDER BY name"
            ).fetchall()
            pois = con.execute(
                "SELECT name, poi_type, elev_m, state FROM au_state_pois ORDER BY name"
            ).fetchall()
        finally:
            con.close()

        assert len(trails) == 3
        by_name = {r[0]: (r[1], r[3]) for r in trails}
        assert by_name["Tongariro Alpine Crossing"] == ("walking_track", "NZ")
        assert by_name["Tama Lakes Track"] == ("walking_track", "NZ")
        assert by_name["Mangatepopo Road"] == ("4wd_track", "NZ")

        assert len(pois) == 4
        poi_by_name = {r[0]: (r[1], r[2], r[3]) for r in pois}
        assert poi_by_name["Mangatepopo Hut"][0] == "hut"
        assert poi_by_name["Mangatepopo Hut"][2] == "NZ"
        assert poi_by_name["Mangatepopo Campsite"][0] == "campground"
        assert poi_by_name["Mount Ngauruhoe"][0] == "trig"
        assert poi_by_name["Mount Ngauruhoe"][1] == pytest.approx(2291.0)
        assert poi_by_name["Tongariro Trig"][0] == "trig"
        assert poi_by_name["Tongariro Trig"][1] == pytest.approx(1967.0)

        # Verify factory + local_gpkg override works air-gapped even outside NZ coverage!
        built = build_nz_overlay_sources(
            [NZOverlayKind.NZ_DOC_TRAILS],
            local_overrides={NZOverlayKind.NZ_DOC_TRAILS: gpkg},
        )
        assert list(built.keys()) == ["nz-doc-trails"]
        # Clipping our Tongariro gpkg onto the Tongariro layout locally succeeds without network
        local_out = built["nz-doc-trails"].fetch_overlay(tongariro, tmp_path / "nz_local_run")
        assert local_out is not None and local_out.is_file()
