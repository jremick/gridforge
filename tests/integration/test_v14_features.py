"""GridForge v1.4.0 Tactical, Cartographic, Offline Pack, Export & North America E2E Tests.

Covers:
  1. Offline CI tests (default, zero network):
     - New themes (`night-red` headlamp-safe & `alpine` winter/glacier) + mock North America
       overlays (`us-trails`, `us-public-lands`, `ca-canvec-trails`) -> exact 40.00 mm 1 km grid.
     - RF Fresnel Point-to-Point Link Card (`RFLinkSpec` + `include_rf_link_card=True`),
       Tactical Graphics (`range_ring`, `bearing_line`, `sector`), and new offline handheld exports:
       OGC GeoPackage raster tiles (`.gpkg`), OsmAnd `.sqlitedb` (`BigPlanet` `z=17-zoom`),
       and Field Bundle `.zip` (PDF + digital archives + GPX 1.1 + README).
     - Pure-GDAL Offline Basemap MBTiles Builder (`basemap.mbtiles`) + per-pack `/api/basemap/*?pack_id=`,
       29-region grouped `/api/packs/regions` (`au_tactical`:7, `au_state`:8, `nz`:6, `na`:8),
       `POST /api/ruler-metrics`, Saved Projects CRUD (`/api/projects`), and `SameOriginWritesMiddleware` 403s.
  2. Live E2E Suite (`@pytest.mark.live` + `GRIDFORGE_RUN_LIVE=1`):
     - US Alpine + Trails/Public Lands — Yosemite Valley CA (`37.745, -119.593`, `11S`,
       `EPSG:32611`, `alpine` theme, `usgs3dep` `NAVD88`, `us-trails` + `us-public-lands` + slope shading).
     - Canada HRDEM + Night-Red — Banff / Sulphur Mtn AB (`51.178, -115.571`, `11U`, `EPSG:32611`,
       `night-red` theme, `ca-hrdem` `CGVD2013`, `ca-canvec-trails`).
     - Mt Kosciuszko NSW — RF Viewshed + 915 MHz Fresnel Link Card + Tactical Range Rings/Sectors +
       `.gpkg` + `.sqlitedb` + Field Bundle `.zip` (`-36.456, 148.263`, `55H`, `EPSG:32755`).
"""

from __future__ import annotations

import json
import os
import resource
import shutil
import sqlite3
import sys
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from gridforge.config import (
    AUOverlayKind,
    BBox,
    CAOverlayKind,
    CartoOptions,
    ColorMode,
    DemSourceKind,
    Orientation,
    PackManifest,
    PaperSize,
    RFLinkSpec,
    SheetLayout,
    SheetSpec,
    TacticalGraphic,
    Units,
    USOverlayKind,
    ViewshedObserver,
    WaypointCategory,
    WaypointInput,
)
from gridforge.geo.crs import compute_sheet_layout
from gridforge.pipeline.runner import prepare_job_workspace, run_render_job
from gridforge.sources.dem_local import LocalDEMSource
from gridforge.sources.osm_pbf import LocalPBFSource

_INTEGRATION_DIR = Path(__file__).resolve().parent
if str(_INTEGRATION_DIR) not in sys.path:
    sys.path.insert(0, str(_INTEGRATION_DIR))

