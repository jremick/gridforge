"""Unit tests for Australia-wide AUElvisDEMSource (GA 5 m LiDAR WCS + DEA SRTM 1s COG)."""

from __future__ import annotations

import shutil
import struct
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
from gridforge.sources.base import ElevationSource
from gridforge.sources.dem_au_elvis import (
    AUElvisDEMSource,
    is_in_australia_coverage,
    wcs_resolution_for_scale,
)

HAS_GDAL_CLI = shutil.which("gdal_translate") is not None


def _sydney_layout(scale: int = 25000) -> SheetLayout:
    spec = SheetSpec(
        center_lat=-33.8568,
        center_lon=151.2153,
        scale=scale,
        paper=PaperSize.A4,
        orientation=Orientation.PORTRAIT,
        color_mode=ColorMode.COLOR,
        units=Units.METRIC,
    )
    margins = FrameMarginsMM()
    fw = 210.0 - margins.left - margins.right
    fh = 297.0 - margins.top - margins.bottom
    gw = fw * scale / 1000.0
    gh = fh * scale / 1000.0
    cx, cy = 334900.0, 6252200.0
    buf = max(1000.0, 0.15 * max(gw, gh))
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
        frame_width_mm=fw,
        frame_height_mm=fh,
        utm_bbox=BBox(min_x=cx - gw / 2, min_y=cy - gh / 2, max_x=cx + gw / 2, max_y=cy + gh / 2),
        utm_fetch_bbox=BBox(
            min_x=cx - gw / 2 - buf,
            min_y=cy - gh / 2 - buf,
            max_x=cx + gw / 2 + buf,
            max_y=cy + gh / 2 + buf,
        ),
        wgs84_bbox=BBox(min_x=151.190, min_y=-33.883, max_x=151.240, max_y=-33.830),
        wgs84_fetch_bbox=BBox(min_x=151.178, min_y=-33.893, max_x=151.252, max_y=-33.820),
    )


def _europe_layout() -> SheetLayout:
    spec = SheetSpec(center_lat=46.55, center_lon=7.98, scale=25000)
    margins = FrameMarginsMM()
    return SheetLayout(
        spec=spec,
        utm_epsg=32632,
        utm_zone=32,
        utm_hemisphere="N",
        gzd="32T",
        center_easting=421700.0,
        center_northing=5155700.0,
        page_width_mm=210.0,
        page_height_mm=297.0,
        margins_mm=margins,
        frame_x_mm=12.0,
        frame_y_mm=14.0,
        frame_width_mm=186.0,
        frame_height_mm=235.0,
        utm_bbox=BBox(min_x=419375.0, min_y=5152762.5, max_x=424025.0, max_y=5158637.5),
        utm_fetch_bbox=BBox(min_x=418375.0, min_y=5151762.5, max_x=425025.0, max_y=5159637.5),
        wgs84_bbox=BBox(min_x=7.95, min_y=46.52, max_x=8.01, max_y=46.58),
        wgs84_fetch_bbox=BBox(min_x=7.93, min_y=46.51, max_x=8.03, max_y=46.59),
    )


def _build_geotiff_bytes(
    tmp_path: Path,
    name: str,
    bbox: BBox,
    *,
    size: int = 32,
    zero_fraction: float = 0.0,
    nodata_fraction: float = 0.0,
    base_elev: float = 42.0,
    relief: float = 80.0,
    nodata_value: float | None = None,
) -> bytes:
    from osgeo import gdal, osr  # type: ignore[import-untyped]

    gdal.UseExceptions()
    out = tmp_path / name
    driver = gdal.GetDriverByName("GTiff")
    ds = driver.Create(str(out), size, size, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((bbox.min_x, bbox.width / size, 0.0, bbox.max_y, 0.0, -(bbox.height / size)))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4283)
    ds.SetProjection(srs.ExportToWkt())
    band = ds.GetRasterBand(1)
    if nodata_value is not None:
        band.SetNoDataValue(nodata_value)

    total = size * size
    n_zero = int(total * zero_fraction)
    n_nodata = int(total * nodata_fraction)
    vals: list[float] = []
    for idx in range(total):
        if idx < n_nodata and nodata_value is not None:
            vals.append(nodata_value)
        elif idx < n_nodata + n_zero:
            vals.append(0.0)
        else:
            vals.append(base_elev + (idx % size) / max(1, size - 1) * relief)

    band.WriteRaster(0, 0, size, size, struct.pack(f"={total}f", *vals))
    band.FlushCache()
    ds.FlushCache()
    ds = None
    return out.read_bytes()


