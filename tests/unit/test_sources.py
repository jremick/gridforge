"""Unit tests for DEM, OSM, overlay data sources, and OSM normalization parity."""

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
    Units,
)
from gridforge.pipeline.normalize import CORE_OSM_LAYERS, normalize_osm_to_gpkg
from gridforge.sources.base import ElevationSource, OverlaySource, VectorSource
from gridforge.sources.dem_copernicus import (
    CopernicusGLO30Source,
    filter_available_tiles,
    format_tile_name,
    parse_tile_list,
    tile_url,
    tiles_for_bbox,
)
from gridforge.sources.dem_local import LocalDEMSource
from gridforge.sources.osm_overpass import (
    USER_AGENT,
    OverpassOSMSource,
    build_overpass_query,
    extract_overpass_remark,
    validate_osm_xml_payload,
)
from gridforge.sources.osm_pbf import LocalPBFSource
from gridforge.sources.overlay_user import UserOverlaySource

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"
HAS_GDAL_CLI = shutil.which("ogr2ogr") is not None and shutil.which("gdalbuildvrt") is not None
HAS_OSMIUM = shutil.which("osmium") is not None


def _blue_mountains_layout(units: Units = Units.METRIC, scale: int = 25000) -> SheetLayout:
    """Deterministic SheetLayout centered on Katoomba NSW (-33.732, 150.312, EPSG:32756)."""
    spec = SheetSpec(
        center_lat=-33.732,
        center_lon=150.312,
        scale=scale,
        paper=PaperSize.A4,
        orientation=Orientation.PORTRAIT,
        color_mode=ColorMode.COLOR,
        units=units,
    )
    margins = FrameMarginsMM()
    frame_w = 210.0 - margins.left - margins.right
    frame_h = 297.0 - margins.top - margins.bottom
    ground_w = frame_w * scale / 1000.0
    ground_h = frame_h * scale / 1000.0
    # Approximate UTM 56S coordinates for (-33.732, 150.312): E=250865, N=6264305
    cx, cy = 250865.0, 6264305.0
    buf = max(1000.0, 0.15 * max(ground_w, ground_h))
    return SheetLayout(
        spec=spec,
        utm_epsg=32756,
        utm_zone=56,
        utm_hemisphere="S",
        gzd="56H",
        center_easting=cx,
        center_northing=cy,
        page_width_mm=210.0,
        page_height_mm=297.0,
        margins_mm=margins,
        frame_x_mm=margins.left,
        frame_y_mm=margins.top,
        frame_width_mm=frame_w,
        frame_height_mm=frame_h,
        utm_bbox=BBox(
            min_x=cx - ground_w / 2.0,
            min_y=cy - ground_h / 2.0,
            max_x=cx + ground_w / 2.0,
            max_y=cy + ground_h / 2.0,
        ),
        utm_fetch_bbox=BBox(
            min_x=cx - ground_w / 2.0 - buf,
            min_y=cy - ground_h / 2.0 - buf,
            max_x=cx + ground_w / 2.0 + buf,
            max_y=cy + ground_h / 2.0 + buf,
        ),
        wgs84_bbox=BBox(min_x=150.285, min_y=-33.760, max_x=150.340, max_y=-33.704),
        wgs84_fetch_bbox=BBox(min_x=150.270, min_y=-33.772, max_x=150.355, max_y=-33.692),
    )


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_source_protocols(tmp_path: Path) -> None:
    assert isinstance(CopernicusGLO30Source(cache_dir=tmp_path), ElevationSource)
    assert isinstance(LocalDEMSource(FIXTURES_DIR / "synthetic_dem.tif"), ElevationSource)
    assert isinstance(OverpassOSMSource(cache_dir=tmp_path), VectorSource)
    assert isinstance(LocalPBFSource(FIXTURES_DIR / "synthetic_osm.osm"), VectorSource)
    assert isinstance(UserOverlaySource(FIXTURES_DIR / "synthetic_overlay.geojson"), OverlaySource)


# ---------------------------------------------------------------------------
# Copernicus GLO-30 tile naming, hemisphere math, and tileList.txt filtering
# ---------------------------------------------------------------------------


