"""Unit tests for v1.4.0 RF/Fresnel Link Profile, 5-way AutoDEM, Canada HRDEM, and US/CA overlays."""

from __future__ import annotations

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
    CAOverlayKind,
    ColorMode,
    RFLinkSpec,
    SheetLayout,
    SheetSpec,
    USOverlayKind,
    ViewshedObserver,
)
from gridforge.geo.crs import compute_sheet_layout
from gridforge.pipeline.terrain import (
    VIEWSHED_RGBA_NIGHT_RED,
    build_slope_shading,
    build_viewshed_products,
    compute_rf_link_profile,
)
from gridforge.sources import (
    AutoDEMSource,
    CACanVecTrailsSource,
    CanadaHRDEMSource,
    USPublicLandsSource,
    USTrailsSource,
    build_ca_overlay_sources,
    build_us_overlay_sources,
    classify_north_america_country,
    is_in_ca_overlay_coverage,
    is_in_canada_coverage,
    is_in_us_3dep_coverage,
)
from gridforge.sources.base import ElevationSource, OverlaySource
from gridforge.sources.dem_ca_hrdem import extract_stac_cog_hrefs

HAS_GDAL = all(
    shutil.which(cmd) is not None
    for cmd in ("gdalwarp", "gdaldem", "gdal_contour", "gdal_viewshed", "ogr2ogr")
)


