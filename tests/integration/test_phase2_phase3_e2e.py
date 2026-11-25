"""Phase 2 & Phase 3 End-to-End Integration Tests for GridForge.

Covers:
  - Offline CI suite (default, zero network):
      * Phase 2 CartoOptions toggles (contour density, hillshade strength, trees/buildings/grid/river arrows)
      * Mock local US regional overlay GeoPackages (FEMA NFHL, NHD flowlines, FAA towers, NOAA ENC)
      * Phase 3 ATAK `.mbtiles` (PNG & JPG) and Garmin `.kmz` (standard 100-tile & tight max_tiles=4 cap)
      * Single-pass `gridforge render --mbtiles ... --kmz ...` and standalone `gridforge export` CLI
  - Live network suite (`@pytest.mark.live` + `GRIDFORGE_RUN_LIVE=1`):
      * Blue Mountains NSW (-33.732, 150.312, EPSG:32756, 56H, 1:25,000, A4 Color & B&W)
        upgraded Phase 2 PDF + `.mbtiles` + `.kmz`
      * Boulder / Flatirons, Colorado (40.005, -105.285, EPSG:32613, 13T, 1:24,000, Letter Color & B&W,
        Imperial, USGS 3DEP NAVD88 + FEMA/NHD/FAA US overlays) PDF + `.mbtiles` + `.kmz`
"""

from __future__ import annotations

import inspect
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from gridforge.config import (
    CartoOptions,
    ColorMode,
    DemSourceKind,
    DensityLevel,
    ExportFormat,
    GarminProfile,
    HillshadeStrength,
    MBTilesFormat,
    Orientation,
    PaperSize,
    SheetLayout,
    SheetSpec,
    Units,
    USOverlayKind,
)
from gridforge.geo.crs import compute_sheet_layout
from gridforge.pipeline.runner import run_render_job
from gridforge.sources.dem_local import LocalDEMSource
from gridforge.sources.osm_pbf import LocalPBFSource

# Import sibling verification modules cleanly regardless of working directory
_INTEGRATION_DIR = Path(__file__).resolve().parent
if str(_INTEGRATION_DIR) not in sys.path:
    sys.path.insert(0, str(_INTEGRATION_DIR))

from verify_exports import (  # noqa: E402
    assert_garmin_kmz_valid,
    assert_mbtiles_valid,
)
from verify_pdf import assert_pdf_valid  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"

# Site 1: Katoomba / Jamison Valley, Blue Mountains NSW, Australia (EPSG:32756 / 56H)
BLUE_MOUNTAINS_LAT = -33.732
BLUE_MOUNTAINS_LON = 150.312
BLUE_MOUNTAINS_EPSG = 32756
BLUE_MOUNTAINS_GZD = "56H"

# Site 2: Boulder / Flatirons, Colorado, USA (EPSG:32613 / 13T — lat 40.005° N is band T!)
BOULDER_LAT = 40.005
BOULDER_LON = -105.285
BOULDER_EPSG = 32613
BOULDER_GZD = "13T"

RUN_LIVE = os.environ.get("GRIDFORGE_RUN_LIVE", "").strip().lower() in {"1", "true", "yes"}