from verify_pdf import (  # noqa: E402
    assert_multipage_pdf_valid,
    assert_pdf_valid,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"

BLUE_MOUNTAINS_LAT = -33.732
BLUE_MOUNTAINS_LON = 150.312
BLUE_MOUNTAINS_EPSG = 32756
BLUE_MOUNTAINS_GZD = "56H"

KOSCIUSZKO_LAT = -36.456
KOSCIUSZKO_LON = 148.263
KOSCIUSZKO_EPSG = 32755
KOSCIUSZKO_GZD = "55H"

# Yosemite Valley / Half Dome, California (UTM 11N / EPSG:32611 / MGRS 11S)
YOSEMITE_LAT = 37.745
YOSEMITE_LON = -119.593
YOSEMITE_EPSG = 32611
YOSEMITE_GZD = "11S"

# Banff / Sulphur Mountain, Alberta (UTM 11N / EPSG:32611 / MGRS 11U)
BANFF_LAT = 51.178
BANFF_LON = -115.571
BANFF_EPSG = 32611
BANFF_GZD = "11U"

RUN_LIVE = os.environ.get("GRIDFORGE_RUN_LIVE", "").strip().lower() in {"1", "true", "yes"}


def _assert_peak_rss_under_2gb(label: str) -> float:
    div = (1024.0 * 1024.0) if sys.platform == "darwin" else 1024.0
    self_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / div
    child_mb = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / div
    peak_mb = max(self_mb, child_mb)
    assert peak_mb < 2048.0, (
        f"[{label}] Peak RSS {peak_mb:.1f} MB (self={self_mb:.1f}, child={child_mb:.1f}) exceeded 2048 MB!"
    )
    return peak_mb


def _create_mock_na_overlay_gpkgs(out_dir: Path, layout: SheetLayout) -> dict[str, Path]:
    """Create valid UTM GeoPackages for mock `us-trails`, `us-public-lands`, and `ca-canvec-trails`."""
    from osgeo import ogr, osr

    ogr.UseExceptions()
    osr.UseExceptions()
    out_dir.mkdir(parents=True, exist_ok=True)

    srs = osr.SpatialReference()
    srs.ImportFromEPSG(layout.utm_epsg)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    driver = ogr.GetDriverByName("GPKG")

    cx = layout.center_easting
    cy = layout.center_northing
    created: dict[str, Path] = {}

    def _add_trails_and_pois(ds: Any, state_code: str, shift: float) -> None:
        lyr_t = ds.CreateLayer("au_state_trails", srs=srs, geom_type=ogr.wkbMultiLineString)
        for fname in ("name", "trail_class", "surface", "state"):
            lyr_t.CreateField(ogr.FieldDefn(fname, ogr.OFTString))
        line = ogr.Geometry(ogr.wkbLineString)
        line.AddPoint_2D(cx - 1000, cy + 300 + shift)
        line.AddPoint_2D(cx + 950, cy - 200 + shift)
        mline = ogr.Geometry(ogr.wkbMultiLineString)
        mline.AddGeometry(line)
        f = ogr.Feature(lyr_t.GetLayerDefn())
        f.SetGeometry(mline)
        f.SetField("name", f"{state_code} Alpine Divide Trail")
        f.SetField("trail_class", "walking_track")
        f.SetField("surface", "natural")
        f.SetField("state", state_code)
        lyr_t.CreateFeature(f)

        lyr_p = ds.CreateLayer("au_state_pois", srs=srs, geom_type=ogr.wkbPoint)
        lyr_p.CreateField(ogr.FieldDefn("name", ogr.OFTString))
        lyr_p.CreateField(ogr.FieldDefn("poi_type", ogr.OFTString))
        lyr_p.CreateField(ogr.FieldDefn("elev_m", ogr.OFTReal))
        lyr_p.CreateField(ogr.FieldDefn("state", ogr.OFTString))
        pt = ogr.Geometry(ogr.wkbPoint)
        pt.AddPoint_2D(cx - 250 + shift, cy + 350)
        fp = ogr.Feature(lyr_p.GetLayerDefn())
        fp.SetGeometry(pt)
        fp.SetField("name", f"{state_code} Backcountry Hut")
        fp.SetField("poi_type", "hut")
        fp.SetField("elev_m", 2950.0)
        fp.SetField("state", state_code)
        lyr_p.CreateFeature(fp)

    def _add_public_lands(ds: Any, state_code: str, manager: str, designation: str, shift: float) -> None:
        lyr = ds.CreateLayer("public_lands", srs=srs, geom_type=ogr.wkbMultiPolygon)
        for fname in ("name", "manager", "designation", "state"):
            lyr.CreateField(ogr.FieldDefn(fname, ogr.OFTString))
        ring = ogr.Geometry(ogr.wkbLinearRing)
        for px, py in (
            (cx - 1200 + shift, cy - 900),
            (cx + 900 + shift, cy - 900),
            (cx + 900 + shift, cy + 800),
            (cx - 1200 + shift, cy + 800),
            (cx - 1200 + shift, cy - 900),
        ):
            ring.AddPoint_2D(px, py)
        poly = ogr.Geometry(ogr.wkbPolygon)
        poly.AddGeometry(ring)
        mpoly = ogr.Geometry(ogr.wkbMultiPolygon)
        mpoly.AddGeometry(poly)
        f = ogr.Feature(lyr.GetLayerDefn())
        f.SetGeometry(mpoly)
        f.SetField("name", f"{state_code} Alpine Wilderness")
        f.SetField("manager", manager)
        f.SetField("designation", designation)
        f.SetField("state", state_code)
        lyr.CreateFeature(f)

    # 1. us-trails
    us_trails_path = out_dir / "us_trails.gpkg"
    ds = driver.CreateDataSource(str(us_trails_path))
    _add_trails_and_pois(ds, "US", 0.0)
    ds = None
    created[USOverlayKind.US_TRAILS.value] = us_trails_path

    # 2. us-public-lands
    us_lands_path = out_dir / "us_public_lands.gpkg"
    ds = driver.CreateDataSource(str(us_lands_path))
    _add_public_lands(ds, "US", "USFS", "Wilderness Area", 0.0)
    ds = None
    created[USOverlayKind.US_PUBLIC_LANDS.value] = us_lands_path

    # 3. ca-canvec-trails (trails + POIs + public_lands)
    ca_path = out_dir / "ca_canvec_trails.gpkg"
    ds = driver.CreateDataSource(str(ca_path))
    _add_trails_and_pois(ds, "CA", 200.0)
    _add_public_lands(ds, "CA", "Parks Canada", "National Park", 150.0)
    ds = None
    created[CAOverlayKind.CA_CANVEC_TRAILS.value] = ca_path

    return created


def _verify_gpkg_tiles_archive(gpkg_path: Path) -> int:
    """Validate OGC GeoPackage raster tiles tables & tile payload integrity; return tile count."""
    assert gpkg_path.is_file() and gpkg_path.stat().st_size > 4_096
    conn = sqlite3.connect(f"file:{gpkg_path}?mode=ro", uri=True)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"gpkg_contents", "gpkg_tile_matrix_set", "gpkg_tile_matrix"}.issubset(tables), (
            f"Missing OGC GeoPackage tile tables in {gpkg_path}: {tables}"
        )
        contents = conn.execute(
            "SELECT table_name, data_type FROM gpkg_contents WHERE data_type='tiles'"
        ).fetchall()
        assert len(contents) >= 1, "No data_type='tiles' entry in gpkg_contents"
        tile_table = contents[0][0]
        rows = conn.execute(
            f"SELECT zoom_level, tile_column, tile_row, tile_data FROM \"{tile_table}\" LIMIT 10"
        ).fetchall()
        total = conn.execute(f"SELECT COUNT(*) FROM \"{tile_table}\"").fetchone()[0]
        assert total > 0 and len(rows) > 0
        for _z, _c, _r, blob in rows:
            assert blob and (blob.startswith(b"\x89PNG\r\n\x1a\n") or blob.startswith(b"\xff\xd8\xff"))
        return int(total)
    finally:
        conn.close()