def test_copernicus_tile_naming_hemispheres() -> None:
    # Southern + Eastern Hemisphere (Blue Mountains NSW: -33.732, 150.312 -> floor = -34, 150)
    nsw_bbox = BBox(min_x=150.28, min_y=-33.76, max_x=150.35, max_y=-33.70)
    assert tiles_for_bbox(nsw_bbox) == ["Copernicus_DSM_COG_10_S34_00_E150_00_DEM"]
    assert tile_url("Copernicus_DSM_COG_10_S34_00_E150_00_DEM") == (
        "https://copernicus-dem-30m.s3.eu-central-1.amazonaws.com/"
        "Copernicus_DSM_COG_10_S34_00_E150_00_DEM/"
        "Copernicus_DSM_COG_10_S34_00_E150_00_DEM.tif"
    )

    # Northern + Western Hemisphere (Colorado: 39.7392, -105.5 -> floor = 39, -106)
    co_bbox = BBox(min_x=-105.55, min_y=39.70, max_x=-105.45, max_y=39.78)
    assert tiles_for_bbox(co_bbox) == ["Copernicus_DSM_COG_10_N39_00_W106_00_DEM"]

    # Equator + Prime Meridian boundary
    eq_bbox = BBox(min_x=-0.1, min_y=-0.1, max_x=0.1, max_y=0.1)
    assert set(tiles_for_bbox(eq_bbox)) == {
        "Copernicus_DSM_COG_10_S01_00_W001_00_DEM",
        "Copernicus_DSM_COG_10_S01_00_E000_00_DEM",
        "Copernicus_DSM_COG_10_N00_00_W001_00_DEM",
        "Copernicus_DSM_COG_10_N00_00_E000_00_DEM",
    }

    # Exact integer upper boundary should not pull unnecessary northern/eastern tile
    exact_edge = BBox(min_x=150.0, min_y=-34.0, max_x=151.0, max_y=-33.0)
    assert tiles_for_bbox(exact_edge) == ["Copernicus_DSM_COG_10_S34_00_E150_00_DEM"]

    # Anti-meridian wraparound (179.8°E -> 180.2°E wraps into W180)
    antimeridian = BBox(min_x=179.8, min_y=-17.2, max_x=180.2, max_y=-16.8)
    assert set(tiles_for_bbox(antimeridian)) == {
        format_tile_name(-18, 179),
        format_tile_name(-18, -180),
        format_tile_name(-17, 179),
        format_tile_name(-17, -180),
    }


def test_copernicus_tile_list_filtering_and_open_ocean_error(tmp_path: Path) -> None:
    manifest_text = "\n".join(
        [
            "Copernicus_DSM_COG_10_S34_00_E150_00_DEM/",
            "Copernicus_DSM_COG_10_N39_00_W106_00_DEM",
            "Copernicus_DSM_COG_10_N39_00_W105_00_DEM/Copernicus_DSM_COG_10_N39_00_W105_00_DEM.tif",
        ]
    )
    available = parse_tile_list(manifest_text)
    assert "Copernicus_DSM_COG_10_S34_00_E150_00_DEM" in available
    assert "Copernicus_DSM_COG_10_N39_00_W106_00_DEM" in available
    assert "Copernicus_DSM_COG_10_N39_00_W105_00_DEM" in available

    # Mixed coastal + ocean candidates -> only land tile survives
    candidates = [
        "Copernicus_DSM_COG_10_S34_00_E150_00_DEM",
        "Copernicus_DSM_COG_10_S34_00_E152_00_DEM",  # Tasman Sea ocean tile
    ]
    assert filter_available_tiles(candidates, available) == [
        "Copernicus_DSM_COG_10_S34_00_E150_00_DEM"
    ]

    # Open ocean fetch raises clear RuntimeError using cached tileList.txt (no network)
    src = CopernicusGLO30Source(cache_dir=tmp_path)
    src.cache_dir.mkdir(parents=True, exist_ok=True)
    src.tile_list_cache_path.write_text(manifest_text, encoding="utf-8")

    ocean_layout = _blue_mountains_layout().model_copy(
        update={
            "wgs84_fetch_bbox": BBox(min_x=160.1, min_y=-33.6, max_x=160.3, max_y=-33.4),
        }
    )
    with pytest.raises(RuntimeError, match="No Copernicus GLO-30 land tiles intersect"):
        src.fetch_dem(ocean_layout, tmp_path / "work")


