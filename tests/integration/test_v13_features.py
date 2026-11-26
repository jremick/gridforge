"""GridForge v1.3.0 Field & Tactical Capabilities E2E Integration Tests.

Covers:
  1. Offline CI tests (default, zero network):
     - RF / Line-of-Sight Viewshed (`ViewshedObserver` on synthetic ridge DEM in Color & B&W):
       verifies RGBA `viewshed.tif` has both visible (alpha > 0) and shadowed (alpha == 0) pixels,
       `viewshed_observers.gpkg` layers, `LOS / RF Viewshed` Map Key text, and exact 40.00 mm 1 km grid.
     - Atlas Overview Index Cover Page (`include_atlas_index=True`) & Edge-Overlap Fold/Join Marks
       (`show_fold_marks=True`): verifies 2x2 Grid Atlas cover page (`A1..B2`) + Strip Atlas CLI
       `--strip-route examples/katoomba_route.geojson --atlas-index --fold-marks`.
     - Air-Gapped Web Basemap (`GET /api/basemap/info`, `GET /api/basemap/{z}/{x}/{y}.png` XYZ->TMS flip
       & `204` out-of-bounds) and Studio Pack Manager API (`GET /api/packs/regions` -> 15 AU regions).
     - New Zealand offline fixture (`nz_overlays=[NZOverlayKind.NZ_DOC_TRAILS]` on Tongariro `60H` / `EPSG:32760`).
  2. Live E2E Suite (`@pytest.mark.live` + `GRIDFORGE_RUN_LIVE=1`):
     - Mt Kosciuszko NSW RF Viewshed + Fold Marks (`-36.456, 148.263`, `55H`, `EPSG:32755`).
     - New Zealand — Tongariro Alpine Crossing (`-39.133, 175.642`, `60H`, `EPSG:32760`, `nz-linz` DEM +
       `nz-doc-trails` DOC tracks/huts/LINZ trigs + Slope-Angle Shading).
     - Multi-Page Strip Atlas with Atlas Overview Index Cover + Fold Marks (`examples/katoomba_route.geojson`).
"""

from __future__ import annotations

import os
import resource
import sqlite3
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

from gridforge.config import (
    AUOverlayKind,
    BBox,
    CartoOptions,
    ColorMode,
    DemSourceKind,
    NZOverlayKind,
    Orientation,
    PaperSize,
    SheetLayout,
    SheetSpec,
    Units,
    ViewshedObserver,
)
from gridforge.geo.crs import compute_sheet_layout
from gridforge.pipeline.runner import prepare_job_workspace, run_render_job
from gridforge.sources.dem_local import LocalDEMSource
from gridforge.sources.osm_pbf import LocalPBFSource

_INTEGRATION_DIR = Path(__file__).resolve().parent
if str(_INTEGRATION_DIR) not in sys.path:
    sys.path.insert(0, str(_INTEGRATION_DIR))

from test_render_e2e import (  # noqa: E402
    _build_synthetic_dem_geotiff,
    _build_synthetic_osm_xml,
)
from verify_pdf import (  # noqa: E402
    assert_calibration_page_valid,
    assert_multipage_pdf_valid,
    assert_pdf_valid,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"
KATOOMBA_ROUTE_GEOJSON = REPO_ROOT / "examples" / "katoomba_route.geojson"

BLUE_MOUNTAINS_LAT = -33.732
BLUE_MOUNTAINS_LON = 150.312
BLUE_MOUNTAINS_EPSG = 32756
BLUE_MOUNTAINS_GZD = "56H"

KOSCIUSZKO_LAT = -36.456
KOSCIUSZKO_LON = 148.263
KOSCIUSZKO_EPSG = 32755
KOSCIUSZKO_GZD = "55H"

TONGARIRO_LAT = -39.133
TONGARIRO_LON = 175.642
TONGARIRO_EPSG = 32760
TONGARIRO_GZD = "60H"

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


def _make_minimal_png_bytes(width: int = 8, height: int = 8, rgb: tuple[int, int, int] = (34, 139, 34)) -> bytes:
    """Create a valid minimal RGB PNG image without external dependencies."""
    raw_rows = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def _chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(raw_rows))
        + _chunk(b"IEND", b"")
    )