def _verify_osmand_sqlitedb(sqlitedb_path: Path) -> int:
    """Validate OsmAnd BigPlanet SQLiteDB schema (inverted zoom z = 17 - web_zoom) & tiles."""
    assert sqlitedb_path.is_file() and sqlitedb_path.stat().st_size > 4_096
    conn = sqlite3.connect(f"file:{sqlitedb_path}?mode=ro", uri=True)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"info", "tiles"}.issubset(tables), f"Missing OsmAnd tables: {tables}"
        total = conn.execute("SELECT COUNT(*) FROM tiles").fetchone()[0]
        assert total > 0
        sample = conn.execute("SELECT x, y, z, image FROM tiles LIMIT 10").fetchall()
        for _x, _y, z_inv, blob in sample:
            # Web zoom 10..17 -> BigPlanet z = 17 - web_zoom in [0..7]
            assert -2 <= int(z_inv) <= 10, f"Unexpected OsmAnd BigPlanet inverted zoom z={z_inv}"
            assert blob and (blob.startswith(b"\x89PNG\r\n\x1a\n") or blob.startswith(b"\xff\xd8\xff"))
        return int(total)
    finally:
        conn.close()


def _verify_field_bundle_zip(zip_path: Path, *, expect_exts: tuple[str, ...] = (".pdf", ".gpx")) -> list[str]:
    """Validate Field Bundle ZIP contents, valid GPX 1.1 XML, and README_FIELD_BUNDLE.txt."""
    assert zip_path.is_file() and zip_path.stat().st_size > 10_000
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = zf.namelist()
        assert any("README" in n.upper() and n.endswith(".txt") for n in names), (
            f"Missing README_FIELD_BUNDLE.txt in {names}"
        )
        for ext in expect_exts:
            assert any(n.lower().endswith(ext) for n in names), (
                f"Expected *{ext} inside Field Bundle ZIP, found: {names}"
            )
        gpx_names = [n for n in names if n.lower().endswith(".gpx")]
        if gpx_names:
            gpx_bytes = zf.read(gpx_names[0])
            root = ET.fromstring(gpx_bytes)
            assert root.tag.endswith("gpx")
            assert root.attrib.get("version") == "1.1"
    return names


