"""GridForge v1.1.0 Field Usability & Alpine Cartography E2E Integration Tests.

Covers:
  1. Offline CI tests (default, zero network):
     - Full v1.1.0 multi-page Blue Mountains render:
       Slope-Angle Shading (`show_slope_shading=True`) + mock `au-nsw-trails` & `au-vic-trails`
       + 3 custom tactical waypoints + Elevation Profile + Route Card + 100 mm Calibration & Romer Page.
       Verifies 4-page PDF consistency, exact 40.00 mm ± 0.15 mm 1 km grid on map sheet,
       exact 100.00 mm ± 0.15 mm horizontal & vertical vector rulers on calibration page,
       MGRS/azimuths on Route Card page, and ascent/descent stats on Elevation Profile page.
     - CLI `--strip-route examples/katoomba_route.geojson` offline auto-strip-atlas +
       Elevation Profile + 100 mm Calibration page.
     - In-process FastAPI `TestClient` checks for `POST /api/strip-plan` (multipart GPX/GeoJSON
       and JSON body) and `POST /api/route-metrics` (leg distances + WMM2025 azimuths).
  2. Live E2E tests (`@pytest.mark.live` + `GRIDFORGE_RUN_LIVE=1`):
     - Mt Kosciuszko NSW (`-36.456, 148.263`, `55H`, `EPSG:32755`, A4 1:25k Color & B&W):
       live `au-nsw-trails` (fire trails, tracks, huts, trigs) + Slope-Angle Shading (>30°) +
       3 alpine waypoints + Route Card + 100 mm Calibration Page.
     - Grampians VIC (`-37.145, 142.515`, `54H`, `EPSG:32754`, A4 1:25k Color):
       live `au-vic-trails` (Vicmap walking/4WD tracks, huts, campgrounds) + Slope-Angle Shading.
     - Katoomba Bushwalk Strip Atlas (`examples/katoomba_route.geojson`):
       live multi-page Strip Atlas + DEM Elevation Profile + 100 mm Calibration Page.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from gridforge.config import (
    AUOverlayKind,
    CartoOptions,
    ColorMode,
    DemSourceKind,
    Orientation,
    PaperSize,
    SheetLayout,
    SheetSpec,
    Units,
    WaypointCategory,
    WaypointInput,
)
from gridforge.geo.crs import compute_sheet_layout
from gridforge.pipeline.runner import run_render_job
from gridforge.sources.dem_local import LocalDEMSource
from gridforge.sources.osm_pbf import LocalPBFSource

_INTEGRATION_DIR = Path(__file__).resolve().parent
if str(_INTEGRATION_DIR) not in sys.path:
    sys.path.insert(0, str(_INTEGRATION_DIR))

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

GRAMPIANS_LAT = -37.145
GRAMPIANS_LON = 142.515
GRAMPIANS_EPSG = 32754
GRAMPIANS_GZD = "54H"

RUN_LIVE = os.environ.get("GRIDFORGE_RUN_LIVE", "").strip().lower() in {"1", "true", "yes"}


def _create_mock_state_trails_gpkg(out_path: Path, layout: SheetLayout, state: str) -> Path:
    """Create a valid UTM GeoPackage matching the unified v1.1.0 NSW/VIC state-trail schema."""
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
    offset = 0.0 if state == "NSW" else 220.0

    ds = driver.CreateDataSource(str(out_path))

    # Layer 1: au_state_trails (MultiLineString: name, trail_class, surface, state)
    lyr_t = ds.CreateLayer("au_state_trails", srs=srs, geom_type=ogr.wkbMultiLineString)
    for fname in ("name", "trail_class", "surface", "state"):
        lyr_t.CreateField(ogr.FieldDefn(fname, ogr.OFTString))

    trail_specs = (
        (
            f"{state} Ridge Fire Trail",
            "fire_trail",
            "unsealed",
            [(cx - 1200, cy + 500 + offset), (cx - 150, cy + 320 + offset), (cx + 950, cy + 120 + offset)],
        ),
        (
            f"{state} Alpine 4WD Track",
            "4wd_track",
            "dirt",
            [(cx - 900, cy - 450 - offset), (cx + 100, cy - 350 - offset), (cx + 1000, cy - 550 - offset)],
        ),
        (
            f"{state} Summit Walking Track",
            "walking_track",
            "natural",
            [(cx - 500, cy - 900), (cx + offset, cy), (cx + 450, cy + 850)],
        ),
    )
    for name, trail_class, surface, pts in trail_specs:
        line = ogr.Geometry(ogr.wkbLineString)
        for px, py in pts:
            line.AddPoint_2D(px, py)
        mline = ogr.Geometry(ogr.wkbMultiLineString)
        mline.AddGeometry(line)
        feat = ogr.Feature(lyr_t.GetLayerDefn())
        feat.SetGeometry(mline)
        feat.SetField("name", name)
        feat.SetField("trail_class", trail_class)
        feat.SetField("surface", surface)
        feat.SetField("state", state)
        lyr_t.CreateFeature(feat)

    # Layer 2: au_state_pois (Point: name, poi_type, elev_m, state)
    lyr_p = ds.CreateLayer("au_state_pois", srs=srs, geom_type=ogr.wkbPoint)
    lyr_p.CreateField(ogr.FieldDefn("name", ogr.OFTString))
    lyr_p.CreateField(ogr.FieldDefn("poi_type", ogr.OFTString))
    lyr_p.CreateField(ogr.FieldDefn("elev_m", ogr.OFTReal))
    lyr_p.CreateField(ogr.FieldDefn("state", ogr.OFTString))

    poi_specs = (
        (f"{state} Alpine Refuge Hut", "hut", 1015.0, cx - 420 + offset, cy + 280),
        (f"{state} Valley Campground", "campground", 690.0, cx + 520, cy - 420 - offset),
        (f"{state} Primary Trig", "trig", 1084.5, cx + 180 + offset, cy + 620),
    )
    for name, poi_type, elev_m, px, py in poi_specs:
        pt = ogr.Geometry(ogr.wkbPoint)
        pt.AddPoint_2D(px, py)
        feat = ogr.Feature(lyr_p.GetLayerDefn())
        feat.SetGeometry(pt)
        feat.SetField("name", name)
        feat.SetField("poi_type", poi_type)
        feat.SetField("elev_m", elev_m)
        feat.SetField("state", state)
        lyr_p.CreateFeature(feat)

    ds = None
    return out_path


def _find_page_indices(page_texts: list[str], expected_title: str) -> dict[str, list[int]]:
    """Classify pages in a multi-page v1.1.0 PDF by their extracted text signatures."""
    classified: dict[str, list[int]] = {
        "map": [],
        "elevation": [],
        "route_card": [],
        "calibration": [],
    }
    title_l = expected_title.lower()
    for idx, txt in enumerate(page_texts):
        low = txt.lower()
        if ("actual size" in low or "100%" in low) and ("romer" in low or "scale verification" in low or "calibration" in low):
            classified["calibration"].append(idx)
        elif "route card" in low or ("wp1" in low and ("mag" in low or "azimuth" in low or "leg" in low)):
            classified["route_card"].append(idx)
        elif "elevation profile" in low or (("ascent" in low or "gain" in low) and ("descent" in low or "loss" in low)):
            classified["elevation"].append(idx)
        elif "1:25,000" in txt or title_l in low:
            classified["map"].append(idx)
    return classified


class TestOfflineV11Usability:
    """Fast zero-network CI verification for all GridForge v1.1.0 usability features."""

    def test_offline_v11_slope_trails_waypoints_and_4page_supplements(self, tmp_path: Path) -> None:
        dem_path = FIXTURES_DIR / "synthetic_dem.tif"
        osm_path = FIXTURES_DIR / "synthetic_osm.osm"
        assert dem_path.is_file() and osm_path.is_file()
        assert KATOOMBA_ROUTE_GEOJSON.is_file()

        waypoints = [
            WaypointInput(
                label="Katoomba Base",
                lat=-33.7190,
                lon=150.3114,
                category=WaypointCategory.CAMP,
                notes="Start camp",
            ),
            WaypointInput(
                label="Echo Point",
                lat=-33.7322,
                lon=150.3117,
                category=WaypointCategory.WATER,
                notes="Water resupply",
            ),
            WaypointInput(
                label="Federal Pass LZ",
                lat=-33.7356,
                lon=150.3168,
                category=WaypointCategory.LZ,
                notes="Clearing",
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
            title="V11 OFFLINE ALPINE SUITE",
            date_iso="2026-06-15",
            carto=CartoOptions(show_slope_shading=True),
            au_overlays=[AUOverlayKind.AU_NSW_TRAILS, AUOverlayKind.AU_VIC_TRAILS],
            waypoints=waypoints,
            include_calibration_page=True,
            include_route_card=True,
            include_elevation_profile=True,
        )
        layout = compute_sheet_layout(spec)

        mock_nsw = _create_mock_state_trails_gpkg(tmp_path / "mock_nsw_trails.gpkg", layout, "NSW")
        mock_vic = _create_mock_state_trails_gpkg(tmp_path / "mock_vic_trails.gpkg", layout, "VIC")

        out_pdf = tmp_path / "v11_offline_4page_color.pdf"
        res = run_render_job(
            spec,
            out_pdf,
            elevation_source=LocalDEMSource(dem_path),
            vector_source=LocalPBFSource(osm_path),
            overlay_path=KATOOMBA_ROUTE_GEOJSON,
            regional_overlay_overrides={
                AUOverlayKind.AU_NSW_TRAILS: mock_nsw,
                AUOverlayKind.AU_VIC_TRAILS: mock_vic,
            },
        )
        assert res.output_pdf == out_pdf
        assert res.utm_epsg == BLUE_MOUNTAINS_EPSG

        # 1. Verify 4-page PDF structure, consistent A4 MediaBox, and non-empty text per page
        mp_report = assert_multipage_pdf_valid(
            out_pdf,
            expected_pages=4,
            expected_width_mm=210.0,
            expected_height_mm=297.0,
        )
        pages_by_role = _find_page_indices(mp_report.page_texts, "V11 OFFLINE ALPINE SUITE")
        assert len(pages_by_role["map"]) == 1, f"Could not uniquely locate map page: {pages_by_role}"
        assert len(pages_by_role["elevation"]) == 1, f"Missing Elevation Profile page: {pages_by_role}"
        assert len(pages_by_role["route_card"]) == 1, f"Missing Route Card page: {pages_by_role}"
        assert len(pages_by_role["calibration"]) == 1, f"Missing Calibration page: {pages_by_role}"

        # 2. Verify physical 1:25,000 scale & 40.00 mm ± 0.15 mm 1 km grid on the map sheet page
        map_idx = pages_by_role["map"][0]
        assert_pdf_valid(
            out_pdf,
            page_index=map_idx,
            allow_multipage=True,
            scale=25000,
            paper="a4",
            orientation="portrait",
            expected_epsg=BLUE_MOUNTAINS_EPSG,
            expected_gzd=BLUE_MOUNTAINS_GZD,
            expected_title="V11 OFFLINE ALPINE SUITE",
            require_osm_attribution=True,
            require_copernicus_attribution=False,
        )

        # 3. Verify Route Card page contains MGRS coordinates, waypoint codes, and bearings/azimuths
        rc_text = mp_report.page_texts[pages_by_role["route_card"][0]]
        for expected_token in ("WP1", "WP2", "WP3", "56H"):
            assert expected_token in rc_text, f"Missing {expected_token!r} on Route Card page: {rc_text[:400]!r}"
        assert any(tok in rc_text.lower() for tok in ("mag", "az", "°", "mils")), (
            f"Missing azimuth/bearing columns on Route Card page: {rc_text[:400]!r}"
        )

        # 4. Verify Elevation Profile page contains ascent/descent & elevation stats
        ep_text = mp_report.page_texts[pages_by_role["elevation"][0]].lower()
        assert any(tok in ep_text for tok in ("ascent", "gain", "climb")), (
            f"Missing ascent/gain stat on Elevation Profile page: {ep_text[:400]!r}"
        )
        assert any(tok in ep_text for tok in ("descent", "loss")), (
            f"Missing descent/loss stat on Elevation Profile page: {ep_text[:400]!r}"
        )

        # 5. Verify exact 100.00 mm ± 0.15 mm horizontal & vertical rulers on Calibration page
        cal_idx = pages_by_role["calibration"][0]
        cal_report = assert_calibration_page_valid(
            out_pdf,
            page_index=cal_idx,
            expected_width_mm=210.0,
            expected_height_mm=297.0,
        )
        assert cal_report.horizontal_ruler_err_mm is not None and cal_report.horizontal_ruler_err_mm <= 0.15
        assert cal_report.vertical_ruler_err_mm is not None and cal_report.vertical_ruler_err_mm <= 0.15

        # 6. Verify B&W Slope-Angle Shading + Calibration Page (2 pages)
        bw_spec = spec.model_copy(
            update={
                "color_mode": ColorMode.BW,
                "title": "V11 OFFLINE ALPINE BW",
                "include_route_card": False,
                "include_elevation_profile": False,
                "include_calibration_page": True,
            }
        )
        out_bw = tmp_path / "v11_offline_2page_bw.pdf"
        run_render_job(
            bw_spec,
            out_bw,
            elevation_source=LocalDEMSource(dem_path),
            vector_source=LocalPBFSource(osm_path),
            regional_overlay_overrides={AUOverlayKind.AU_NSW_TRAILS: mock_nsw},
        )
        assert_multipage_pdf_valid(out_bw, expected_pages=2, expected_width_mm=210.0, expected_height_mm=297.0)
        assert_pdf_valid(
            out_bw,
            page_index=0,
            allow_multipage=True,
            scale=25000,
            paper="a4",
            orientation="portrait",
            expected_epsg=BLUE_MOUNTAINS_EPSG,
            expected_gzd=BLUE_MOUNTAINS_GZD,
            expected_title="V11 OFFLINE ALPINE BW",
            require_osm_attribution=True,
            require_copernicus_attribution=False,
        )
        assert_calibration_page_valid(out_bw, page_index=-1, expected_width_mm=210.0, expected_height_mm=297.0)

    def test_offline_cli_strip_route_katoomba_atlas(self, tmp_path: Path) -> None:
        from gridforge.geo.strip_atlas import parse_route_file, plan_strip_atlas

        dem_path = FIXTURES_DIR / "synthetic_dem.tif"
        osm_path = FIXTURES_DIR / "synthetic_osm.osm"
        assert KATOOMBA_ROUTE_GEOJSON.is_file()

        route_pts = parse_route_file(KATOOMBA_ROUTE_GEOJSON)
        plan = plan_strip_atlas(
            route_pts,
            scale=25000,
            paper=PaperSize.A4,
            orientation=Orientation.PORTRAIT,
            units=Units.METRIC,
        )
        assert plan.utm_epsg == BLUE_MOUNTAINS_EPSG
        assert plan.gzd == BLUE_MOUNTAINS_GZD
        assert len(plan.sheets) >= 1

        out_pdf = tmp_path / "offline_katoomba_strip_atlas.pdf"
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
                "KATOOMBA OFFLINE STRIP",
                "--slope-shading",
                "--elevation-profile",
                "--calibration-page",
                "--offline-dem",
                str(dem_path),
                "--offline-pbf",
                str(osm_path),
                "--output",
                str(out_pdf),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, f"CLI --strip-route failed:\n{proc.stderr}\n{proc.stdout}"
        assert out_pdf.is_file()

        expected_pages = len(plan.sheets) + 2  # Elevation Profile + N strip sheets + Calibration Page
        mp_report = assert_multipage_pdf_valid(
            out_pdf,
            expected_pages=expected_pages,
            expected_width_mm=210.0,
            expected_height_mm=297.0,
        )
        pages_by_role = _find_page_indices(mp_report.page_texts, "KATOOMBA OFFLINE STRIP")
        assert len(pages_by_role["map"]) == len(plan.sheets), (
            f"Expected {len(plan.sheets)} strip map pages, classified={pages_by_role}"
        )
        assert len(pages_by_role["elevation"]) == 1, f"Missing Elevation Profile page: {pages_by_role}"
        assert len(pages_by_role["calibration"]) == 1, f"Missing Calibration page: {pages_by_role}"

        for map_idx in pages_by_role["map"]:
            assert_pdf_valid(
                out_pdf,
                page_index=map_idx,
                allow_multipage=True,
                scale=25000,
                paper="a4",
                orientation="portrait",
                expected_epsg=BLUE_MOUNTAINS_EPSG,
                expected_gzd=BLUE_MOUNTAINS_GZD,
                require_osm_attribution=True,
                require_copernicus_attribution=False,
            )

        assert_calibration_page_valid(
            out_pdf,
            page_index=pages_by_role["calibration"][0],
            expected_width_mm=210.0,
            expected_height_mm=297.0,
        )

    def test_offline_web_api_strip_plan_and_route_metrics(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fastapi_testclient = pytest.importorskip("fastapi.testclient")
        from gridforge.web.app import create_app

        data_dir = tmp_path / "web_data"
        packs_dir = tmp_path / "packs"
        data_dir.mkdir(parents=True, exist_ok=True)
        packs_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("GRIDFORGE_DATA_DIR", str(data_dir))
        monkeypatch.setenv("GRIDFORGE_PACKS_DIR", str(packs_dir))

        app = create_app()
        route_bytes = KATOOMBA_ROUTE_GEOJSON.read_bytes()
        route_json = json.loads(route_bytes.decode("utf-8"))

        with fastapi_testclient.TestClient(app, base_url="http://localhost:8080") as client:
            # 1. POST /api/strip-plan via multipart/form-data
            sp_mp = client.post(
                "/api/strip-plan",
                data={
                    "scale": "25000",
                    "paper": "a4",
                    "orientation": "portrait",
                    "units": "metric",
                },
                files={"route_file": ("katoomba_route.geojson", route_bytes, "application/geo+json")},
            )
            assert sp_mp.status_code == 200, f"/api/strip-plan multipart failed: {sp_mp.text}"
            sp_body = sp_mp.json()
            assert sp_body["utm_epsg"] == BLUE_MOUNTAINS_EPSG
            assert sp_body["gzd"] == BLUE_MOUNTAINS_GZD
            assert sp_body["route_distance_km"] > 4.0
            assert len(sp_body["sheets"]) >= 1
            assert sp_body["sheets"][0]["sheet_id"] == "S1"
            assert sp_body["route_geojson"]["type"] == "FeatureCollection"
            assert sp_body["footprint_geojson"]["type"] == "FeatureCollection"
            assert sp_body["grid_geojson"]["type"] == "FeatureCollection"

            # 2. POST /api/strip-plan via JSON route_geojson
            sp_js = client.post(
                "/api/strip-plan",
                json={
                    "route_geojson": route_json,
                    "scale": 25000,
                    "paper": "a4",
                    "orientation": "portrait",
                    "units": "metric",
                },
            )
            assert sp_js.status_code == 200, f"/api/strip-plan JSON failed: {sp_js.text}"
            assert sp_js.json()["utm_epsg"] == BLUE_MOUNTAINS_EPSG
            assert len(sp_js.json()["sheets"]) == len(sp_body["sheets"])

            # 3. POST /api/route-metrics
            rm = client.post(
                "/api/route-metrics",
                json={
                    "waypoints": [
                        {"label": "Katoomba Station", "lat": -33.7133, "lon": 150.3113, "category": "waypoint"},
                        {"label": "Echo Point", "lat": -33.7322, "lon": 150.3117, "category": "camp"},
                        {"label": "Leura Forest", "lat": -33.7347, "lon": 150.3185, "category": "water"},
                    ],
                    "units": "metric",
                    "date_iso": "2026-06-15",
                },
            )
            assert rm.status_code == 200, f"/api/route-metrics failed: {rm.text}"
            rm_body = rm.json()
            assert len(rm_body["waypoints"]) == 3
            assert len(rm_body["legs"]) == 2
            assert rm_body["waypoints"][0]["code"] == "WP1"
            assert rm_body["waypoints"][0]["mgrs_10m"].startswith("56H")
            assert rm_body["total_distance_km"] > 2.0
            for leg in rm_body["legs"]:
                assert leg["leg_dist_km"] > 0.0
                assert 0.0 <= leg["mag_az_deg"] <= 360.0
                assert 0.0 <= leg["mag_az_mils"] <= 6400.0


@pytest.mark.live
@pytest.mark.skipif(
    not RUN_LIVE,
    reason="Live v1.1.0 tests skipped by default; set GRIDFORGE_RUN_LIVE=1 to run.",
)
class TestLiveV11Usability:
    """Live E2E verification for Mt Kosciuszko NSW, Grampians VIC, and Katoomba Strip Atlas."""

    def _out_dir(self, tmp_path: Path) -> Path:
        override = os.environ.get("GRIDFORGE_LIVE_OUT_DIR")
        if override:
            path = Path(override)
            path.mkdir(parents=True, exist_ok=True)
            return path
        return tmp_path

    def test_live_kosciuszko_nsw_trails_slope_waypoints_color_and_bw(self, tmp_path: Path) -> None:
        out_dir = self._out_dir(tmp_path)
        alpine_waypoints = [
            WaypointInput(
                label="Mt Kosciuszko Summit",
                lat=-36.4559,
                lon=148.2636,
                category=WaypointCategory.WAYPOINT,
                notes="2228 m highest peak",
            ),
            WaypointInput(
                label="Rawson Pass",
                lat=-36.4631,
                lon=148.2736,
                category=WaypointCategory.HAZARD,
                notes="Exposed alpine saddle",
            ),
            WaypointInput(
                label="Seamans Hut",
                lat=-36.4535,
                lon=148.2877,
                category=WaypointCategory.CAMP,
                notes="Emergency shelter hut",
            ),
        ]

        base_spec = SheetSpec(
            center_lat=KOSCIUSZKO_LAT,
            center_lon=KOSCIUSZKO_LON,
            scale=25000,
            paper=PaperSize.A4,
            orientation=Orientation.PORTRAIT,
            color_mode=ColorMode.COLOR,
            units=Units.METRIC,
            title="MT KOSCIUSZKO — ALPINE NSW",
            dem_source=DemSourceKind.AU_ELVIS,
            au_overlays=[AUOverlayKind.AU_NSW_TRAILS],
            carto=CartoOptions(show_slope_shading=True),
            waypoints=alpine_waypoints,
            include_route_card=True,
            include_calibration_page=True,
        )

        for mode, filename, title in (
            (ColorMode.COLOR, "v11_kosciuszko_25k_a4_color.pdf", "MT KOSCIUSZKO — ALPINE NSW"),
            (ColorMode.BW, "v11_kosciuszko_25k_a4_bw.pdf", "MT KOSCIUSZKO — ALPINE NSW (B&W)"),
        ):
            out_pdf = out_dir / filename
            spec = base_spec.model_copy(update={"color_mode": mode, "title": title})
            res = run_render_job(spec, out_pdf)
            assert res.utm_epsg == KOSCIUSZKO_EPSG

            mp = assert_multipage_pdf_valid(
                out_pdf,
                expected_pages=3,
                expected_width_mm=210.0,
                expected_height_mm=297.0,
            )
            roles = _find_page_indices(mp.page_texts, "MT KOSCIUSZKO")
            assert len(roles["map"]) == 1, f"Failed to locate map page for {filename}: {roles}"
            assert len(roles["route_card"]) == 1, f"Missing Route Card page for {filename}: {roles}"
            assert len(roles["calibration"]) == 1, f"Missing Calibration page for {filename}: {roles}"

            assert_pdf_valid(
                out_pdf,
                page_index=roles["map"][0],
                allow_multipage=True,
                scale=25000,
                paper="a4",
                orientation="portrait",
                expected_epsg=KOSCIUSZKO_EPSG,
                expected_gzd=KOSCIUSZKO_GZD,
                expected_title="MT KOSCIUSZKO",
                extra_required_text=["AHD", "New South Wales"],
                require_osm_attribution=True,
                require_copernicus_attribution=False,
            )
            rc_txt = mp.page_texts[roles["route_card"][0]]
            for tok in ("WP1", "WP2", "WP3", "55H"):
                assert tok in rc_txt, f"Missing {tok!r} on Kosciuszko Route Card: {rc_txt[:400]!r}"

            assert_calibration_page_valid(
                out_pdf,
                page_index=roles["calibration"][0],
                expected_width_mm=210.0,
                expected_height_mm=297.0,
            )

    def test_live_grampians_vic_trails_and_slope_shading(self, tmp_path: Path) -> None:
        out_pdf = self._out_dir(tmp_path) / "v11_grampians_25k_a4_color.pdf"
        spec = SheetSpec(
            center_lat=GRAMPIANS_LAT,
            center_lon=GRAMPIANS_LON,
            scale=25000,
            paper=PaperSize.A4,
            orientation=Orientation.PORTRAIT,
            color_mode=ColorMode.COLOR,
            units=Units.METRIC,
            title="GRAMPIANS — GARIWERD VIC",
            dem_source=DemSourceKind.AU_ELVIS,
            au_overlays=[AUOverlayKind.AU_VIC_TRAILS],
            carto=CartoOptions(show_slope_shading=True),
        )
        res = run_render_job(spec, out_pdf)
        assert res.utm_epsg == GRAMPIANS_EPSG

        assert_pdf_valid(
            out_pdf,
            scale=25000,
            paper="a4",
            orientation="portrait",
            expected_epsg=GRAMPIANS_EPSG,
            expected_gzd=GRAMPIANS_GZD,
            expected_title="GRAMPIANS",
            extra_required_text=["AHD", "Victoria"],
            require_osm_attribution=True,
            require_copernicus_attribution=False,
        )

    def test_live_katoomba_strip_atlas_elevation_profile_and_calibration(self, tmp_path: Path) -> None:
        from gridforge.geo.strip_atlas import parse_route_file, plan_strip_atlas

        out_pdf = self._out_dir(tmp_path) / "v11_katoomba_strip_atlas_25k_a4.pdf"
        route_pts = parse_route_file(KATOOMBA_ROUTE_GEOJSON)
        plan = plan_strip_atlas(
            route_pts,
            scale=25000,
            paper=PaperSize.A4,
            orientation=Orientation.PORTRAIT,
            units=Units.METRIC,
        )

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
                "--slope-shading",
                "--elevation-profile",
                "--calibration-page",
                "--title",
                "KATOOMBA BUSHWALK STRIP ATLAS",
                "--output",
                str(out_pdf),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, f"Live strip atlas CLI failed:\n{proc.stderr}\n{proc.stdout}"
        assert out_pdf.is_file()

        expected_pages = len(plan.sheets) + 2
        mp = assert_multipage_pdf_valid(
            out_pdf,
            expected_pages=expected_pages,
            expected_width_mm=210.0,
            expected_height_mm=297.0,
        )
        roles = _find_page_indices(mp.page_texts, "KATOOMBA BUSHWALK STRIP ATLAS")
        assert len(roles["map"]) == len(plan.sheets), f"Expected {len(plan.sheets)} map sheets: {roles}"
        assert len(roles["elevation"]) == 1, f"Missing Elevation Profile page: {roles}"
        assert len(roles["calibration"]) == 1, f"Missing Calibration page: {roles}"

        for map_idx in roles["map"]:
            assert_pdf_valid(
                out_pdf,
                page_index=map_idx,
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

        ep_txt = mp.page_texts[roles["elevation"][0]].lower()
        assert any(tok in ep_txt for tok in ("ascent", "gain", "climb"))
        assert any(tok in ep_txt for tok in ("descent", "loss"))

        assert_calibration_page_valid(
            out_pdf,
            page_index=roles["calibration"][0],
            expected_width_mm=210.0,
            expected_height_mm=297.0,
        )