# ---------------------------------------------------------------------------
# LocalDEMSource
# ---------------------------------------------------------------------------


def test_local_dem_source_file_and_errors(tmp_path: Path) -> None:
    layout = _blue_mountains_layout()
    dem_file = FIXTURES_DIR / "synthetic_dem.tif"
    src = LocalDEMSource(dem_file)
    resolved = src.fetch_dem(layout, tmp_path / "work")
    assert resolved == dem_file.resolve()

    with pytest.raises(FileNotFoundError):
        LocalDEMSource(tmp_path / "does_not_exist.tif").fetch_dem(layout, tmp_path / "work2")

    empty_dir = tmp_path / "empty_dems"
    empty_dir.mkdir()
    with pytest.raises(FileNotFoundError):
        LocalDEMSource(empty_dir).fetch_dem(layout, tmp_path / "work3")


@pytest.mark.skipif(not HAS_GDAL_CLI, reason="gdalbuildvrt not available on host")
def test_local_dem_source_directory_vrt(tmp_path: Path) -> None:
    layout = _blue_mountains_layout()
    dem_dir = tmp_path / "dem_tiles"
    dem_dir.mkdir()
    shutil.copy2(FIXTURES_DIR / "synthetic_dem.tif", dem_dir / "tile_a.tif")

    vrt_path = LocalDEMSource(dem_dir).fetch_dem(layout, tmp_path / "work")
    assert vrt_path.suffix == ".vrt"
    assert vrt_path.is_file()
    assert "tile_a.tif" in vrt_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# OverpassOSMSource: query format, <remark> detection, failover, disk cache
# ---------------------------------------------------------------------------


def test_overpass_query_is_gdal_compatible() -> None:
    bbox = BBox(min_x=150.27, min_y=-33.77, max_x=150.36, max_y=-33.69)
    q = build_overpass_query(bbox)
    assert "[out:xml][timeout:90][maxsize:536870912];" in q
    assert "(nwr(-33.770000,150.270000,-33.690000,150.360000); >;);" in q
    assert q.strip().endswith("out body;")
    assert "skel" not in q.lower()
    assert "qt;" not in q.lower()


def test_overpass_remark_and_truncation_detection() -> None:
    remark_xml = (
        b'<?xml version="1.0" encoding="UTF-8"?>\n'
        b"<osm version='0.6'>\n"
        b"<remark> runtime error: Query timed out in \"recurse\" at line 2 after 91 seconds. </remark>\n"
        b"</osm>\n"
    )
    remark = extract_overpass_remark(remark_xml)
    assert remark is not None
    assert "Query timed out" in remark
    with pytest.raises(RuntimeError, match="Overpass runtime error remark"):
        validate_osm_xml_payload(remark_xml)

    truncated = b'<?xml version="1.0"?><osm version="0.6"><node id="1" lat="0" lon="0"/>'
    with pytest.raises(RuntimeError, match="truncated"):
        validate_osm_xml_payload(truncated)

    valid = (FIXTURES_DIR / "synthetic_osm.osm").read_bytes()
    assert extract_overpass_remark(valid) is None
    validate_osm_xml_payload(valid)


