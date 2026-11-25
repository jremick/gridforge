"""Unit tests for gridforge.geo.crs."""

from __future__ import annotations

import pytest
from pyproj import Transformer

from gridforge.config import (
    PAPER_DIMENSIONS_MM,
    VALID_SCALES,
    Orientation,
    PaperSize,
    SheetSpec,
)
from gridforge.geo.crs import (
    compute_sheet_layout,
    parse_center,
    resolve_utm_zone,
    utm_zone_lon_bounds,
)


class TestParseCenter:
    def test_parse_lat_lon_with_spaces(self) -> None:
        lat, lon, mgrs_str = parse_center("-33.732, 150.312")
        assert lat == pytest.approx(-33.732)
        assert lon == pytest.approx(150.312)
        assert mgrs_str is None

    def test_parse_lat_lon_compact(self) -> None:
        lat, lon, mgrs_str = parse_center("30.2675,-97.74278")
        assert lat == pytest.approx(30.2675)
        assert lon == pytest.approx(-97.74278)
        assert mgrs_str is None

    def test_parse_mgrs_compact_and_spaced_match(self) -> None:
        lat1, lon1, norm1 = parse_center("56HKH510640")
        lat2, lon2, norm2 = parse_center("56H KH 510 640")
        lat3, lon3, norm3 = parse_center("  56h  kh  510  640  ")
        assert norm1 == "56HKH510640"
        assert norm2 == "56HKH510640"
        assert norm3 == "56HKH510640"
        assert lat1 == pytest.approx(lat2) == pytest.approx(lat3)
        assert lon1 == pytest.approx(lon2) == pytest.approx(lon3)
        # Katoomba / Blue Mountains area in 56H KH
        assert -34.0 < lat1 < -33.5
        assert 150.0 < lon1 < 150.8

    def test_parse_mgrs_1m_precision(self) -> None:
        lat, lon, norm = parse_center("56HKH5100064000")
        assert norm == "56HKH5100064000"
        assert -34.0 < lat < -33.5
        assert 150.0 < lon < 150.8

    def test_parse_mgrs_cell_center_behavior(self) -> None:
        # mgrs>=1.6.0 returns grid cell center, so 1 km cell (51 64) center
        # is shifted ~500 m NE of 1 m cell (51000 64000).
        lat_1m, lon_1m, _ = parse_center("56HKH5100064000")
        lat_1km, lon_1km, _ = parse_center("56H KH 51 64")
        to_utm = Transformer.from_crs("EPSG:4326", "EPSG:32756", always_xy=True)
        e1, n1 = to_utm.transform(lon_1m, lat_1m)
        e2, n2 = to_utm.transform(lon_1km, lat_1km)
        assert (e2 - e1) == pytest.approx(499.5, abs=2.0)
        assert (n2 - n1) == pytest.approx(499.5, abs=2.0)

    @pytest.mark.parametrize(
        "bad_input",
        [
            "",
            "   ",
            "85.0, 0.0",
            "-81.0, 0.0",
            "0.0, 181.0",
            "ZAH1234567890",  # North polar UPS
            "BAN1234567890",  # South polar UPS
            "56H KH 510 64",  # Odd total digits
            "32X AB 1234 5678",  # Non-existent Svalbard zone
            "not-a-coordinate",
        ],
    )
    def test_parse_center_rejects_invalid_and_polar(self, bad_input: str) -> None:
        with pytest.raises(ValueError):
            parse_center(bad_input)


class TestResolveUTMZone:
    def test_blue_mountains_nsw(self) -> None:
        zone, hemi, epsg, gzd = resolve_utm_zone(-33.732, 150.312)
        assert (zone, hemi, epsg, gzd) == (56, "S", 32756, "56H")

    def test_austin_tx(self) -> None:
        zone, hemi, epsg, gzd = resolve_utm_zone(30.2675, -97.74278)
        assert (zone, hemi, epsg, gzd) == (14, "N", 32614, "14R")

    def test_colorado_rockies(self) -> None:
        zone, hemi, epsg, gzd = resolve_utm_zone(39.7392, -105.519)
        assert (zone, hemi, epsg, gzd) == (13, "N", 32613, "13S")

    def test_norway_32v_exception(self) -> None:
        # Bergen, Norway: lon=5.3°E would naively be zone 31, must be 32V!
        zone, hemi, epsg, gzd = resolve_utm_zone(60.4, 5.3)
        assert (zone, hemi, epsg, gzd) == (32, "N", 32632, "32V")
        # West of 3°E in band V stays 31V.
        assert resolve_utm_zone(60.4, 2.5) == (31, "N", 32631, "31V")
        assert utm_zone_lon_bounds(32, "V") == (3.0, 12.0)
        assert utm_zone_lon_bounds(31, "V") == (0.0, 3.0)

    def test_svalbard_exceptions(self) -> None:
        # Longyearbyen, Svalbard: 78.2°N, 15.6°E -> 33X (naive would be 33, test 10.0°E -> 33X too!)
        assert resolve_utm_zone(78.2, 15.6) == (33, "N", 32633, "33X")
        assert resolve_utm_zone(78.2, 10.0) == (33, "N", 32633, "33X")
        assert resolve_utm_zone(78.2, 7.5) == (31, "N", 32631, "31X")
        assert resolve_utm_zone(78.2, 22.0) == (35, "N", 32635, "35X")
        assert resolve_utm_zone(78.2, 35.0) == (37, "N", 32637, "37X")
        assert utm_zone_lon_bounds(33, "X") == (9.0, 21.0)

    def test_polar_bounds_rejected(self) -> None:
        with pytest.raises(ValueError, match="polar UPS"):
            resolve_utm_zone(85.0, 0.0)
        with pytest.raises(ValueError, match="polar UPS"):
            resolve_utm_zone(-81.0, 0.0)

    def test_longitude_extremes(self) -> None:
        assert resolve_utm_zone(0.0, -180.0) == (1, "N", 32601, "1N")
        assert resolve_utm_zone(0.0, 180.0) == (60, "N", 32660, "60N")


