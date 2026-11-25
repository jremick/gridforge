"""Unit tests for gridforge.geo.mgrs_grid."""

from __future__ import annotations

from pathlib import Path

import pytest
from osgeo import ogr
from pyproj import Transformer

from gridforge.config import VALID_SCALES, Orientation, PaperSize, SheetSpec
from gridforge.geo.crs import compute_sheet_layout
from gridforge.geo.mgrs_grid import generate_mgrs_grid


class TestMGRSGrid:
    def test_blue_mountains_grid_and_gpkg(self, tmp_path: Path) -> None:
        spec = SheetSpec(
            center_lat=-33.732,
            center_lon=150.312,
            scale=25000,
            paper=PaperSize.A4,
            orientation=Orientation.PORTRAIT,
        )
        layout = compute_sheet_layout(spec)
        out_gpkg = tmp_path / "mgrs_grid.gpkg"

        meta = generate_mgrs_grid(layout, out_gpkg)

        assert out_gpkg.exists()
        assert meta.gzd == "56H"
        assert meta.square_ids_100km == ["KH"]
        assert meta.grid_spacing_m == 1000
        assert meta.grid_spacing_mm == pytest.approx(40.0)
        assert meta.crosses_100km_boundary is False
        assert meta.crosses_utm_zone_boundary is False

        # Center 10 m MGRS formatting: "56H KH EEEE NNNN"
        parts = meta.center_mgrs_10m.split()
        assert len(parts) == 4
        assert parts[0] == "56H"
        assert parts[1] == "KH"
        assert len(parts[2]) == 4 and parts[2].isdigit()
        assert len(parts[3]) == 4 and parts[3].isdigit()

        # Verify ticks on all 4 edges
        for edge in ("top", "bottom", "left", "right"):
            edge_ticks = [t for t in meta.ticks if t.edge == edge]
            assert len(edge_ticks) >= 4
            assert sum(1 for t in edge_ticks if t.is_first_on_edge) == 1
            assert edge_ticks[0].is_first_on_edge is True

            max_mm = (
                layout.frame_width_mm
                if edge in ("top", "bottom")
                else layout.frame_height_mm
            )
            for t in edge_ticks:
                assert 0.0 <= t.offset_mm <= max_mm + 1e-6
                assert len(t.principal_digits) == 2 and t.principal_digits.isdigit()
                assert t.prefix_digits.isdigit()
                suffix = "m E" if edge in ("top", "bottom") else "m N"
                assert t.full_label == f"{t.easting_or_northing_m}{suffix}"

            # Consecutive ticks must be spaced by exact grid_spacing_mm (40.0 mm)
            for t1, t2 in zip(edge_ticks, edge_ticks[1:]):
                assert (t2.offset_mm - t1.offset_mm) == pytest.approx(
                    meta.grid_spacing_mm, abs=1e-6
                )

        # Verify GeoPackage layers, SRS, and strict orthogonality
        ds = ogr.Open(str(out_gpkg))
        assert ds is not None
        layer_names = {ds.GetLayerByIndex(i).GetName() for i in range(ds.GetLayerCount())}
        assert layer_names == {"grid_1km", "grid_100km", "zone_boundary", "square_labels"}

        lyr_1km = ds.GetLayerByName("grid_1km")
        assert lyr_1km.GetSpatialRef().GetAuthorityCode(None) == "32756"
        assert lyr_1km.GetFeatureCount() >= 8
        for feat in lyr_1km:
            geom = feat.GetGeometryRef()
            p0 = geom.GetPoint(0)
            p1 = geom.GetPoint(1)
            axis = feat.GetField("axis")
            if axis == "E":
                assert p0[0] == pytest.approx(p1[0])
                assert p0[0] == pytest.approx(feat.GetField("coord_m"))
            else:
                assert axis == "N"
                assert p0[1] == pytest.approx(p1[1])
                assert p0[1] == pytest.approx(feat.GetField("coord_m"))

        lyr_100km = ds.GetLayerByName("grid_100km")
        assert lyr_100km.GetFeatureCount() == 0

        lyr_sq = ds.GetLayerByName("square_labels")
        assert lyr_sq.GetFeatureCount() == 1
        sq_feat = lyr_sq.GetNextFeature()
        assert sq_feat.GetField("gzd") == "56H"
        assert sq_feat.GetField("square_id") == "KH"
        assert sq_feat.GetField("full_id") == "56H KH"
        ds = None

    @pytest.mark.parametrize("scale", VALID_SCALES)
    def test_grid_spacing_mm_exact_for_all_scales(
        self, scale: int, tmp_path: Path
    ) -> None:
        spec = SheetSpec(
            center_lat=-33.732,
            center_lon=150.312,
            scale=scale,
            paper=PaperSize.LETTER,
            orientation=Orientation.PORTRAIT,
        )
        layout = compute_sheet_layout(spec)
        meta = generate_mgrs_grid(layout, tmp_path / f"grid_{scale}.gpkg")
        expected_spacing = 1000.0 / scale * 1000.0
        assert meta.grid_spacing_mm == pytest.approx(expected_spacing, rel=1e-9)

        top_ticks = [t for t in meta.ticks if t.edge == "top"]
        assert len(top_ticks) >= 2
        for t1, t2 in zip(top_ticks, top_ticks[1:]):
            assert (t2.offset_mm - t1.offset_mm) == pytest.approx(
                expected_spacing, abs=1e-6
            )

    def test_100km_easting_boundary_crossing(self, tmp_path: Path) -> None:
        # Place center right on Easting = 300,000 m in EPSG:32756 (KH | LH boundary).
        to_wgs84 = Transformer.from_crs("EPSG:32756", "EPSG:4326", always_xy=True)
        lon, lat = to_wgs84.transform(300000.0, 6264000.0)
        spec = SheetSpec(
            center_lat=lat,
            center_lon=lon,
            scale=25000,
            paper=PaperSize.A4,
        )
        layout = compute_sheet_layout(spec)
        out_gpkg = tmp_path / "mgrs_100km.gpkg"

        meta = generate_mgrs_grid(layout, out_gpkg)

        assert meta.crosses_100km_boundary is True
        assert len(meta.square_ids_100km) == 2
        assert set(meta.square_ids_100km) == {"KH", "LH"}

        ticks_100k = [t for t in meta.ticks if t.is_100km]
        assert len(ticks_100k) == 2  # top and bottom edges at 300000m E
        assert all(t.principal_digits == "00" for t in ticks_100k)
        assert all(t.easting_or_northing_m == 300000 for t in ticks_100k)

        ds = ogr.Open(str(out_gpkg))
        assert ds is not None
        lyr_100km = ds.GetLayerByName("grid_100km")
        assert lyr_100km.GetFeatureCount() == 1
        feat = lyr_100km.GetNextFeature()
        assert feat.GetField("axis") == "E"
        assert feat.GetField("coord_m") == 300000

        lyr_sq = ds.GetLayerByName("square_labels")
        assert lyr_sq.GetFeatureCount() == 2
        ds = None

    def test_100km_four_way_corner_crossing(self, tmp_path: Path) -> None:
        # Place center right on (300,000 E, 6,300,000 N) in EPSG:32756.
        to_wgs84 = Transformer.from_crs("EPSG:32756", "EPSG:4326", always_xy=True)
        lon, lat = to_wgs84.transform(300000.0, 6300000.0)
        spec = SheetSpec(
            center_lat=lat,
            center_lon=lon,
            scale=25000,
            paper=PaperSize.A4,
        )
        layout = compute_sheet_layout(spec)
        out_gpkg = tmp_path / "mgrs_4way.gpkg"

        meta = generate_mgrs_grid(layout, out_gpkg)

        assert meta.crosses_100km_boundary is True
        assert len(meta.square_ids_100km) == 4

        ds = ogr.Open(str(out_gpkg))
        assert ds is not None
        assert ds.GetLayerByName("grid_100km").GetFeatureCount() == 2
        assert ds.GetLayerByName("square_labels").GetFeatureCount() == 4
        ds = None

    def test_utm_zone_boundary_crossing(self, tmp_path: Path) -> None:
        # Center just east of the 150°E meridian separating Zone 55H and Zone 56H.
        spec = SheetSpec(
            center_lat=-33.732,
            center_lon=150.005,
            scale=50000,
            paper=PaperSize.A4,
        )
        layout = compute_sheet_layout(spec)
        assert layout.utm_zone == 56
        out_gpkg = tmp_path / "mgrs_zone_bnd.gpkg"

        meta = generate_mgrs_grid(layout, out_gpkg)

        assert meta.crosses_utm_zone_boundary is True

        ds = ogr.Open(str(out_gpkg))
        assert ds is not None
        lyr_zb = ds.GetLayerByName("zone_boundary")
        assert lyr_zb.GetFeatureCount() >= 1
        feat = lyr_zb.GetNextFeature()
        assert "150°E" in feat.GetField("label")
        ds = None
