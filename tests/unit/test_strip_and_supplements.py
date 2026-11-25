"""Unit tests for v1.1.0 strip atlas planner, waypoints/route legs, and supplemental PDFs."""

from __future__ import annotations

import math
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from osgeo import ogr
from pypdf import PdfReader

from gridforge.config import (
    Orientation,
    PaperSize,
    SheetSpec,
    Units,
    WaypointCategory,
    WaypointInput,
)
from gridforge.geo.crs import compute_sheet_layout
from gridforge.geo.declination import compute_declination
from gridforge.geo.strip_atlas import (
    parse_route_file,
    parse_route_geojson,
    plan_strip_atlas,
    render_elevation_profile_svg,
    sample_route_elevation,
)
from gridforge.geo.supplements import (
    merge_pdf_pages,
    render_calibration_pdf,
    render_elevation_profile_pdf,
    render_route_card_pdf,
)
from gridforge.geo.waypoints import compute_route_metrics, write_waypoints_gpkg

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"
EXAMPLE_ROUTE = REPO_ROOT / "examples" / "katoomba_route.geojson"

# Reuse our verified PDF vector line parser for exact 100.00 mm ruler checks!
_INTEGRATION_DIR = REPO_ROOT / "tests" / "integration"
if str(_INTEGRATION_DIR) not in sys.path:
    sys.path.insert(0, str(_INTEGRATION_DIR))


def _norm_text(raw: str | None) -> str:
    return re.sub(r"\s+", " ", raw or "").strip()


def _extract_hv_line_lengths_mm(pdf_path: Path) -> tuple[list[float], list[float]]:
    """Extract horizontal & vertical vector line segment lengths (mm) from page 0."""
    from verify_pdf import _extract_pdf_vectors

    reader = PdfReader(str(pdf_path))
    vectors = _extract_pdf_vectors(reader, 0)
    h_lengths = [
        abs(x1 - x0)
        for x0, y0, x1, y1 in vectors.horizontal_segments
        if abs(y1 - y0) < 0.05 and abs(x1 - x0) > 1.0
    ]
    v_lengths = [
        abs(y1 - y0)
        for x0, y0, x1, y1 in vectors.vertical_segments
        if abs(x1 - x0) < 0.05 and abs(y1 - y0) > 1.0
    ]
    return (h_lengths, v_lengths)


class TestStripAtlasPlanner:
    def test_parse_katoomba_example_geojson_and_plan_strip(self) -> None:
        assert EXAMPLE_ROUTE.is_file()
        pts = parse_route_file(EXAMPLE_ROUTE)
        assert len(pts) == 16

        plan = plan_strip_atlas(
            pts,
            scale=25000,
            paper=PaperSize.A4,
            orientation=Orientation.PORTRAIT,
        )
        assert plan.utm_epsg == 32756
        assert plan.gzd == "56H"
        assert plan.sheet_count == len(plan.sheets) >= 1
        assert plan.route_distance_km > 4.0
        assert plan.sheets[0].sheet_id == "S1"
        assert plan.sheets[0].center_mgrs_10m.startswith("56H KH ")
        assert plan.route_geojson["type"] == "FeatureCollection"
        assert plan.footprint_geojson["type"] == "FeatureCollection"
        assert len(plan.footprint_geojson["features"]) == plan.sheet_count
        assert len(plan.grid_geojson["features"]) >= 6

    def test_parse_gpx_and_multi_sheet_strip_coverage(self, tmp_path: Path) -> None:
        # 14 km North-South alpine traverse near Katoomba -> requires multiple A4 1:25k sheets!
        gpx_pts = [
            (-33.78 + i * 0.012, 150.31 + math.sin(i * 0.4) * 0.008)
            for i in range(11)
        ]
        trkpts_xml = "\n".join(
            f'      <trkpt lat="{lat:.6f}" lon="{lon:.6f}"><ele>900</ele></trkpt>'
            for lat, lon in gpx_pts
        )
        gpx_path = tmp_path / "traverse.gpx"
        gpx_path.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">\n'
            f"  <trk><name>Alpine Traverse</name><trkseg>\n{trkpts_xml}\n  </trkseg></trk>\n"
            "</gpx>\n",
            encoding="utf-8",
        )

        parsed = parse_route_file(gpx_path)
        assert len(parsed) == 11

        plan = plan_strip_atlas(
            parsed,
            scale=25000,
            paper=PaperSize.A4,
            orientation=Orientation.PORTRAIT,
            overlap_pct=10.0,
        )
        assert plan.sheet_count >= 3
        assert [s.sheet_id for s in plan.sheets] == [
            f"S{i}" for i in range(1, plan.sheet_count + 1)
        ]

        # Verify 100% of route vertices lie inside at least one planned UTM sheet bbox!
        from pyproj import Transformer

        to_utm = Transformer.from_crs("EPSG:4326", f"EPSG:{plan.utm_epsg}", always_xy=True)
        for lon, lat in parsed:
            ux, uy = to_utm.transform(lon, lat)
            covered = any(
                s.utm_bbox.min_x <= ux <= s.utm_bbox.max_x
                and s.utm_bbox.min_y <= uy <= s.utm_bbox.max_y
                for s in plan.sheets
            )
            assert covered, f"Route vertex ({lat}, {lon}) not covered by strip atlas!"

        # Tight max_sheets raises actionable ValueError
        with pytest.raises(ValueError, match="requires more than 1 sheets"):
            plan_strip_atlas(parsed, scale=25000, paper=PaperSize.A4, max_sheets=1)

    def test_sample_route_elevation_and_svg(self, tmp_path: Path) -> None:
        dem_fixture = FIXTURES_DIR / "synthetic_dem.tif"
        assert dem_fixture.is_file()
        pts = parse_route_file(EXAMPLE_ROUTE)

        profile = sample_route_elevation(
            pts,
            [dem_fixture],
            utm_epsg=32756,
            waypoint_ticks=[(0.0, "Start"), (2.1, "Echo Pt")],
        )
        assert len(profile.distances_km) >= 20
        assert len(profile.distances_km) == len(profile.elevations_m)
        assert profile.total_distance_km > 4.0
        assert profile.max_elev_m > profile.min_elev_m
        assert profile.total_ascent_m > 0.0
        assert profile.total_descent_m > 0.0

        for bw, units in ((False, Units.METRIC), (True, Units.IMPERIAL)):
            svg_path = tmp_path / f"elev_{units.value}_{'bw' if bw else 'col'}.svg"
            render_elevation_profile_svg(profile, units, svg_path, bw=bw)
            assert svg_path.is_file()
            root = ET.fromstring(svg_path.read_text(encoding="utf-8"))
            assert root.tag.endswith("svg")
            text = " ".join(t.strip() for t in root.itertext() if t.strip())
            assert "ELEVATION PROFILE" in text
            assert ("mi" if units == Units.IMPERIAL else "km") in text