class TestComputeSheetLayout:
    @pytest.mark.parametrize("scale", VALID_SCALES)
    @pytest.mark.parametrize("paper", list(PaperSize))
    @pytest.mark.parametrize("orientation", list(Orientation))
    def test_exact_scale_math_all_combinations(
        self,
        scale: int,
        paper: PaperSize,
        orientation: Orientation,
    ) -> None:
        spec = SheetSpec(
            center_lat=-33.732,
            center_lon=150.312,
            scale=scale,
            paper=paper,
            orientation=orientation,
        )
        layout = compute_sheet_layout(spec)

        raw_w, raw_h = PAPER_DIMENSIONS_MM[paper]
        expected_w, expected_h = (
            (raw_h, raw_w) if orientation == Orientation.LANDSCAPE else (raw_w, raw_h)
        )
        assert layout.page_width_mm == pytest.approx(expected_w)
        assert layout.page_height_mm == pytest.approx(expected_h)

        expected_frame_w = expected_w - layout.margins_mm.left - layout.margins_mm.right
        expected_frame_h = expected_h - layout.margins_mm.top - layout.margins_mm.bottom
        assert layout.frame_width_mm == pytest.approx(expected_frame_w)
        assert layout.frame_height_mm == pytest.approx(expected_frame_h)

        # True-scale ground footprint invariant
        expected_ground_w = layout.frame_width_mm * scale / 1000.0
        expected_ground_h = layout.frame_height_mm * scale / 1000.0
        assert layout.utm_bbox.width == pytest.approx(expected_ground_w, rel=1e-9)
        assert layout.utm_bbox.height == pytest.approx(expected_ground_h, rel=1e-9)

        # Fetch buffer invariant
        expected_buf = max(1000.0, 0.15 * max(expected_ground_w, expected_ground_h))
        assert layout.utm_fetch_bbox.width == pytest.approx(
            expected_ground_w + 2.0 * expected_buf, rel=1e-9
        )
        assert layout.utm_fetch_bbox.height == pytest.approx(
            expected_ground_h + 2.0 * expected_buf, rel=1e-9
        )

    def test_wgs84_bbox_encloses_corners_and_midpoints(self) -> None:
        # High-latitude Norway sheet where UTM meridian convergence & curvature are significant.
        spec = SheetSpec(
            center_lat=60.4,
            center_lon=5.3,
            scale=50000,
            paper=PaperSize.A3,
            orientation=Orientation.LANDSCAPE,
        )
        layout = compute_sheet_layout(spec)
        assert layout.utm_epsg == 32632
        assert layout.gzd == "32V"

        to_wgs84 = Transformer.from_crs(
            f"EPSG:{layout.utm_epsg}", "EPSG:4326", always_xy=True
        )
        b = layout.utm_bbox
        mx = (b.min_x + b.max_x) / 2.0
        my = (b.min_y + b.max_y) / 2.0
        sample_pts = [
            (b.min_x, b.min_y),
            (b.max_x, b.min_y),
            (b.min_x, b.max_y),
            (b.max_x, b.max_y),
            (mx, b.min_y),
            (mx, b.max_y),
            (b.min_x, my),
            (b.max_x, my),
        ]
        for x, y in sample_pts:
            lon, lat = to_wgs84.transform(x, y)
            assert layout.wgs84_bbox.min_x <= lon + 1e-12
            assert lon <= layout.wgs84_bbox.max_x + 1e-12
            assert layout.wgs84_bbox.min_y <= lat + 1e-12
            assert lat <= layout.wgs84_bbox.max_y + 1e-12

        assert layout.wgs84_fetch_bbox.min_x < layout.wgs84_bbox.min_x
        assert layout.wgs84_fetch_bbox.max_x > layout.wgs84_bbox.max_x
        assert layout.wgs84_fetch_bbox.min_y < layout.wgs84_bbox.min_y
        assert layout.wgs84_fetch_bbox.max_y > layout.wgs84_bbox.max_y