def _create_profile_dem(
    path: Path,
    *,
    utm_epsg: int,
    bbox: BBox,
    center_hump_m: float,
    base_m: float = 500.0,
    pixel_m: float = 20.0,
) -> Path:
    """Create a synthetic UTM DEM with flat endpoints at ``base_m`` and a central E-W ridge of ``center_hump_m``."""
    import numpy as np  # type: ignore[import-not-found]
    from osgeo import gdal, osr  # type: ignore[import-untyped]

    gdal.UseExceptions()
    osr.UseExceptions()
    cols = int(math.ceil(bbox.width / pixel_m))
    rows = int(math.ceil(bbox.height / pixel_m))
    mid_r = rows / 2.0
    sigma_r = max(2.0, rows * 0.04)

    rr = np.arange(rows, dtype=np.float32)[:, None]
    profile = base_m + center_hump_m * np.exp(-0.5 * ((rr - mid_r) / sigma_r) ** 2)
    elev = np.broadcast_to(profile, (rows, cols)).astype(np.float32).copy()

    srs = osr.SpatialReference()
    srs.ImportFromEPSG(utm_epsg)
    ds = gdal.GetDriverByName("GTiff").Create(str(path), cols, rows, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((bbox.min_x, pixel_m, 0.0, bbox.max_y, 0.0, -pixel_m))
    ds.SetProjection(srs.ExportToWkt())
    band = ds.GetRasterBand(1)
    band.SetNoDataValue(-9999.0)
    band.WriteArray(elev)
    band.FlushCache()
    ds = None
    return path


def _utm_to_wgs84(utm_epsg: int, x: float, y: float) -> tuple[float, float]:
    from pyproj import Transformer

    tx = Transformer.from_crs(f"EPSG:{utm_epsg}", "EPSG:4326", always_xy=True)
    lon, lat = tx.transform(x, y)
    return float(lat), float(lon)


def _small_bbox(lat: float, lon: float, d: float = 0.03) -> BBox:
    return BBox(min_x=lon - d, min_y=lat - d, max_x=lon + d, max_y=lat + d)


# ---------------------------------------------------------------------------
# 1. RF Point-to-Point Link Profile & Night-Red / Alpine Terrain Modes
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not HAS_GDAL, reason="GDAL required for RF link profile tests")
def test_rf_link_profile_clear_marginal_obstructed_and_fresnel_physics(tmp_path: Path) -> None:
    layout = compute_sheet_layout(SheetSpec(center_lat=-36.456, center_lon=148.263, scale=25000))
    cx, cy = layout.center_easting, layout.center_northing
    lat_a, lon_a = _utm_to_wgs84(layout.utm_epsg, cx, cy - 2000.0)
    lat_b, lon_b = _utm_to_wgs84(layout.utm_epsg, cx, cy + 2000.0)

    # A. CLEAR link (flat valley with 30 m masts -> wide clearance > 60% of 1st Fresnel zone at 915 MHz)
    dem_clear = _create_profile_dem(
        tmp_path / "dem_clear.tif",
        utm_epsg=layout.utm_epsg,
        bbox=layout.utm_fetch_bbox,
        center_hump_m=0.0,
    )
    link_915 = RFLinkSpec(
        from_lat=lat_a,
        from_lon=lon_a,
        to_lat=lat_b,
        to_lon=lon_b,
        from_height_m=30.0,
        to_height_m=30.0,
        freq_mhz=915.0,
        from_label="South Peak",
        to_label="North Relay",
    )
    prof_clear = compute_rf_link_profile(dem_clear, link_915, layout.utm_epsg, date_iso="2026-06-01")
    assert prof_clear["status"] == "CLEAR"
    assert prof_clear["los_clear"] is True
    assert prof_clear["fresnel_60pct_clear"] is True
    assert 3.9 <= prof_clear["distance_km"] <= 4.1
    # Theoretical FSPL for 4.0 km @ 915 MHz = 20*log10(4) + 20*log10(915) + 32.45 ≈ 103.72 dB
    expected_fspl = 20.0 * math.log10(prof_clear["distance_km"]) + 20.0 * math.log10(915.0) + 32.44778
    assert prof_clear["fspl_db"] == pytest.approx(expected_fspl, abs=0.15)
    assert prof_clear["min_fresnel_pct"] > 100.0
    assert len(prof_clear["samples"]) == 200
    assert prof_clear["from_mgrs"].startswith("55H ")
    assert prof_clear["to_mgrs"].startswith("55H ")
    assert 0.0 <= prof_clear["bearing_true_deg"] < 360.0
    assert 0.0 <= prof_clear["bearing_grid_deg"] < 360.0
    assert 0.0 <= prof_clear["bearing_mag_deg"] < 360.0
    assert 0.0 <= prof_clear["bearing_mils"] < 6400.0

    # Check Fresnel scaling with frequency (146 MHz vs 5800 MHz at midpoint)
    link_vhf = link_915.model_copy(update={"freq_mhz": 146.0})
    link_5g = link_915.model_copy(update={"freq_mhz": 5800.0})
    p_vhf = compute_rf_link_profile(dem_clear, link_vhf, layout.utm_epsg, date_iso="2026-06-01")
    p_5g = compute_rf_link_profile(dem_clear, link_5g, layout.utm_epsg, date_iso="2026-06-01")
    mid_vhf = p_vhf["samples"][100]
    mid_5g = p_5g["samples"][100]
    r1_vhf = mid_vhf["los_m"] - mid_vhf["fresnel_lo_m"]
    r1_5g = mid_5g["los_m"] - mid_5g["fresnel_lo_m"]
    assert (r1_vhf / r1_5g) == pytest.approx(math.sqrt(5800.0 / 146.0), rel=0.02)

    # B. MARGINAL link (hump 26 m high -> LOS clears by ~4 m, but < 60% of 18 m F1 radius -> MARGINAL)
    dem_marg = _create_profile_dem(
        tmp_path / "dem_marg.tif",
        utm_epsg=layout.utm_epsg,
        bbox=layout.utm_fetch_bbox,
        center_hump_m=26.0,
    )
    prof_marg = compute_rf_link_profile(dem_marg, link_915, layout.utm_epsg, date_iso="2026-06-01")
    assert prof_marg["status"] == "MARGINAL"
    assert prof_marg["los_clear"] is True
    assert prof_marg["fresnel_60pct_clear"] is False
    assert 0.0 < prof_marg["min_clearance_m"] < 8.0
    assert 0.0 < prof_marg["min_fresnel_pct"] < 60.0

    # C. OBSTRUCTED link (central ridge 120 m above baseline -> blocks LOS!)
    dem_obs = _create_profile_dem(
        tmp_path / "dem_obs.tif",
        utm_epsg=layout.utm_epsg,
        bbox=layout.utm_fetch_bbox,
        center_hump_m=120.0,
    )
    prof_obs = compute_rf_link_profile(dem_obs, link_915, layout.utm_epsg, date_iso="2026-06-01")
    assert prof_obs["status"] == "OBSTRUCTED"
    assert prof_obs["los_clear"] is False
    assert prof_obs["fresnel_60pct_clear"] is False
    assert prof_obs["min_clearance_m"] < -50.0

    # D. NIGHT_RED and ALPINE support in slope shading & viewshed products
    slope_nr = build_slope_shading(dem_obs, ColorMode.NIGHT_RED, tmp_path / "slope_nr")
    slope_alp = build_slope_shading(dem_obs, ColorMode.ALPINE, tmp_path / "slope_alp")
    assert slope_nr.is_file() and slope_alp.is_file()

    obs = ViewshedObserver(label="Night Relay", lat=lat_a, lon=lon_a, observer_height_m=10.0, max_distance_m=2000.0)
    vs_nr, _ = build_viewshed_products(dem_clear, [obs], layout.utm_epsg, ColorMode.NIGHT_RED, tmp_path / "vs_nr")
    from osgeo import gdal  # type: ignore[import-untyped]

    ds = gdal.Open(str(vs_nr), gdal.GA_ReadOnly)
    assert ds is not None and ds.RasterCount == 4
    gt = ds.GetGeoTransform()
    col = int(math.floor((cx - gt[0]) / gt[1]))
    row = int(math.floor(((cy - 1800.0) - gt[3]) / gt[5]))
    px = tuple(int(ds.GetRasterBand(b).ReadAsArray(col, row, 1, 1)[0, 0]) for b in (1, 2, 3, 4))
    ds = None
    assert px == VIEWSHED_RGBA_NIGHT_RED


