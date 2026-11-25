"""End-to-end integration tests for GridForge topographic PDF generation & physical scale verification.

Offline tests run fast with zero network access using synthetic local DEM + OSM fixtures.
Live tests hit Copernicus GLO-30 COG + Overpass API for the Blue Mountains NSW test site
and are gated behind `@pytest.mark.live` and `GRIDFORGE_RUN_LIVE=1`.
"""

from __future__ import annotations

import math
import os
import subprocess
import sys
from pathlib import Path

import pytest

from gridforge.config import ColorMode, Orientation, PaperSize, SheetSpec, Units
from gridforge.pipeline.runner import run_render_job
from gridforge.sources.dem_local import LocalDEMSource
from gridforge.sources.osm_pbf import LocalPBFSource

# Import sibling verification library regardless of working directory / pytest rootdir
_INTEGRATION_DIR = Path(__file__).resolve().parent
if str(_INTEGRATION_DIR) not in sys.path:
    sys.path.insert(0, str(_INTEGRATION_DIR))

from verify_pdf import (  # noqa: E402
    assert_pdf_valid,
    rasterize_pdf_for_inspection,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"

# Katoomba / Jamison Valley, Blue Mountains NSW, Australia (UTM Zone 56S / EPSG:32756 / MGRS 56H)
BLUE_MOUNTAINS_LAT = -33.732
BLUE_MOUNTAINS_LON = 150.312
BLUE_MOUNTAINS_EPSG = 32756
BLUE_MOUNTAINS_GZD = "56H"

RUN_LIVE = os.environ.get("GRIDFORGE_RUN_LIVE", "").strip().lower() in {"1", "true", "yes"}


def _build_synthetic_dem_geotiff(
    out_tif: Path,
    center_lat: float = BLUE_MOUNTAINS_LAT,
    center_lon: float = BLUE_MOUNTAINS_LON,
    half_span_deg: float = 0.08,
    size: int = 160,
) -> Path:
    """Create a realistic Float32 WGS84 GeoTIFF DEM with undulating ridges & valleys (~450-1050 m)."""
    from osgeo import gdal, osr

    gdal.UseExceptions()

    min_lon = center_lon - half_span_deg
    max_lon = center_lon + half_span_deg
    min_lat = center_lat - half_span_deg
    max_lat = center_lat + half_span_deg

    pixel_w = (max_lon - min_lon) / size
    pixel_h = (max_lat - min_lat) / size

    driver = gdal.GetDriverByName("GTiff")
    ds = driver.Create(str(out_tif), size, size, 1, gdal.GDT_Float32, options=["COMPRESS=DEFLATE"])
    ds.SetGeoTransform((min_lon, pixel_w, 0.0, max_lat, 0.0, -pixel_h))

    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())

    import array

    buf = array.array("f", [0.0]) * (size * size)
    for row in range(size):
        ny = (row / (size - 1)) * 2.0 - 1.0  # -1 (north) .. +1 (south)
        for col in range(size):
            nx = (col / (size - 1)) * 2.0 - 1.0  # -1 (west) .. +1 (east)
            # Ridge running NE-SW + escarpment + central valley + secondary peaks
            ridge = 260.0 * math.exp(-((nx - 0.25 * ny) ** 2) / 0.22)
            peak1 = 180.0 * math.exp(-((nx + 0.35) ** 2 + (ny + 0.25) ** 2) / 0.09)
            peak2 = 140.0 * math.exp(-((nx - 0.40) ** 2 + (ny - 0.30) ** 2) / 0.11)
            valley = -170.0 * math.exp(-((nx - 0.10 + 0.4 * ny) ** 2) / 0.06)
            waves = 45.0 * math.sin(nx * 5.2) * math.cos(ny * 4.7)
            elev = 680.0 + ridge + peak1 + peak2 + valley + waves
            buf[row * size + col] = float(elev)

    band = ds.GetRasterBand(1)
    band.SetNoDataValue(-9999.0)
    band.WriteRaster(0, 0, size, size, buf.tobytes(), buf_type=gdal.GDT_Float32)
    band.FlushCache()
    ds = None
    return out_tif


