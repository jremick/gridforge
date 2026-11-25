"""Unit tests for v1.1.0 GDAL slope-angle shading and NSW/VIC state trail/POI overlays."""

from __future__ import annotations

import math
import shutil
import sqlite3
from pathlib import Path

import httpx
import pytest

from gridforge.config import (
    AUOverlayKind,
    BBox,
    CartoOptions,
    ColorMode,
    FrameMarginsMM,
    Orientation,
    PaperSize,
    SheetLayout,
    SheetSpec,
    Units,
)
from gridforge.pipeline.terrain import build_slope_shading, build_terrain_products
from gridforge.sources.base import OverlaySource
from gridforge.sources.overlays_au import build_au_overlay_sources
from gridforge.sources.overlays_au_states import (
    NSWTrailsSource,
    VICTrailsSource,
    is_in_nsw_coverage,
    is_in_vic_coverage,
)

HAS_GDAL = all(shutil.which(cmd) is not None for cmd in ("gdalwarp", "gdaldem", "gdal_contour", "ogr2ogr"))


def _make_layout(
    *,
    lat: float,
    lon: float,
    utm_epsg: int,
    utm_zone: int,
    gzd: str,
    cx: float,
    cy: float,
    color_mode: ColorMode = ColorMode.COLOR,
    show_slope_shading: bool = False,
) -> SheetLayout:
    spec = SheetSpec(
        center_lat=lat,
        center_lon=lon,
        scale=25000,
        paper=PaperSize.A4,
        orientation=Orientation.PORTRAIT,
        color_mode=color_mode,
        units=Units.METRIC,
        carto=CartoOptions(show_slope_shading=show_slope_shading),
    )
    margins = FrameMarginsMM()
    fw = 210.0 - margins.left - margins.right
    fh = 297.0 - margins.top - margins.bottom
    gw = fw * 25000 / 1000.0
    gh = fh * 25000 / 1000.0
    buf = 1000.0
    dlat = 0.035
    dlon = 0.045
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


def _kosciuszko_layout(*, color_mode: ColorMode = ColorMode.COLOR, show_slope_shading: bool = False) -> SheetLayout:
    return _make_layout(
        lat=-36.456,
        lon=148.263,
        utm_epsg=32755,
        utm_zone=55,
        gzd="55H",
        cx=613155.0,
        cy=5964800.0,
        color_mode=color_mode,
        show_slope_shading=show_slope_shading,
    )


def _grampians_layout() -> SheetLayout:
    return _make_layout(
        lat=-37.145,
        lon=142.515,
        utm_epsg=32754,
        utm_zone=54,
        gzd="54H",
        cx=634550.0,
        cy=5887950.0,
    )


def _uluru_layout() -> SheetLayout:
    return _make_layout(
        lat=-25.3444,
        lon=131.0369,
        utm_epsg=32752,
        utm_zone=52,
        gzd="52J",
        cx=704925.0,
        cy=7195345.0,
    )


def _create_banded_slope_dem(path: Path, utm_epsg: int, bbox: BBox, pixel_m: float = 10.0) -> dict[str, int]:
    """Create a synthetic UTM DEM with 6 horizontal strips of exact known slope degrees.

    Returns ``{band_name: sample_row}`` pointing to the middle row of each constant-slope strip.
    """
    import numpy as np  # type: ignore[import-not-found]
    from osgeo import gdal, osr  # type: ignore[import-untyped]

    gdal.UseExceptions()
    cols = int(math.ceil(bbox.width / pixel_m))
    rows = int(math.ceil(bbox.height / pixel_m))

    strips: list[tuple[str, float]] = [
        ("flat", 0.0),
        ("yellow_28", 28.5),
        ("orange_32", 32.5),
        ("red_40", 40.0),
        ("purple_47", 47.5),
        ("indigo_55", 55.0),
    ]
    strip_h = rows // len(strips)
    elev = np.full((rows, cols), 1500.0, dtype=np.float32)
    sample_rows: dict[str, int] = {}

    for idx, (name, deg) in enumerate(strips):
        r0 = idx * strip_h
        r1 = rows if idx == len(strips) - 1 else (idx + 1) * strip_h
        dz_per_px = math.tan(math.radians(deg)) * pixel_m
        local_rows = np.arange(r1 - r0, dtype=np.float32)[:, None]
        elev[r0:r1, :] = 1500.0 + local_rows * dz_per_px
        sample_rows[name] = r0 + (r1 - r0) // 2

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
    return sample_rows