class TestWaypointsAndRouteMetrics:
    def test_compute_route_metrics_and_gpkg(self, tmp_path: Path) -> None:
        dem_fixture = FIXTURES_DIR / "synthetic_dem.tif"
        waypoints = [
            WaypointInput(label="Katoomba Stn", lat=-33.7133, lon=150.3113, category=WaypointCategory.WAYPOINT),
            WaypointInput(label="Echo Point", lat=-33.7322, lon=150.3117, category=WaypointCategory.CAMP),
            WaypointInput(label="Leura Forest", lat=-33.7347, lon=150.3185, category=WaypointCategory.WATER),
        ]
        metrics = compute_route_metrics(
            waypoints,
            units=Units.METRIC,
            date_iso="2026-06-15",
            dem_path=dem_fixture,
        )
        assert len(metrics.waypoints) == 3
        assert len(metrics.legs) == 2
        assert metrics.waypoints[0].code == "WP1"
        assert metrics.waypoints[0].mgrs_10m.startswith("56H KH ")
        assert metrics.waypoints[0].elev_m is not None
        assert metrics.waypoints[0].leg_dist_km is None
        assert metrics.total_distance_km > 2.0

        # Leg 1 goes almost due South (~179° True); Blue Mountains easterly decl ~12.5°E -> Mag Az ~166°
        leg1 = metrics.legs[0]
        assert 175.0 < leg1.true_az_deg < 185.0
        assert leg1.mag_az_deg == pytest.approx(
            (leg1.true_az_deg - leg1.declination_deg) % 360.0, abs=0.2
        )
        assert leg1.mag_az_mils == pytest.approx(
            leg1.mag_az_deg * 6400.0 / 360.0, abs=3.0
        )

        # Write GeoPackage & inspect OGR layers
        out_gpkg = tmp_path / "waypoints.gpkg"
        write_waypoints_gpkg(metrics, 32756, out_gpkg)
        assert out_gpkg.is_file()

        ds = ogr.Open(str(out_gpkg))
        assert ds is not None
        lyr_pts = ds.GetLayerByName("waypoint_points")
        lyr_rt = ds.GetLayerByName("waypoint_route")
        assert lyr_pts is not None and lyr_pts.GetFeatureCount() == 3
        assert lyr_rt is not None and lyr_rt.GetFeatureCount() == 1
        assert lyr_pts.GetSpatialRef().GetAuthorityCode(None) == "32756"
        ds = None