def _create_synthetic_basemap_mbtiles(out_path: Path, *, z: int = 10, x: int = 939, xyz_y: int = 614) -> bytes:
    """Build a valid MBTiles 1.3 archive storing a single PNG at TMS-flipped y."""
    png_bytes = _make_minimal_png_bytes(16, 16, (40, 120, 80))
    tms_y = (1 << z) - 1 - xyz_y

    conn = sqlite3.connect(str(out_path))
    try:
        conn.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
        conn.execute(
            "CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB)"
        )
        conn.executemany(
            "INSERT INTO metadata (name, value) VALUES (?, ?)",
            [
                ("name", "GridForge Synthetic Offline Basemap"),
                ("format", "png"),
                ("minzoom", str(z)),
                ("maxzoom", str(z)),
                ("bounds", "150.0,-34.0,151.0,-33.0"),
                ("attribution", "Synthetic Test Basemap"),
            ],
        )
        conn.execute(
            "INSERT INTO tiles (zoom_level, tile_column, tile_row, tile_data) VALUES (?, ?, ?, ?)",
            (z, x, tms_y, png_bytes),
        )
        conn.commit()
    finally:
        conn.close()
    return png_bytes


def _create_mock_nz_doc_gpkg(out_path: Path, layout: SheetLayout) -> Path:
    """Create a valid UTM GeoPackage matching the unified v1.3.0 NZ DOC/LINZ 2-layer schema."""
    from osgeo import ogr, osr

    ogr.UseExceptions()
    osr.UseExceptions()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()

    srs = osr.SpatialReference()
    srs.ImportFromEPSG(layout.utm_epsg)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    driver = ogr.GetDriverByName("GPKG")

    cx = layout.center_easting
    cy = layout.center_northing
    ds = driver.CreateDataSource(str(out_path))

    lyr_t = ds.CreateLayer("au_state_trails", srs=srs, geom_type=ogr.wkbMultiLineString)
    for fname in ("name", "trail_class", "surface", "state"):
        lyr_t.CreateField(ogr.FieldDefn(fname, ogr.OFTString))

    for name, tclass, surface, pts in (
        (
            "Tongariro Alpine Crossing",
            "walking_track",
            "natural",
            [(cx - 1100, cy - 500), (cx, cy + 50), (cx + 1000, cy + 600)],
        ),
        (
            "Mangatepopo Access Track",
            "4wd_track",
            "gravel",
            [(cx - 1300, cy - 600), (cx - 400, cy - 200)],
        ),
    ):
        line = ogr.Geometry(ogr.wkbLineString)
        for px, py in pts:
            line.AddPoint_2D(px, py)
        mline = ogr.Geometry(ogr.wkbMultiLineString)
        mline.AddGeometry(line)
        feat = ogr.Feature(lyr_t.GetLayerDefn())
        feat.SetGeometry(mline)
        feat.SetField("name", name)
        feat.SetField("trail_class", tclass)
        feat.SetField("surface", surface)
        feat.SetField("state", "NZ")
        lyr_t.CreateFeature(feat)

    lyr_p = ds.CreateLayer("au_state_pois", srs=srs, geom_type=ogr.wkbPoint)
    lyr_p.CreateField(ogr.FieldDefn("name", ogr.OFTString))
    lyr_p.CreateField(ogr.FieldDefn("poi_type", ogr.OFTString))
    lyr_p.CreateField(ogr.FieldDefn("elev_m", ogr.OFTReal))
    lyr_p.CreateField(ogr.FieldDefn("state", ogr.OFTString))

    for name, ptype, elev_m, px, py in (
        ("Mangatepopo Hut", "hut", 1190.0, cx - 650, cy - 280),
        ("Ketetahi Campsite", "campground", 1370.0, cx + 620, cy + 440),
        ("MT TONGARIRO TRIG", "trig", 1967.0, cx + 120, cy + 160),
    ):
        pt = ogr.Geometry(ogr.wkbPoint)
        pt.AddPoint_2D(px, py)
        feat = ogr.Feature(lyr_p.GetLayerDefn())
        feat.SetGeometry(pt)
        feat.SetField("name", name)
        feat.SetField("poi_type", ptype)
        feat.SetField("elev_m", elev_m)
        feat.SetField("state", "NZ")
        lyr_p.CreateFeature(feat)

    ds = None
    return out_path


