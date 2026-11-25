"""Unit tests for USGS 3DEP (1 m LiDAR / 10 m NAVD88 seamless) and AutoDEMSource."""

from __future__ import annotations

import shutil
import struct
from pathlib import Path
from typing import Any

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
from gridforge.sources.dem_copernicus import CopernicusGLO30Source
from gridforge.sources.dem_usgs3dep import (
    AutoDEMSource,
    USGS3DEPSource,
    is_in_us_3dep_coverage,
    select_1m_tile_urls,
)

HAS_GDAL_CLI = shutil.which("gdalwarp") is not None and shutil.which("gdal_translate") is not None


def _boulder_layout() -> SheetLayout:
    """Boulder / Flatirons, Colorado (40.005, -105.285, EPSG:32613, 13T)."""
    spec = SheetSpec(
        center_lat=40.005,
        center_lon=-105.285,
        scale=24000,
        paper=PaperSize.LETTER,
        orientation=Orientation.PORTRAIT,
        color_mode=ColorMode.COLOR,
        units=Units.IMPERIAL,
    )
    margins = FrameMarginsMM()
    fw = 215.9 - margins.left - margins.right
    fh = 279.4 - margins.top - margins.bottom
    gw = fw * 24000 / 1000.0
    gh = fh * 24000 / 1000.0
    cx, cy = 475668.0, 4428365.0
    buf = 1000.0
    return SheetLayout(
        spec=spec,
        utm_epsg=32613,
        utm_zone=13,
        utm_hemisphere="N",
        gzd="13T",
        center_easting=cx,
        center_northing=cy,
        page_width_mm=215.9,
        page_height_mm=279.4,
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
        wgs84_bbox=BBox(min_x=-105.312, min_y=39.980, max_x=-105.258, max_y=40.030),
        wgs84_fetch_bbox=BBox(min_x=-105.325, min_y=39.968, max_x=-105.245, max_y=40.042),
    )


def _blue_mountains_layout() -> SheetLayout:
    spec = SheetSpec(center_lat=-33.732, center_lon=150.312, scale=25000)
    margins = FrameMarginsMM()
    return SheetLayout(
        spec=spec,
        utm_epsg=32756,
        utm_zone=56,
        utm_hemisphere="S",
        gzd="56H",
        center_easting=250865.0,
        center_northing=6264305.0,
        page_width_mm=210.0,
        page_height_mm=297.0,
        margins_mm=margins,
        frame_x_mm=12.0,
        frame_y_mm=14.0,
        frame_width_mm=186.0,
        frame_height_mm=235.0,
        utm_bbox=BBox(min_x=248540.0, min_y=6261367.5, max_x=253190.0, max_y=6267242.5),
        utm_fetch_bbox=BBox(min_x=247540.0, min_y=6260367.5, max_x=254190.0, max_y=6268242.5),
        wgs84_bbox=BBox(min_x=150.285, min_y=-33.760, max_x=150.340, max_y=-33.704),
        wgs84_fetch_bbox=BBox(min_x=150.270, min_y=-33.772, max_x=150.355, max_y=-33.692),
    )


def _write_test_geotiff(
    out_path: Path,
    bbox: BBox,
    *,
    epsg: int = 4326,
    size: int = 24,
    fill_value: float = 1750.0,
    nodata_fraction: float = 0.0,
) -> Path:
    from osgeo import gdal, osr  # type: ignore[import-untyped]

    gdal.UseExceptions()
    driver = gdal.GetDriverByName("GTiff")
    ds = driver.Create(str(out_path), size, size, 1, gdal.GDT_Float32, options=["COMPRESS=DEFLATE"])
    ds.SetGeoTransform((bbox.min_x, bbox.width / size, 0.0, bbox.max_y, 0.0, -(bbox.height / size)))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(epsg)
    ds.SetProjection(srs.ExportToWkt())
    band = ds.GetRasterBand(1)
    band.SetNoDataValue(-9999.0)

    total = size * size
    nodata_count = int(total * nodata_fraction)
    vals = [-9999.0 if i < nodata_count else fill_value + (i % size) * 2.0 for i in range(total)]
    band.WriteRaster(0, 0, size, size, struct.pack(f"={total}f", *vals))
    band.FlushCache()
    ds.FlushCache()
    ds = None
    return out_path


