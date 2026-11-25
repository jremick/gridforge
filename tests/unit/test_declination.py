"""Unit tests for gridforge.geo.declination."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from gridforge.config import PaperSize, SheetSpec
from gridforge.geo.crs import compute_sheet_layout
from gridforge.geo.declination import (
    DEG_TO_NATO_MILS,
    _compute_visual_prong_angles,
    compute_declination,
    render_declination_svg,
    render_grid_ref_box_svg,
)
from gridforge.geo.mgrs_grid import generate_mgrs_grid


def _extract_svg_text(svg_path: Path) -> str:
    raw = svg_path.read_text(encoding="utf-8")
    root = ET.fromstring(raw)
    assert root.tag.endswith("svg")
    return " ".join(text.strip() for text in root.itertext() if text and text.strip())


class TestComputeDeclination:
    def test_blue_mountains_nsw(self) -> None:
        spec = SheetSpec(
            center_lat=-33.732,
            center_lon=150.312,
            scale=25000,
            paper=PaperSize.A4,
            date_iso="2026-06-15",
        )
        layout = compute_sheet_layout(spec)
        info = compute_declination(layout, elevation_m=1000.0)

        assert info.model_name == "WMM2025"
        assert 2026.4 < info.decimal_year < 2026.5
        # West of CM 153°E in Southern Hemisphere -> positive grid convergence (~+1.49°)
        assert info.grid_convergence_deg == pytest.approx(1.493, abs=0.05)
        assert info.grid_convergence_deg > 0.0
        # Blue Mountains easterly magnetic declination ~12–13°E
        assert 11.5 < info.magnetic_declination_deg < 13.8
        # TC 3-25.26 formula: G-M = D - gamma
        assert info.gm_angle_deg == pytest.approx(
            info.magnetic_declination_deg - info.grid_convergence_deg, abs=1e-9
        )
        assert info.gm_angle_mils == pytest.approx(
            info.gm_angle_deg * DEG_TO_NATO_MILS, abs=1e-6
        )
        assert info.grid_convergence_mils == pytest.approx(
            info.grid_convergence_deg * DEG_TO_NATO_MILS, abs=1e-6
        )
        assert abs(info.annual_change_deg) < 0.5

    def test_austin_tx_grid_convergence_reference(self) -> None:
        # Verified PROJ reference: Austin TX (-97.74278, 30.2675) in EPSG:32614 -> +0.633763°
        spec = SheetSpec(
            center_lat=30.2675,
            center_lon=-97.74278,
            scale=24000,
            paper=PaperSize.LETTER,
            date_iso="2026-01-01",
        )
        layout = compute_sheet_layout(spec)
        assert layout.utm_epsg == 32614

        info = compute_declination(layout, elevation_m=150.0)
        assert info.grid_convergence_deg == pytest.approx(0.633763, abs=0.002)
        assert info.gm_angle_deg == pytest.approx(
            info.magnetic_declination_deg - info.grid_convergence_deg, abs=1e-9
        )

    def test_colorado_rockies_negative_convergence(self) -> None:
        spec = SheetSpec(
            center_lat=39.7392,
            center_lon=-105.519,
            scale=24000,
            paper=PaperSize.LETTER,
            date_iso="2026-01-01",
        )
        layout = compute_sheet_layout(spec)
        assert layout.utm_epsg == 32613

        info = compute_declination(layout, elevation_m=2800.0)
        # West of CM -105° in Northern Hemisphere -> negative convergence
        assert info.grid_convergence_deg < 0.0
        # Easterly declination ~7–9°E
        assert 6.0 < info.magnetic_declination_deg < 10.0
        assert info.gm_angle_deg == pytest.approx(
            info.magnetic_declination_deg - info.grid_convergence_deg, abs=1e-9
        )
        assert info.gm_angle_deg > info.magnetic_declination_deg

    def test_wmm2025_date_edges_and_out_of_range(self) -> None:
        for edge_date in ("2025-01-01", "2029-12-31"):
            spec = SheetSpec(
                center_lat=-33.732,
                center_lon=150.312,
                date_iso=edge_date,
            )
            info = compute_declination(compute_sheet_layout(spec))
            assert 2025.0 <= info.decimal_year <= 2030.0

        for bad_date in ("2020-06-01", "2031-01-01"):
            spec = SheetSpec(
                center_lat=-33.732,
                center_lon=150.312,
                date_iso=bad_date,
            )
            with pytest.raises(ValueError, match="outside WMM2025 valid range"):
                compute_declination(compute_sheet_layout(spec))


class TestCollarSVGs:
    @pytest.mark.parametrize("bw", [False, True])
    def test_render_declination_svg_easterly_gm(self, bw: bool, tmp_path: Path) -> None:
        spec = SheetSpec(
            center_lat=-33.732,
            center_lon=150.312,
            scale=25000,
            paper=PaperSize.A4,
            date_iso="2026-06-15",
        )
        layout = compute_sheet_layout(spec)
        info = compute_declination(layout, elevation_m=1000.0)
        svg_path = tmp_path / f"declination_{'bw' if bw else 'color'}.svg"

        returned = render_declination_svg(info, svg_path, bw=bw)
        assert returned == svg_path
        assert svg_path.exists()

        text = _extract_svg_text(svg_path)
        for token in (
            "GN",
            "TN",
            "MN",
            "NOT TO SCALE",
            "G-M ANGLE",
            "GRID CONVERGENCE",
            "MAGNETIC DECLINATION",
            "WMM2025",
            "mils",
            "Grid → Mag: SUBTRACT",
            "Mag → Grid: ADD",
        ):
            assert token in text

    def test_render_declination_svg_westerly_gm(self, tmp_path: Path) -> None:
        # Boston MA: strongly westerly magnetic declination (~ -14°W) -> Westerly G-M
        spec = SheetSpec(
            center_lat=42.3601,
            center_lon=-71.0589,
            scale=25000,
            paper=PaperSize.LETTER,
            date_iso="2026-06-15",
        )
        layout = compute_sheet_layout(spec)
        info = compute_declination(layout)
        assert info.gm_angle_deg < 0.0

        svg_path = tmp_path / "declination_west.svg"
        render_declination_svg(info, svg_path)

        text = _extract_svg_text(svg_path)
        assert "Grid → Mag: ADD" in text
        assert "Mag → Grid: SUBTRACT" in text
        assert "° W" in text

    def test_visual_prong_angles_generous_separation(self) -> None:
        # Opposite sides (Blue Mountains): TN west, MN east -> >=25° GN-MN gap
        tn_vis, mn_vis = _compute_visual_prong_angles(-1.49, 11.0)
        assert tn_vis <= -21.0
        assert mn_vis >= 25.0

        # Same side (Colorado Rockies): TN east, MN further east -> >=21° between prongs
        tn_vis_co, mn_vis_co = _compute_visual_prong_angles(0.3, 8.3)
        assert tn_vis_co >= 21.0
        assert (mn_vis_co - tn_vis_co) >= 21.0
        assert mn_vis_co <= 43.0

    def test_render_grid_ref_box_svg(self, tmp_path: Path) -> None:
        spec = SheetSpec(
            center_lat=-33.732,
            center_lon=150.312,
            scale=25000,
            paper=PaperSize.A4,
        )
        layout = compute_sheet_layout(spec)
        meta = generate_mgrs_grid(layout, tmp_path / "mgrs.gpkg")
        svg_path = tmp_path / "grid_ref_box.svg"

        returned = render_grid_ref_box_svg(meta, layout, svg_path)
        assert returned == svg_path
        assert svg_path.exists()

        text = _extract_svg_text(svg_path)
        for token in (
            "MGRS GRID REFERENCE BOX",
            "GRID ZONE DESIGNATION",
            "100,000m SQUARE ID",
            "56H",
            "KH",
            meta.center_mgrs_10m,
            "EPSG:32756",
            "1:25,000",
        ):
            assert token in text