def _read_rgba_pixel(tif_path: Path, row: int, col: int) -> tuple[int, int, int, int]:
    from osgeo import gdal  # type: ignore[import-untyped]

    ds = gdal.Open(str(tif_path), gdal.GA_ReadOnly)
    assert ds is not None
    try:
        assert ds.RasterCount == 4
        return tuple(int(ds.GetRasterBand(b).ReadAsArray(col, row, 1, 1)[0, 0]) for b in (1, 2, 3, 4))  # type: ignore[return-value]
    finally:
        ds = None


@pytest.mark.skipif(not HAS_GDAL, reason="GDAL required for slope-angle shading tests")
def test_slope_shading_color_and_bw_bands_and_cleanup(tmp_path: Path) -> None:
    from osgeo import gdal, osr  # type: ignore[import-untyped]

    layout_off = _kosciuszko_layout(color_mode=ColorMode.COLOR, show_slope_shading=False)
    dem_path = tmp_path / "banded_dem.tif"
    sample_rows = _create_banded_slope_dem(dem_path, layout_off.utm_epsg, layout_off.utm_fetch_bbox, pixel_m=10.0)

    # Disabled by default -> slope_tif is None
    res_off = build_terrain_products(dem_path, layout_off, tmp_path / "work_off")
    assert res_off.slope_tif is None
    assert not (tmp_path / "work_off" / "slope_shading.tif").exists()

    # Color mode -> exact Caltopo/avalanche RGBA bands + temp cleanup
    layout_color = _kosciuszko_layout(color_mode=ColorMode.COLOR, show_slope_shading=True)
    work_color = tmp_path / "work_color"
    res_color = build_terrain_products(dem_path, layout_color, work_color)
    assert res_color.slope_tif is not None and res_color.slope_tif.is_file()
    assert res_color.slope_tif.name == "slope_shading.tif"
    assert not (work_color / "slope_deg.tif").exists()
    assert not (work_color / "slope_color_table.txt").exists()

    ds = gdal.Open(str(res_color.slope_tif))
    assert ds is not None and ds.RasterCount == 4
    assert ds.GetRasterBand(1).DataType == gdal.GDT_Byte
    srs = osr.SpatialReference(wkt=ds.GetProjection())
    srs.AutoIdentifyEPSG()
    assert srs.GetAuthorityCode(None) == "32755"
    mid_col = ds.RasterXSize // 2
    ds = None

    assert _read_rgba_pixel(res_color.slope_tif, sample_rows["flat"], mid_col) == (0, 0, 0, 0)
    assert _read_rgba_pixel(res_color.slope_tif, sample_rows["yellow_28"], mid_col) == (246, 224, 94, 115)
    assert _read_rgba_pixel(res_color.slope_tif, sample_rows["orange_32"], mid_col) == (237, 137, 54, 155)
    assert _read_rgba_pixel(res_color.slope_tif, sample_rows["red_40"], mid_col) == (229, 62, 62, 185)
    assert _read_rgba_pixel(res_color.slope_tif, sample_rows["purple_47"], mid_col) == (128, 90, 213, 195)
    assert _read_rgba_pixel(res_color.slope_tif, sample_rows["indigo_55"], mid_col) == (59, 7, 100, 205)

    # B&W mode -> progressive monochrome alpha-gray bands (>= 30°, >= 35°, >= 45°)
    bw_tif = build_slope_shading(res_color.dem_utm_tif, ColorMode.BW, tmp_path / "work_bw")
    assert bw_tif.is_file()
    assert _read_rgba_pixel(bw_tif, sample_rows["flat"], mid_col) == (0, 0, 0, 0)
    assert _read_rgba_pixel(bw_tif, sample_rows["yellow_28"], mid_col) == (0, 0, 0, 0)
    assert _read_rgba_pixel(bw_tif, sample_rows["orange_32"], mid_col) == (120, 120, 120, 85)
    assert _read_rgba_pixel(bw_tif, sample_rows["red_40"], mid_col) == (55, 55, 55, 135)
    assert _read_rgba_pixel(bw_tif, sample_rows["purple_47"], mid_col) == (15, 15, 15, 180)
    assert _read_rgba_pixel(bw_tif, sample_rows["indigo_55"], mid_col) == (15, 15, 15, 180)