def test_overpass_failover_429_remark_and_disk_cache(tmp_path: Path) -> None:
    layout = _blue_mountains_layout()
    valid_osm = (FIXTURES_DIR / "synthetic_osm.osm").read_bytes()
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((str(request.url), request.headers.get("User-Agent", "")))
        if "mirror1" in str(request.url):
            return httpx.Response(429, headers={"Retry-After": "1"}, text="Too Many Requests")
        if "mirror2" in str(request.url):
            return httpx.Response(
                200,
                content=b'<?xml version="1.0"?><osm version="0.6"><remark>runtime error: out of memory</remark></osm>',
            )
        return httpx.Response(200, content=valid_osm)

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        src = OverpassOSMSource(
            endpoints=[
                "https://mirror1.example/api/interpreter",
                "https://mirror2.example/api/interpreter",
                "https://mirror3.example/api/interpreter",
            ],
            cache_dir=tmp_path / "cache",
            rate_limit_backoff_s=0.0,
            transient_backoff_s=0.0,
            client=client,
        )

        # First call fails over from mirror1 (429) -> mirror2 (200 + <remark>) -> mirror3 (200 OK)
        out1 = src.fetch_osm(layout, tmp_path / "run1")
        assert out1.is_file()
        assert len(calls) == 3
        assert all(ua == USER_AGENT for _, ua in calls)

        # Second call hits disk cache (zero additional HTTP requests)
        out2 = src.fetch_osm(layout, tmp_path / "run2")
        assert out2.is_file()
        assert len(calls) == 3


# ---------------------------------------------------------------------------
# Online XML vs Offline PBF Normalization Parity + Empty Layer Schema Guarantee
# ---------------------------------------------------------------------------


def _inspect_gpkg_schema_and_counts(gpkg_path: Path) -> dict[str, tuple[set[str], int]]:
    from osgeo import ogr  # type: ignore[import-untyped]

    ogr.UseExceptions()
    ds = ogr.Open(str(gpkg_path))
    assert ds is not None
    try:
        summary: dict[str, tuple[set[str], int]] = {}
        for idx in range(ds.GetLayerCount()):
            layer = ds.GetLayerByIndex(idx)
            defn = layer.GetLayerDefn()
            cols = {defn.GetFieldDefn(i).GetName() for i in range(defn.GetFieldCount())}
            summary[layer.GetName()] = (cols, layer.GetFeatureCount())
        return summary
    finally:
        ds = None


@pytest.mark.skipif(
    not (HAS_GDAL_CLI and HAS_OSMIUM),
    reason="ogr2ogr and osmium are required for normalization parity tests",
)
def test_online_xml_and_offline_pbf_normalization_parity(tmp_path: Path) -> None:
    layout = _blue_mountains_layout()
    raw_xml = FIXTURES_DIR / "synthetic_osm.osm"

    # Path A: Online Overpass XML -> normalize_osm_to_gpkg
    gpkg_online = normalize_osm_to_gpkg(raw_xml, layout, tmp_path / "online" / "osm.gpkg")

    # Path B: Offline LocalPBFSource (osmium extract -> .osm.pbf) -> normalize_osm_to_gpkg
    extracted_pbf = LocalPBFSource(raw_xml).fetch_osm(layout, tmp_path / "offline_src")
    assert extracted_pbf.suffix == ".pbf"
    gpkg_offline = normalize_osm_to_gpkg(extracted_pbf, layout, tmp_path / "offline" / "osm.gpkg")

    online_info = _inspect_gpkg_schema_and_counts(gpkg_online)
    offline_info = _inspect_gpkg_schema_and_counts(gpkg_offline)

    for layer_name in CORE_OSM_LAYERS:
        assert layer_name in online_info
        assert layer_name in offline_info
        assert online_info[layer_name][0] == offline_info[layer_name][0]
        assert online_info[layer_name][1] == offline_info[layer_name][1]

    # Verify expected synthetic features survived normalization with exact cartographic attributes
    assert online_info["points"][1] >= 5
    assert online_info["lines"][1] >= 7
    assert online_info["multipolygons"][1] >= 5

    with sqlite3.connect(str(gpkg_offline)) as conn:
        peaks = conn.execute(
            "SELECT name, ele FROM points WHERE natural = 'peak' ORDER BY name"
        ).fetchall()
        assert ("Mount Solitary", "950") in peaks

        intermittent = conn.execute(
            "SELECT name FROM lines WHERE waterway = 'stream' AND intermittent = 'yes'"
        ).fetchall()
        assert ("Dry Gully",) in intermittent

        woods = conn.execute(
            "SELECT name, leaf_type FROM multipolygons WHERE natural = 'wood' ORDER BY name"
        ).fetchall()
        assert ("Jamison Forest", "broadleaved") in woods
        assert ("Pine Stand", "needleleaved") in woods

        # Verify SRS is EPSG:32756 across all core layers
        srs_rows = conn.execute(
            "SELECT table_name, srs_id FROM gpkg_contents WHERE table_name IN ('points','lines','multilinestrings','multipolygons')"
        ).fetchall()
        assert len(srs_rows) == 4
        assert all(srs_id == 32756 for _, srs_id in srs_rows)