def test_protocols_and_us_coverage_bounds(tmp_path: Path) -> None:
    usgs = USGS3DEPSource(cache_dir=tmp_path)
    auto = AutoDEMSource(cache_dir=tmp_path)
    assert isinstance(usgs, ElevationSource)
    assert isinstance(auto, ElevationSource)
    assert usgs.vertical_datum == "NAVD88"
    assert "3DEP" in usgs.attribution

    # US regions
    assert is_in_us_3dep_coverage(BBox(min_x=-105.33, min_y=39.96, max_x=-105.24, max_y=40.05))  # Colorado
    assert is_in_us_3dep_coverage(BBox(min_x=-149.95, min_y=61.15, max_x=-149.80, max_y=61.25))  # Alaska
    assert is_in_us_3dep_coverage(BBox(min_x=-155.60, min_y=19.40, max_x=-155.40, max_y=19.60))  # Hawaii
    assert is_in_us_3dep_coverage(BBox(min_x=-66.20, min_y=18.30, max_x=-66.00, max_y=18.50))    # Puerto Rico

    # Non-US regions
    assert not is_in_us_3dep_coverage(BBox(min_x=150.27, min_y=-33.77, max_x=150.35, max_y=-33.69))  # Australia
    assert not is_in_us_3dep_coverage(BBox(min_x=8.45, min_y=47.30, max_x=8.60, max_y=47.45))        # Switzerland


def test_select_1m_tile_urls_orders_oldest_to_newest_and_filters() -> None:
    bbox = BBox(min_x=-105.33, min_y=39.96, max_x=-105.24, max_y=40.05)
    payload: dict[str, Any] = {
        "items": [
            {
                "downloadURL": "https://prd-tnm.s3.amazonaws.com/Projects/CO_Arapaho_D23/TIFF/x47y443.tif",
                "publicationDate": "2024-09-15",
                "boundingBox": {"minX": -105.35, "minY": 39.93, "maxX": -105.23, "maxY": 40.02},
            },
            {
                "downloadURL": "https://prd-tnm.s3.amazonaws.com/Projects/CO_DRCOG_2020_B20/TIFF/x47y443.tif",
                "publicationDate": "2021-04-01",
                "boundingBox": {"minX": -105.35, "minY": 39.93, "maxX": -105.23, "maxY": 40.02},
            },
            {
                # Exact duplicate URL should be deduplicated
                "downloadURL": "https://prd-tnm.s3.amazonaws.com/Projects/CO_DRCOG_2020_B20/TIFF/x47y443.tif",
                "publicationDate": "2021-04-01",
                "boundingBox": {"minX": -105.35, "minY": 39.93, "maxX": -105.23, "maxY": 40.02},
            },
            {
                "downloadURL": "https://prd-tnm.s3.amazonaws.com/Projects/CO_Arapaho_D23/TIFF/x47y444.tif",
                "publicationDate": "2024-09-15",
                "boundingBox": {"minX": -105.35, "minY": 40.02, "maxX": -105.23, "maxY": 40.11},
            },
            {
                "downloadURL": "https://prd-tnm.s3.amazonaws.com/Projects/FAR_AWAY/TIFF/far.tif",
                "publicationDate": "2024-01-01",
                "boundingBox": {"minX": -90.0, "minY": 30.0, "maxX": -89.9, "maxY": 30.1},
            },
            {
                "downloadURL": "https://prd-tnm.s3.amazonaws.com/Projects/CO_Arapaho_D23/meta.xml",
                "publicationDate": "2024-09-15",
                "boundingBox": {"minX": -105.35, "minY": 39.93, "maxX": -105.23, "maxY": 40.02},
            },
        ]
    }
    urls = select_1m_tile_urls(payload, bbox)
    assert len(urls) == 3
    # Oldest complementary project comes first so newer LiDAR overlays on top in gdalwarp
    assert "CO_DRCOG_2020_B20/TIFF/x47y443.tif" in urls[0]
    assert "CO_Arapaho_D23/TIFF/x47y443.tif" in urls[1]
    assert "CO_Arapaho_D23/TIFF/x47y444.tif" in urls[2]


def test_usgs3dep_outside_us_raises_immediately(tmp_path: Path) -> None:
    src = USGS3DEPSource(cache_dir=tmp_path)
    with pytest.raises(RuntimeError, match="Outside USGS 3DEP coverage"):
        src.fetch_dem(_blue_mountains_layout(), tmp_path / "work")


@pytest.mark.skipif(not HAS_GDAL_CLI, reason="GDAL CLI tools required for raster clip tests")
def test_usgs3dep_1m_discovery_and_clip_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    layout = _boulder_layout()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "downloadURL": "https://prd-tnm.s3.amazonaws.com/mock_1m_boulder.tif",
                        "publicationDate": "2024-09-15",
                        "boundingBox": {"minX": -105.35, "minY": 39.95, "maxX": -105.23, "maxY": 40.06},
                    }
                ]
            },
        )

    def fake_run_subprocess(cmd: list[str], env: dict[str, str], context: str) -> None:
        out_path = Path(cmd[-1])
        assert cmd[0] == "gdalwarp"
        assert "/vsicurl/https://prd-tnm.s3.amazonaws.com/mock_1m_boulder.tif" in cmd
        _write_test_geotiff(out_path, layout.utm_fetch_bbox, epsg=layout.utm_epsg, fill_value=1820.0)

    monkeypatch.setattr("gridforge.sources.dem_usgs3dep._run_subprocess", fake_run_subprocess)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        src = USGS3DEPSource(cache_dir=tmp_path / "cache", client=client)
        out1 = src.fetch_dem(layout, tmp_path / "run1")
        assert out1.is_file()
        assert src.resolved_tier == "1m"
        assert len(calls) == 1

        # Second call hits disk clip cache -> zero additional HTTP requests
        out2 = src.fetch_dem(layout, tmp_path / "run2")
        assert out2.is_file()
        assert src.resolved_tier == "1m"
        assert len(calls) == 1