def test_state_overlay_factory_and_coverage_bounds(tmp_path: Path) -> None:
    sources = build_au_overlay_sources(
        [AUOverlayKind.AU_NSW_TRAILS, "au-vic-trails", "au_nsw_trails"],
        local_overrides={"au-vic-trails": tmp_path / "vic_local.gpkg"},
        cache_dir=tmp_path,
    )
    assert list(sources.keys()) == ["au-nsw-trails", "au-vic-trails"]
    assert isinstance(sources["au-nsw-trails"], NSWTrailsSource)
    assert isinstance(sources["au-vic-trails"], VICTrailsSource)
    assert all(isinstance(s, OverlaySource) for s in sources.values())
    assert sources["au-vic-trails"].local_gpkg == tmp_path / "vic_local.gpkg"
    assert "State of New South Wales" in sources["au-nsw-trails"].attribution
    assert "State of Victoria" in sources["au-vic-trails"].attribution

    # Bounds helpers
    assert is_in_nsw_coverage(_kosciuszko_layout().wgs84_fetch_bbox)
    assert not is_in_nsw_coverage(_uluru_layout().wgs84_fetch_bbox)
    assert is_in_vic_coverage(_grampians_layout().wgs84_fetch_bbox)
    assert not is_in_vic_coverage(_uluru_layout().wgs84_fetch_bbox)
    # Blue Mountains (-33.732) is north of VIC (-33.8)
    assert not is_in_vic_coverage(BBox(min_x=150.28, min_y=-33.76, max_x=150.34, max_y=-33.70))


def test_out_of_state_short_circuits_without_http(tmp_path: Path) -> None:
    called = False

    def fail_handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        raise AssertionError(f"Should not issue HTTP request out of state: {request.url}")

    uluru = _uluru_layout()
    with httpx.Client(transport=httpx.MockTransport(fail_handler)) as client:
        assert NSWTrailsSource(cache_dir=tmp_path / "c1", client=client).fetch_overlay(uluru, tmp_path / "w1") is None
        assert VICTrailsSource(cache_dir=tmp_path / "c2", client=client).fetch_overlay(uluru, tmp_path / "w2") is None
    assert not called