class TestOfflineV13Features:
    """Fast zero-network CI verification for all GridForge v1.3.0 capabilities."""

    def test_offline_viewshed_color_and_bw(self, tmp_path: Path) -> None:
        from osgeo import gdal, ogr

        gdal.UseExceptions()
        ogr.UseExceptions()

        dem_path = FIXTURES_DIR / "synthetic_dem.tif"
        osm_path = FIXTURES_DIR / "synthetic_osm.osm"
        assert dem_path.is_file() and osm_path.is_file()

        # Observer placed on elevated ridge inside synthetic Blue Mountains DEM
        observer = ViewshedObserver(
            label="Ridge Repeater",
            lat=BLUE_MOUNTAINS_LAT + 0.005,
            lon=BLUE_MOUNTAINS_LON - 0.005,
            observer_height_m=15.0,
            target_height_m=1.5,
            max_distance_m=3000.0,
        )

        color_spec = SheetSpec(
            center_lat=BLUE_MOUNTAINS_LAT,
            center_lon=BLUE_MOUNTAINS_LON,
            scale=25000,
            paper=PaperSize.A4,
            orientation=Orientation.PORTRAIT,
            color_mode=ColorMode.COLOR,
            units=Units.METRIC,
            title="V13 OFFLINE VIEWSHED COLOR",
            date_iso="2026-06-15",
            viewsheds=[observer],
        )

        # 1. Inspect workspace viewshed raster & observer GPKG directly
        work_dir = tmp_path / "ws_viewshed"
        ws = prepare_job_workspace(
            color_spec,
            work_dir,
            elevation_source=LocalDEMSource(dem_path),
            vector_source=LocalPBFSource(osm_path),
        )
        assert ws.viewshed_tif is not None and ws.viewshed_tif.is_file()
        assert ws.viewshed_observers_gpkg is not None and ws.viewshed_observers_gpkg.is_file()

        ds = gdal.Open(str(ws.viewshed_tif))
        assert ds is not None
        assert ds.RasterCount == 4, f"Expected 4-band RGBA viewshed.tif, got {ds.RasterCount}"
        alpha_bytes = ds.GetRasterBand(4).ReadRaster(0, 0, ds.RasterXSize, ds.RasterYSize)
        ds = None
        visible_px = sum(1 for b in alpha_bytes if b > 0)
        shadowed_px = len(alpha_bytes) - visible_px
        assert visible_px > 0, "Expected visible (alpha > 0) pixels in viewshed.tif!"
        assert shadowed_px > 0, "Expected shadowed / out-of-range (alpha == 0) pixels in viewshed.tif!"

        ods = ogr.Open(str(ws.viewshed_observers_gpkg))
        assert ods is not None
        layer_names = {ods.GetLayerByIndex(i).GetName() for i in range(ods.GetLayerCount())}
        assert "viewshed_observers" in layer_names
        assert ods.GetLayerByName("viewshed_observers").GetFeatureCount() == 1
        ods = None

        # 2. Full Color PDF render & verification
        out_color = tmp_path / "v13_offline_viewshed_color.pdf"
        run_render_job(
            color_spec,
            out_color,
            elevation_source=LocalDEMSource(dem_path),
            vector_source=LocalPBFSource(osm_path),
        )
        assert_pdf_valid(
            out_color,
            scale=25000,
            paper="a4",
            orientation="portrait",
            expected_epsg=BLUE_MOUNTAINS_EPSG,
            expected_gzd=BLUE_MOUNTAINS_GZD,
            expected_title="V13 OFFLINE VIEWSHED COLOR",
            extra_required_text=["Viewshed"],
            require_osm_attribution=True,
            require_copernicus_attribution=False,
        )

        # 3. Full B&W PDF render & verification
        bw_spec = color_spec.model_copy(
            update={"color_mode": ColorMode.BW, "title": "V13 OFFLINE VIEWSHED BW"}
        )
        out_bw = tmp_path / "v13_offline_viewshed_bw.pdf"
        run_render_job(
            bw_spec,
            out_bw,
            elevation_source=LocalDEMSource(dem_path),
            vector_source=LocalPBFSource(osm_path),
        )
        assert_pdf_valid(
            out_bw,
            scale=25000,
            paper="a4",
            orientation="portrait",
            expected_epsg=BLUE_MOUNTAINS_EPSG,
            expected_gzd=BLUE_MOUNTAINS_GZD,
            expected_title="V13 OFFLINE VIEWSHED BW",
            extra_required_text=["Viewshed"],
            require_osm_attribution=True,
            require_copernicus_attribution=False,
        )
        assert out_color.read_bytes() != out_bw.read_bytes()

    def test_offline_atlas_index_cover_and_fold_marks(self, tmp_path: Path) -> None:
        from gridforge.geo.supplements import AtlasIndexSheet, merge_pdf_pages, render_atlas_index_pdf

        dem_path = FIXTURES_DIR / "synthetic_dem.tif"
        osm_path = FIXTURES_DIR / "synthetic_osm.osm"
        assert dem_path.is_file() and osm_path.is_file()
        assert KATOOMBA_ROUTE_GEOJSON.is_file()

        # 1. Verify 2x2 Grid Atlas Index Cover Page + Fold Marks on sheet A1
        base_spec = SheetSpec(
            center_lat=BLUE_MOUNTAINS_LAT,
            center_lon=BLUE_MOUNTAINS_LON,
            scale=25000,
            paper=PaperSize.A4,
            orientation=Orientation.PORTRAIT,
            color_mode=ColorMode.COLOR,
            title="V13 GRID ATLAS A1",
            show_fold_marks=True,
            include_atlas_index=True,
        )
        layout = compute_sheet_layout(base_spec)
        w_m = layout.utm_bbox.width
        h_m = layout.utm_bbox.height

        grid_sheets: list[AtlasIndexSheet] = []
        page_num = 2
        for row_idx, row_lbl in enumerate(("A", "B")):
            for col_idx, col_lbl in enumerate(("1", "2")):
                dx = col_idx * w_m * 0.90
                dy = -row_idx * h_m * 0.90
                grid_sheets.append(
                    AtlasIndexSheet(
                        sheet_id=f"{row_lbl}{col_lbl}",
                        page_number=page_num,
                        center_lat=BLUE_MOUNTAINS_LAT - row_idx * 0.04,
                        center_lon=BLUE_MOUNTAINS_LON + col_idx * 0.04,
                        center_mgrs_10m="56H KH 5095 6431",
                        utm_bbox=BBox(
                            min_x=layout.utm_bbox.min_x + dx,
                            min_y=layout.utm_bbox.min_y + dy,
                            max_x=layout.utm_bbox.max_x + dx,
                            max_y=layout.utm_bbox.max_y + dy,
                        ),
                    )
                )
                page_num += 1

        cover_pdf = tmp_path / "grid_2x2_index_cover.pdf"
        render_atlas_index_pdf(
            grid_sheets,
            page_width_mm=210.0,
            page_height_mm=297.0,
            scale=25000,
            utm_epsg=BLUE_MOUNTAINS_EPSG,
            title="V13 2X2 GRID ATLAS",
            overlap_pct=10.0,
            output_pdf=cover_pdf,
        )
        sheet_a1_pdf = tmp_path / "grid_2x2_sheet_a1.pdf"
        run_render_job(
            base_spec,
            sheet_a1_pdf,
            elevation_source=LocalDEMSource(dem_path),
            vector_source=LocalPBFSource(osm_path),
        )
        merged_grid_pdf = tmp_path / "grid_2x2_merged.pdf"
        merge_pdf_pages([cover_pdf, sheet_a1_pdf], merged_grid_pdf)

        mp_grid = assert_multipage_pdf_valid(
            merged_grid_pdf,
            expected_pages=2,
            expected_width_mm=210.0,
            expected_height_mm=297.0,
        )
        cover_txt = mp_grid.page_texts[0]
        for label in ("A1", "A2", "B1", "B2"):
            assert label in cover_txt, f"Missing sheet label {label!r} on Atlas Index Cover: {cover_txt[:400]!r}"

        # Confirm sheet A1 with show_fold_marks=True still passes strict physical scale & tick checks
        assert_pdf_valid(
            merged_grid_pdf,
            page_index=1,
            allow_multipage=True,
            scale=25000,
            paper="a4",
            orientation="portrait",
            expected_epsg=BLUE_MOUNTAINS_EPSG,
            expected_gzd=BLUE_MOUNTAINS_GZD,
            expected_title="V13 GRID ATLAS A1",
            require_osm_attribution=True,
            require_copernicus_attribution=False,
        )

        # 2. Verify CLI Strip Atlas with --atlas-index --fold-marks
        strip_pdf = tmp_path / "offline_strip_with_index.pdf"
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "gridforge.cli",
                "render",
                "--strip-route",
                str(KATOOMBA_ROUTE_GEOJSON),
                "--scale",
                "25000",
                "--paper",
                "a4",
                "--mode",
                "color",
                "--title",
                "V13 STRIP INDEX OFFLINE",
                "--atlas-index",
                "--fold-marks",
                "--offline-dem",
                str(dem_path),
                "--offline-pbf",
                str(osm_path),
                "--output",
                str(strip_pdf),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, f"CLI --atlas-index --fold-marks failed:\n{proc.stderr}\n{proc.stdout}"
        mp_strip = assert_multipage_pdf_valid(
            strip_pdf,
            expected_pages=2,  # Atlas Index Cover + S1 strip sheet
            expected_width_mm=210.0,
            expected_height_mm=297.0,
        )
        assert "S1" in mp_strip.page_texts[0], f"Missing S1 on strip atlas index cover: {mp_strip.page_texts[0][:300]!r}"
        assert_pdf_valid(
            strip_pdf,
            page_index=1,
            allow_multipage=True,
            scale=25000,
            paper="a4",
            orientation="portrait",
            expected_epsg=BLUE_MOUNTAINS_EPSG,
            expected_gzd=BLUE_MOUNTAINS_GZD,
            require_osm_attribution=True,
            require_copernicus_attribution=False,
        )

    def test_offline_airgapped_basemap_and_pack_manager_apis(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fastapi_testclient = pytest.importorskip("fastapi.testclient")
        from gridforge.web.app import create_app

        data_dir = tmp_path / "web_data"
        packs_dir = tmp_path / "packs"
        data_dir.mkdir(parents=True, exist_ok=True)
        packs_dir.mkdir(parents=True, exist_ok=True)

        basemap_path = tmp_path / "basemap.mbtiles"
        expected_png = _create_synthetic_basemap_mbtiles(basemap_path, z=10, x=939, xyz_y=614)

        monkeypatch.setenv("GRIDFORGE_DATA_DIR", str(data_dir))
        monkeypatch.setenv("GRIDFORGE_PACKS_DIR", str(packs_dir))
        monkeypatch.setenv("GRIDFORGE_BASEMAP_MBTILES", str(basemap_path))

        app = create_app()
        with fastapi_testclient.TestClient(app, base_url="http://localhost:8080") as client:
            # 1. Air-gapped basemap metadata
            info = client.get("/api/basemap/info")
            assert info.status_code == 200, f"/api/basemap/info failed: {info.text}"
            assert "access-control-allow-origin" not in info.headers
            ibody = info.json()
            assert ibody["available"] is True
            assert ibody["format"] == "png"
            assert ibody["minzoom"] == 10
            assert ibody["maxzoom"] == 10

            # 2. Valid XYZ tile request -> TMS flip -> exact PNG bytes
            tile_ok = client.get("/api/basemap/10/939/614.png")
            assert tile_ok.status_code == 200, f"Tile fetch failed: {tile_ok.status_code}"
            assert "image/png" in tile_ok.headers.get("content-type", "")
            assert tile_ok.content.startswith(b"\x89PNG\r\n\x1a\n")
            assert tile_ok.content == expected_png

            # 3. Out-of-bounds tile -> 204 No Content
            tile_oob = client.get("/api/basemap/10/0/0.png")
            assert tile_oob.status_code == 204

            # 4. Pack Manager regions catalog -> includes all 15 built-in Australian regions (+ v1.4 NZ/NA)
            regions = client.get("/api/packs/regions")
            assert regions.status_code == 200, f"/api/packs/regions failed: {regions.text}"
            rbody = regions.json()
            rlist = rbody["regions"] if isinstance(rbody, dict) and "regions" in rbody else rbody
            assert len(rlist) >= 15, f"Expected >= 15 built-in regions, got {len(rlist)}"
            au_regions = [r for r in rlist if r.get("group", "").startswith("au") or r.get("country") == "AU"]
            assert len(au_regions) == 15 or len(rlist) == 15, f"Expected 15 AU regions in {len(rlist)} total"

            tasks = client.get("/api/packs/tasks")
            assert tasks.status_code == 200

            # 5. PR #1 cross-origin write protection on v1.3.0 POST endpoints
            evil_build = client.post(
                "/api/packs/build",
                json={"region_or_bbox": "blue-mountains"},
                headers={"Origin": "https://evil.example"},
            )
            assert evil_build.status_code == 403
            evil_vs = client.post(
                "/api/viewshed-preview",
                json={"observer": {"lat": -33.732, "lon": 150.312}},
                headers={"Origin": "https://evil.example"},
            )
            assert evil_vs.status_code == 403

    def test_offline_nz_doc_trails_fixture(self, tmp_path: Path) -> None:
        dem_path = _build_synthetic_dem_geotiff(
            tmp_path / "nz_tongariro_dem.tif",
            center_lat=TONGARIRO_LAT,
            center_lon=TONGARIRO_LON,
        )
        osm_path = _build_synthetic_osm_xml(
            tmp_path / "nz_tongariro.osm",
            center_lat=TONGARIRO_LAT,
            center_lon=TONGARIRO_LON,
        )

        spec = SheetSpec(
            center_lat=TONGARIRO_LAT,
            center_lon=TONGARIRO_LON,
            scale=25000,
            paper=PaperSize.A4,
            orientation=Orientation.PORTRAIT,
            color_mode=ColorMode.COLOR,
            units=Units.METRIC,
            title="V13 OFFLINE NZ TONGARIRO",
            date_iso="2026-06-15",
            nz_overlays=[NZOverlayKind.NZ_DOC_TRAILS],
            carto=CartoOptions(show_slope_shading=True),
        )
        layout = compute_sheet_layout(spec)
        mock_nz_gpkg = _create_mock_nz_doc_gpkg(tmp_path / "mock_nz_doc_trails.gpkg", layout)

        out_pdf = tmp_path / "v13_offline_nz_tongariro_25k_a4.pdf"
        res = run_render_job(
            spec,
            out_pdf,
            elevation_source=LocalDEMSource(dem_path),
            vector_source=LocalPBFSource(osm_path),
            regional_overlay_overrides={NZOverlayKind.NZ_DOC_TRAILS: mock_nz_gpkg},
        )
        assert res.utm_epsg == TONGARIRO_EPSG
        assert_pdf_valid(
            out_pdf,
            scale=25000,
            paper="a4",
            orientation="portrait",
            expected_epsg=TONGARIRO_EPSG,
            expected_gzd=TONGARIRO_GZD,
            expected_title="V13 OFFLINE NZ TONGARIRO",
            require_osm_attribution=True,
            require_copernicus_attribution=False,
        )


@pytest.mark.live
@pytest.mark.skipif(
    not RUN_LIVE,
    reason="Live v1.3.0 E2E tests skipped by default; set GRIDFORGE_RUN_LIVE=1 to run.",
)
class TestLiveV13Features:
    """Live E2E verification for RF Viewshed, New Zealand LINZ/DOC, and Atlas Index + Fold Marks."""

    def _out_dir(self, tmp_path: Path) -> Path:
        override = os.environ.get("GRIDFORGE_LIVE_OUT_DIR")
        if override:
            path = Path(override)
            path.mkdir(parents=True, exist_ok=True)
            return path
        return tmp_path

    def test_live_kosciuszko_viewshed_and_fold_marks(self, tmp_path: Path) -> None:
        out_pdf = self._out_dir(tmp_path) / "v13_kosciuszko_viewshed_25k_a4_color.pdf"
        observer = ViewshedObserver(
            label="Kosciuszko Relay",
            lat=-36.4559,
            lon=148.2636,
            observer_height_m=10.0,
            target_height_m=1.5,
            max_distance_m=5000.0,
        )
        spec = SheetSpec(
            center_lat=KOSCIUSZKO_LAT,
            center_lon=KOSCIUSZKO_LON,
            scale=25000,
            paper=PaperSize.A4,
            orientation=Orientation.PORTRAIT,
            color_mode=ColorMode.COLOR,
            units=Units.METRIC,
            title="MT KOSCIUSZKO — RF VIEWSHED",
            dem_source=DemSourceKind.AU_ELVIS,
            au_overlays=[AUOverlayKind.AU_NSW_TRAILS],
            viewsheds=[observer],
            show_fold_marks=True,
        )
        res = run_render_job(spec, out_pdf)
        assert res.utm_epsg == KOSCIUSZKO_EPSG
        _assert_peak_rss_under_2gb("live-kosciuszko-viewshed")

        assert_pdf_valid(
            out_pdf,
            scale=25000,
            paper="a4",
            orientation="portrait",
            expected_epsg=KOSCIUSZKO_EPSG,
            expected_gzd=KOSCIUSZKO_GZD,
            expected_title="MT KOSCIUSZKO",
            extra_required_text=["AHD", "Viewshed"],
            require_osm_attribution=True,
            require_copernicus_attribution=False,
        )

    def test_live_qld_2m_state_lidar_lamington(self, tmp_path: Path) -> None:
        from gridforge.sources.dem_au_elvis import AUElvisDEMSource

        out_pdf = self._out_dir(tmp_path) / "v13_qld_2m_lidar_lamington_25k_a4.pdf"
        spec = SheetSpec(
            center_lat=-28.200,
            center_lon=153.190,
            scale=25000,
            paper=PaperSize.A4,
            orientation=Orientation.PORTRAIT,
            color_mode=ColorMode.COLOR,
            units=Units.METRIC,
            title="LAMINGTON — QLD 2M LIDAR",
            dem_source=DemSourceKind.AU_ELVIS,
            au_overlays=[AUOverlayKind.AU_QLD_TRAILS],
            carto=CartoOptions(show_slope_shading=True),
        )
        dem_src = AUElvisDEMSource()
        res = run_render_job(spec, out_pdf, elevation_source=dem_src)
        assert res.utm_epsg == 32756
        assert dem_src.resolved_tier in {"qld-2m-lidar", "5m-lidar"}, (
            f"Expected high-res Queensland LiDAR tier, got {dem_src.resolved_tier!r}"
        )
        _assert_peak_rss_under_2gb("live-qld-2m-lidar")

        assert_pdf_valid(
            out_pdf,
            scale=25000,
            paper="a4",
            orientation="portrait",
            expected_epsg=32756,
            expected_gzd="56J",
            expected_title="LAMINGTON",
            extra_required_text=["AHD", "Queensland"],
            require_osm_attribution=True,
            require_copernicus_attribution=False,
        )

    def test_live_nz_tongariro_linz_dem_doc_trails_and_slope(self, tmp_path: Path) -> None:
        out_pdf = self._out_dir(tmp_path) / "v13_nz_tongariro_25k_a4_color.pdf"
        spec = SheetSpec(
            center_lat=TONGARIRO_LAT,
            center_lon=TONGARIRO_LON,
            scale=25000,
            paper=PaperSize.A4,
            orientation=Orientation.PORTRAIT,
            color_mode=ColorMode.COLOR,
            units=Units.METRIC,
            title="TONGARIRO ALPINE CROSSING — NZ",
            dem_source=DemSourceKind.NZ_LINZ,
            nz_overlays=[NZOverlayKind.NZ_DOC_TRAILS],
            carto=CartoOptions(show_slope_shading=True),
        )
        res = run_render_job(spec, out_pdf)
        assert res.utm_epsg == TONGARIRO_EPSG
        _assert_peak_rss_under_2gb("live-nz-tongariro")

        report = assert_pdf_valid(
            out_pdf,
            scale=25000,
            paper="a4",
            orientation="portrait",
            expected_epsg=TONGARIRO_EPSG,
            expected_gzd=TONGARIRO_GZD,
            expected_title="TONGARIRO",
            require_osm_attribution=True,
            require_copernicus_attribution=False,
        )
        text_l = report.extracted_text.lower()
        assert any(tok in text_l for tok in ("conservation", "linz", "new zealand", "nzvd2016")), (
            f"Missing New Zealand DOC/LINZ CC BY 4.0 attribution in collar text: {report.extracted_text[:400]!r}"
        )

    def test_live_katoomba_strip_atlas_index_cover_and_fold_marks(self, tmp_path: Path) -> None:
        out_pdf = self._out_dir(tmp_path) / "v13_katoomba_atlas_index_25k_a4.pdf"
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "gridforge.cli",
                "render",
                "--strip-route",
                str(KATOOMBA_ROUTE_GEOJSON),
                "--scale",
                "25000",
                "--paper",
                "a4",
                "--mode",
                "color",
                "--dem-source",
                "au-elvis",
                "--au-overlay",
                "au-nsw-trails",
                "--atlas-index",
                "--fold-marks",
                "--elevation-profile",
                "--calibration-page",
                "--title",
                "KATOOMBA ATLAS INDEX",
                "--output",
                str(out_pdf),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, f"Live strip atlas with index failed:\n{proc.stderr}\n{proc.stdout}"
        _assert_peak_rss_under_2gb("live-katoomba-atlas-index")

        mp = assert_multipage_pdf_valid(
            out_pdf,
            expected_pages=4,  # Atlas Index Cover + Elevation Profile + Strip Sheet S1 + Calibration Page
            expected_width_mm=210.0,
            expected_height_mm=297.0,
        )
        assert "S1" in mp.page_texts[0], f"Missing S1 on Atlas Index Cover: {mp.page_texts[0][:300]!r}"

        # Locate the map sheet page (contains 1:25,000 + EPSG:32756 + OpenStreetMap)
        map_pages = [
            i for i, txt in enumerate(mp.page_texts) if "OpenStreetMap" in txt and "EPSG:32756" in txt
        ]
        assert len(map_pages) == 1, f"Expected 1 map page in Katoomba strip atlas, found {map_pages}"
        assert_pdf_valid(
            out_pdf,
            page_index=map_pages[0],
            allow_multipage=True,
            scale=25000,
            paper="a4",
            orientation="portrait",
            expected_epsg=BLUE_MOUNTAINS_EPSG,
            expected_gzd=BLUE_MOUNTAINS_GZD,
            extra_required_text=["AHD"],
            require_osm_attribution=True,
            require_copernicus_attribution=False,
        )
        assert_calibration_page_valid(
            out_pdf,
            page_index=-1,
            expected_width_mm=210.0,
            expected_height_mm=297.0,
        )