@pytest.mark.skipif(not HAS_GDAL_CLI, reason="GDAL CLI tools required for raster fallback tests")
def test_usgs3dep_incomplete_1m_falls_back_to_10m_navd88(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _boulder_layout()
    invoked_cmds: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "downloadURL": "https://prd-tnm.s3.amazonaws.com/partial_1m.tif",
                        "publicationDate": "2024-01-01",
                        "boundingBox": {"minX": -105.35, "minY": 39.95, "maxX": -105.23, "maxY": 40.06},
                    }
                ]
            },
        )

    def fake_run_subprocess(cmd: list[str], env: dict[str, str], context: str) -> None:
        invoked_cmds.append(cmd)
        out_path = Path(cmd[-1])
        if cmd[0] == "gdalwarp":
            # Simulate partial LiDAR tile (40% nodata -> below 95% coverage threshold)
            _write_test_geotiff(
                out_path,
                layout.utm_fetch_bbox,
                epsg=layout.utm_epsg,
                fill_value=1800.0,
                nodata_fraction=0.40,
            )
        else:
            # 10m seamless NAVD88 VRT fallback has full valid coverage
            assert "USGS_Seamless_DEM_13.vrt" in " ".join(cmd)
            _write_test_geotiff(out_path, layout.wgs84_fetch_bbox, epsg=4326, fill_value=1800.0)

    monkeypatch.setattr("gridforge.sources.dem_usgs3dep._run_subprocess", fake_run_subprocess)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        src = USGS3DEPSource(cache_dir=tmp_path / "cache", client=client)
        out = src.fetch_dem(layout, tmp_path / "work")
        assert out.is_file()
        assert src.resolved_tier == "10m"
        assert src.vertical_datum == "NAVD88"
        assert [c[0] for c in invoked_cmds] == ["gdalwarp", "gdal_translate"]


def test_auto_dem_source_routes_us_vs_australia_and_fallback(tmp_path: Path) -> None:
    boulder = _boulder_layout()
    nsw = _blue_mountains_layout()

    class _StubUSGS(USGS3DEPSource):
        def __init__(self, should_fail: bool = False) -> None:
            super().__init__(cache_dir=tmp_path)
            self.should_fail = should_fail
            self.called = 0

        def fetch_dem(self, layout: SheetLayout, work_dir: Path) -> Path:
            self.called += 1
            if self.should_fail:
                raise RuntimeError("Outside USGS 3DEP coverage")
            out = work_dir / "dem_raw.tif"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"mock-3dep")
            return out

    class _StubGLO30(CopernicusGLO30Source):
        def __init__(self) -> None:
            super().__init__(cache_dir=tmp_path)
            self.called = 0

        def fetch_dem(self, layout: SheetLayout, work_dir: Path) -> Path:
            self.called += 1
            out = work_dir / "dem_raw.tif"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"mock-glo30")
            return out

    # 1. US sheet -> chooses USGS 3DEP (NAVD88)
    usgs_ok = _StubUSGS(should_fail=False)
    glo30 = _StubGLO30()
    auto = AutoDEMSource(usgs_source=usgs_ok, copernicus_source=glo30)
    auto.fetch_dem(boulder, tmp_path / "us_ok")
    assert usgs_ok.called == 1
    assert glo30.called == 0
    assert auto.resolved_source_name == "usgs-3dep"
    assert auto.resolved_vertical_datum == "NAVD88"
    assert "3DEP" in auto.resolved_attribution
    assert auto.attribution == auto.resolved_attribution

    # 2. Australia sheet -> skips USGS 3DEP and uses Copernicus GLO-30 (EGM2008)
    auto.fetch_dem(nsw, tmp_path / "au_ok")
    assert usgs_ok.called == 1
    assert glo30.called == 1
    assert auto.resolved_source_name == "copernicus-glo30"
    assert auto.resolved_vertical_datum == "EGM2008"
    assert "Copernicus" in auto.resolved_attribution

    # 3. US sheet where 3DEP fails -> falls back cleanly to Copernicus GLO-30 (EGM2008)
    usgs_fail = _StubUSGS(should_fail=True)
    glo30_fb = _StubGLO30()
    auto_fb = AutoDEMSource(usgs_source=usgs_fail, copernicus_source=glo30_fb)
    auto_fb.fetch_dem(boulder, tmp_path / "us_fb")
    assert usgs_fail.called == 1
    assert glo30_fb.called == 1
    assert auto_fb.resolved_source_name == "copernicus-glo30"
    assert auto_fb.resolved_vertical_datum == "EGM2008"