def _build_synthetic_osm_xml(
    out_osm: Path,
    center_lat: float = BLUE_MOUNTAINS_LAT,
    center_lon: float = BLUE_MOUNTAINS_LON,
) -> Path:
    """Create a strict ID-sorted (nodes -> ways -> relations) OSM XML covering a full A4 1:25k sheet."""
    nodes: list[tuple[int, float, float, dict[str, str]]] = []
    ways: list[tuple[int, list[int], dict[str, str]]] = []

    nid = 1
    wid = 1

    def add_node(dlat: float, dlon: float, tags: dict[str, str] | None = None) -> int:
        nonlocal nid
        cur = nid
        nid += 1
        nodes.append((cur, center_lat + dlat, center_lon + dlon, tags or {}))
        return cur

    def add_way(coords: list[tuple[float, float]], tags: dict[str, str], closed: bool = False) -> int:
        nonlocal wid
        refs = [add_node(dlat, dlon) for dlat, dlon in coords]
        if closed and refs[0] != refs[-1]:
            refs.append(refs[0])
        cur = wid
        wid += 1
        ways.append((cur, refs, tags))
        return cur

    # Named peaks and places
    add_node(0.008, -0.010, {"natural": "peak", "name": "Mount GridForge", "ele": "1012"})
    add_node(-0.011, 0.012, {"natural": "peak", "name": "Escarpment Knob", "ele": "945"})
    add_node(0.002, 0.003, {"place": "town", "name": "Katoomba Synthetic"})
    add_node(-0.015, -0.008, {"place": "locality", "name": "Jamison Valley"})

    # Forest multipolygon / closed way
    add_way(
        [(-0.030, -0.030), (-0.030, 0.005), (0.025, 0.005), (0.025, -0.030)],
        {"natural": "wood", "landuse": "forest", "name": "Synthetic State Forest"},
        closed=True,
    )
    # Lake / waterbody
    add_way(
        [(-0.006, 0.008), (-0.006, 0.018), (0.002, 0.018), (0.002, 0.008)],
        {"natural": "water", "water": "lake", "name": "Mirror Lake"},
        closed=True,
    )
    # Buildings
    for idx, (blat, blon) in enumerate(((0.003, 0.002), (0.0035, 0.0032), (0.0022, 0.0041))):
        add_way(
            [
                (blat, blon),
                (blat, blon + 0.0005),
                (blat + 0.0004, blon + 0.0005),
                (blat + 0.0004, blon),
            ],
            {"building": "yes", "name": f"Shelter {idx + 1}"},
            closed=True,
        )

    # Primary highway (E-W across sheet)
    add_way(
        [(0.005, -0.045), (0.004, -0.015), (0.003, 0.005), (0.006, 0.045)],
        {"highway": "primary", "name": "Great Western Hwy", "ref": "A32"},
    )
    # Secondary road (N-S)
    add_way(
        [(0.040, 0.002), (0.010, 0.001), (-0.010, 0.003), (-0.040, 0.000)],
        {"highway": "secondary", "name": "Cliff Drive"},
    )
    # Track and footway trail
    add_way(
        [(-0.005, -0.022), (-0.012, -0.010), (-0.018, 0.006)],
        {"highway": "track", "tracktype": "grade3", "name": "Fire Trail"},
    )
    add_way(
        [(0.012, -0.015), (0.002, -0.006), (-0.009, 0.012)],
        {"highway": "path", "name": "Ridge Walking Track"},
    )
    # Railway
    add_way(
        [(0.014, -0.045), (0.012, -0.005), (0.011, 0.045)],
        {"railway": "rail", "name": "Blue Mountains Line"},
    )
    # River & intermittent creek
    add_way(
        [(0.035, -0.012), (0.010, -0.004), (-0.006, 0.010), (-0.035, 0.016)],
        {"waterway": "river", "name": "Kedumba River"},
    )
    add_way(
        [(0.020, 0.020), (0.008, 0.014), (0.000, 0.011)],
        {"waterway": "stream", "intermittent": "yes", "name": "Dry Gully"},
    )
    # Power line
    add_way(
        [(-0.025, -0.040), (-0.020, 0.000), (-0.018, 0.040)],
        {"power": "line"},
    )

    def esc(val: str) -> str:
        return val.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")

    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<osm version="0.6" generator="gridforge-test">']
    for node_id, lat, lon, tags in nodes:
        if tags:
            lines.append(
                f'  <node id="{node_id}" visible="true" version="1" lat="{lat:.6f}" lon="{lon:.6f}">'
            )
            for k, v in tags.items():
                lines.append(f'    <tag k="{esc(k)}" v="{esc(v)}"/>')
            lines.append("  </node>")
        else:
            lines.append(
                f'  <node id="{node_id}" visible="true" version="1" lat="{lat:.6f}" lon="{lon:.6f}"/>'
            )
    for way_id, refs, tags in ways:
        lines.append(f'  <way id="{way_id}" visible="true" version="1">')
        for r in refs:
            lines.append(f'    <nd ref="{r}"/>')
        for k, v in tags.items():
            lines.append(f'    <tag k="{esc(k)}" v="{esc(v)}"/>')
        lines.append("  </way>")
    lines.append("</osm>\n")
    out_osm.write_text("\n".join(lines), encoding="utf-8")
    return out_osm


