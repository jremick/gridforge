"""Unit tests for US public-domain overlays (FEMA NFHL, USGS NHD, FAA DOF, NOAA ENC)."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import httpx
import pytest

from gridforge.config import (
    BBox,
    ColorMode,
    FrameMarginsMM,
    Orientation,
    PaperSize,
    SheetLayout,
    SheetSpec,
    USOverlayKind,
    Units,
)
from gridforge.sources.base import OverlaySource
from gridforge.sources.overlays_us import (
    FAATowersSource,
    FEMAFloodSource,
    NHDHydroSource,
    NOAAENCSource,
    build_us_overlay_sources,
    classify_nhd_fcode,
)

HAS_GDAL = shutil.which("ogr2ogr") is not None


def _boulder_layout() -> SheetLayout:
    spec = SheetSpec(
        center_lat=40.005,
        center_lon=-105.285,
        scale=24000,
        paper=PaperSize.LETTER,
        orientation=Orientation.PORTRAIT,
        color_mode=ColorMode.COLOR,
        units=Units.IMPERIAL,
    )
    margins = FrameMarginsMM()
    fw = 215.9 - margins.left - margins.right
    fh = 279.4 - margins.top - margins.bottom
    gw = fw * 24000 / 1000.0
    gh = fh * 24000 / 1000.0
    cx, cy = 475668.0, 4428365.0
    buf = 1000.0
    return SheetLayout(
        spec=spec,
        utm_epsg=32613,
        utm_zone=13,
        utm_hemisphere="N",
        gzd="13T",
        center_easting=cx,
        center_northing=cy,
        page_width_mm=215.9,
        page_height_mm=279.4,
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
        wgs84_bbox=BBox(min_x=-105.312, min_y=39.980, max_x=-105.258, max_y=40.030),
        wgs84_fetch_bbox=BBox(min_x=-105.325, min_y=39.968, max_x=-105.245, max_y=40.042),
    )


def test_us_overlay_protocols_factory_and_nhd_fcode(tmp_path: Path) -> None:
    assert classify_nhd_fcode(46003) == "intermittent"
    assert classify_nhd_fcode(46006) == "perennial"
    assert classify_nhd_fcode(46007) == "ephemeral"
    assert classify_nhd_fcode(33600) == "canal"
    assert classify_nhd_fcode(None) == "canal"

    sources = build_us_overlay_sources(
        [USOverlayKind.FEMA, "nhd", USOverlayKind.TOWERS, USOverlayKind.ENC, USOverlayKind.FEMA],
        local_overrides={"fema": tmp_path / "local_fema.gpkg"},
        cache_dir=tmp_path,
    )
    assert list(sources.keys()) == ["fema", "nhd", "towers", "enc"]
    assert all(isinstance(src, OverlaySource) for src in sources.values())
    assert isinstance(sources["fema"], FEMAFloodSource)
    assert sources["fema"].local_gpkg == tmp_path / "local_fema.gpkg"


@pytest.mark.skipif(not HAS_GDAL, reason="GDAL/OGR required for GeoPackage output tests")
def test_fema_flood_source_geojson_cache_and_failover(tmp_path: Path) -> None:
    layout = _boulder_layout()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "primary.example" in str(request.url):
            return httpx.Response(503, text="Service Unavailable")
        return httpx.Response(
            200,
            json={
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"FLD_ZONE": "AE", "ZONE_SUBTY": "FLOODWAY", "SFHA_TF": "T"},
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [[
                                [-105.292, 40.002],
                                [-105.278, 40.002],
                                [-105.278, 40.010],
                                [-105.292, 40.010],
                                [-105.292, 40.002],
                            ]],
                        },
                    }
                ],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        src = FEMAFloodSource(
            endpoints=[
                "https://primary.example/NFHL/MapServer/28/query",
                "https://fallback.example/NFHL/MapServer/28/query",
            ],
            cache_dir=tmp_path / "cache",
            client=client,
        )
        gpkg = src.fetch_overlay(layout, tmp_path / "run1")
        assert gpkg is not None and gpkg.name == "fema.gpkg"
        initial_calls = len(calls)
        assert initial_calls >= 2

        with sqlite3.connect(str(gpkg)) as conn:
            srs = conn.execute("SELECT srs_id FROM gpkg_contents WHERE table_name='fema_flood'").fetchone()[0]
            assert srs == 32613
            rows = conn.execute("SELECT fld_zone, zone_subty, sfha_tf FROM fema_flood").fetchall()
            assert rows == [("AE", "FLOODWAY", "T")]

        # Second call uses disk cache -> zero extra HTTP calls
        gpkg2 = src.fetch_overlay(layout, tmp_path / "run2")
        assert gpkg2 is not None
        assert len(calls) == initial_calls


@pytest.mark.skipif(not HAS_GDAL, reason="GDAL/OGR required for GeoPackage output tests")
def test_nhd_hydro_source_fcode_classification(tmp_path: Path) -> None:
    layout = _boulder_layout()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"gnis_name": "Boulder Creek", "fcode": 46006},
                        "geometry": {
                            "type": "LineString",
                            "coordinates": [[-105.295, 40.010], [-105.275, 40.012]],
                        },
                    },
                    {
                        "type": "Feature",
                        "properties": {"gnis_name": "Bear Canyon", "fcode": 46003},
                        "geometry": {
                            "type": "LineString",
                            "coordinates": [[-105.290, 39.995], [-105.280, 40.000]],
                        },
                    },
                    {
                        "type": "Feature",
                        "properties": {"gnis_name": "Dry Wash", "fcode": 46007},
                        "geometry": {
                            "type": "LineString",
                            "coordinates": [[-105.288, 39.990], [-105.282, 39.992]],
                        },
                    },
                    {
                        "type": "Feature",
                        "properties": {"gnis_name": "Silver Lake Ditch", "fcode": 33600},
                        "geometry": {
                            "type": "LineString",
                            "coordinates": [[-105.292, 40.016], [-105.280, 40.017]],
                        },
                    },
                ],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        src = NHDHydroSource(endpoint="https://nhd.example/query", cache_dir=tmp_path / "cache", client=client)
        gpkg = src.fetch_overlay(layout, tmp_path / "work")
        assert gpkg is not None and gpkg.name == "nhd.gpkg"

        with sqlite3.connect(str(gpkg)) as conn:
            srs = conn.execute("SELECT srs_id FROM gpkg_contents WHERE table_name='nhd_flowlines'").fetchone()[0]
            assert srs == 32613
            rows = dict(
                conn.execute("SELECT gnis_name, flow_type FROM nhd_flowlines").fetchall()
            )
            assert rows == {
                "Boulder Creek": "perennial",
                "Bear Canyon": "intermittent",
                "Dry Wash": "ephemeral",
                "Silver Lake Ditch": "canal",
            }


@pytest.mark.skipif(not HAS_GDAL, reason="GDAL/OGR required for GeoPackage output tests")
def test_faa_towers_source_dof_schema_mapping(tmp_path: Path) -> None:
    layout = _boulder_layout()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {
                            "OAS_Number": "08-012345",
                            "Type_Code": "TOWER",
                            "AGL": 325,
                            "AMSL": 5840,
                            "Lighting": "R",
                        },
                        "geometry": {"type": "Point", "coordinates": [-105.285, 40.005]},
                    }
                ],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        src = FAATowersSource(endpoint="https://faa.example/query", cache_dir=tmp_path / "cache", client=client)
        gpkg = src.fetch_overlay(layout, tmp_path / "work")
        assert gpkg is not None and gpkg.name == "towers.gpkg"

        with sqlite3.connect(str(gpkg)) as conn:
            srs = conn.execute("SELECT srs_id FROM gpkg_contents WHERE table_name='us_towers'").fetchone()[0]
            assert srs == 32613
            row = conn.execute(
                "SELECT source, name, obstacle_type, agl_ft, amsl_ft, lighting FROM us_towers"
            ).fetchone()
            assert row == ("FAA-DOF", "08-012345", "TOWER", 325.0, 5840.0, "R")


@pytest.mark.skipif(not HAS_GDAL, reason="GDAL/OGR required for GeoPackage output tests")
def test_noaa_enc_source_depth_contours_and_navaids(tmp_path: Path) -> None:
    layout = _boulder_layout()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/104/query" in url:
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {"VALDCO": 10.0},
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[-105.290, 40.004], [-105.280, 40.006]],
                            },
                        }
                    ],
                },
            )
        if "/6/query" in url:
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {"OBJNAM": "Channel Buoy 4", "COLOUR": "red"},
                            "geometry": {"type": "Point", "coordinates": [-105.285, 40.005]},
                        }
                    ],
                },
            )
        return httpx.Response(200, json={"type": "FeatureCollection", "features": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        src = NOAAENCSource(base_url="https://enc.example/MapServer", cache_dir=tmp_path / "cache", client=client)
        gpkg = src.fetch_overlay(layout, tmp_path / "work")
        assert gpkg is not None and gpkg.name == "enc.gpkg"

        with sqlite3.connect(str(gpkg)) as conn:
            depths = conn.execute("SELECT valdco FROM enc_depth_contours").fetchall()
            navaids = conn.execute("SELECT objnam, navaid_type, colour FROM enc_navaids").fetchall()
            assert depths == [(10.0,)]
            assert navaids == [("Channel Buoy 4", "buoy", "red")]


def test_us_overlays_return_none_gracefully_on_empty_or_http_error(tmp_path: Path) -> None:
    layout = _boulder_layout()

    def empty_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"type": "FeatureCollection", "features": []})

    def error_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("simulated timeout")

    with httpx.Client(transport=httpx.MockTransport(empty_handler)) as c_empty:
        assert NHDHydroSource(endpoint="https://nhd.example/q", cache_dir=tmp_path / "c1", client=c_empty).fetch_overlay(
            layout, tmp_path / "w1"
        ) is None

    with httpx.Client(transport=httpx.MockTransport(error_handler)) as c_err:
        assert FEMAFloodSource(
            endpoints=["https://fema.example/q"], cache_dir=tmp_path / "c2", client=c_err
        ).fetch_overlay(layout, tmp_path / "w2") is None


@pytest.mark.skipif(not HAS_GDAL, reason="GDAL/OGR required for local_gpkg override test")
def test_us_overlay_airgapped_local_gpkg_override(tmp_path: Path) -> None:
    from osgeo import ogr, osr  # type: ignore[import-untyped]

    layout = _boulder_layout()
    local_src = tmp_path / "airgapped_towers.gpkg"

    ogr.UseExceptions()
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds = ogr.GetDriverByName("GPKG").CreateDataSource(str(local_src))
    lyr = ds.CreateLayer("us_towers", srs=srs, geom_type=ogr.wkbPoint)
    lyr.CreateField(ogr.FieldDefn("name", ogr.OFTString))
    lyr.CreateField(ogr.FieldDefn("agl_ft", ogr.OFTReal))
    feat = ogr.Feature(lyr.GetLayerDefn())
    pt = ogr.Geometry(ogr.wkbPoint)
    pt.AddPoint(-105.285, 40.005)
    feat.SetGeometry(pt)
    feat.SetField("name", "Flatirons Mast")
    feat.SetField("agl_ft", 240.0)
    lyr.CreateFeature(feat)
    feat = None
    ds = None

    src = FAATowersSource(local_gpkg=local_src)
    out = src.fetch_overlay(layout, tmp_path / "work_local")
    assert out is not None and out.name == "towers.gpkg"
    with sqlite3.connect(str(out)) as conn:
        srs_id = conn.execute("SELECT srs_id FROM gpkg_contents WHERE table_name='us_towers'").fetchone()[0]
        assert srs_id == 32613
        rows = conn.execute("SELECT name, agl_ft FROM us_towers").fetchall()
        assert rows == [("Flatirons Mast", 240.0)]

    with pytest.raises(FileNotFoundError):
        FAATowersSource(local_gpkg=tmp_path / "missing.gpkg").fetch_overlay(layout, tmp_path / "w_err")
