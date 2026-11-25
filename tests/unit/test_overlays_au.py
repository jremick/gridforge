"""Unit tests for Australia-wide CC BY 4.0 regional overlays (GA Hydro, CAPAD, GA Infra, GA Aviation)."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import httpx
import pytest

from gridforge.config import (
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
from gridforge.sources.overlays_au import (
    CAPAD_MAX_ALLOWABLE_OFFSET_DEG,
    CAPADParksSource,
    GAAviationSource,
    GAHydroSource,
    GAInfraSource,
    build_au_overlay_sources,
)

HAS_GDAL = shutil.which("ogr2ogr") is not None


def _uluru_layout() -> SheetLayout:
    """Uluru / Kata Tjuta NT (-25.3444, 131.0369, UTM Zone 52S / EPSG:32752 / 52J)."""
    spec = SheetSpec(
        center_lat=-25.3444,
        center_lon=131.0369,
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
    cx, cy = 704925.0, 7195345.0
    buf = 1000.0
    return SheetLayout(
        spec=spec,
        utm_epsg=32752,
        utm_zone=52,
        utm_hemisphere="S",
        gzd="52J",
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
        wgs84_bbox=BBox(min_x=131.010, min_y=-25.372, max_x=131.064, max_y=-25.317),
        wgs84_fetch_bbox=BBox(min_x=130.998, min_y=-25.383, max_x=131.076, max_y=-25.306),
    )


def test_au_overlay_protocols_and_factory(tmp_path: Path) -> None:
    sources = build_au_overlay_sources(
        [
            AUOverlayKind.AU_HYDRO,
            "au-parks",
            "au_infra",
            AUOverlayKind.AU_AVIATION,
            AUOverlayKind.AU_HYDRO,
        ],
        local_overrides={"au_parks": tmp_path / "local_capad.gpkg"},
        cache_dir=tmp_path,
    )
    assert list(sources.keys()) == ["au-hydro", "au-parks", "au-infra", "au-aviation"]
    assert all(isinstance(src, OverlaySource) for src in sources.values())
    assert isinstance(sources["au-parks"], CAPADParksSource)
    assert sources["au-parks"].local_gpkg == tmp_path / "local_capad.gpkg"
    assert all("CC BY 4.0" in src.attribution for src in sources.values())


@pytest.mark.skipif(not HAS_GDAL, reason="GDAL/OGR required for GeoPackage tests")
def test_ga_hydro_source_points_lines_and_polys(tmp_path: Path) -> None:
    layout = _uluru_layout()
    seen_where: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/1/query" in url:
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {
                                "name": "BORE G37",
                                "featuretype": "Bore",
                                "type": 14,
                                "perenniality": None,
                            },
                            "geometry": {"type": "Point", "coordinates": [131.036, -25.344]},
                        }
                    ],
                },
            )
        if "/3/query" in url:
            seen_where["lines"] = request.url.params.get("where", "")
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {
                                "name": "MUTITJULU CREEK",
                                "featuretype": "Watercourse",
                                "perenniality": "Non Perennial",
                                "hierarchy": "Minor",
                            },
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[131.030, -25.348, 520.0], [131.042, -25.340, 510.0]],
                            },
                        }
                    ],
                },
            )
        if "/5/query" in url:
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {
                                "name": "MUTITJULU WATERHOLE",
                                "featuretype": "Waterhole",
                                "perenniality": "Perennial",
                            },
                            "geometry": {
                                "type": "Polygon",
                                "coordinates": [[
                                    [131.034, -25.346],
                                    [131.038, -25.346],
                                    [131.038, -25.343],
                                    [131.034, -25.343],
                                    [131.034, -25.346],
                                ]],
                            },
                        }
                    ],
                },
            )
        return httpx.Response(200, json={"type": "FeatureCollection", "features": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        src = GAHydroSource(base_url="https://hydro.example/MapServer", cache_dir=tmp_path / "cache", client=client)
        gpkg = src.fetch_overlay(layout, tmp_path / "work")
        assert gpkg is not None and gpkg.name == "au_hydro.gpkg"
        assert "Connector" in seen_where["lines"]

        with sqlite3.connect(str(gpkg)) as conn:
            srs_ids = {r[0] for r in conn.execute("SELECT srs_id FROM gpkg_contents").fetchall()}
            assert srs_ids == {32752}
            assert conn.execute("SELECT name, featuretype FROM au_hydro_points").fetchall() == [
                ("BORE G37", "Bore")
            ]
            assert conn.execute(
                "SELECT name, perenniality, hierarchy FROM au_hydro_lines"
            ).fetchall() == [("MUTITJULU CREEK", "Non Perennial", "Minor")]
            assert conn.execute("SELECT name, featuretype FROM au_hydro_polys").fetchall() == [
                ("MUTITJULU WATERHOLE", "Waterhole")
            ]


@pytest.mark.skipif(not HAS_GDAL, reason="GDAL/OGR required for GeoPackage tests")
def test_capad_parks_source_simplification_and_cache(tmp_path: Path) -> None:
    layout = _uluru_layout()
    calls: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.url.params))
        return httpx.Response(
            200,
            json={
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {
                            "NAME": "Uluru-Kata Tjuta",
                            "TYPE": "National Park (Commonwealth)",
                            "TYPE_ABBR": "NPC",
                            "IUCN": "II",
                            "STATE": "NT",
                        },
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [[
                                [131.020, -25.360],
                                [131.055, -25.360],
                                [131.055, -25.330],
                                [131.020, -25.330],
                                [131.020, -25.360],
                            ]],
                        },
                    }
                ],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        src = CAPADParksSource(endpoint="https://capad.example/0/query", cache_dir=tmp_path / "cache", client=client)
        gpkg = src.fetch_overlay(layout, tmp_path / "run1")
        assert gpkg is not None and gpkg.name == "au_parks.gpkg"
        assert len(calls) == 1
        assert calls[0]["maxAllowableOffset"] == CAPAD_MAX_ALLOWABLE_OFFSET_DEG

        with sqlite3.connect(str(gpkg)) as conn:
            row = conn.execute(
                "SELECT name, park_type, type_abbr, iucn, state FROM au_protected_areas"
            ).fetchone()
            assert row == ("Uluru-Kata Tjuta", "National Park (Commonwealth)", "NPC", "II", "NT")

        # Disk cache hit on second call
        gpkg2 = src.fetch_overlay(layout, tmp_path / "run2")
        assert gpkg2 is not None
        assert len(calls) == 1


@pytest.mark.skipif(not HAS_GDAL, reason="GDAL/OGR required for GeoPackage tests")
def test_ga_infra_source_resilient_partial_timeout_and_esrijson(tmp_path: Path) -> None:
    layout = _uluru_layout()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        # Simulate GA electricity substations timing out while power lines & gas pipelines succeed!
        if "elec.example/0/query" in url:
            raise httpx.ReadTimeout("simulated timeout on substations")
        if "elec.example/2/query" in url:
            # Return EsriJSON when f=json after rejecting f=geojson to test fallback
            if request.url.params.get("f") == "geojson":
                return httpx.Response(500, text="geojson unsupported")
            return httpx.Response(
                200,
                json={
                    "features": [
                        {
                            "attributes": {
                                "FEATURE_NAME": "Yulara Line",
                                "CAPACITY_KV": 132,
                                "STATUS": "Operational",
                                "STATE": "NT",
                            },
                            "geometry": {"paths": [[[131.025, -25.345], [131.050, -25.340]]]},
                        }
                    ]
                },
            )
        if "pipe.example/1/query" in url:
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {
                                "feature_name": "Amadeus Gas Pipeline",
                                "feature_type": "Gas Pipeline",
                                "status": "Operational",
                                "operator": "APA Group",
                            },
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[131.028, -25.350], [131.048, -25.335]],
                            },
                        }
                    ],
                },
            )
        return httpx.Response(200, json={"type": "FeatureCollection", "features": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        src = GAInfraSource(
            elec_base_url="https://elec.example",
            pipe_base_url="https://pipe.example",
            cache_dir=tmp_path / "cache",
            client=client,
        )
        gpkg = src.fetch_overlay(layout, tmp_path / "work")
        assert gpkg is not None and gpkg.name == "au_infra.gpkg"

        with sqlite3.connect(str(gpkg)) as conn:
            assert conn.execute("SELECT name, capacity_kv, state FROM au_power_lines").fetchall() == [
                ("Yulara Line", 132.0, "NT")
            ]
            assert conn.execute("SELECT COUNT(*) FROM au_substations").fetchone()[0] == 0
            assert conn.execute(
                "SELECT name, pipeline_type, operator FROM au_pipelines"
            ).fetchall() == [("Amadeus Gas Pipeline", "Gas Pipeline", "APA Group")]


@pytest.mark.skipif(not HAS_GDAL, reason="GDAL/OGR required for GeoPackage tests")
def test_ga_aviation_source_airfields_runways_and_homesteads(tmp_path: Path) -> None:
    layout = _uluru_layout()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/419/query" in url:
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {"name": "Ayers Rock ALA", "featuretype": "Aircraft Facility", "type": "Landing Ground"},
                            "geometry": {"type": "Point", "coordinates": [131.036, -25.344]},
                        }
                    ],
                },
            )
        if "/420/query" in url:
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {"featuretype": "Runway", "type": "Sealed"},
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[131.032, -25.344], [131.042, -25.344]],
                            },
                        }
                    ],
                },
            )
        if "/237/query" in url:
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {"name": "Curtin Springs Homestead", "featuretype": "Homestead"},
                            "geometry": {"type": "Point", "coordinates": [131.040, -25.350]},
                        }
                    ],
                },
            )
        return httpx.Response(200, json={"type": "FeatureCollection", "features": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        src = GAAviationSource(base_url="https://topo.example/MapServer", cache_dir=tmp_path / "cache", client=client)
        gpkg = src.fetch_overlay(layout, tmp_path / "work")
        assert gpkg is not None and gpkg.name == "au_aviation.gpkg"

        with sqlite3.connect(str(gpkg)) as conn:
            assert conn.execute("SELECT name, type FROM au_airfields").fetchall() == [
                ("Ayers Rock ALA", "Landing Ground")
            ]
            assert conn.execute("SELECT featuretype, type FROM au_runways").fetchall() == [
                ("Runway", "Sealed")
            ]
            assert conn.execute("SELECT name, featuretype FROM au_homesteads").fetchall() == [
                ("Curtin Springs Homestead", "Homestead")
            ]


def test_au_overlays_empty_and_http_error_return_none(tmp_path: Path) -> None:
    layout = _uluru_layout()

    def empty_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"type": "FeatureCollection", "features": []})

    def error_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("simulated timeout")

    with httpx.Client(transport=httpx.MockTransport(empty_handler)) as c_empty:
        assert CAPADParksSource(endpoint="https://capad.example/q", cache_dir=tmp_path / "c1", client=c_empty).fetch_overlay(
            layout, tmp_path / "w1"
        ) is None

    with httpx.Client(transport=httpx.MockTransport(error_handler)) as c_err:
        assert GAHydroSource(base_url="https://hydro.example", cache_dir=tmp_path / "c2", client=c_err).fetch_overlay(
            layout, tmp_path / "w2"
        ) is None


@pytest.mark.skipif(not HAS_GDAL, reason="GDAL/OGR required for local_gpkg override test")
def test_au_overlay_airgapped_local_gpkg_override(tmp_path: Path) -> None:
    from osgeo import ogr, osr  # type: ignore[import-untyped]

    layout = _uluru_layout()
    local_src = tmp_path / "airgapped_parks.gpkg"

    ogr.UseExceptions()
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds = ogr.GetDriverByName("GPKG").CreateDataSource(str(local_src))
    lyr = ds.CreateLayer("au_protected_areas", srs=srs, geom_type=ogr.wkbPolygon)
    lyr.CreateField(ogr.FieldDefn("name", ogr.OFTString))
    lyr.CreateField(ogr.FieldDefn("type_abbr", ogr.OFTString))
    feat = ogr.Feature(lyr.GetLayerDefn())
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for x, y in ((131.02, -25.36), (131.05, -25.36), (131.05, -25.33), (131.02, -25.33), (131.02, -25.36)):
        ring.AddPoint(x, y)
    poly = ogr.Geometry(ogr.wkbPolygon)
    poly.AddGeometry(ring)
    feat.SetGeometry(poly)
    feat.SetField("name", "Uluru-Kata Tjuta")
    feat.SetField("type_abbr", "NPC")
    lyr.CreateFeature(feat)
    feat = None
    ds = None

    out = CAPADParksSource(local_gpkg=local_src).fetch_overlay(layout, tmp_path / "work_local")
    assert out is not None and out.name == "au_parks.gpkg"
    with sqlite3.connect(str(out)) as conn:
        srs_id = conn.execute(
            "SELECT srs_id FROM gpkg_contents WHERE table_name='au_protected_areas'"
        ).fetchone()[0]
        assert srs_id == 32752
        assert conn.execute("SELECT name, type_abbr FROM au_protected_areas").fetchall() == [
            ("Uluru-Kata Tjuta", "NPC")
        ]

    with pytest.raises(FileNotFoundError):
        CAPADParksSource(local_gpkg=tmp_path / "missing.gpkg").fetch_overlay(layout, tmp_path / "w_err")