def _dem_covers_bbox(dem_path: Path, min_lon: float, min_lat: float, max_lon: float, max_lat: float) -> bool:
    """Return True if an existing GeoTIFF DEM in EPSG:4326 covers the given WGS84 bbox."""
    try:
        from osgeo import gdal

        ds = gdal.Open(str(dem_path))
        if ds is None:
            return False
        gt = ds.GetGeoTransform()
        x0 = gt[0]
        x1 = gt[0] + gt[1] * ds.RasterXSize
        y0 = gt[3] + gt[5] * ds.RasterYSize
        y1 = gt[3]
        ds = None
        dmin_x, dmax_x = min(x0, x1), max(x0, x1)
        dmin_y, dmax_y = min(y0, y1), max(y0, y1)
        return dmin_x <= min_lon and dmax_x >= max_lon and dmin_y <= min_lat and dmax_y >= max_lat
    except Exception:
        return False


@pytest.fixture(scope="module")
def offline_sheet_fixtures(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """Provide a full-sheet synthetic DEM GeoTIFF and OSM XML covering Blue Mountains (-33.732, 150.312).

    Uses tests/fixtures/* directly if a fixture there covers the full A4 1:25,000 footprint;
    otherwise generates deterministic full-sheet fixtures in a temporary directory.
    """
    existing_tifs = sorted(FIXTURES_DIR.glob("*.tif")) if FIXTURES_DIR.exists() else []
    existing_osms = (
        sorted(list(FIXTURES_DIR.glob("*.osm")) + list(FIXTURES_DIR.glob("*.osm.pbf")))
        if FIXTURES_DIR.exists()
        else []
    )
    if existing_tifs and existing_osms:
        # Check if the repo's fixture DEM covers a full A4 1:25k sheet (~0.06 deg around center)
        if _dem_covers_bbox(
            existing_tifs[0],
            BLUE_MOUNTAINS_LON - 0.05,
            BLUE_MOUNTAINS_LAT - 0.05,
            BLUE_MOUNTAINS_LON + 0.05,
            BLUE_MOUNTAINS_LAT + 0.05,
        ):
            return (existing_tifs[0], existing_osms[0])

    fixture_dir = tmp_path_factory.mktemp("gridforge_offline_fixtures")
    dem_path = _build_synthetic_dem_geotiff(fixture_dir / "synthetic_blue_mountains_dem.tif")
    osm_path = _build_synthetic_osm_xml(fixture_dir / "synthetic_blue_mountains.osm")
    return (dem_path, osm_path)


class TestOfflineFixtureRender:
    """Fast, deterministic offline E2E PDF rendering and physical-scale verification tests."""

    def test_offline_a4_25k_color_and_bw(
        self,
        offline_sheet_fixtures: tuple[Path, Path],
        tmp_path: Path,
    ) -> None:
        dem_path, osm_path = offline_sheet_fixtures

        color_pdf = tmp_path / "offline_a4_25k_color.pdf"
        bw_pdf = tmp_path / "offline_a4_25k_bw.pdf"

        color_spec = SheetSpec(
            center_lat=BLUE_MOUNTAINS_LAT,
            center_lon=BLUE_MOUNTAINS_LON,
            scale=25000,
            paper=PaperSize.A4,
            orientation=Orientation.PORTRAIT,
            color_mode=ColorMode.COLOR,
            units=Units.METRIC,
            title="OFFLINE TEST — BLUE MOUNTAINS",
            date_iso="2026-06-01",
        )
        color_res = run_render_job(
            color_spec,
            color_pdf,
            elevation_source=LocalDEMSource(dem_path),
            vector_source=LocalPBFSource(osm_path),
        )
        assert color_res.output_pdf == color_pdf
        assert color_res.utm_epsg == BLUE_MOUNTAINS_EPSG
        assert color_res.scale == 25000

        color_report = assert_pdf_valid(
            color_pdf,
            scale=25000,
            paper="a4",
            orientation="portrait",
            expected_epsg=BLUE_MOUNTAINS_EPSG,
            expected_gzd=BLUE_MOUNTAINS_GZD,
            expected_title="OFFLINE TEST",
            require_osm_attribution=True,
            require_copernicus_attribution=False,
        )
        assert color_report.max_vertical_spacing_err_mm is not None
        assert color_report.max_vertical_spacing_err_mm <= 0.15
        assert color_report.max_horizontal_spacing_err_mm is not None
        assert color_report.max_horizontal_spacing_err_mm <= 0.15

        bw_spec = color_spec.model_copy(
            update={
                "color_mode": ColorMode.BW,
                "title": "OFFLINE TEST — B&W",
            }
        )
        bw_res = run_render_job(
            bw_spec,
            bw_pdf,
            elevation_source=LocalDEMSource(dem_path),
            vector_source=LocalPBFSource(osm_path),
        )
        assert bw_res.output_pdf == bw_pdf

        assert_pdf_valid(
            bw_pdf,
            scale=25000,
            paper="a4",
            orientation="portrait",
            expected_epsg=BLUE_MOUNTAINS_EPSG,
            expected_gzd=BLUE_MOUNTAINS_GZD,
            expected_title="OFFLINE TEST",
            require_osm_attribution=True,
            require_copernicus_attribution=False,
        )

        # Ensure Color and B&W outputs are genuinely distinct renders
        assert color_pdf.read_bytes() != bw_pdf.read_bytes()

        # Verify visual rasterization helper succeeds cleanly via pdftoppm
        pngs = rasterize_pdf_for_inspection(color_pdf, tmp_path / "rasters", prefix="offline_color")
        assert set(pngs.keys()) == {"full", "center", "ticks_tl", "collar_left", "collar_right"}
        for png in pngs.values():
            assert png.is_file() and png.stat().st_size > 1_000

    def test_offline_letter_landscape_24k_imperial_cli(
        self,
        offline_sheet_fixtures: tuple[Path, Path],
        tmp_path: Path,
    ) -> None:
        """Verify CLI `gridforge inspect` and `gridforge render` on Letter Landscape at 1:24,000 (41.67 mm grid)."""
        dem_path, osm_path = offline_sheet_fixtures
        out_pdf = tmp_path / "offline_letter_24k_landscape.pdf"

        # 1. `gridforge inspect`
        inspect_proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "gridforge.cli",
                "inspect",
                "--center",
                f"{BLUE_MOUNTAINS_LAT},{BLUE_MOUNTAINS_LON}",
                "--scale",
                "24000",
                "--paper",
                "letter",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert inspect_proc.returncode == 0, f"inspect failed: {inspect_proc.stderr}\n{inspect_proc.stdout}"
        assert "32756" in inspect_proc.stdout
        assert "56H" in inspect_proc.stdout

        # 2. `gridforge render`
        render_proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "gridforge.cli",
                "render",
                "--center",
                f"{BLUE_MOUNTAINS_LAT},{BLUE_MOUNTAINS_LON}",
                "--scale",
                "24000",
                "--paper",
                "letter",
                "--orientation",
                "landscape",
                "--mode",
                "color",
                "--units",
                "imperial",
                "--title",
                "CLI LETTER LANDSCAPE 24K",
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
        assert render_proc.returncode == 0, f"render failed: {render_proc.stderr}\n{render_proc.stdout}"
        assert out_pdf.is_file()

        assert_pdf_valid(
            out_pdf,
            scale=24000,
            paper="letter",
            orientation="landscape",
            expected_epsg=BLUE_MOUNTAINS_EPSG,
            expected_gzd=BLUE_MOUNTAINS_GZD,
            expected_title="CLI LETTER LANDSCAPE 24K",
            require_osm_attribution=True,
            require_copernicus_attribution=False,
        )


@pytest.mark.live
@pytest.mark.skipif(
    not RUN_LIVE,
    reason="Live Blue Mountains network test skipped by default; set GRIDFORGE_RUN_LIVE=1 to run.",
)
class TestLiveBlueMountainsRender:
    """Live cloud-native E2E verification on Katoomba / Jamison Valley, Blue Mountains NSW."""

    def _out_dir(self, tmp_path: Path) -> Path:
        override = os.environ.get("GRIDFORGE_LIVE_OUT_DIR")
        if override:
            path = Path(override)
            path.mkdir(parents=True, exist_ok=True)
            return path
        return tmp_path

    def test_live_blue_mountains_a4_color(self, tmp_path: Path) -> None:
        out_pdf = self._out_dir(tmp_path) / "blue_mountains_a4_25k_color.pdf"
        spec = SheetSpec(
            center_lat=BLUE_MOUNTAINS_LAT,
            center_lon=BLUE_MOUNTAINS_LON,
            scale=25000,
            paper=PaperSize.A4,
            orientation=Orientation.PORTRAIT,
            color_mode=ColorMode.COLOR,
            units=Units.METRIC,
            title="BLUE MOUNTAINS — KATOOMBA",
        )
        res = run_render_job(spec, out_pdf)
        assert res.output_pdf == out_pdf
        assert res.utm_epsg == BLUE_MOUNTAINS_EPSG

        assert_pdf_valid(
            out_pdf,
            scale=25000,
            paper="a4",
            orientation="portrait",
            expected_epsg=BLUE_MOUNTAINS_EPSG,
            expected_gzd=BLUE_MOUNTAINS_GZD,
            expected_title="BLUE MOUNTAINS",
            require_osm_attribution=True,
            require_copernicus_attribution=True,
        )

    def test_live_blue_mountains_a4_bw(self, tmp_path: Path) -> None:
        out_pdf = self._out_dir(tmp_path) / "blue_mountains_a4_25k_bw.pdf"
        spec = SheetSpec(
            center_lat=BLUE_MOUNTAINS_LAT,
            center_lon=BLUE_MOUNTAINS_LON,
            scale=25000,
            paper=PaperSize.A4,
            orientation=Orientation.PORTRAIT,
            color_mode=ColorMode.BW,
            units=Units.METRIC,
            title="BLUE MOUNTAINS — KATOOMBA (B&W)",
        )
        res = run_render_job(spec, out_pdf)
        assert res.output_pdf == out_pdf
        assert res.utm_epsg == BLUE_MOUNTAINS_EPSG

        assert_pdf_valid(
            out_pdf,
            scale=25000,
            paper="a4",
            orientation="portrait",
            expected_epsg=BLUE_MOUNTAINS_EPSG,
            expected_gzd=BLUE_MOUNTAINS_GZD,
            expected_title="BLUE MOUNTAINS",
            require_osm_attribution=True,
            require_copernicus_attribution=True,
        )

    def test_live_blue_mountains_letter_color(self, tmp_path: Path) -> None:
        out_pdf = self._out_dir(tmp_path) / "blue_mountains_letter_25k_color.pdf"
        spec = SheetSpec(
            center_lat=BLUE_MOUNTAINS_LAT,
            center_lon=BLUE_MOUNTAINS_LON,
            scale=25000,
            paper=PaperSize.LETTER,
            orientation=Orientation.PORTRAIT,
            color_mode=ColorMode.COLOR,
            units=Units.IMPERIAL,
            title="BLUE MOUNTAINS — KATOOMBA",
        )
        res = run_render_job(spec, out_pdf)
        assert res.output_pdf == out_pdf
        assert res.utm_epsg == BLUE_MOUNTAINS_EPSG

        assert_pdf_valid(
            out_pdf,
            scale=25000,
            paper="letter",
            orientation="portrait",
            expected_epsg=BLUE_MOUNTAINS_EPSG,
            expected_gzd=BLUE_MOUNTAINS_GZD,
            expected_title="BLUE MOUNTAINS",
            require_osm_attribution=True,
            require_copernicus_attribution=True,
        )