# ---------------------------------------------------------------------------
# 2. Accurate US/Canada Border & 5-Way AutoDEMSource
# ---------------------------------------------------------------------------


def test_north_america_border_classifier_and_5way_auto_dem(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ca_cities = [
        ("Victoria BC", 48.43, -123.37),
        ("Vancouver BC", 49.28, -123.12),
        ("Squamish BC", 49.70, -123.15),
        ("Banff AB", 51.18, -115.57),
        ("Toronto ON", 43.65, -79.38),
        ("Algonquin ON", 45.58, -78.36),
        ("Ottawa ON", 45.42, -75.70),
        ("Montreal QC", 45.50, -73.57),
        ("Quebec City QC", 46.81, -71.21),
        ("Halifax NS", 44.65, -63.57),
        ("Whitehorse YT", 60.72, -135.05),
    ]
    for name, lat, lon in ca_cities:
        assert classify_north_america_country(lat, lon) == "CA", f"{name} should classify as CA"
        bb = _small_bbox(lat, lon)
        assert is_in_canada_coverage(bb), f"{name} should be in Canada coverage"
        assert not is_in_us_3dep_coverage(bb), f"{name} must NOT match US 3DEP coverage"

    us_cities = [
        ("Seattle WA", 47.61, -122.33),
        ("Glacier NP MT", 48.70, -113.79),
        ("Boulder CO", 40.00, -105.28),
        ("Yosemite CA", 37.75, -119.59),
        ("Detroit MI", 42.33, -83.05),
        ("Buffalo NY", 42.89, -78.88),
        ("Burlington VT", 44.48, -73.21),
        ("Acadia ME", 44.34, -68.27),
        ("Juneau AK", 58.30, -134.42),
        ("Anchorage AK", 61.22, -149.90),
        ("Honolulu HI", 21.31, -157.86),
        ("San Juan PR", 18.47, -66.11),
    ]
    for name, lat, lon in us_cities:
        assert classify_north_america_country(lat, lon) == "US", f"{name} should classify as US"
        bb = _small_bbox(lat, lon)
        assert is_in_us_3dep_coverage(bb), f"{name} should be in US coverage"

    assert classify_north_america_country(-33.86, 151.21) is None
    assert classify_north_america_country(-39.13, 175.64) is None
    assert classify_north_america_country(46.02, 7.75) is None

    # STAC COG extraction helper
    stac = {
        "features": [
            {"assets": {"dtm": {"href": "https://canelevation-dem.s3.ca-central-1.amazonaws.com/a.tif"}}},
            {"assets": {"dtm": {"href": "https://canelevation-dem.s3.ca-central-1.amazonaws.com/a.tif"}}},
            {"assets": {"dem": {"href": "https://canelevation-dem.s3.ca-central-1.amazonaws.com/b.tif"}}},
        ]
    }
    assert extract_stac_cog_hrefs(stac) == [
        "https://canelevation-dem.s3.ca-central-1.amazonaws.com/a.tif",
        "https://canelevation-dem.s3.ca-central-1.amazonaws.com/b.tif",
    ]

    # CanadaHRDEMSource protocol & GLO-30 datum fallback
    dummy_tif = tmp_path / "dummy.tif"
    dummy_tif.write_bytes(b"FAKE")

    class _FakeCopernicus:
        name = "copernicus-glo30"
        attribution = "Copernicus GLO-30"

        def fetch_dem(self, layout: SheetLayout, work_dir: Path) -> Path:
            work_dir.mkdir(parents=True, exist_ok=True)
            out = work_dir / "dem_raw.tif"
            shutil.copy2(dummy_tif, out)
            return out

    ca_src = CanadaHRDEMSource(cache_dir=tmp_path / "ca_cache", copernicus_fallback=_FakeCopernicus())  # type: ignore[arg-type]
    assert isinstance(ca_src, ElevationSource)
    assert ca_src.vertical_datum == "CGVD2013"
    banff = compute_sheet_layout(SheetSpec(center_lat=51.178, center_lon=-115.571, scale=25000))
    monkeypatch.setattr(ca_src, "_fetch_hrdem_tier", lambda *a, **kw: False)
    monkeypatch.setattr(ca_src, "_fetch_mrdem_30", lambda *a, **kw: False)
    ca_src.fetch_dem(banff, tmp_path / "ca_fb")
    assert ca_src.resolved_tier == "glo30-fallback"
    assert ca_src.resolved_source_name == "copernicus-glo30"
    assert ca_src.resolved_vertical_datum == "EGM2008"

    # 5-way AutoDEMSource routing (US -> CA -> AU -> NZ -> Global)
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
        cache_dir=tmp_path / "auto5",
        usgs_source=_StubElev("usgs-3dep", "NAVD88", "USGS"),  # type: ignore[arg-type]
        ca_source=_StubElev("ca-hrdem", "CGVD2013", "NRCan"),  # type: ignore[arg-type]
        au_source=_StubElev("au-elvis", "AHD", "GA"),  # type: ignore[arg-type]
        nz_source=_StubElev("nz-linz", "NZVD2016", "LINZ"),  # type: ignore[arg-type]
        copernicus_source=_StubElev("copernicus-glo30", "EGM2008", "Copernicus"),  # type: ignore[arg-type]
    )
    us_l = compute_sheet_layout(SheetSpec(center_lat=40.005, center_lon=-105.285, scale=25000))
    ca_l = compute_sheet_layout(SheetSpec(center_lat=49.701, center_lon=-123.155, scale=25000))
    au_l = compute_sheet_layout(SheetSpec(center_lat=-33.715, center_lon=150.312, scale=25000))
    nz_l = compute_sheet_layout(SheetSpec(center_lat=-39.133, center_lon=175.642, scale=25000))
    eu_l = compute_sheet_layout(SheetSpec(center_lat=46.55, center_lon=7.98, scale=25000))

    auto.fetch_dem(us_l, tmp_path / "r_us")
    assert auto.resolved_source_name == "usgs-3dep" and auto.resolved_vertical_datum == "NAVD88"
    auto.fetch_dem(ca_l, tmp_path / "r_ca")
    assert auto.resolved_source_name == "ca-hrdem" and auto.resolved_vertical_datum == "CGVD2013"
    auto.fetch_dem(au_l, tmp_path / "r_au")
    assert auto.resolved_source_name == "au-elvis" and auto.resolved_vertical_datum == "AHD"
    auto.fetch_dem(nz_l, tmp_path / "r_nz")
    assert auto.resolved_source_name == "nz-linz" and auto.resolved_vertical_datum == "NZVD2016"
    auto.fetch_dem(eu_l, tmp_path / "r_eu")
    assert auto.resolved_source_name == "copernicus-glo30" and auto.resolved_vertical_datum == "EGM2008"
    assert calls == ["usgs-3dep", "ca-hrdem", "au-elvis", "nz-linz", "copernicus-glo30"]