class TestOfflineV14Features:
    """Fast zero-network CI verification for all GridForge v1.4.0 capabilities."""

    def test_offline_night_red_and_alpine_themes_with_na_overlays(self, tmp_path: Path) -> None:
        dem_path = FIXTURES_DIR / "synthetic_dem.tif"
        osm_path = FIXTURES_DIR / "synthetic_osm.osm"
        assert dem_path.is_file() and osm_path.is_file()

        base_spec = SheetSpec(
            center_lat=BLUE_MOUNTAINS_LAT,
            center_lon=BLUE_MOUNTAINS_LON,
            scale=25000,
            paper=PaperSize.A4,
            orientation=Orientation.PORTRAIT,
            color_mode=ColorMode.NIGHT_RED,
            units=Units.METRIC,
            title="V14 NIGHT-RED THEME",
            date_iso="2026-06-15",
            us_overlays=[USOverlayKind.US_TRAILS, USOverlayKind.US_PUBLIC_LANDS],
            ca_overlays=[CAOverlayKind.CA_CANVEC_TRAILS],
            carto=CartoOptions(show_slope_shading=True),
        )
        layout = compute_sheet_layout(base_spec)
        na_mocks = _create_mock_na_overlay_gpkgs(tmp_path / "na_mocks", layout)
        overrides = {
            USOverlayKind.US_TRAILS: na_mocks[USOverlayKind.US_TRAILS.value],
            USOverlayKind.US_PUBLIC_LANDS: na_mocks[USOverlayKind.US_PUBLIC_LANDS.value],
            CAOverlayKind.CA_CANVEC_TRAILS: na_mocks[CAOverlayKind.CA_CANVEC_TRAILS.value],
        }

        night_pdf = tmp_path / "v14_offline_night_red.pdf"
        run_render_job(
            base_spec,
            night_pdf,
            elevation_source=LocalDEMSource(dem_path),
            vector_source=LocalPBFSource(osm_path),
            regional_overlay_overrides=overrides,
        )
        assert_pdf_valid(
            night_pdf,
            scale=25000,
            paper="a4",
            orientation="portrait",
            expected_epsg=BLUE_MOUNTAINS_EPSG,
            expected_gzd=BLUE_MOUNTAINS_GZD,
            expected_title="V14 NIGHT-RED THEME",
            extra_required_text=["NIGHT-RED"],
            require_osm_attribution=True,
            require_copernicus_attribution=False,
        )

        alpine_spec = base_spec.model_copy(
            update={"color_mode": ColorMode.ALPINE, "title": "V14 ALPINE WINTER THEME"}
        )
        alpine_pdf = tmp_path / "v14_offline_alpine.pdf"
        run_render_job(
            alpine_spec,
            alpine_pdf,
            elevation_source=LocalDEMSource(dem_path),
            vector_source=LocalPBFSource(osm_path),
            regional_overlay_overrides=overrides,
        )
        assert_pdf_valid(
            alpine_pdf,
            scale=25000,
            paper="a4",
            orientation="portrait",
            expected_epsg=BLUE_MOUNTAINS_EPSG,
            expected_gzd=BLUE_MOUNTAINS_GZD,
            expected_title="V14 ALPINE WINTER THEME",
            extra_required_text=["ALPINE"],
            require_osm_attribution=True,
            require_copernicus_attribution=False,
        )
        assert night_pdf.read_bytes() != alpine_pdf.read_bytes()

    def test_offline_rf_link_card_tactical_graphics_and_device_bundle(self, tmp_path: Path) -> None:
        from osgeo import ogr

        from gridforge.export.bundle import export_field_bundle_zip
        from gridforge.export.gpkg_tiles import export_gpkg_tiles
        from gridforge.export.osmand import export_osmand_sqlitedb
        from gridforge.export.raster import render_collarless_geotiff

        ogr.UseExceptions()
        dem_path = FIXTURES_DIR / "synthetic_dem.tif"
        osm_path = FIXTURES_DIR / "synthetic_osm.osm"
        assert dem_path.is_file() and osm_path.is_file()

        rf_link = RFLinkSpec(
            from_lat=BLUE_MOUNTAINS_LAT + 0.006,
            from_lon=BLUE_MOUNTAINS_LON - 0.008,
            to_lat=BLUE_MOUNTAINS_LAT - 0.006,
            to_lon=BLUE_MOUNTAINS_LON + 0.009,
            from_height_m=10.0,
            to_height_m=5.0,
            freq_mhz=915.0,
            from_label="Relay Alpha",
            to_label="Base Bravo",
        )
        graphics = [
            TacticalGraphic(
                kind="range_ring",
                label="FOB Rings",
                points=[(BLUE_MOUNTAINS_LAT, BLUE_MOUNTAINS_LON)],
                radii_km=[0.5, 1.0],
            ),
            TacticalGraphic(
                kind="bearing_line",
                label="Azimuth Leg",
                points=[
                    (BLUE_MOUNTAINS_LAT + 0.004, BLUE_MOUNTAINS_LON - 0.004),
                    (BLUE_MOUNTAINS_LAT - 0.005, BLUE_MOUNTAINS_LON + 0.005),
                ],
            ),
            TacticalGraphic(
                kind="sector",
                label="AOI North",
                points=[
                    (BLUE_MOUNTAINS_LAT + 0.002, BLUE_MOUNTAINS_LON - 0.006),
                    (BLUE_MOUNTAINS_LAT + 0.010, BLUE_MOUNTAINS_LON - 0.002),
                    (BLUE_MOUNTAINS_LAT + 0.008, BLUE_MOUNTAINS_LON + 0.006),
                    (BLUE_MOUNTAINS_LAT + 0.002, BLUE_MOUNTAINS_LON + 0.003),
                ],
            ),
        ]
        waypoints = [
            WaypointInput(
                label="Relay Alpha",
                lat=rf_link.from_lat,
                lon=rf_link.from_lon,
                category=WaypointCategory.WAYPOINT,
            ),
            WaypointInput(
                label="Base Bravo",
                lat=rf_link.to_lat,
                lon=rf_link.to_lon,
                category=WaypointCategory.CAMP,
            ),
        ]

        spec = SheetSpec(
            center_lat=BLUE_MOUNTAINS_LAT,
            center_lon=BLUE_MOUNTAINS_LON,
            scale=25000,
            paper=PaperSize.A4,
            orientation=Orientation.PORTRAIT,
            color_mode=ColorMode.COLOR,
            units=Units.METRIC,
            title="V14 RF LINK & TACTICAL",
            date_iso="2026-06-15",
            dpi=150,
            waypoints=waypoints,
            rf_links=[rf_link],
            include_rf_link_card=True,
            tactical_graphics=graphics,
        )

        # 1. Inspect tactical_graphics_gpkg layers directly
        ws = prepare_job_workspace(
            spec,
            tmp_path / "ws_tactical",
            elevation_source=LocalDEMSource(dem_path),
            vector_source=LocalPBFSource(osm_path),
        )
        assert ws.tactical_graphics_gpkg is not None and ws.tactical_graphics_gpkg.is_file()
        ds = ogr.Open(str(ws.tactical_graphics_gpkg))
        assert ds is not None
        layers = {ds.GetLayerByIndex(i).GetName(): ds.GetLayerByIndex(i).GetFeatureCount() for i in range(ds.GetLayerCount())}
        ds = None
        assert layers.get("tactical_sectors", 0) >= 1
        assert layers.get("tactical_rings", 0) >= 2
        assert layers.get("tactical_lines", 0) >= 1

        # 2. Multi-page PDF with Map Sheet + RF Fresnel Link Card
        out_pdf = tmp_path / "v14_rf_tactical_2page.pdf"
        run_render_job(
            spec,
            out_pdf,
            elevation_source=LocalDEMSource(dem_path),
            vector_source=LocalPBFSource(osm_path),
        )
        mp = assert_multipage_pdf_valid(out_pdf, expected_pages=2, expected_width_mm=210.0, expected_height_mm=297.0)
        assert_pdf_valid(
            out_pdf,
            page_index=0,
            allow_multipage=True,
            scale=25000,
            paper="a4",
            orientation="portrait",
            expected_epsg=BLUE_MOUNTAINS_EPSG,
            expected_gzd=BLUE_MOUNTAINS_GZD,
            expected_title="V14 RF LINK & TACTICAL",
            require_osm_attribution=True,
            require_copernicus_attribution=False,
        )
        rf_card_txt = mp.page_texts[1]
        assert "915" in rf_card_txt, f"Missing 915 MHz on RF Link Card: {rf_card_txt[:400]!r}"
        assert any(s in rf_card_txt.upper() for s in ("CLEAR", "MARGINAL", "OBSTRUCTED")), (
            f"Missing RF clearance verdict on RF Link Card: {rf_card_txt[:400]!r}"
        )
        assert "56H" in rf_card_txt

        # 3. OGC GeoPackage raster tiles (.gpkg), OsmAnd (.sqlitedb), and Field Bundle (.zip)
        collarless = render_collarless_geotiff(ws, tmp_path / "collarless_150.tif", dpi=150, include_grid=True)
        gpkg_out = tmp_path / "v14_offline.gpkg"
        osmand_out = tmp_path / "v14_offline.sqlitedb"
        bundle_out = tmp_path / "v14_offline_field_bundle.zip"

        export_gpkg_tiles(ws, gpkg_out, dpi=150, collarless_tif=collarless)
        export_osmand_sqlitedb(ws, osmand_out, dpi=150, collarless_tif=collarless)
        export_field_bundle_zip(
            ws,
            bundle_out,
            artifacts=[out_pdf, gpkg_out, osmand_out],
        )

        assert _verify_gpkg_tiles_archive(gpkg_out) > 0
        assert _verify_osmand_sqlitedb(osmand_out) > 0
        _verify_field_bundle_zip(bundle_out, expect_exts=(".pdf", ".gpkg", ".sqlitedb", ".gpx"))

    def test_offline_basemap_builder_29_regions_projects_and_ruler_apis(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fastapi_testclient = pytest.importorskip("fastapi.testclient")
        from gridforge.packs.basemap_builder import build_offline_basemap_mbtiles
        from gridforge.web.app import create_app

        dem_path = FIXTURES_DIR / "synthetic_dem.tif"
        assert dem_path.is_file()

        # 1. Build pure-GDAL hypsometric offline basemap MBTiles into a synthetic pack
        data_dir = tmp_path / "web_data"
        packs_dir = tmp_path / "packs"
        pack_dir = packs_dir / "synth_pack"
        data_dir.mkdir(parents=True, exist_ok=True)
        pack_dir.mkdir(parents=True, exist_ok=True)

        basemap_path = pack_dir / "basemap.mbtiles"
        pack_bbox = BBox(min_x=150.23, min_y=-33.81, max_x=150.39, max_y=-33.65)
        build_offline_basemap_mbtiles(
            pack_bbox,
            basemap_path,
            source_dem=dem_path,
            min_zoom=10,
            max_zoom=11,
            name="Synthetic Blue Mountains Basemap",
            tile_format="png",
        )
        assert basemap_path.is_file() and basemap_path.stat().st_size > 2_048

        manifest = PackManifest(
            pack_id="synth_pack",
            name="Synthetic Basemap Pack",
            region="blue-mountains",
            bbox_wgs84=pack_bbox,
            utm_zones=["56S"],
            dem_path=None,
            osm_pbf_path=None,
            basemap_path="basemap.mbtiles",
            created_at=datetime.now(timezone.utc).isoformat(),
            size_bytes=basemap_path.stat().st_size,
        )
        (pack_dir / "pack.json").write_text(manifest.model_dump_json(indent=2), encoding="utf-8")

        monkeypatch.setenv("GRIDFORGE_DATA_DIR", str(data_dir))
        monkeypatch.setenv("GRIDFORGE_PACKS_DIR", str(packs_dir))
        monkeypatch.delenv("GRIDFORGE_BASEMAP_MBTILES", raising=False)

        app = create_app()
        with fastapi_testclient.TestClient(app, base_url="http://localhost:8080") as client:
            # 2. Per-pack offline basemap discovery & tile serving
            binfo = client.get("/api/basemap/info", params={"pack_id": "synth_pack"})
            assert binfo.status_code == 200, f"per-pack /api/basemap/info failed: {binfo.text}"
            ibody = binfo.json()
            assert ibody["available"] is True
            assert ibody["minzoom"] == 10 and ibody["maxzoom"] == 11

            # Inspect one generated tile coordinate from MBTiles & fetch via XYZ endpoint
            conn = sqlite3.connect(f"file:{basemap_path}?mode=ro", uri=True)
            z, x, tms_y = conn.execute("SELECT zoom_level, tile_column, tile_row FROM tiles LIMIT 1").fetchone()
            conn.close()
            xyz_y = (1 << z) - 1 - tms_y
            tile_resp = client.get(f"/api/basemap/{z}/{x}/{xyz_y}.png", params={"pack_id": "synth_pack"})
            assert tile_resp.status_code == 200
            assert tile_resp.content.startswith(b"\x89PNG\r\n\x1a\n")

            # 3. 29 grouped offline pack regions (au_tactical:7, au_state:8, nz:6, na:8)
            reg_resp = client.get("/api/packs/regions")
            assert reg_resp.status_code == 200
            regions = reg_resp.json()["regions"]
            assert len(regions) == 29, f"Expected 29 grouped regions, got {len(regions)}"
            group_counts: dict[str, int] = {}
            for reg in regions:
                grp = reg["group"]
                group_counts[grp] = group_counts.get(grp, 0) + 1
            assert group_counts == {"au_tactical": 7, "au_state": 8, "nz": 6, "na": 8}, (
                f"Unexpected region group counts: {group_counts}"
            )

            # 4. Tactical Ruler Metrics API
            ruler = client.post(
                "/api/ruler-metrics",
                json={
                    "points": [
                        {"lat": -33.7133, "lon": 150.3113},
                        {"lat": -33.7322, "lon": 150.3117},
                        {"lat": -33.7347, "lon": 150.3185},
                    ]
                },
            )
            assert ruler.status_code == 200, f"/api/ruler-metrics failed: {ruler.text}"
            rbody = ruler.json()
            assert rbody["total_distance_m"] > 2000.0
            assert len(rbody["segments"]) == 2
            for seg in rbody["segments"]:
                assert seg["distance_m"] > 0
                assert 0.0 <= seg["bearing_mag_deg"] <= 360.0
                assert 0.0 <= seg["bearing_mils"] <= 6400.0

            # 5. Saved Trip Projects CRUD
            created = client.post(
                "/api/projects",
                json={
                    "name": "Kosciuszko Winter Ops",
                    "description": "Alpine 1:25k sheet with LoRa relay link",
                    "state": {"center": "-36.456,148.263", "scale": 25000, "color_mode": "alpine"},
                },
            )
            assert created.status_code in (200, 201), f"Create project failed: {created.text}"
            proj = created.json()
            pid = proj.get("project_id") or proj.get("id")
            assert pid

            listed = client.get("/api/projects")
            assert listed.status_code == 200
            assert any((p.get("project_id") or p.get("id")) == pid for p in listed.json()["projects"])

            patched = client.patch(f"/api/projects/{pid}", json={"name": "Kosciuszko Winter Ops v2"})
            assert patched.status_code == 200
            assert patched.json()["name"] == "Kosciuszko Winter Ops v2"

            deleted = client.delete(f"/api/projects/{pid}")
            assert deleted.status_code in (200, 204)
            assert client.get(f"/api/projects/{pid}").status_code == 404

            # 6. PR #1 SameOriginWritesMiddleware 403 protection on new v1.4.0 write routes
            for route, payload in (
                ("/api/projects", {"name": "Evil", "state": {}}),
                ("/api/ruler-metrics", {"points": [{"lat": -33.7, "lon": 150.3}, {"lat": -33.71, "lon": 150.31}]}),
                (
                    "/api/rf-link-profile",
                    {"from_lat": -33.7, "from_lon": 150.3, "to_lat": -33.71, "to_lon": 150.31},
                ),
            ):
                evil = client.post(route, json=payload, headers={"Origin": "https://evil.example"})
                assert evil.status_code == 403, f"Expected 403 on cross-origin POST {route}, got {evil.status_code}"


@pytest.mark.live
@pytest.mark.skipif(
    not RUN_LIVE,
    reason="Live v1.4.0 E2E tests skipped by default; set GRIDFORGE_RUN_LIVE=1 to run.",
)
class TestLiveV14Features:
    """Live E2E verification for US Alpine, Canada HRDEM Night-Red, and Kosciuszko RF/Bundle."""

    def _out_dir(self, tmp_path: Path) -> Path:
        override = os.environ.get("GRIDFORGE_LIVE_OUT_DIR")
        if override:
            path = Path(override)
            path.mkdir(parents=True, exist_ok=True)
            return path
        return tmp_path

    def test_live_us_yosemite_alpine_trails_and_public_lands(self, tmp_path: Path) -> None:
        out_pdf = self._out_dir(tmp_path) / "v14_us_yosemite_24k_letter_alpine.pdf"
        spec = SheetSpec(
            center_lat=YOSEMITE_LAT,
            center_lon=YOSEMITE_LON,
            scale=24000,
            paper=PaperSize.LETTER,
            orientation=Orientation.PORTRAIT,
            color_mode=ColorMode.ALPINE,
            units=Units.IMPERIAL,
            title="YOSEMITE VALLEY — ALPINE CA",
            dem_source=DemSourceKind.USGS3DEP,
            us_overlays=[USOverlayKind.US_TRAILS, USOverlayKind.US_PUBLIC_LANDS],
            carto=CartoOptions(show_slope_shading=True),
        )
        res = run_render_job(spec, out_pdf)
        assert res.utm_epsg == YOSEMITE_EPSG
        _assert_peak_rss_under_2gb("live-us-alpine")

        assert_pdf_valid(
            out_pdf,
            scale=24000,
            paper="letter",
            orientation="portrait",
            expected_epsg=YOSEMITE_EPSG,
            expected_gzd=YOSEMITE_GZD,
            expected_title="YOSEMITE VALLEY",
            extra_required_text=["NAVD88", "ALPINE"],
            require_osm_attribution=True,
            require_copernicus_attribution=False,
        )

    def test_live_canada_banff_hrdem_night_red_and_canvec(self, tmp_path: Path) -> None:
        out_pdf = self._out_dir(tmp_path) / "v14_ca_banff_25k_a4_night_red.pdf"
        spec = SheetSpec(
            center_lat=BANFF_LAT,
            center_lon=BANFF_LON,
            scale=25000,
            paper=PaperSize.A4,
            orientation=Orientation.PORTRAIT,
            color_mode=ColorMode.NIGHT_RED,
            units=Units.METRIC,
            title="BANFF — SULPHUR MTN AB",
            dem_source=DemSourceKind.CA_HRDEM,
            ca_overlays=[CAOverlayKind.CA_CANVEC_TRAILS],
        )
        res = run_render_job(spec, out_pdf)
        assert res.utm_epsg == BANFF_EPSG
        _assert_peak_rss_under_2gb("live-ca-banff-night-red")

        report = assert_pdf_valid(
            out_pdf,
            scale=25000,
            paper="a4",
            orientation="portrait",
            expected_epsg=BANFF_EPSG,
            expected_gzd=BANFF_GZD,
            expected_title="BANFF",
            extra_required_text=["NIGHT-RED"],
            require_osm_attribution=True,
            require_copernicus_attribution=False,
        )
        text_l = report.extracted_text.lower()
        assert any(tok in text_l for tok in ("canada", "nrcan", "cgvd2013")), (
            f"Missing Canada NRCan/CGVD2013 attribution in collar text: {report.extracted_text[:400]!r}"
        )

    def test_live_kosciuszko_rf_fresnel_tactical_rings_and_field_bundle(self, tmp_path: Path) -> None:
        out_dir = self._out_dir(tmp_path)
        out_pdf = out_dir / "v14_kosciuszko_rf_tactical_25k_a4.pdf"
        gpkg_out = out_dir / "v14_kosciuszko_25k.gpkg"
        osmand_out = out_dir / "v14_kosciuszko_25k.sqlitedb"
        bundle_out = out_dir / "v14_kosciuszko_field_bundle.zip"

        rf_link = RFLinkSpec(
            from_lat=-36.4559,
            from_lon=148.2636,
            to_lat=-36.4535,
            to_lon=148.2877,
            from_height_m=10.0,
            to_height_m=5.0,
            freq_mhz=915.0,
            from_label="Kosciuszko Relay",
            to_label="Seamans Hut",
        )
        graphics = [
            TacticalGraphic(
                kind="range_ring",
                label="Summit Rings",
                points=[(-36.4559, 148.2636)],
                radii_km=[1.0, 2.0],
            ),
            TacticalGraphic(
                kind="sector",
                label="Alpine SAR Sector",
                points=[
                    (-36.445, 148.250),
                    (-36.445, 148.278),
                    (-36.462, 148.278),
                    (-36.462, 148.250),
                ],
            ),
        ]
        waypoints = [
            WaypointInput(label="Kosciuszko Relay", lat=-36.4559, lon=148.2636, category=WaypointCategory.WAYPOINT),
            WaypointInput(label="Seamans Hut", lat=-36.4535, lon=148.2877, category=WaypointCategory.CAMP),
        ]

        viewshed_obs = ViewshedObserver(
            label="Kosciuszko Relay",
            lat=-36.4559,
            lon=148.2636,
            observer_height_m=10.0,
            target_height_m=1.5,
            max_distance_m=3500.0,
        )

        spec = SheetSpec(
            center_lat=KOSCIUSZKO_LAT,
            center_lon=KOSCIUSZKO_LON,
            scale=25000,
            paper=PaperSize.A4,
            orientation=Orientation.PORTRAIT,
            color_mode=ColorMode.COLOR,
            units=Units.METRIC,
            title="MT KOSCIUSZKO — RF LINK & TACTICAL",
            dem_source=DemSourceKind.AU_ELVIS,
            au_overlays=[AUOverlayKind.AU_NSW_TRAILS],
            viewsheds=[viewshed_obs],
            waypoints=waypoints,
            rf_links=[rf_link],
            include_rf_link_card=True,
            tactical_graphics=graphics,
        )

        res = run_render_job(
            spec,
            out_pdf,
            gpkg_out=gpkg_out,
            osmand_out=osmand_out,
            field_bundle_out=bundle_out,
        )
        assert res.utm_epsg == KOSCIUSZKO_EPSG
        _assert_peak_rss_under_2gb("live-kosciuszko-rf-bundle")

        mp = assert_multipage_pdf_valid(out_pdf, expected_pages=2, expected_width_mm=210.0, expected_height_mm=297.0)
        assert_pdf_valid(
            out_pdf,
            page_index=0,
            allow_multipage=True,
            scale=25000,
            paper="a4",
            orientation="portrait",
            expected_epsg=KOSCIUSZKO_EPSG,
            expected_gzd=KOSCIUSZKO_GZD,
            expected_title="MT KOSCIUSZKO",
            extra_required_text=["AHD"],
            require_osm_attribution=True,
            require_copernicus_attribution=False,
        )
        rf_txt = mp.page_texts[1]
        assert "915" in rf_txt
        assert any(tok in rf_txt.upper() for tok in ("CLEAR", "MARGINAL", "OBSTRUCTED"))

        assert _verify_gpkg_tiles_archive(gpkg_out) > 0
        assert _verify_osmand_sqlitedb(osmand_out) > 0
        _verify_field_bundle_zip(bundle_out, expect_exts=(".pdf", ".gpkg", ".sqlitedb", ".gpx"))