def _create_mock_us_overlay_gpkgs(out_dir: Path, layout: SheetLayout) -> dict[str, Path]:
    """Create valid UTM GeoPackages matching Phase 2 FEMA, NHD, FAA Towers, and NOAA ENC schemas."""
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

    # 1. FEMA NFHL flood hazard polygon (fema_flood)
    fema_path = out_dir / "fema.gpkg"
    ds = driver.CreateDataSource(str(fema_path))
    lyr = ds.CreateLayer("fema_flood", srs=srs, geom_type=ogr.wkbPolygon)
    for fname in ("fld_zone", "zone_subty", "sfha_tf"):
        lyr.CreateField(ogr.FieldDefn(fname, ogr.OFTString))
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for px, py in (
        (cx - 600, cy - 400),
        (cx + 400, cy - 400),
        (cx + 400, cy + 300),
        (cx - 600, cy + 300),
        (cx - 600, cy - 400),
    ):
        ring.AddPoint(px, py)
    poly = ogr.Geometry(ogr.wkbPolygon)
    poly.AddGeometry(ring)
    feat = ogr.Feature(lyr.GetLayerDefn())
    feat.SetGeometry(poly)
    feat.SetField("fld_zone", "AE")
    feat.SetField("zone_subty", "FLOODWAY")
    feat.SetField("sfha_tf", "T")
    lyr.CreateFeature(feat)
    ds = None
    created[USOverlayKind.FEMA.value] = fema_path

    # 2. USGS NHD flowlines (nhd_flowlines)
    nhd_path = out_dir / "nhd.gpkg"
    ds = driver.CreateDataSource(str(nhd_path))
    lyr = ds.CreateLayer("nhd_flowlines", srs=srs, geom_type=ogr.wkbLineString)
    lyr.CreateField(ogr.FieldDefn("gnis_name", ogr.OFTString))
    lyr.CreateField(ogr.FieldDefn("fcode", ogr.OFTInteger))
    lyr.CreateField(ogr.FieldDefn("flow_type", ogr.OFTString))
    for name, fcode, ftype, pts in (
        ("Mock Perennial Creek", 46006, "perennial", [(cx - 1200, cy + 900), (cx - 200, cy + 100), (cx + 900, cy - 700)]),
        ("Mock Seasonal Wash", 46003, "intermittent", [(cx + 800, cy + 1100), (cx + 400, cy + 400), (cx - 100, cy + 100)]),
    ):
        line = ogr.Geometry(ogr.wkbLineString)
        for px, py in pts:
            line.AddPoint(px, py)
        feat = ogr.Feature(lyr.GetLayerDefn())
        feat.SetGeometry(line)
        feat.SetField("gnis_name", name)
        feat.SetField("fcode", fcode)
        feat.SetField("flow_type", ftype)
        lyr.CreateFeature(feat)
    ds = None
    created[USOverlayKind.NHD.value] = nhd_path

    # 3. FAA Digital Obstacle File towers (us_towers)
    towers_path = out_dir / "towers.gpkg"
    ds = driver.CreateDataSource(str(towers_path))
    lyr = ds.CreateLayer("us_towers", srs=srs, geom_type=ogr.wkbPoint)
    for fname, ftype in (
        ("source", ogr.OFTString),
        ("name", ogr.OFTString),
        ("obstacle_type", ogr.OFTString),
        ("agl_ft", ogr.OFTReal),
        ("amsl_ft", ogr.OFTReal),
        ("lighting", ogr.OFTString),
    ):
        lyr.CreateField(ogr.FieldDefn(fname, ftype))
    pt = ogr.Geometry(ogr.wkbPoint)
    pt.AddPoint(cx + 350, cy + 550)
    feat = ogr.Feature(lyr.GetLayerDefn())
    feat.SetGeometry(pt)
    feat.SetField("source", "FAA")
    feat.SetField("name", "MOCK COMM TOWER")
    feat.SetField("obstacle_type", "TOWER")
    feat.SetField("agl_ft", 412.0)
    feat.SetField("amsl_ft", 3420.0)
    feat.SetField("lighting", "R")
    lyr.CreateFeature(feat)
    ds = None
    created[USOverlayKind.TOWERS.value] = towers_path

    # 4. NOAA ENC depth contours & navaids (enc_depth_contours, enc_navaids)
    enc_path = out_dir / "enc.gpkg"
    ds = driver.CreateDataSource(str(enc_path))
    lyr_c = ds.CreateLayer("enc_depth_contours", srs=srs, geom_type=ogr.wkbLineString)
    lyr_c.CreateField(ogr.FieldDefn("valdco", ogr.OFTReal))
    dline = ogr.Geometry(ogr.wkbLineString)
    dline.AddPoint(cx - 900, cy - 900)
    dline.AddPoint(cx + 900, cy - 800)
    feat_c = ogr.Feature(lyr_c.GetLayerDefn())
    feat_c.SetGeometry(dline)
    feat_c.SetField("valdco", 10.0)
    lyr_c.CreateFeature(feat_c)

    lyr_n = ds.CreateLayer("enc_navaids", srs=srs, geom_type=ogr.wkbPoint)
    for fname in ("objnam", "navaid_type", "colour"):
        lyr_n.CreateField(ogr.FieldDefn(fname, ogr.OFTString))
    npt = ogr.Geometry(ogr.wkbPoint)
    npt.AddPoint(cx - 500, cy - 850)
    feat_n = ogr.Feature(lyr_n.GetLayerDefn())
    feat_n.SetGeometry(npt)
    feat_n.SetField("objnam", "Mock Channel Light")
    feat_n.SetField("navaid_type", "beacon")
    feat_n.SetField("colour", "red")
    lyr_n.CreateFeature(feat_n)
    ds = None
    created[USOverlayKind.ENC.value] = enc_path

    return created