def test_au_elvis_protocol_coverage_and_scale_resolution(tmp_path: Path) -> None:
    src = AUElvisDEMSource(cache_dir=tmp_path)
    assert isinstance(src, ElevationSource)
    assert src.name == "au-elvis"
    assert src.vertical_datum == "AHD"
    assert "Geoscience Australia" in src.attribution
    assert "CC BY 4.0" in src.attribution

    # All 8 Australian states/territories covered
    assert is_in_australia_coverage(BBox(min_x=151.18, min_y=-33.89, max_x=151.25, max_y=-33.82))  # NSW Sydney
    assert is_in_australia_coverage(BBox(min_x=144.90, min_y=-37.85, max_x=145.00, max_y=-37.78))  # VIC Melbourne
    assert is_in_australia_coverage(BBox(min_x=145.38, min_y=-16.30, max_x=145.46, max_y=-16.20))  # QLD Daintree
    assert is_in_australia_coverage(BBox(min_x=115.82, min_y=-31.98, max_x=115.90, max_y=-31.92))  # WA Perth
    assert is_in_australia_coverage(BBox(min_x=138.55, min_y=-34.96, max_x=138.65, max_y=-34.89))  # SA Adelaide
    assert is_in_australia_coverage(BBox(min_x=145.90, min_y=-41.72, max_x=146.00, max_y=-41.64))  # TAS Cradle Mtn
    assert is_in_australia_coverage(BBox(min_x=130.98, min_y=-25.38, max_x=131.08, max_y=-25.30))  # NT Uluru
    assert is_in_australia_coverage(BBox(min_x=149.08, min_y=-35.32, max_x=149.18, max_y=-35.24))  # ACT Canberra

    # Non-Australia regions excluded
    assert not is_in_australia_coverage(BBox(min_x=-105.33, min_y=39.96, max_x=-105.24, max_y=40.05))  # US Colorado
    assert not is_in_australia_coverage(BBox(min_x=7.93, min_y=46.51, max_x=8.03, max_y=46.59))        # Switzerland
    assert not is_in_australia_coverage(BBox(min_x=174.70, min_y=-36.90, max_x=174.82, max_y=-36.80))  # New Zealand

    # WCS resolution scaling protects A3 1:50k memory/bandwidth budget
    assert wcs_resolution_for_scale(15000)[2] == "5m"
    assert wcs_resolution_for_scale(25000)[2] == "5m"
    assert wcs_resolution_for_scale(50000)[2] == "10m"


def test_au_elvis_outside_australia_raises(tmp_path: Path) -> None:
    src = AUElvisDEMSource(cache_dir=tmp_path)
    with pytest.raises(RuntimeError, match="Outside Australian DEM coverage"):
        src.fetch_dem(_europe_layout(), tmp_path / "work")


@pytest.mark.skipif(not HAS_GDAL_CLI, reason="GDAL required for GeoTIFF inspection")
def test_au_elvis_5m_lidar_wcs_and_disk_cache(tmp_path: Path) -> None:
    layout = _sydney_layout(scale=25000)
    valid_5m_bytes = _build_geotiff_bytes(
        tmp_path,
        "mock_sydney_5m.tif",
        layout.wgs84_fetch_bbox,
        zero_fraction=0.0,
        base_elev=-4.0,
        relief=82.0,
    )
    calls: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.url.params))
        return httpx.Response(200, content=valid_5m_bytes, headers={"content-type": "image/tiff"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        src = AUElvisDEMSource(cache_dir=tmp_path / "cache", client=client)
        out1 = src.fetch_dem(layout, tmp_path / "run1")
        assert out1.is_file()
        assert src.resolved_tier == "5m-lidar"
        assert len(calls) == 1
        assert calls[0]["VERSION"] == "1.0.0"
        assert calls[0]["COVERAGE"] == "1"
        assert calls[0]["CRS"] == "EPSG:4283"

        # Second call hits disk clip cache -> zero additional HTTP calls
        out2 = src.fetch_dem(layout, tmp_path / "run2")
        assert out2.is_file()
        assert src.resolved_tier == "5m-lidar"
        assert len(calls) == 1


    # Simulate real coastal estuary water (25% 0.0m smoothly grading into 1..60m shoreline)
    # -> MUST be accepted as 5m-lidar (like Perth Swan River / Hobart Derwent)!
    size = 32
    coastal_vals: list[float] = []
    for r in range(size):
        for c in range(size):
            if r < 8:
                coastal_vals.append(0.0)
            else:
                # Smooth coastal ramp starting at 1.0m right at the shoreline (zero cliff ratio = 0%)
                coastal_vals.append(1.0 + (r - 8) * 3.0 + c * 0.2)

    from osgeo import gdal, osr  # type: ignore[import-untyped]

    gdal.UseExceptions()
    coastal_tif = tmp_path / "mock_perth_estuary.tif"
    ds = gdal.GetDriverByName("GTiff").Create(str(coastal_tif), size, size, 1, gdal.GDT_Float32)
    b = layout.wgs84_fetch_bbox
    ds.SetGeoTransform((b.min_x, b.width / size, 0.0, b.max_y, 0.0, -(b.height / size)))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4283)
    ds.SetProjection(srs.ExportToWkt())
    ds.GetRasterBand(1).WriteRaster(0, 0, size, size, struct.pack(f"={size * size}f", *coastal_vals))
    ds.FlushCache()
    ds = None
    coastal_bytes = coastal_tif.read_bytes()

    with httpx.Client(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, content=coastal_bytes, headers={"content-type": "image/tiff"})
        )
    ) as client_coastal:
        src_coastal = AUElvisDEMSource(cache_dir=tmp_path / "cache_coastal", client=client_coastal)
        out_coastal = src_coastal.fetch_dem(layout, tmp_path / "run_coastal")
        assert out_coastal.is_file()
        assert src_coastal.resolved_tier == "5m-lidar"


