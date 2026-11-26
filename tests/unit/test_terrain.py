"""Unit tests for GDAL terrain processing: DEM warp, multi-directional hillshade, and contours."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from gridforge.config import (
    BBox,
    CartoOptions,
    ColorMode,
    DensityLevel,
    FrameMarginsMM,
    HillshadeStrength,
    Orientation,
    PaperSize,
    SheetLayout,
    SheetSpec,
    Units,
)
from gridforge.pipeline.terrain import (
    build_terrain_products,
    choose_contour_interval,
    choose_warp_resolution_and_z,
)

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"
HAS_GDAL_TERRAIN = all(
    shutil.which(cmd) is not None for cmd in ("gdalwarp", "gdaldem", "gdal_contour")
)


def _blue_mountains_layout(units: Units = Units.METRIC, scale: int = 25000) -> SheetLayout:
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


def test_choose_contour_interval_metric_and_imperial() -> None:
    # Metric defaults
    assert choose_contour_interval(15000, Units.METRIC, 500.0) == (10.0, 5, "10 m (index 50 m)")
    assert choose_contour_interval(24000, Units.METRIC, 600.0) == (10.0, 5, "10 m (index 50 m)")
    assert choose_contour_interval(25000, Units.METRIC, 700.0) == (10.0, 5, "10 m (index 50 m)")
    assert choose_contour_interval(25000, Units.METRIC, 1500.0) == (20.0, 5, "20 m (index 100 m)")
    assert choose_contour_interval(48000, Units.METRIC, 600.0) == (20.0, 5, "20 m (index 100 m)")
    assert choose_contour_interval(50000, Units.METRIC, 600.0) == (20.0, 5, "20 m (index 100 m)")

    # Imperial defaults
    assert choose_contour_interval(15000, Units.IMPERIAL, 500.0) == (20.0, 5, "20 ft (index 100 ft)")
    assert choose_contour_interval(24000, Units.IMPERIAL, 600.0) == (40.0, 5, "40 ft (index 200 ft)")
    assert choose_contour_interval(25000, Units.IMPERIAL, 600.0) == (40.0, 5, "40 ft (index 200 ft)")
    assert choose_contour_interval(48000, Units.IMPERIAL, 600.0) == (80.0, 5, "80 ft (index 400 ft)")
    assert choose_contour_interval(50000, Units.IMPERIAL, 600.0) == (80.0, 5, "80 ft (index 400 ft)")


def test_build_terrain_products_missing_dem_raises(tmp_path: Path) -> None:
    layout = _blue_mountains_layout()
    with pytest.raises(FileNotFoundError):
        build_terrain_products(tmp_path / "missing.tif", layout, tmp_path / "work")


@pytest.mark.skipif(not HAS_GDAL_TERRAIN, reason="GDAL CLI tools required for terrain pipeline tests")
def test_build_terrain_products_metric(tmp_path: Path) -> None:
    from osgeo import gdal, osr  # type: ignore[import-untyped]

    layout = _blue_mountains_layout(units=Units.METRIC, scale=25000)
    dem_raw = FIXTURES_DIR / "synthetic_dem.tif"
    result = build_terrain_products(dem_raw, layout, tmp_path / "metric_work")

    assert result.dem_utm_tif.is_file()
    assert result.hillshade_tif.is_file()
    assert result.terrain_gpkg.is_file()
    assert result.contour_interval == 10.0
    assert result.index_interval == 50.0
    assert result.contour_interval_display == "10 m (index 50 m)"
    assert 300.0 < result.min_elev < result.center_elev_m < result.max_elev < 1100.0

    # Verify dem_utm.tif projection and pixel size
    dem_ds = gdal.Open(str(result.dem_utm_tif))
    assert dem_ds is not None
    srs = osr.SpatialReference(wkt=dem_ds.GetProjection())
    srs.AutoIdentifyEPSG()
    assert srs.GetAuthorityCode(None) == "32756"
    gt = dem_ds.GetGeoTransform()
    assert abs(gt[1] - 10.0) < 0.05
    assert abs(abs(gt[5]) - 10.0) < 0.05
    dem_ds = None

    # Verify hillshade.tif is single-band Byte with real multi-directional shading contrast
    hs_ds = gdal.Open(str(result.hillshade_tif))
    assert hs_ds is not None
    assert hs_ds.RasterCount == 1
    hs_band = hs_ds.GetRasterBand(1)
    assert hs_band.DataType == gdal.GDT_Byte
    hs_min, hs_max, _hs_mean, hs_std = hs_band.ComputeStatistics(False)
    assert hs_min < hs_max
    assert hs_std > 3.0
    hs_ds = None

    # Verify terrain.gpkg contours table, is_index tagging, and integer elev_label
    with sqlite3.connect(str(result.terrain_gpkg)) as conn:
        srs_id = conn.execute(
            "SELECT srs_id FROM gpkg_contents WHERE table_name = 'contours'"
        ).fetchone()[0]
        assert srs_id == 32756

        rows = conn.execute("SELECT elev, is_index, elev_label FROM contours").fetchall()
        assert len(rows) > 20
        index_rows = [r for r in rows if r[1] == 1]
        minor_rows = [r for r in rows if r[1] == 0]
        assert len(index_rows) > 0
        assert len(minor_rows) > 0

        for elev, is_index, elev_label in rows:
            assert abs(elev - round(elev / 10.0) * 10.0) < 1e-3
            expected_index = 1 if abs(elev - round(elev / 50.0) * 50.0) < 1e-3 else 0
            assert is_index == expected_index
            assert elev_label == str(int(round(elev)))


@pytest.mark.skipif(not HAS_GDAL_TERRAIN, reason="GDAL CLI tools required for terrain pipeline tests")
def test_build_terrain_products_imperial(tmp_path: Path) -> None:
    layout = _blue_mountains_layout(units=Units.IMPERIAL, scale=25000)
    dem_raw = FIXTURES_DIR / "synthetic_dem.tif"
    result = build_terrain_products(dem_raw, layout, tmp_path / "imperial_work")

    assert result.contour_interval == 40.0
    assert result.index_interval == 200.0
    assert result.contour_interval_display == "40 ft (index 200 ft)"
    # Center elevation remains in meters for WMM accuracy!
    assert 400.0 < result.center_elev_m < 1000.0

    with sqlite3.connect(str(result.terrain_gpkg)) as conn:
        rows = conn.execute("SELECT elev, is_index, elev_label FROM contours").fetchall()
        assert len(rows) > 10
        # Synthetic Katoomba terrain (~340–960 m) spans ~1,100–3,150 ft
        elevs = [r[0] for r in rows]
        assert max(elevs) > 2000.0
        assert any(r[1] == 1 for r in rows)
        for elev, is_index, elev_label in rows:
            assert abs(elev - round(elev / 40.0) * 40.0) < 1e-3
            expected_index = 1 if abs(elev - round(elev / 200.0) * 200.0) < 1e-3 else 0
            assert is_index == expected_index
            assert elev_label == str(int(round(elev)))


@pytest.mark.skipif(not HAS_GDAL_TERRAIN, reason="GDAL CLI tools required for terrain pipeline tests")
def test_build_terrain_products_50k_metric(tmp_path: Path) -> None:
    layout = _blue_mountains_layout(units=Units.METRIC, scale=50000)
    dem_raw = FIXTURES_DIR / "synthetic_dem.tif"
    result = build_terrain_products(dem_raw, layout, tmp_path / "metric_50k_work")

    assert result.contour_interval == 20.0
    assert result.index_interval == 100.0
    assert result.contour_interval_display == "20 m (index 100 m)"

    with sqlite3.connect(str(result.terrain_gpkg)) as conn:
        rows = conn.execute("SELECT elev, is_index FROM contours").fetchall()
        assert len(rows) > 10
        for elev, is_index in rows:
            assert abs(elev - round(elev / 20.0) * 20.0) < 1e-3
            expected_index = 1 if abs(elev - round(elev / 100.0) * 100.0) < 1e-3 else 0
            assert is_index == expected_index


def test_contour_density_and_lidar_resolution_selection() -> None:
    # Contour density scaling (LOW = 2x, HIGH = 0.5x clamped at 5m / 10ft)
    assert choose_contour_interval(25000, Units.METRIC, 600.0, DensityLevel.LOW) == (
        20.0,
        5,
        "20 m (index 100 m)",
    )
    assert choose_contour_interval(25000, Units.METRIC, 600.0, DensityLevel.HIGH) == (
        5.0,
        5,
        "5 m (index 25 m)",
    )
    assert choose_contour_interval(15000, Units.METRIC, 400.0, DensityLevel.HIGH) == (
        5.0,
        5,
        "5 m (index 25 m)",
    )
    assert choose_contour_interval(24000, Units.IMPERIAL, 600.0, DensityLevel.LOW) == (
        80.0,
        5,
        "80 ft (index 400 ft)",
    )
    assert choose_contour_interval(24000, Units.IMPERIAL, 600.0, DensityLevel.HIGH) == (
        20.0,
        5,
        "20 ft (index 100 ft)",
    )
    assert choose_contour_interval(15000, Units.IMPERIAL, 400.0, DensityLevel.HIGH) == (
        10.0,
        5,
        "10 ft (index 50 ft)",
    )

    # LiDAR resolution & vertical exaggeration selection
    assert choose_warp_resolution_and_z(1.0) == (2.0, 1.0)
    assert choose_warp_resolution_and_z(2.0) == (2.0, 1.0)
    assert choose_warp_resolution_and_z(3.0) == (3.0, 1.0)
    assert choose_warp_resolution_and_z(5.0) == (5.0, 1.15)
    assert choose_warp_resolution_and_z(30.0) == (10.0, 1.15)

    # Fine tactical scales (1:10k, 1:12.5k, 1:15k) preserve true 1.0 m LiDAR with safe memory pixel cap
    assert choose_warp_resolution_and_z(1.0, scale=10000, max_extent_m=2400.0) == (1.0, 1.0)
    assert choose_warp_resolution_and_z(1.0, scale=12500, max_extent_m=3200.0) == (1.0, 1.0)
    res_a3_15k, z_a3 = choose_warp_resolution_and_z(1.0, scale=15000, max_extent_m=6200.0)
    assert z_a3 == 1.0
    assert 6200.0 / res_a3_15k <= 4096.0

    # Fine contour intervals (2 m / 5 ft on high-res DEMs, clamped to 5 m / 10 ft on 30 m SRTM)
    assert choose_contour_interval(10000, Units.METRIC, 300.0, DensityLevel.NORMAL, native_res_m=1.0) == (
        5.0,
        5,
        "5 m (index 25 m)",
    )
    assert choose_contour_interval(10000, Units.METRIC, 300.0, DensityLevel.HIGH, native_res_m=1.0) == (
        2.0,
        5,
        "2 m (index 10 m)",
    )
    assert choose_contour_interval(12500, Units.METRIC, 300.0, DensityLevel.HIGH, native_res_m=30.0) == (
        5.0,
        5,
        "5 m (index 25 m)",
    )
    assert choose_contour_interval(10000, Units.IMPERIAL, 300.0, DensityLevel.NORMAL, native_res_m=1.0) == (
        10.0,
        5,
        "10 ft (index 50 ft)",
    )
    assert choose_contour_interval(10000, Units.IMPERIAL, 300.0, DensityLevel.HIGH, native_res_m=1.0) == (
        5.0,
        5,
        "5 ft (index 25 ft)",
    )
    assert choose_contour_interval(10000, Units.IMPERIAL, 300.0, DensityLevel.HIGH, native_res_m=30.0) == (
        10.0,
        5,
        "10 ft (index 50 ft)",
    )


@pytest.mark.skipif(not HAS_GDAL_TERRAIN, reason="GDAL CLI tools required for terrain pipeline tests")
def test_build_terrain_products_high_density_and_hillshade_off(tmp_path: Path) -> None:
    from osgeo import gdal  # type: ignore[import-untyped]

    base = _blue_mountains_layout(units=Units.METRIC, scale=25000)
    spec = base.spec.model_copy(
        update={
            "carto": CartoOptions(
                contour_density=DensityLevel.HIGH,
                hillshade_strength=HillshadeStrength.OFF,
            )
        }
    )
    layout = base.model_copy(update={"spec": spec})
    result = build_terrain_products(FIXTURES_DIR / "synthetic_dem.tif", layout, tmp_path / "high_off")

    assert result.contour_interval == 5.0
    assert result.index_interval == 25.0
    assert result.contour_interval_display == "5 m (index 25 m)"

    # Neutral flat-255 hillshade produced when OFF
    hs_ds = gdal.Open(str(result.hillshade_tif))
    assert hs_ds is not None
    hs_min, hs_max, _, _ = hs_ds.GetRasterBand(1).ComputeStatistics(False)
    assert hs_min == 255.0 and hs_max == 255.0
    hs_ds = None

    with sqlite3.connect(str(result.terrain_gpkg)) as conn:
        rows = conn.execute("SELECT elev, is_index FROM contours").fetchall()
        assert len(rows) > 40
        for elev, is_index in rows:
            assert abs(elev - round(elev / 5.0) * 5.0) < 1e-3
            expected_index = 1 if abs(elev - round(elev / 25.0) * 25.0) < 1e-3 else 0
            assert is_index == expected_index