def _pass_regional_overrides_kwarg(fn: Any, overrides: dict[str, Path]) -> dict[str, Any]:
    """Match runner's regional/US overlay override parameter name cleanly."""
    sig = inspect.signature(fn)
    for candidate in (
        "us_overlay_overrides",
        "regional_overlay_overrides",
        "local_us_overlays",
        "local_overlay_overrides",
    ):
        if candidate in sig.parameters:
            return {candidate: overrides}
    return {}


class TestOfflinePhase2Phase3:
    """Fast, zero-network offline E2E tests covering Phase 2 cartography & Phase 3 exports."""

    def test_offline_phase2_carto_toggles_and_mock_us_overlays(self, tmp_path: Path) -> None:
        dem_path = FIXTURES_DIR / "synthetic_dem.tif"
        osm_path = FIXTURES_DIR / "synthetic_osm.osm"
        assert dem_path.is_file() and osm_path.is_file()

        spec = SheetSpec(
            center_lat=BLUE_MOUNTAINS_LAT,
            center_lon=BLUE_MOUNTAINS_LON,
            scale=25000,
            paper=PaperSize.A4,
            orientation=Orientation.PORTRAIT,
            color_mode=ColorMode.COLOR,
            units=Units.METRIC,
            title="PHASE 2 CARTO + US OVERLAYS",
            date_iso="2026-06-15",
            carto=CartoOptions(
                contour_density=DensityLevel.HIGH,
                hillshade_strength=HillshadeStrength.LIGHT,
                label_density=DensityLevel.NORMAL,
                show_trees=False,
                show_buildings=True,
                show_river_arrows=True,
            ),
            us_overlays=[USOverlayKind.FEMA, USOverlayKind.NHD, USOverlayKind.TOWERS],
        )
        layout = compute_sheet_layout(spec)
        mock_us = _create_mock_us_overlay_gpkgs(tmp_path / "mock_us", layout)

        extra_kwargs = _pass_regional_overrides_kwarg(run_render_job, mock_us)
        out_pdf = tmp_path / "offline_phase2_high_contour_us_overlays.pdf"
        res = run_render_job(
            spec,
            out_pdf,
            elevation_source=LocalDEMSource(dem_path),
            vector_source=LocalPBFSource(osm_path),
            **extra_kwargs,
        )
        assert res.output_pdf == out_pdf

        # HIGH contour density at 1:25k metric -> 5 m (index 25 m)
        assert_pdf_valid(
            out_pdf,
            scale=25000,
            paper="a4",
            orientation="portrait",
            expected_epsg=BLUE_MOUNTAINS_EPSG,
            expected_gzd=BLUE_MOUNTAINS_GZD,
            expected_title="PHASE 2 CARTO",
            extra_required_text=["5 m"],
            require_osm_attribution=True,
            require_copernicus_attribution=False,
        )

        # Also verify B&W mode with hillshade OFF and low label density renders cleanly
        bw_spec = spec.model_copy(
            update={
                "color_mode": ColorMode.BW,
                "title": "PHASE 2 BW NO-HILLSHADE",
                "carto": CartoOptions(
                    contour_density=DensityLevel.LOW,
                    hillshade_strength=HillshadeStrength.OFF,
                    label_density=DensityLevel.LOW,
                    show_trees=True,
                    show_buildings=False,
                    show_power=False,
                    show_pipelines=False,
                    show_river_arrows=False,
                ),
            }
        )
        bw_pdf = tmp_path / "offline_phase2_bw_no_hs.pdf"
        run_render_job(
            bw_spec,
            bw_pdf,
            elevation_source=LocalDEMSource(dem_path),
            vector_source=LocalPBFSource(osm_path),
            **extra_kwargs,
        )
        assert_pdf_valid(
            bw_pdf,
            scale=25000,
            paper="a4",
            orientation="portrait",
            expected_epsg=BLUE_MOUNTAINS_EPSG,
            expected_gzd=BLUE_MOUNTAINS_GZD,
            expected_title="PHASE 2 BW NO-HILLSHADE",
            extra_required_text=["20 m"],
            require_osm_attribution=True,
            require_copernicus_attribution=False,
        )

    def test_offline_phase3_mbtiles_and_kmz_exports(self, tmp_path: Path) -> None:
        from gridforge.export.kmz import export_garmin_kmz
        from gridforge.export.mbtiles import export_mbtiles
        from gridforge.export.raster import render_collarless_geotiff
        from gridforge.pipeline.runner import prepare_job_workspace

        dem_path = FIXTURES_DIR / "synthetic_dem.tif"
        osm_path = FIXTURES_DIR / "synthetic_osm.osm"

        spec = SheetSpec(
            center_lat=BLUE_MOUNTAINS_LAT,
            center_lon=BLUE_MOUNTAINS_LON,
            scale=25000,
            paper=PaperSize.A4,
            orientation=Orientation.PORTRAIT,
            color_mode=ColorMode.COLOR,
            units=Units.METRIC,
            title="OFFLINE EXPORT TEST",
            date_iso="2026-06-15",
            dpi=150,
        )

        work_dir = tmp_path / "workspace"
        workspace = prepare_job_workspace(
            spec,
            work_dir,
            elevation_source=LocalDEMSource(dem_path),
            vector_source=LocalPBFSource(osm_path),
        )
        expected_wgs84 = workspace.layout.wgs84_bbox.as_tuple()

        # Render one shared collarless GeoTIFF at 150 DPI for fast offline verification
        collarless_tif = render_collarless_geotiff(
            workspace,
            tmp_path / "collarless_150.tif",
            dpi=150,
            include_grid=True,
        )
        assert collarless_tif.is_file() and collarless_tif.stat().st_size > 10_000

        # 1. ATAK MBTiles (PNG)
        mbt_png = tmp_path / "offline_atak_png.mbtiles"
        res_png = export_mbtiles(
            workspace,
            mbt_png,
            tile_format=MBTilesFormat.PNG,
            dpi=150,
            collarless_tif=collarless_tif,
        )
        assert res_png.format == ExportFormat.MBTILES
        assert res_png.tile_count > 0
        assert_mbtiles_valid(mbt_png, expected_format="png", expected_bounds=expected_wgs84)

        # 2. ATAK MBTiles (JPG)
        mbt_jpg = tmp_path / "offline_atak_jpg.mbtiles"
        res_jpg = export_mbtiles(
            workspace,
            mbt_jpg,
            tile_format=MBTilesFormat.JPG,
            dpi=150,
            collarless_tif=collarless_tif,
        )
        assert res_jpg.format == ExportFormat.MBTILES
        assert res_jpg.tile_count > 0
        assert_mbtiles_valid(mbt_jpg, expected_format="jpg", expected_bounds=expected_wgs84)

        # 3. Garmin KMZ (standard 100-tile cap)
        kmz_std = tmp_path / "offline_garmin_std.kmz"
        res_kmz_std = export_garmin_kmz(
            workspace,
            kmz_std,
            profile=GarminProfile.STANDARD,
            dpi=150,
            collarless_tif=collarless_tif,
        )
        assert res_kmz_std.format == ExportFormat.KMZ
        assert 1 <= res_kmz_std.tile_count <= 100
        assert_garmin_kmz_valid(kmz_std, max_tiles=100, expected_bounds=expected_wgs84)

        # 4. Garmin KMZ (tight max_tiles=4 cap proving auto-downsizing!)
        kmz_tight = tmp_path / "offline_garmin_4tiles.kmz"
        res_kmz_tight = export_garmin_kmz(
            workspace,
            kmz_tight,
            max_tiles=4,
            dpi=150,
            collarless_tif=collarless_tif,
        )
        assert 1 <= res_kmz_tight.tile_count <= 4
        assert_garmin_kmz_valid(kmz_tight, max_tiles=4, expected_bounds=expected_wgs84)

    def test_offline_cli_render_with_exports_and_standalone_export(self, tmp_path: Path) -> None:
        """Verify `gridforge render --mbtiles ... --kmz ...` AND `gridforge export` CLI."""
        dem_path = FIXTURES_DIR / "synthetic_dem.tif"
        osm_path = FIXTURES_DIR / "synthetic_osm.osm"

        out_pdf = tmp_path / "cli_combo.pdf"
        out_mbt = tmp_path / "cli_combo.mbtiles"
        out_kmz = tmp_path / "cli_combo.kmz"

        proc_render = subprocess.run(
            [
                sys.executable,
                "-m",
                "gridforge.cli",
                "render",
                "--center",
                f"{BLUE_MOUNTAINS_LAT},{BLUE_MOUNTAINS_LON}",
                "--scale",
                "25000",
                "--paper",
                "a4",
                "--mode",
                "color",
                "--contour-density",
                "normal",
                "--hillshade-strength",
                "light",
                "--river-arrows",
                "--offline-dem",
                str(dem_path),
                "--offline-pbf",
                str(osm_path),
                "--dpi",
                "150",
                "--output",
                str(out_pdf),
                "--mbtiles",
                str(out_mbt),
                "--kmz",
                str(out_kmz),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc_render.returncode == 0, f"CLI render failed:\n{proc_render.stderr}\n{proc_render.stdout}"
        assert_pdf_valid(
            out_pdf,
            scale=25000,
            paper="a4",
            orientation="portrait",
            expected_epsg=BLUE_MOUNTAINS_EPSG,
            expected_gzd=BLUE_MOUNTAINS_GZD,
            require_osm_attribution=True,
            require_copernicus_attribution=False,
        )
        assert_mbtiles_valid(out_mbt, expected_format="png")
        assert_garmin_kmz_valid(out_kmz, max_tiles=100)

        # Standalone `gridforge export --format kmz --no-grid --max-tiles 4`
        out_standalone_kmz = tmp_path / "standalone_gridless.kmz"
        proc_export = subprocess.run(
            [
                sys.executable,
                "-m",
                "gridforge.cli",
                "export",
                "--center",
                f"{BLUE_MOUNTAINS_LAT},{BLUE_MOUNTAINS_LON}",
                "--scale",
                "25000",
                "--paper",
                "a4",
                "--mode",
                "bw",
                "--format",
                "kmz",
                "--no-grid",
                "--max-tiles",
                "4",
                "--dpi",
                "150",
                "--offline-dem",
                str(dem_path),
                "--offline-pbf",
                str(osm_path),
                "--output",
                str(out_standalone_kmz),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc_export.returncode == 0, f"CLI export failed:\n{proc_export.stderr}\n{proc_export.stdout}"
        assert_garmin_kmz_valid(out_standalone_kmz, max_tiles=4)


@pytest.mark.live
@pytest.mark.skipif(
    not RUN_LIVE,
    reason="Live Phase 2/3 network tests skipped by default; set GRIDFORGE_RUN_LIVE=1 to run.",
)
class TestLivePhase2Phase3:
    """Live cloud-native Phase 2 + Phase 3 E2E verification on Blue Mountains NSW & Boulder CO."""

    def _out_dir(self, tmp_path: Path) -> Path:
        override = os.environ.get("GRIDFORGE_LIVE_OUT_DIR")
        if override:
            path = Path(override)
            path.mkdir(parents=True, exist_ok=True)
            return path
        return tmp_path

    def test_live_blue_mountains_phase2_color_bw_and_exports(self, tmp_path: Path) -> None:
        out_dir = self._out_dir(tmp_path)
        color_pdf = out_dir / "p2_blue_mountains_a4_25k_color.pdf"
        bw_pdf = out_dir / "p2_blue_mountains_a4_25k_bw.pdf"
        mbt_path = out_dir / "p3_blue_mountains_25k_color.mbtiles"
        kmz_path = out_dir / "p3_blue_mountains_25k_color.kmz"

        color_spec = SheetSpec(
            center_lat=BLUE_MOUNTAINS_LAT,
            center_lon=BLUE_MOUNTAINS_LON,
            scale=25000,
            paper=PaperSize.A4,
            orientation=Orientation.PORTRAIT,
            color_mode=ColorMode.COLOR,
            units=Units.METRIC,
            title="BLUE MOUNTAINS — KATOOMBA",
            dem_source=DemSourceKind.AUTO,
            carto=CartoOptions(show_river_arrows=True, show_trees=True, show_buildings=True),
        )
        layout = compute_sheet_layout(color_spec)
        expected_wgs84 = layout.wgs84_bbox.as_tuple()

        res_color = run_render_job(
            color_spec,
            color_pdf,
            mbtiles_out=mbt_path,
            kmz_out=kmz_path,
        )
        assert res_color.output_pdf == color_pdf
        assert res_color.utm_epsg == BLUE_MOUNTAINS_EPSG

        assert_pdf_valid(
            color_pdf,
            scale=25000,
            paper="a4",
            orientation="portrait",
            expected_epsg=BLUE_MOUNTAINS_EPSG,
            expected_gzd=BLUE_MOUNTAINS_GZD,
            expected_title="BLUE MOUNTAINS",
            extra_required_text=["EGM2008"],
            require_osm_attribution=True,
            require_copernicus_attribution=True,
        )
        assert_mbtiles_valid(mbt_path, expected_format="png", expected_bounds=expected_wgs84)
        assert_garmin_kmz_valid(kmz_path, max_tiles=100, expected_bounds=expected_wgs84)

        # Upgraded Phase 2 B&W sheet
        bw_spec = color_spec.model_copy(
            update={
                "color_mode": ColorMode.BW,
                "title": "BLUE MOUNTAINS — KATOOMBA (B&W)",
            }
        )
        run_render_job(bw_spec, bw_pdf)
        assert_pdf_valid(
            bw_pdf,
            scale=25000,
            paper="a4",
            orientation="portrait",
            expected_epsg=BLUE_MOUNTAINS_EPSG,
            expected_gzd=BLUE_MOUNTAINS_GZD,
            expected_title="BLUE MOUNTAINS",
            extra_required_text=["EGM2008"],
            require_osm_attribution=True,
            require_copernicus_attribution=True,
        )

    def test_live_boulder_colorado_us_3dep_overlays_and_exports(self, tmp_path: Path) -> None:
        out_dir = self._out_dir(tmp_path)
        color_pdf = out_dir / "p2_boulder_letter_24k_color.pdf"
        bw_pdf = out_dir / "p2_boulder_letter_24k_bw.pdf"
        mbt_path = out_dir / "p3_boulder_24k_color.mbtiles"
        kmz_path = out_dir / "p3_boulder_24k_color.kmz"

        us_spec = SheetSpec(
            center_lat=BOULDER_LAT,
            center_lon=BOULDER_LON,
            scale=24000,
            paper=PaperSize.LETTER,
            orientation=Orientation.PORTRAIT,
            color_mode=ColorMode.COLOR,
            units=Units.IMPERIAL,
            title="BOULDER — FLATIRONS, CO",
            dem_source=DemSourceKind.USGS3DEP,
            us_overlays=[USOverlayKind.FEMA, USOverlayKind.NHD, USOverlayKind.TOWERS],
            carto=CartoOptions(show_river_arrows=True, show_trees=True, show_buildings=True),
        )
        layout = compute_sheet_layout(us_spec)
        expected_wgs84 = layout.wgs84_bbox.as_tuple()

        res_color = run_render_job(
            us_spec,
            color_pdf,
            mbtiles_out=mbt_path,
            kmz_out=kmz_path,
        )
        assert res_color.output_pdf == color_pdf
        assert res_color.utm_epsg == BOULDER_EPSG

        # 1:24,000 -> 41.6667 mm ± 0.15 mm 1 km grid spacing, Letter 215.9 x 279.4 mm, NAVD88 + USGS 3DEP
        assert_pdf_valid(
            color_pdf,
            scale=24000,
            paper="letter",
            orientation="portrait",
            expected_epsg=BOULDER_EPSG,
            expected_gzd=BOULDER_GZD,
            expected_title="BOULDER",
            extra_required_text=["NAVD88", "3DEP"],
            require_osm_attribution=True,
            require_copernicus_attribution=False,
        )
        assert_mbtiles_valid(mbt_path, expected_format="png", expected_bounds=expected_wgs84)
        assert_garmin_kmz_valid(kmz_path, max_tiles=100, expected_bounds=expected_wgs84)

        # Boulder B&W sheet
        bw_spec = us_spec.model_copy(
            update={
                "color_mode": ColorMode.BW,
                "title": "BOULDER — FLATIRONS, CO (B&W)",
            }
        )
        run_render_job(bw_spec, bw_pdf)
        assert_pdf_valid(
            bw_pdf,
            scale=24000,
            paper="letter",
            orientation="portrait",
            expected_epsg=BOULDER_EPSG,
            expected_gzd=BOULDER_GZD,
            expected_title="BOULDER",
            extra_required_text=["NAVD88", "3DEP"],
            require_osm_attribution=True,
            require_copernicus_attribution=False,
        )