class TestTrueScaleSupplementalPDFs:
    @pytest.mark.parametrize(
        ("paper_w", "paper_h", "scale", "bw"),
        [
            (210.0, 297.0, 25000, False),  # A4 Portrait Color
            (297.0, 210.0, 25000, True),   # A4 Landscape B&W
            (215.9, 279.4, 24000, False),  # Letter Portrait Color
            (297.0, 420.0, 50000, False),  # A3 Portrait Color
        ],
    )
    def test_calibration_pdf_exact_100mm_rulers_and_mediabox(
        self,
        tmp_path: Path,
        paper_w: float,
        paper_h: float,
        scale: int,
        bw: bool,
    ) -> None:
        from verify_pdf import assert_calibration_page_valid

        out_pdf = tmp_path / f"cal_{int(paper_w)}x{int(paper_h)}_{scale}.pdf"
        render_calibration_pdf(paper_w, paper_h, scale, out_pdf, bw=bw)
        assert out_pdf.is_file()

        # Verify against verifier's canonical assert_calibration_page_valid() contract!
        report = assert_calibration_page_valid(
            out_pdf,
            page_index=0,
            expected_width_mm=paper_w,
            expected_height_mm=paper_h,
            tolerance_mm=0.15,
        )
        assert report.horizontal_ruler_err_mm is not None and report.horizontal_ruler_err_mm <= 0.15
        assert report.vertical_ruler_err_mm is not None and report.vertical_ruler_err_mm <= 0.15

        reader = PdfReader(str(out_pdf))
        assert len(reader.pages) == 1
        text = _norm_text(reader.pages[0].extract_text())
        for token in ("100.0", "Actual Size", "1:25,000", "1:24,000", "1:50,000", "MGRS ROMER"):
            assert token in text

        # Also verify 4.00 in = 101.60 mm imperial verification bar and MGRS Romer 1:25k (40.00 mm)
        h_lens, v_lens = _extract_hv_line_lengths_mm(out_pdf)
        assert any(abs(ln - 100.0) <= 0.15 for ln in h_lens)
        assert any(abs(ln - 100.0) <= 0.15 for ln in v_lens)
        assert any(abs(ln - 101.6) <= 0.15 for ln in h_lens)
        assert any(abs(ln - 40.0) <= 0.15 for ln in h_lens)

    def test_route_card_elevation_profile_and_merge_pdfs(self, tmp_path: Path) -> None:
        spec = SheetSpec(
            center_lat=-33.732,
            center_lon=150.312,
            scale=25000,
            paper=PaperSize.A4,
            title="KATOOMBA FIELD TEST",
            date_iso="2026-06-15",
        )
        layout = compute_sheet_layout(spec)
        decl = compute_declination(layout, elevation_m=950.0)

        waypoints = [
            WaypointInput(label="Katoomba Stn", lat=-33.7133, lon=150.3113),
            WaypointInput(label="Echo Point", lat=-33.7322, lon=150.3117, category=WaypointCategory.CAMP),
        ]
        metrics = compute_route_metrics(
            waypoints,
            units=Units.METRIC,
            date_iso="2026-06-15",
            dem_path=FIXTURES_DIR / "synthetic_dem.tif",
        )
        card_pdf = render_route_card_pdf(metrics, layout, decl, tmp_path / "route_card.pdf")
        card_reader = PdfReader(str(card_pdf))
        assert len(card_reader.pages) == 1
        card_text = _norm_text(card_reader.pages[0].extract_text())
        assert "TACTICAL ROUTE CARD" in card_text
        assert "WP1" in card_text and "Echo Point" in card_text
        assert "56H KH" in card_text

        pts = parse_route_file(EXAMPLE_ROUTE)
        strip_plan = plan_strip_atlas(pts, scale=25000, paper=PaperSize.A4)
        profile = sample_route_elevation(pts, [FIXTURES_DIR / "synthetic_dem.tif"], 32756)
        elev_pdf = render_elevation_profile_pdf(
            profile, strip_plan, layout, tmp_path / "elev_profile.pdf"
        )
        elev_reader = PdfReader(str(elev_pdf))
        assert len(elev_reader.pages) == 1
        elev_text = _norm_text(elev_reader.pages[0].extract_text())
        assert "ELEVATION PROFILE" in elev_text
        assert "STRIP ATLAS SHEET INDEX" in elev_text
        assert "S1" in elev_text

        cal_pdf = render_calibration_pdf(210.0, 297.0, 25000, tmp_path / "cal.pdf")
        merged = merge_pdf_pages([elev_pdf, card_pdf, cal_pdf], tmp_path / "merged.pdf")
        assert len(PdfReader(str(merged)).pages) == 3
