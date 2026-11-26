"""Unit tests for v1.2.0 TAS, QLD, ACT state trail overlays and AutoStateTrailsSource cross-border router."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import httpx
import pytest

from gridforge.config import (
    AU_STATE_TRAIL_OVERLAY_KINDS,
    AUOverlayKind,
    BBox,
    ColorMode,
    FrameMarginsMM,
    Orientation,
    PaperSize,
    SheetLayout,
    SheetSpec,
    Units,
)
from gridforge.sources.base import OverlaySource
from gridforge.sources.overlays_au import build_au_overlay_sources
from gridforge.sources.overlays_au_states import (
    ACTTrailsSource,
    AutoStateTrailsSource,
    QLDTrailsSource,
    TASTrailsSource,
    is_in_act_coverage,
    is_in_qld_coverage,
    is_in_tas_coverage,
    resolve_intersecting_states,
    write_state_trails_gpkg,
)

HAS_GDAL = shutil.which("ogr2ogr") is not None


def _make_layout(
    *,
    lat: float,
    lon: float,
    utm_epsg: int,
    utm_zone: int,
    gzd: str,
    cx: float,
    cy: float,
    dlat: float = 0.035,
    dlon: float = 0.045,
) -> SheetLayout:
    spec = SheetSpec(
        center_lat=lat,
        center_lon=lon,
        scale=25000,
        paper=PaperSize.A4,
        orientation=Orientation.PORTRAIT,
        color_mode=ColorMode.COLOR,
        units=Units.METRIC,
    )
    margins = FrameMarginsMM()
    fw = 210.0 - margins.left - margins.right
    fh = 297.0 - margins.top - margins.bottom
    gw = fw * 25000 / 1000.0
    gh = fh * 25000 / 1000.0
    buf = 1000.0
    return SheetLayout(
        spec=spec,
        utm_epsg=utm_epsg,
        utm_zone=utm_zone,
        utm_hemisphere="S",
        gzd=gzd,
        center_easting=cx,
        center_northing=cy,
        page_width_mm=210.0,
        page_height_mm=297.0,
        margins_mm=margins,
        frame_x_mm=margins.left,
        frame_y_mm=margins.top,
        frame_width_mm=fw,
        frame_height_mm=fh,
        utm_bbox=BBox(min_x=cx - gw / 2, min_y=cy - gh / 2, max_x=cx + gw / 2, max_y=cy + gh / 2),
        utm_fetch_bbox=BBox(
            min_x=cx - gw / 2 - buf,
            min_y=cy - gh / 2 - buf,
            max_x=cx + gw / 2 + buf,
            max_y=cy + gh / 2 + buf,
        ),
        wgs84_bbox=BBox(min_x=lon - dlon * 0.7, min_y=lat - dlat * 0.7, max_x=lon + dlon * 0.7, max_y=lat + dlat * 0.7),
        wgs84_fetch_bbox=BBox(min_x=lon - dlon, min_y=lat - dlat, max_x=lon + dlon, max_y=lat + dlat),
    )


def _cradle_mtn_layout() -> SheetLayout:
    return _make_layout(lat=-41.684, lon=145.951, utm_epsg=32755, utm_zone=55, gzd="55G", cx=412719.0, cy=5384714.0)


def _lamington_layout() -> SheetLayout:
    return _make_layout(lat=-28.200, lon=153.190, utm_epsg=32756, utm_zone=56, gzd="56J", cx=518640.0, cy=6880420.0)


def _namadgi_layout() -> SheetLayout:
    return _make_layout(lat=-35.640, lon=148.980, utm_epsg=32755, utm_zone=55, gzd="55H", cx=679360.0, cy=6054210.0)


def _uluru_layout() -> SheetLayout:
    return _make_layout(lat=-25.3444, lon=131.0369, utm_epsg=32752, utm_zone=52, gzd="52J", cx=704925.0, cy=7195345.0)


def _colorado_layout() -> SheetLayout:
    spec = SheetSpec(center_lat=40.015, center_lon=-105.270, scale=24000)
    margins = FrameMarginsMM()
    return SheetLayout(
        spec=spec,
        utm_epsg=32613,
        utm_zone=13,
        utm_hemisphere="N",
        gzd="13T",
        center_easting=476950.0,
        center_northing=4429500.0,
        page_width_mm=210.0,
        page_height_mm=297.0,
        margins_mm=margins,
        frame_x_mm=margins.left,
        frame_y_mm=margins.top,
        frame_width_mm=186.0,
        frame_height_mm=235.0,
        utm_bbox=BBox(min_x=474700.0, min_y=4426700.0, max_x=479200.0, max_y=4432300.0),
        utm_fetch_bbox=BBox(min_x=473700.0, min_y=4425700.0, max_x=480200.0, max_y=4433300.0),
        wgs84_bbox=BBox(min_x=-105.30, min_y=39.99, max_x=-105.24, max_y=40.04),
        wgs84_fetch_bbox=BBox(min_x=-105.31, min_y=39.98, max_x=-105.23, max_y=40.05),
    )


def test_east_factory_coverage_and_act_strict_routing(tmp_path: Path) -> None:
    sources = build_au_overlay_sources(
        [
            AUOverlayKind.AU_STATE_TRAILS,
            AUOverlayKind.AU_TAS_TRAILS,
            "au-qld-trails",
            "au_act_trails",
        ],
        local_overrides={"au-tas-trails": tmp_path / "tas_local.gpkg"},
        cache_dir=tmp_path,
    )
    assert list(sources.keys()) == [
        "au-state-trails",
        "au-tas-trails",
        "au-qld-trails",
        "au-act-trails",
    ]
    assert isinstance(sources["au-state-trails"], AutoStateTrailsSource)
    assert isinstance(sources["au-tas-trails"], TASTrailsSource)
    assert isinstance(sources["au-qld-trails"], QLDTrailsSource)
    assert isinstance(sources["au-act-trails"], ACTTrailsSource)
    assert all(isinstance(s, OverlaySource) for s in sources.values())
    assert sources["au-tas-trails"].local_gpkg == tmp_path / "tas_local.gpkg"
    assert "State of Tasmania" in sources["au-tas-trails"].attribution
    assert "State of Queensland" in sources["au-qld-trails"].attribution
    assert "Australian Capital Territory" in sources["au-act-trails"].attribution

    # Bounds checks
    assert is_in_tas_coverage(_cradle_mtn_layout().wgs84_fetch_bbox)
    assert not is_in_tas_coverage(_lamington_layout().wgs84_fetch_bbox)
    assert is_in_qld_coverage(_lamington_layout().wgs84_fetch_bbox)
    assert not is_in_qld_coverage(_cradle_mtn_layout().wgs84_fetch_bbox)
    assert is_in_act_coverage(_namadgi_layout().wgs84_fetch_bbox)
    assert not is_in_act_coverage(_uluru_layout().wgs84_fetch_bbox)

    # Smart ACT routing: strictly inside ACT -> ['ACT'] only!
    assert resolve_intersecting_states(_namadgi_layout().wgs84_fetch_bbox) == ["ACT"]
    # Straddling ACT-NSW border -> ['ACT', 'NSW']!
    straddle = BBox(min_x=149.20, min_y=-35.40, max_x=149.60, max_y=-35.20)
    assert resolve_intersecting_states(straddle) == ["ACT", "NSW"]
    # Cradle Mountain TAS -> ['TAS']
    assert resolve_intersecting_states(_cradle_mtn_layout().wgs84_fetch_bbox) == ["TAS"]
    # Lamington QLD/NSW border -> ['NSW', 'QLD']
    assert resolve_intersecting_states(_lamington_layout().wgs84_fetch_bbox) == ["NSW", "QLD"]
    # Outside Australia -> []
    assert resolve_intersecting_states(_colorado_layout().wgs84_fetch_bbox) == []


def test_east_sources_out_of_state_short_circuit(tmp_path: Path) -> None:
    def fail_handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Should not hit HTTP out of state: {request.url}")

    uluru = _uluru_layout()
    us_layout = _colorado_layout()
    with httpx.Client(transport=httpx.MockTransport(fail_handler)) as client:
        assert TASTrailsSource(cache_dir=tmp_path / "c1", client=client).fetch_overlay(uluru, tmp_path / "w1") is None
        assert QLDTrailsSource(cache_dir=tmp_path / "c2", client=client).fetch_overlay(uluru, tmp_path / "w2") is None
        assert ACTTrailsSource(cache_dir=tmp_path / "c3", client=client).fetch_overlay(uluru, tmp_path / "w3") is None
        assert AutoStateTrailsSource(cache_dir=tmp_path / "c4", client=client).fetch_overlay(us_layout, tmp_path / "w4") is None


@pytest.mark.skipif(not HAS_GDAL, reason="GDAL/OGR required for GeoPackage tests")
def test_tas_trails_source_normalization_and_cache(tmp_path: Path) -> None:
    layout = _cradle_mtn_layout()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        url = str(request.url)
        if "/opendata/42/query" in url:
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {
                                "TRANS_TYPE": "Track",
                                "TSEG_FEAT": "Normal Transport Segment",
                                "TRAN_CLASS": "AS2156 Track Class 3 (PWS)",
                                "SURFACE_TY": "Boardwalk",
                                "PRI_NAME": "Overland Track",
                            },
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[145.945, -41.690], [145.958, -41.678]],
                            },
                        },
                        {
                            "type": "Feature",
                            "properties": {
                                "TRANS_TYPE": "Road",
                                "TSEG_FEAT": "Fire Trail",
                                "TRAN_CLASS": "Vehicular Track",
                                "SURFACE_TY": "4WD required",
                                "PRI_NAME": "Main Fire Trail",
                            },
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[145.940, -41.688], [145.955, -41.680]],
                            },
                        },
                        {
                            "type": "Feature",
                            "properties": {
                                "TRANS_TYPE": "Road",
                                "TSEG_FEAT": "Normal Transport Segment",
                                "TRAN_CLASS": "Vehicular Track",
                                "SURFACE_TY": "Unsealed",
                                "PRI_NAME": "Forestry 4WD",
                            },
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[145.942, -41.685], [145.956, -41.682]],
                            },
                        },
                    ],
                },
            )
        if "/opendata/12/query" in url:
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {"BUILD_TY": "Hut", "BLD_PUR": "Walkers", "BUILD_NAME": "Kitchen Hut"},
                            "geometry": {"type": "Point", "coordinates": [145.950, -41.685]},
                        },
                        {
                            "type": "Feature",
                            "properties": {"BUILD_TY": "Hut", "BLD_PUR": "Walkers", "BUILD_NAME": "Waterfall Valley Hut"},
                            "geometry": {"type": "Point", "coordinates": [145.952, -41.689]},
                        },
                    ],
                },
            )
        if "/opendata/34/query" in url:
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {"FEAT_TYPE": "Camping Ground", "FEAT_NAME": "Waldheim Campground"},
                            "geometry": {"type": "Point", "coordinates": [145.955, -41.680]},
                        }
                    ],
                },
            )
        if "/topo/69/query" in url:
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {"INFTY2_TXT": "Lookout", "NAME": "Marions Lookout"},
                            "geometry": {"type": "Point", "coordinates": [145.949, -41.683]},
                        }
                    ],
                },
            )
        if "/opendata/36/query" in url:
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {"COM_TYPE2": "Picnic Area", "NAME": "Dove Lake Picnic Area"},
                            "geometry": {"type": "Point", "coordinates": [145.960, -41.676]},
                        }
                    ],
                },
            )
        if "/opendata/40/query" in url:
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {"PACK_ID": "ST364", "SCS_NAME": "CRADLE MTN", "HEIGHT": 1545.19},
                            "geometry": {"type": "Point", "coordinates": [145.951, -41.684]},
                        },
                        # Duplicate elevationless broadcast tower at same spot -> deduped in favor of 1545.19m!
                        {
                            "type": "Feature",
                            "properties": {"PACK_ID": "ST364TV", "SCS_NAME": "CRADLE MTN", "HEIGHT": None},
                            "geometry": {"type": "Point", "coordinates": [145.9511, -41.6841]},
                        },
                    ],
                },
            )
        return httpx.Response(200, json={"type": "FeatureCollection", "features": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        src = TASTrailsSource(
            opendata_base_url="https://tas.example/opendata",
            topo_base_url="https://tas.example/topo",
            cache_dir=tmp_path / "cache",
            client=client,
        )
        gpkg = src.fetch_overlay(layout, tmp_path / "run1")
        assert gpkg is not None and gpkg.name == "au_tas_trails.gpkg"
        assert calls == 6

        with sqlite3.connect(str(gpkg)) as conn:
            assert {r[0] for r in conn.execute("SELECT srs_id FROM gpkg_contents").fetchall()} == {32755}
            trails = conn.execute(
                "SELECT name, trail_class, surface, state FROM au_state_trails ORDER BY trail_class, name"
            ).fetchall()
            assert trails == [
                ("Forestry 4WD", "4wd_track", "unsealed", "TAS"),
                ("Main Fire Trail", "fire_trail", "unsealed", "TAS"),
                ("Overland Track", "walking_track", "boardwalk", "TAS"),
            ]
            pois = conn.execute(
                "SELECT name, poi_type, elev_m, state FROM au_state_pois ORDER BY poi_type, name"
            ).fetchall()
            assert [(p[0], p[1], p[3]) for p in pois] == [
                ("Waldheim Campground", "campground", "TAS"),
                ("Kitchen Hut", "hut", "TAS"),
                ("Waterfall Valley Hut", "hut", "TAS"),
                ("Marions Lookout", "lookout", "TAS"),
                ("Dove Lake Picnic Area", "picnic", "TAS"),
                ("CRADLE MTN", "trig", "TAS"),
            ]
            assert [p[2] for p in pois if p[1] == "trig"][0] == pytest.approx(1545.19)

        # Second call hits disk cache
        assert src.fetch_overlay(layout, tmp_path / "run2") is not None
        assert calls == 6


@pytest.mark.skipif(not HAS_GDAL, reason="GDAL/OGR required for GeoPackage tests")
def test_qld_trails_source_normalization_dedupe_and_trigs(tmp_path: Path) -> None:
    layout = _lamington_layout()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/parks/6/query" in url:
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {
                                "objectid": 55627,
                                "name": "Egg Rock Firebreak (Great Walk)",
                                "visitor_id": "Gold Coast Hinterland Great Walk",
                                "subcat": "Tracks and Trails",
                                "material": "Natural",
                            },
                            "geometry": {"type": "LineString", "coordinates": [[153.180, -28.205], [153.200, -28.195]]},
                        }
                    ],
                },
            )
        if "/parks/5/query" in url:
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        # Duplicate objectid from /6 -> deduped!
                        {
                            "type": "Feature",
                            "properties": {
                                "objectid": 55627,
                                "name": "Egg Rock Firebreak (Great Walk)",
                                "subcat": "Tracks and Trails",
                                "material": "Natural",
                            },
                            "geometry": {"type": "LineString", "coordinates": [[153.180, -28.205], [153.200, -28.195]]},
                        },
                        {
                            "type": "Feature",
                            "properties": {
                                "objectid": 70505,
                                "name": "Daves Creek Circuit",
                                "subcat": "Tracks and Trails",
                                "assettype": "Walking track",
                                "material": "Natural",
                            },
                            "geometry": {"type": "LineString", "coordinates": [[153.185, -28.202], [153.195, -28.196]]},
                        },
                    ],
                },
            )
        if "/parks/2/query" in url:
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {
                                "objectid": 90001,
                                "name": "Caves Management Road",
                                "subcat": "Roads",
                                "access_": "Management only",
                                "material": "Natural",
                            },
                            "geometry": {"type": "LineString", "coordinates": [[153.182, -28.204], [153.192, -28.198]]},
                        },
                        # Vehicle barrier structure -> skipped cleanly
                        {
                            "type": "Feature",
                            "properties": {
                                "objectid": 90002,
                                "subcat": "Access Structures",
                                "assettype": "Vehicle barrier or Bollards",
                            },
                            "geometry": {"type": "LineString", "coordinates": [[153.184, -28.201], [153.185, -28.201]]},
                        },
                    ],
                },
            )
        if "/roads/query" in url:
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {
                                "road_name_full": "Numinbah 4WD Track",
                                "sub_class": "4WD",
                                "trafficability": "4WD",
                                "surface_type": "Unsealed",
                            },
                            "geometry": {"type": "LineString", "coordinates": [[153.178, -28.208], [153.198, -28.202]]},
                        }
                    ],
                },
            )
        if "/tourist/query" in url:
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {"feature_type": "Lookout", "name": "Tullawallal Lookout"},
                            "geometry": {"type": "Point", "coordinates": [153.190, -28.200]},
                        },
                        {
                            "type": "Feature",
                            "properties": {"feature_type": "Camping Ground", "name": "Binna Burra Camp"},
                            "geometry": {"type": "Point", "coordinates": [153.188, -28.198]},
                        },
                    ],
                },
            )
        if "/survey/query" in url:
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {
                                "mrk_id": "51234",
                                "alt1_nm": "HOBWEE",
                                "mrktype_de": "Standard Plaque",
                                "ahdfix_de": "Trig",
                                "ahdheight": 1174.2,
                            },
                            "geometry": {"type": "Point", "coordinates": [153.192, -28.204]},
                        }
                    ],
                },
            )
        return httpx.Response(200, json={"type": "FeatureCollection", "features": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        src = QLDTrailsSource(
            parks_base_url="https://qld.example/parks",
            roads_url="https://qld.example/roads/query",
            tourist_url="https://qld.example/tourist/query",
            survey_url="https://qld.example/survey/query",
            cache_dir=tmp_path / "cache",
            client=client,
        )
        gpkg = src.fetch_overlay(layout, tmp_path / "work")
        assert gpkg is not None and gpkg.name == "au_qld_trails.gpkg"

        with sqlite3.connect(str(gpkg)) as conn:
            trails = conn.execute(
                "SELECT name, trail_class, surface, state FROM au_state_trails ORDER BY trail_class, name"
            ).fetchall()
            assert trails == [
                ("Numinbah 4WD Track", "4wd_track", "unsealed", "QLD"),
                ("Egg Rock Firebreak (Great Walk)", "fire_trail", "natural", "QLD"),
                ("Caves Management Road", "management_trail", "natural", "QLD"),
                ("Daves Creek Circuit", "walking_track", "natural", "QLD"),
            ]
            pois = conn.execute(
                "SELECT name, poi_type, elev_m, state FROM au_state_pois ORDER BY poi_type, name"
            ).fetchall()
            assert [(p[0], p[1], p[3]) for p in pois] == [
                ("Binna Burra Camp", "campground", "QLD"),
                ("Tullawallal Lookout", "lookout", "QLD"),
                ("HOBWEE", "trig", "QLD"),
            ]
            assert [p[2] for p in pois if p[1] == "trig"][0] == pytest.approx(1174.2)


@pytest.mark.skipif(not HAS_GDAL, reason="GDAL/OGR required for GeoPackage tests")
def test_act_trails_source_huts_polygon_centroid_bbq_dedupe_and_trigs(tmp_path: Path) -> None:
    layout = _namadgi_layout()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/ACTGOV_TRANSPORT_TRACK/FeatureServer/0/query" in url:
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {"ROAD_NAME": "SMOKERS TRAIL"},
                            "geometry": {"type": "LineString", "coordinates": [[148.970, -35.645], [148.990, -35.635]]},
                        },
                        {
                            "type": "Feature",
                            "properties": {"ROAD_NAME": "COTTER FIRE TRAIL"},
                            "geometry": {"type": "LineString", "coordinates": [[148.968, -35.642], [148.985, -35.638]]},
                        },
                    ],
                },
            )
        if "/ACT_Equestrian_Routes/FeatureServer/0/query" in url:
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {"BICENTENNIAL_NATIONAL_TRAIL": "YES"},
                            "geometry": {"type": "LineString", "coordinates": [[148.972, -35.648], [148.988, -35.632]]},
                        }
                    ],
                },
            )
        if "/ACTGOV_Heritage_Register/FeatureServer/1/query" in url:
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        # Polygon hut -> converted cleanly to Point centroid!
                        {
                            "type": "Feature",
                            "properties": {"SITE_NAME": "Hospital Creek Hut", "NAME": "Stockmen's Huts"},
                            "geometry": {
                                "type": "Polygon",
                                "coordinates": [[[148.979, -35.641], [148.981, -35.641], [148.981, -35.639], [148.979, -35.639], [148.979, -35.641]]],
                            },
                        },
                        # Blank SITE_NAME falls back to NAME
                        {
                            "type": "Feature",
                            "properties": {"SITE_NAME": " ", "NAME": "Brandy Flat Hut Precinct"},
                            "geometry": {"type": "Point", "coordinates": [148.985, -35.645]},
                        },
                    ],
                },
            )
        if "/ACTGOV_FEATURE_NAME/FeatureServer/0/query" in url:
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {"TYPE": "LOOKOUT", "DISPLAY_NAME": "Orroral Lookout"},
                            "geometry": {"type": "Point", "coordinates": [148.976, -35.636]},
                        }
                    ],
                },
            )
        if "/ACTGOV_Barbecue_Assets/FeatureServer/1/query" in url:
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        # Multiple BBQs at same campground -> collapsed to 1 campground POI
                        {
                            "type": "Feature",
                            "properties": {"ASSET_NAME": "ORRORAL BBQ 1", "LOCATION": "ORRORAL CAMPGROUND"},
                            "geometry": {"type": "Point", "coordinates": [148.982, -35.640]},
                        },
                        {
                            "type": "Feature",
                            "properties": {"ASSET_NAME": "ORRORAL BBQ 2", "LOCATION": "ORRORAL CAMPGROUND"},
                            "geometry": {"type": "Point", "coordinates": [148.9825, -35.6402]},
                        },
                        # Generic park location -> skipped
                        {
                            "type": "Feature",
                            "properties": {"ASSET_NAME": "WOOD BBQ", "LOCATION": "NAMADGI NATIONAL PARK"},
                            "geometry": {"type": "Point", "coordinates": [148.983, -35.641]},
                        },
                    ],
                },
            )
        if "/ACTGOV_SURVEY_CONTROL_MARK_CURRENT_View/FeatureServer/3/query" in url:
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {
                                "MARK_TYPE": "MC",
                                "MARK_NAME_FULL": "ORRORAL(P)",
                                "ALIAS": "TS5659",
                                "REDUCED_LEVEL": 1609.11,
                                "MARK_CONSTRUCTION": "PILLAR",
                            },
                            "geometry": {"type": "Point", "coordinates": [148.978, -35.642]},
                        }
                    ],
                },
            )
        return httpx.Response(200, json={"type": "FeatureCollection", "features": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        src = ACTTrailsSource(base_url="https://act.example", cache_dir=tmp_path / "cache", client=client)
        gpkg = src.fetch_overlay(layout, tmp_path / "work")
        assert gpkg is not None and gpkg.name == "au_act_trails.gpkg"

        with sqlite3.connect(str(gpkg)) as conn:
            trails = conn.execute(
                "SELECT name, trail_class, surface, state FROM au_state_trails ORDER BY trail_class, name"
            ).fetchall()
            assert trails == [
                ("COTTER FIRE TRAIL", "fire_trail", "unsealed", "ACT"),
                ("SMOKERS TRAIL", "management_trail", "unsealed", "ACT"),
                ("Bicentennial National Trail", "walking_track", "natural", "ACT"),
            ]
            pois = conn.execute(
                "SELECT name, poi_type, elev_m, state FROM au_state_pois ORDER BY poi_type, name"
            ).fetchall()
            assert [(p[0], p[1], p[3]) for p in pois] == [
                ("ORRORAL CAMPGROUND", "campground", "ACT"),
                ("Brandy Flat Hut Precinct", "hut", "ACT"),
                ("Hospital Creek Hut", "hut", "ACT"),
                ("Orroral Lookout", "lookout", "ACT"),
                ("ORRORAL(P)", "trig", "ACT"),
            ]
            assert [p[2] for p in pois if p[1] == "trig"][0] == pytest.approx(1609.11)


@pytest.mark.skipif(not HAS_GDAL, reason="GDAL/OGR required for GeoPackage tests")
def test_auto_state_trails_cross_border_merge_dynamic_attributions_and_local_override(tmp_path: Path) -> None:
    # Lamington straddles QLD-NSW border -> resolve_intersecting_states returns ['NSW', 'QLD']!
    layout = _lamington_layout()

    class _StubStateSource:
        def __init__(self, state: str, attr: str, trail_name: str, poi_name: str, elev: float | None, empty: bool = False):
            self.name = f"au-{state.lower()}-trails"
            self.attribution = attr
            self._state = state
            self._trail_name = trail_name
            self._poi_name = poi_name
            self._elev = elev
            self._empty = empty

        def fetch_overlay(self, lay: SheetLayout, work_dir: Path) -> Path | None:
            if self._empty:
                return None
            work_dir.mkdir(parents=True, exist_ok=True)
            return write_state_trails_gpkg(
                work_dir / f"au_{self._state.lower()}_trails.gpkg",
                lay,
                [
                    (
                        {"type": "LineString", "coordinates": [[153.180, -28.205], [153.200, -28.195]]},
                        {"name": self._trail_name, "trail_class": "fire_trail", "surface": "unsealed", "state": self._state},
                    )
                ],
                [
                    (
                        {"type": "Point", "coordinates": [153.190, -28.200]},
                        {"name": self._poi_name, "poi_type": "trig", "elev_m": self._elev, "state": self._state},
                    )
                ],
            )

    # 1. Cross-border merge: both NSW and QLD contribute -> merged GPKG + both attributions + border POI dedupe!
    nsw_stub = _StubStateSource("NSW", "NSW CC BY 4.0", "Border Fire Trail NSW", "BORDER TRIG", None)
    qld_stub = _StubStateSource("QLD", "QLD CC BY 4.0", "Border Fire Trail QLD", "BORDER TRIG", 1174.2)
    auto = AutoStateTrailsSource(state_sources={"NSW": nsw_stub, "QLD": qld_stub})
    merged = auto.fetch_overlay(layout, tmp_path / "merged_work")
    assert merged is not None and merged.name == "au_state_trails.gpkg"
    assert auto.resolved_attributions == ["NSW CC BY 4.0", "QLD CC BY 4.0"]
    assert auto.attribution == "NSW CC BY 4.0 | QLD CC BY 4.0"

    with sqlite3.connect(str(merged)) as conn:
        trails = conn.execute("SELECT name, state FROM au_state_trails ORDER BY state").fetchall()
        assert trails == [("Border Fire Trail NSW", "NSW"), ("Border Fire Trail QLD", "QLD")]
        # Duplicate BORDER TRIG collapsed into 1 keeping elev_m=1174.2!
        pois = conn.execute("SELECT name, poi_type, elev_m FROM au_state_pois").fetchall()
        assert len(pois) == 1
        assert pois[0][0] == "BORDER TRIG" and pois[0][2] == pytest.approx(1174.2)

    # 2. Single contributing state -> resolved_attributions contains ONLY the contributing state!
    nsw_empty = _StubStateSource("NSW", "NSW CC BY 4.0", "", "", None, empty=True)
    auto_one = AutoStateTrailsSource(state_sources={"NSW": nsw_empty, "QLD": qld_stub})
    single = auto_one.fetch_overlay(layout, tmp_path / "single_work")
    assert single is not None and single.name == "au_state_trails.gpkg"
    assert auto_one.resolved_attributions == ["QLD CC BY 4.0"]
    assert auto_one.attribution == "QLD CC BY 4.0"

    # 3. Air-gapped local_gpkg override works directly
    local_out = AutoStateTrailsSource(local_gpkg=merged).fetch_overlay(layout, tmp_path / "local_work")
    assert local_out is not None and local_out.name == "au_state_trails.gpkg"
    with sqlite3.connect(str(local_out)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM au_state_trails").fetchone()[0] == 2


def test_all_nine_au_state_trail_overlay_kinds_wire_cleanly(tmp_path: Path) -> None:
    from gridforge.sources import (
        ACTTrailsSource as ExportedACT,
        AutoStateTrailsSource as ExportedAuto,
        NSWTrailsSource as ExportedNSW,
        NTTrailsSource as ExportedNT,
        QLDTrailsSource as ExportedQLD,
        SATrailsSource as ExportedSA,
        TASTrailsSource as ExportedTAS,
        VICTrailsSource as ExportedVIC,
        WATrailsSource as ExportedWA,
    )

    built = build_au_overlay_sources(AU_STATE_TRAIL_OVERLAY_KINDS, cache_dir=tmp_path)
    assert len(built) == 9
    assert isinstance(built["au-state-trails"], ExportedAuto)
    assert isinstance(built["au-nsw-trails"], ExportedNSW)
    assert isinstance(built["au-vic-trails"], ExportedVIC)
    assert isinstance(built["au-tas-trails"], ExportedTAS)
    assert isinstance(built["au-qld-trails"], ExportedQLD)
    assert isinstance(built["au-wa-trails"], ExportedWA)
    assert isinstance(built["au-sa-trails"], ExportedSA)
    assert isinstance(built["au-nt-trails"], ExportedNT)
    assert isinstance(built["au-act-trails"], ExportedACT)
    assert all(isinstance(s, OverlaySource) for s in built.values())

    auto = AutoStateTrailsSource(cache_dir=tmp_path)
    for code in ("NSW", "VIC", "TAS", "QLD", "WA", "SA", "NT", "ACT"):
        src = auto._get_state_source(code)
        assert isinstance(src, OverlaySource)