@pytest.mark.skipif(not HAS_GDAL, reason="GDAL/OGR required for GeoPackage state overlay tests")
def test_nsw_trails_source_normalization_and_cache(tmp_path: Path) -> None:
    layout = _kosciuszko_layout()
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        url = str(request.url)
        if "/transport/9/query" in url:
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {"brimsid": "FT-104", "operationalstatus": "Operational", "surface": 2},
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[148.250, -36.460], [148.270, -36.450]],
                            },
                        }
                    ],
                },
            )
        if "/transport/5/query" in url:
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {
                                "classsubtype": 6,
                                "roadnamebase": "MAIN RANGE",
                                "roadnametype": "TRACK",
                                "surface": 0,
                            },
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[148.255, -36.458, 2000.0], [148.268, -36.452, 2228.0]],
                            },
                        },
                        {
                            "type": "Feature",
                            "properties": {
                                "classsubtype": 5,
                                "roadnamebase": "SUMMIT",
                                "roadnametype": "TRAIL",
                                "surface": 2,
                            },
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[148.260, -36.462], [148.272, -36.456]],
                            },
                        },
                        {
                            "type": "Feature",
                            "properties": {
                                "classsubtype": 5,
                                "roadnamebase": "SCHLINK",
                                "roadnametype": "MANAGEMENT TRAIL",
                                "surface": 2,
                            },
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[148.252, -36.455], [148.266, -36.449]],
                            },
                        },
                    ],
                },
            )
        if "/trig/query" in url:
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {
                                "marktype": "TS",
                                "marknumber": 1001,
                                "trigname": "KOSCIUSZKO",
                                "ahdheight_label": "2229.470",
                            },
                            "geometry": {"type": "Point", "coordinates": [148.2635, -36.4559]},
                        }
                    ],
                },
            )
        if "/poi/query" in url:
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {"poitype": "Camping Ground", "poilabel": "Wilkinsons Creek Camp"},
                            "geometry": {"type": "Point", "coordinates": [148.258, -36.452]},
                        },
                        {
                            "type": "Feature",
                            "properties": {"poitype": "Lookout", "poilabel": "Muellers Pass Lookout"},
                            "geometry": {"type": "Point", "coordinates": [148.261, -36.450]},
                        },
                        {
                            "type": "Feature",
                            "properties": {"poitype": "Picnic Area", "poilabel": "Rawson Pass"},
                            "geometry": {"type": "Point", "coordinates": [148.265, -36.461]},
                        },
                        {
                            "type": "Feature",
                            "properties": {"poitype": "Post Office", "poilabel": "Ignored POI"},
                            "geometry": {"type": "Point", "coordinates": [148.264, -36.457]},
                        },
                        {
                            "type": "Feature",
                            "properties": {"poitype": "Hut", "poilabel": "SEAMANS HUT"},
                            "geometry": {"type": "Point", "coordinates": [148.271, -36.463]},
                        },
                    ],
                },
            )
        if "/huts/query" in url:
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        # Duplicate hut at same spot -> deduped
                        {
                            "type": "Feature",
                            "properties": {"generalname": "SEAMANS HUT"},
                            "geometry": {"type": "Point", "coordinates": [148.2711, -36.4631]},
                        },
                        {
                            "type": "Feature",
                            "properties": {"generalname": "COOTAPATAMBA HUT"},
                            "geometry": {"type": "Point", "coordinates": [148.262, -36.464]},
                        },
                    ],
                },
            )
        return httpx.Response(200, json={"type": "FeatureCollection", "features": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        src = NSWTrailsSource(
            transport_base_url="https://nsw.example/transport",
            trig_url="https://nsw.example/trig/query",
            poi_url="https://nsw.example/poi/query",
            huts_url="https://nsw.example/huts/query",
            cache_dir=tmp_path / "cache",
            client=client,
        )
        gpkg = src.fetch_overlay(layout, tmp_path / "run1")
        assert gpkg is not None and gpkg.name == "au_nsw_trails.gpkg"
        first_calls = call_count
        assert first_calls == 5

        with sqlite3.connect(str(gpkg)) as conn:
            srs_ids = {r[0] for r in conn.execute("SELECT srs_id FROM gpkg_contents").fetchall()}
            assert srs_ids == {32755}

            trails = conn.execute(
                "SELECT name, trail_class, surface, state FROM au_state_trails ORDER BY trail_class, name"
            ).fetchall()
            assert trails == [
                ("SUMMIT TRAIL", "4wd_track", "unsealed", "NSW"),
                ("Fire Trail FT-104", "fire_trail", "unsealed", "NSW"),
                ("SCHLINK MANAGEMENT TRAIL", "management_trail", "unsealed", "NSW"),
                ("MAIN RANGE TRACK", "walking_track", "natural", "NSW"),
            ]

            pois = conn.execute(
                "SELECT name, poi_type, elev_m, state FROM au_state_pois ORDER BY poi_type, name"
            ).fetchall()
            assert [p[:2] + (p[3],) for p in pois] == [
                ("Wilkinsons Creek Camp", "campground", "NSW"),
                ("COOTAPATAMBA HUT", "hut", "NSW"),
                ("SEAMANS HUT", "hut", "NSW"),
                ("Muellers Pass Lookout", "lookout", "NSW"),
                ("Rawson Pass", "picnic", "NSW"),
                ("KOSCIUSZKO", "trig", "NSW"),
            ]
            trig_elev = [p[2] for p in pois if p[1] == "trig"][0]
            assert trig_elev == pytest.approx(2229.47)

        # Second call hits disk cache (0 additional HTTP requests)
        gpkg2 = src.fetch_overlay(layout, tmp_path / "run2")
        assert gpkg2 is not None
        assert call_count == first_calls


@pytest.mark.skipif(not HAS_GDAL, reason="GDAL/OGR required for GeoPackage state overlay tests")
def test_vic_trails_source_wfs_normalization_and_cache(tmp_path: Path) -> None:
    layout = _grampians_layout()
    seen_cql: dict[str, str] = {}
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        tname = request.url.params.get("typeNames", "")
        seen_cql[tname] = request.url.params.get("CQL_FILTER", "")
        if tname == "open-data-platform:tr_road":
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {
                                "ezi_road_name_label": "WONDERLAND FIRELINE TRACK",
                                "feature_type_code": "road",
                                "class_code": 7,
                                "road_seal": "2",
                            },
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[142.500, -37.150], [142.525, -37.140]],
                            },
                        },
                        {
                            "type": "Feature",
                            "properties": {
                                "ezi_road_name_label": "PINNACLE WALKING TRACK",
                                "feature_type_code": "trail",
                                "class_code": 8,
                                "road_seal": "3",
                            },
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[142.508, -37.148], [142.520, -37.142]],
                            },
                        },
                        {
                            "type": "Feature",
                            "properties": {
                                "ezi_road_name_label": "BOROKA TRACK",
                                "feature_type_code": "road",
                                "class_code": 7,
                                "road_seal": "2",
                            },
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[142.504, -37.146], [142.518, -37.139]],
                            },
                        },
                        {
                            "type": "Feature",
                            "properties": {
                                "ezi_road_name_label": "SILVERBAND ACCESS",
                                "feature_type_code": "road",
                                "class_code": 6,
                                "road_seal": "2",
                            },
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[142.506, -37.152], [142.522, -37.147]],
                            },
                        },
                        {
                            "type": "Feature",
                            "properties": {
                                "ezi_road_name_label": "Unnamed",
                                "feature_type_code": "trail",
                                "class_code": 9,
                                "road_seal": "0",
                            },
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[142.509, -37.149], [142.517, -37.144]],
                            },
                        },
                    ],
                },
            )
        if tname == "open-data-platform:recweb_tracks":
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {"name": "Grampians Peaks Trail", "access_dsc": "Walking Track"},
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[142.510, -37.151], [142.519, -37.138]],
                            },
                        }
                    ],
                },
            )
        if tname == "open-data-platform:foi_point":
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {"feature_subtype": "hut", "name_label": "Cleve Cole Hut"},
                            "geometry": {"type": "Point", "coordinates": [142.515, -37.145]},
                        },
                        {
                            "type": "Feature",
                            "properties": {"feature_subtype": "lookout", "name_label": "The Pinnacle Lookout"},
                            "geometry": {"type": "Point", "coordinates": [142.516, -37.144]},
                        },
                        {
                            "type": "Feature",
                            "properties": {"feature_subtype": "picnic site", "name_label": "Sundial Picnic Area"},
                            "geometry": {"type": "Point", "coordinates": [142.512, -37.147]},
                        },
                    ],
                },
            )
        if tname == "open-data-platform:recweb_hut":
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        # Duplicate hut -> deduped
                        {
                            "type": "Feature",
                            "properties": {"name": "Cleve Cole Hut"},
                            "geometry": {"type": "Point", "coordinates": [142.5151, -37.1451]},
                        },
                    ],
                },
            )
        if tname == "open-data-platform:recweb_site":
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {"name": "Borough Huts Campground", "site_type": "Camping Area"},
                            "geometry": {"type": "Point", "coordinates": [142.521, -37.149]},
                        }
                    ],
                },
            )
        if tname == "open-data-platform:geomark_point":
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {
                                "feature_subtype": "survey monument",
                                "name_label": "MT WILLIAM TRIG",
                                "elevation": 1167.0,
                            },
                            "geometry": {"type": "Point", "coordinates": [142.518, -37.143]},
                        }
                    ],
                },
            )
        return httpx.Response(200, json={"type": "FeatureCollection", "features": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        src = VICTrailsSource(wfs_url="https://vic.example/wfs", cache_dir=tmp_path / "cache", client=client)
        gpkg = src.fetch_overlay(layout, tmp_path / "run1")
        assert gpkg is not None and gpkg.name == "au_vic_trails.gpkg"
        assert call_count == 6
        assert "class_code IN (7,8,9)" in seen_cql["open-data-platform:tr_road"]
        assert "BBOX(geom," in seen_cql["open-data-platform:tr_road"]

        with sqlite3.connect(str(gpkg)) as conn:
            srs_ids = {r[0] for r in conn.execute("SELECT srs_id FROM gpkg_contents").fetchall()}
            assert srs_ids == {32754}

            trails = conn.execute(
                "SELECT name, trail_class, surface, state FROM au_state_trails ORDER BY trail_class, name"
            ).fetchall()
            assert trails == [
                ("BOROKA TRACK", "4wd_track", "unsealed", "VIC"),
                ("WONDERLAND FIRELINE TRACK", "fire_trail", "unsealed", "VIC"),
                ("SILVERBAND ACCESS", "management_trail", "unsealed", "VIC"),
                ("", "walking_track", "natural", "VIC"),
                ("Grampians Peaks Trail", "walking_track", "natural", "VIC"),
                ("PINNACLE WALKING TRACK", "walking_track", "natural", "VIC"),
            ]

            pois = conn.execute(
                "SELECT name, poi_type, elev_m, state FROM au_state_pois ORDER BY poi_type, name"
            ).fetchall()
            assert [p[:2] + (p[3],) for p in pois] == [
                ("Borough Huts Campground", "campground", "VIC"),
                ("Cleve Cole Hut", "hut", "VIC"),
                ("The Pinnacle Lookout", "lookout", "VIC"),
                ("Sundial Picnic Area", "picnic", "VIC"),
                ("MT WILLIAM TRIG", "trig", "VIC"),
            ]
            assert [p[2] for p in pois if p[1] == "trig"][0] == pytest.approx(1167.0)

        # Disk cache hit on second call
        gpkg2 = src.fetch_overlay(layout, tmp_path / "run2")
        assert gpkg2 is not None
        assert call_count == 6


@pytest.mark.skipif(not HAS_GDAL, reason="GDAL/OGR required for local_gpkg override tests")
def test_state_overlays_error_resilience_and_local_gpkg_override(tmp_path: Path) -> None:
    from osgeo import ogr, osr  # type: ignore[import-untyped]

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("simulated timeout")

    kosci = _kosciuszko_layout()
    gramp = _grampians_layout()
    with httpx.Client(transport=httpx.MockTransport(timeout_handler)) as client:
        assert NSWTrailsSource(cache_dir=tmp_path / "c_err1", client=client).fetch_overlay(kosci, tmp_path / "w_err1") is None
        assert VICTrailsSource(cache_dir=tmp_path / "c_err2", client=client).fetch_overlay(gramp, tmp_path / "w_err2") is None

    # Create air-gapped local_gpkg with unified 2-layer schema (in Kosciuszko coords)
    local_gpkg = tmp_path / "local_state_trails.gpkg"
    ogr.UseExceptions()
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds = ogr.GetDriverByName("GPKG").CreateDataSource(str(local_gpkg))
    trails_lyr = ds.CreateLayer("au_state_trails", srs=srs, geom_type=ogr.wkbLineString)
    for field in ("name", "trail_class", "surface", "state"):
        trails_lyr.CreateField(ogr.FieldDefn(field, ogr.OFTString))
    feat = ogr.Feature(trails_lyr.GetLayerDefn())
    line = ogr.Geometry(ogr.wkbLineString)
    line.AddPoint(148.255, -36.458)
    line.AddPoint(148.270, -36.452)
    feat.SetGeometry(line)
    feat.SetField("name", "Alpine Fire Trail")
    feat.SetField("trail_class", "fire_trail")
    feat.SetField("surface", "unsealed")
    feat.SetField("state", "NSW")
    trails_lyr.CreateFeature(feat)
    feat = None

    pois_lyr = ds.CreateLayer("au_state_pois", srs=srs, geom_type=ogr.wkbPoint)
    for field in ("name", "poi_type", "state"):
        pois_lyr.CreateField(ogr.FieldDefn(field, ogr.OFTString))
    pois_lyr.CreateField(ogr.FieldDefn("elev_m", ogr.OFTReal))
    pfeat = ogr.Feature(pois_lyr.GetLayerDefn())
    pt = ogr.Geometry(ogr.wkbPoint)
    pt.AddPoint(148.263, -36.456)
    pfeat.SetGeometry(pt)
    pfeat.SetField("name", "Seamans Hut")
    pfeat.SetField("poi_type", "hut")
    pfeat.SetField("elev_m", 2030.0)
    pfeat.SetField("state", "NSW")
    pois_lyr.CreateFeature(pfeat)
    pfeat = None
    ds = None

    # Air-gapped local_gpkg works for both NSW and VIC sources regardless of online state bounds
    out_nsw = NSWTrailsSource(local_gpkg=local_gpkg).fetch_overlay(kosci, tmp_path / "work_local_nsw")
    out_vic = VICTrailsSource(local_gpkg=local_gpkg).fetch_overlay(kosci, tmp_path / "work_local_vic")
    assert out_nsw is not None and out_nsw.name == "au_nsw_trails.gpkg"
    assert out_vic is not None and out_vic.name == "au_vic_trails.gpkg"

    for out_path in (out_nsw, out_vic):
        with sqlite3.connect(str(out_path)) as conn:
            assert conn.execute("SELECT name, trail_class FROM au_state_trails").fetchall() == [
                ("Alpine Fire Trail", "fire_trail")
            ]
            assert conn.execute("SELECT name, poi_type, elev_m FROM au_state_pois").fetchall() == [
                ("Seamans Hut", "hut", 2030.0)
            ]

    with pytest.raises(FileNotFoundError):
        NSWTrailsSource(local_gpkg=tmp_path / "missing.gpkg").fetch_overlay(kosci, tmp_path / "w_missing")