# ---------------------------------------------------------------------------
# 3. US Trails & US Public Lands Overlays (overlays_us.py)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not HAS_GDAL, reason="GDAL/OGR required for US overlay tests")
def test_us_trails_and_public_lands_normalization_and_local_override(tmp_path: Path) -> None:
    yosemite = compute_sheet_layout(SheetSpec(center_lat=37.745, center_lon=-119.593, scale=25000))
    banff = compute_sheet_layout(SheetSpec(center_lat=51.178, center_lon=-115.571, scale=25000))

    def mock_us_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "EDW_TrailNFSPublish_01" in url:
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {"trail_name": "JOHN MUIR TRAIL", "trail_type": "TERRA", "trail_class": "3"},
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[-119.60, 37.74], [-119.58, 37.75]],
                            },
                        }
                    ],
                },
            )
        if "NPS_Public_Trails" in url:
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {
                                "MAPLABEL": "Mist Trail",
                                "TRLSURFACE": "Granite",
                                "TRLUSE": "Hiker/Pedestrian",
                                "TRLTYPE": "Standard Terra Trail",
                            },
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[-119.59, 37.73], [-119.57, 37.74]],
                            },
                        }
                    ],
                },
            )
        if "EDW_RecreationOpportunities_01" in url:
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {
                                "recareaname": "Upper Pines Campground",
                                "markeractivity": "Camping",
                                "markeractivitygroup": "Camping",
                            },
                            "geometry": {"type": "Point", "coordinates": [-119.575, 37.742]},
                        }
                    ],
                },
            )
        if "NPS_Public_POIs_Geographic" in url:
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {"MAPLABEL": "Glacier Point Overlook", "POITYPE": "Overlook"},
                            "geometry": {"type": "Point", "coordinates": [-119.573, 37.731]},
                        }
                    ],
                },
            )
        if "EDW_Wilderness_01" in url:
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {"wildernessname": "Yosemite Wilderness"},
                            "geometry": {
                                "type": "Polygon",
                                "coordinates": [[[-119.62, 37.72], [-119.56, 37.72], [-119.56, 37.76], [-119.62, 37.76], [-119.62, 37.72]]],
                            },
                        }
                    ],
                },
            )
        if "Federal_Fee_Managers_Authoritative_PADUS" in url:
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {"Unit_Nm": "Yosemite National Park", "Mang_Name": "NPS", "Loc_Ds": "National Park"},
                            "geometry": {
                                "type": "Polygon",
                                "coordinates": [[[-119.63, 37.71], [-119.55, 37.71], [-119.55, 37.77], [-119.63, 37.77], [-119.63, 37.71]]],
                            },
                        }
                    ],
                },
            )
        if "PADUS_Public_Access" in url:
            return httpx.Response(200, json={"type": "FeatureCollection", "features": []})
        raise AssertionError(f"Unexpected US URL: {url}")

    with httpx.Client(transport=httpx.MockTransport(mock_us_handler)) as client:
        trails_src = USTrailsSource(cache_dir=tmp_path / "us_cache", client=client)
        lands_src = USPublicLandsSource(cache_dir=tmp_path / "us_cache", client=client)
        assert isinstance(trails_src, OverlaySource)
        assert isinstance(lands_src, OverlaySource)

        # Outside US (Banff Canada) short-circuits cleanly without HTTP
        assert trails_src.fetch_overlay(banff, tmp_path / "us_skip_t") is None
        assert lands_src.fetch_overlay(banff, tmp_path / "us_skip_l") is None

        trails_gpkg = trails_src.fetch_overlay(yosemite, tmp_path / "us_trails_run")
        assert trails_gpkg is not None and trails_gpkg.is_file()
        con = sqlite3.connect(str(trails_gpkg))
        try:
            t_rows = con.execute("SELECT name, trail_class, state FROM au_state_trails ORDER BY name").fetchall()
            p_rows = con.execute("SELECT name, poi_type, state FROM au_state_pois ORDER BY name").fetchall()
        finally:
            con.close()
        assert len(t_rows) == 2
        assert all(r[2] == "US" for r in t_rows)
        assert {r[0] for r in t_rows} == {"John Muir Trail", "Mist Trail"}
        assert dict((r[0], r[1]) for r in p_rows) == {
            "Glacier Point Overlook": "lookout",
            "Upper Pines Campground": "campground",
        }

        lands_gpkg = lands_src.fetch_overlay(yosemite, tmp_path / "us_lands_run")
        assert lands_gpkg is not None and lands_gpkg.is_file()
        con_l = sqlite3.connect(str(lands_gpkg))
        try:
            l_rows = con_l.execute("SELECT name, manager, designation, state FROM public_lands ORDER BY name").fetchall()
        finally:
            con_l.close()
        assert l_rows == [
            ("Yosemite National Park", "NPS", "National Park", "US"),
            ("Yosemite Wilderness", "USFS", "Wilderness Area", "US"),
        ]

        # Factory + air-gapped local_gpkg override
        built = build_us_overlay_sources(
            [USOverlayKind.US_TRAILS, USOverlayKind.US_PUBLIC_LANDS],
            local_overrides={
                USOverlayKind.US_TRAILS: trails_gpkg,
                USOverlayKind.US_PUBLIC_LANDS: lands_gpkg,
            },
        )
        assert set(built.keys()) == {"us-trails", "us-public-lands"}
        assert built["us-trails"].fetch_overlay(yosemite, tmp_path / "us_t_loc") is not None
        assert built["us-public-lands"].fetch_overlay(yosemite, tmp_path / "us_l_loc") is not None