@pytest.mark.skipif(not HAS_GDAL_CLI, reason="GDAL required for GeoTIFF coverage guard tests")
@pytest.mark.parametrize(
    "case_name,zero_frac,base_elev,relief",
    [
        ("empty_all_zero_uluru", 1.0, 0.0, 0.0),
        ("partial_fringe_penrith_19pct_zero", 0.19, 120.0, 200.0),
    ],
)
def test_au_elvis_rejects_empty_or_partial_zero_5m_and_falls_back_to_srtm_1s(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case_name: str,
    zero_frac: float,
    base_elev: float,
    relief: float,
) -> None:
    layout = _sydney_layout(scale=25000)
    bad_5m_bytes = _build_geotiff_bytes(
        tmp_path,
        f"bad_5m_{case_name}.tif",
        layout.wgs84_fetch_bbox,
        zero_fraction=zero_frac,
        base_elev=base_elev,
        relief=relief,
    )
    srtm_bytes = _build_geotiff_bytes(
        tmp_path,
        f"srtm_{case_name}.tif",
        layout.wgs84_fetch_bbox,
        zero_fraction=0.0,
        base_elev=400.0,
        relief=550.0,
        nodata_value=-3.4028235e38,
    )
    gdal_cmds: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=bad_5m_bytes, headers={"content-type": "image/tiff"})

    def fake_run_subprocess(cmd: list[str], env: dict[str, str], context: str) -> None:
        gdal_cmds.append(cmd)
        assert "dea-public-data.s3.ap-southeast-2.amazonaws.com" in " ".join(cmd)
        assert "dems1sv1_0.tif" in " ".join(cmd)
        Path(cmd[-1]).write_bytes(srtm_bytes)

    monkeypatch.setattr("gridforge.sources.dem_au_elvis._run_subprocess", fake_run_subprocess)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        src = AUElvisDEMSource(cache_dir=tmp_path / f"cache_{case_name}", client=client)
        out = src.fetch_dem(layout, tmp_path / f"work_{case_name}")
        assert out.is_file()
        assert src.resolved_tier == "srtm-1s"
        assert src.vertical_datum == "AHD"
        assert len(gdal_cmds) == 1


@pytest.mark.skipif(not HAS_GDAL_CLI, reason="GDAL required for WCS backup test")
def test_au_elvis_srtm_cog_failure_uses_ga_wcs_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _sydney_layout(scale=50000)
    backup_bytes = _build_geotiff_bytes(
        tmp_path,
        "backup_srtm.tif",
        layout.wgs84_fetch_bbox,
        zero_fraction=0.0,
        base_elev=220.0,
        relief=300.0,
    )
    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        if "DEM_SRTM_1Second_Hydro_Enforced_2024" in str(request.url):
            return httpx.Response(200, content=backup_bytes, headers={"content-type": "image/tiff"})
        return httpx.Response(503, text="WCS 5m down")

    def failing_gdal_translate(cmd: list[str], env: dict[str, str], context: str) -> None:
        raise RuntimeError("Simulated S3 outage")

    monkeypatch.setattr("gridforge.sources.dem_au_elvis._run_subprocess", failing_gdal_translate)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        src = AUElvisDEMSource(cache_dir=tmp_path / "cache_bk", client=client)
        out = src.fetch_dem(layout, tmp_path / "work_bk")
        assert out.is_file()
        assert src.resolved_tier == "srtm-1s"
        assert any("DEM_SRTM_1Second_Hydro_Enforced_2024" in u for u in requested_urls)