@pytest.mark.skipif(not HAS_GDAL_CLI, reason="ogr2ogr required for empty layer test")
def test_normalize_guarantees_all_core_layers_when_sparse(tmp_path: Path) -> None:
    layout = _blue_mountains_layout()
    minimal_xml = tmp_path / "minimal_points_only.osm"
    minimal_xml.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<osm version="0.6" generator="gridforge-synthetic-minimal">\n'
        '  <bounds minlat="-33.750" minlon="150.290" maxlat="-33.710" maxlon="150.330"/>\n'
        '  <node id="1" lat="-33.7320" lon="150.3120" version="1">\n'
        '    <tag k="natural" v="peak"/>\n'
        '    <tag k="name" v="Lone Cairn"/>\n'
        '    <tag k="ele" v="750"/>\n'
        '  </node>\n'
        '</osm>\n',
        encoding="utf-8",
    )
    gpkg = normalize_osm_to_gpkg(minimal_xml, layout, tmp_path / "sparse.gpkg")

    info = _inspect_gpkg_schema_and_counts(gpkg)
    for layer_name in CORE_OSM_LAYERS:
        assert layer_name in info

    assert info["points"][1] == 1
    assert info["lines"][1] == 0
    assert info["multipolygons"][1] == 0
    # Cartographic columns exist even on 0-feature layers so PyQGIS rules never fail
    assert {"highway", "waterway", "intermittent", "railway", "power"}.issubset(info["lines"][0])
    assert {"natural", "landuse", "leaf_type", "building", "water"}.issubset(
        info["multipolygons"][0]
    )


# ---------------------------------------------------------------------------
# UserOverlaySource (GeoJSON, GPX, CSV)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not HAS_GDAL_CLI, reason="ogr2ogr required for overlay tests")
def test_user_overlay_source_geojson_gpx_and_csv(tmp_path: Path) -> None:
    layout = _blue_mountains_layout()
    src = UserOverlaySource(
        [
            FIXTURES_DIR / "synthetic_overlay.geojson",
            FIXTURES_DIR / "synthetic_overlay.gpx",
            FIXTURES_DIR / "synthetic_overlay.csv",
        ]
    )
    out_gpkg = src.fetch_overlay(layout, tmp_path / "overlay_work")
    assert out_gpkg is not None
    assert out_gpkg.name == "overlays.gpkg"

    info = _inspect_gpkg_schema_and_counts(out_gpkg)
    # 1 GeoJSON point + 1 GPX waypoint + 2 CSV points = 4 points
    assert info["overlay_points"][1] == 4
    # 1 GeoJSON LineString + 1 GPX track = 2 lines
    assert info["overlay_lines"][1] == 2
    # 1 GeoJSON Polygon = 1 polygon
    assert info["overlay_polygons"][1] == 1

    with sqlite3.connect(str(out_gpkg)) as conn:
        srs_ids = {
            row[0]
            for row in conn.execute("SELECT srs_id FROM gpkg_contents").fetchall()
        }
        assert srs_ids == {32756}


def test_user_overlay_csv_missing_lat_lon_raises(tmp_path: Path) -> None:
    layout = _blue_mountains_layout()
    bad_csv = tmp_path / "bad.csv"
    bad_csv.write_text("id,label\n1,foo\n", encoding="utf-8")
    with pytest.raises(ValueError, match="longitude and latitude columns"):
        UserOverlaySource(bad_csv).fetch_overlay(layout, tmp_path / "work")