# ---------------------------------------------------------------------------
# 4. Canada Trails, POIs & CPCAD Protected Areas (overlays_ca.py)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not HAS_GDAL, reason="GDAL/OGR required for Canada overlay tests")
def test_ca_canvec_trails_pois_cpcad_and_local_override(tmp_path: Path) -> None:
    squamish = compute_sheet_layout(SheetSpec(center_lat=49.701, center_lon=-123.155, scale=25000))
    boulder = compute_sheet_layout(SheetSpec(center_lat=40.005, center_lon=-105.285, scale=25000))
    assert is_in_ca_overlay_coverage(squamish.wgs84_fetch_bbox)
    assert not is_in_ca_overlay_coverage(boulder.wgs84_fetch_bbox)

    def mock_ca_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "vw_Trails_Sentiers_APCA_V2_FGP" in url:
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {"Label_e_20k_5k": "Tunnel Bench Loop"},
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[-123.16, 49.69], [-123.14, 49.71]],
                            },
                        }
                    ],
                },
            )
        if "openmaps.gov.bc.ca" in url:
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {
                                "PROJECT_NAME": "Stawamus Chief Trail",
                                "PROJECT_TYPE": "Recreation Trail Reserve",
                                "LIFE_CYCLE_STATUS_CODE": "ACTIVE",
                            },
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[-123.155, 49.685], [-123.145, 49.695]],
                            },
                        },
                        {
                            "type": "Feature",
                            "properties": {
                                "PROJECT_NAME": "Old Retired Trail",
                                "LIFE_CYCLE_STATUS_CODE": "RETIRED",
                            },
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[-123.16, 49.70], [-123.15, 49.70]],
                            },
                        },
                    ],
                },
            )
        if "Accommodation_Hebergement_APCA_OpenOuvert" in url:
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {"Name_e": "Elfin Lakes Shelter", "Descr_e": "Backcountry alpine hut"},
                            "geometry": {"type": "Point", "coordinates": [-123.150, 49.702]},
                        }
                    ],
                },
            )
        if "Facilities_Installations_Point_OpenData" in url:
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {"Name_e": "Chief Trailhead", "Label_e": "Parking / Trailhead"},
                            "geometry": {"type": "Point", "coordinates": [-123.158, 49.692]},
                        }
                    ],
                },
            )
        if "Interest_Point_Interet_APCA_OpenOuvert" in url:
            return httpx.Response(200, json={"type": "FeatureCollection", "features": []})
        if "CWS_SCF/CPCAD" in url:
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {"NAME_E": "Garibaldi Provincial Park", "IUCN_CAT": 2},
                            "geometry": {
                                "type": "Polygon",
                                "coordinates": [[[-123.18, 49.67], [-123.12, 49.67], [-123.12, 49.73], [-123.18, 49.73], [-123.18, 49.67]]],
                            },
                        }
                    ],
                },
            )
        raise AssertionError(f"Unexpected CA URL: {url}")

    with httpx.Client(transport=httpx.MockTransport(mock_ca_handler)) as client:
        src = CACanVecTrailsSource(cache_dir=tmp_path / "ca_ov_cache", client=client)
        assert isinstance(src, OverlaySource)
        assert src.fetch_overlay(boulder, tmp_path / "ca_skip") is None

        gpkg = src.fetch_overlay(squamish, tmp_path / "ca_run")
        assert gpkg is not None and gpkg.is_file()
        con = sqlite3.connect(str(gpkg))
        try:
            trails = con.execute("SELECT name, trail_class, state FROM au_state_trails ORDER BY name").fetchall()
            pois = con.execute("SELECT name, poi_type, state FROM au_state_pois ORDER BY name").fetchall()
            lands = con.execute("SELECT name, manager, designation, state FROM public_lands ORDER BY name").fetchall()
        finally:
            con.close()

        # Retired DataBC trail excluded!
        assert trails == [
            ("Stawamus Chief Trail", "walking_track", "CA"),
            ("Tunnel Bench Loop", "walking_track", "CA"),
        ]
        assert pois == [
            ("Chief Trailhead", "trailhead", "CA"),
            ("Elfin Lakes Shelter", "hut", "CA"),
        ]
        assert lands == [
            ("Garibaldi Provincial Park", "Provincial Parks", "Provincial Park", "CA"),
        ]

        built = build_ca_overlay_sources(
            [CAOverlayKind.CA_CANVEC_TRAILS],
            local_overrides={CAOverlayKind.CA_CANVEC_TRAILS: gpkg},
        )
        assert list(built.keys()) == ["ca-canvec-trails"]
        assert built["ca-canvec-trails"].fetch_overlay(squamish, tmp_path / "ca_loc") is not None
