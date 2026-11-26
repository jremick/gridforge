"""Unit tests for Phase 3 offline device exports (collarless GeoTIFF, ATAK MBTiles, Garmin KMZ)."""

from __future__ import annotations

import io
import sqlite3
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest
from osgeo import gdal, osr
from PIL import Image

from gridforge.config import (
    ColorMode,
    ExportFormat,
    GarminProfile,
    JobWorkspace,
    MBTilesFormat,
    Orientation,
    PaperSize,
    SheetSpec,
)
from gridforge.export.kmz import (
    compute_optimal_tile_grid,
    export_garmin_kmz,
    is_baseline_jpeg,
)
from gridforge.export.mbtiles import export_mbtiles
from gridforge.export.raster import (
    compute_collarless_dimensions,
    render_collarless_geotiff,
)
from gridforge.geo.crs import compute_sheet_layout
from gridforge.geo.declination import (
    compute_declination,
    render_declination_svg,
    render_grid_ref_box_svg,
)
from gridforge.geo.mgrs_grid import generate_mgrs_grid

KML_NS = {"kml": "http://www.opengis.net/kml/2.2"}
FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"


def _make_stub_workspace(
    tmp_path: Path,
    *,
    color_mode: ColorMode = ColorMode.COLOR,
    title: str = "BLUE MOUNTAINS EXPORT TEST",
) -> JobWorkspace:
    spec = SheetSpec(
        center_lat=-33.732,
        center_lon=150.312,
        scale=25000,
        paper=PaperSize.A4,
        orientation=Orientation.PORTRAIT,
        color_mode=color_mode,
        title=title,
        date_iso="2026-06-15",
    )
    layout = compute_sheet_layout(spec)
    job_dir = tmp_path / "job"
    job_dir.mkdir(parents=True, exist_ok=True)

    mgrs_gpkg = job_dir / "mgrs_grid.gpkg"
    mgrs_meta = generate_mgrs_grid(layout, mgrs_gpkg)
    declination = compute_declination(layout, elevation_m=1000.0)
    decl_svg = render_declination_svg(declination, job_dir / "declination.svg")
    grid_svg = render_grid_ref_box_svg(mgrs_meta, layout, job_dir / "grid_ref_box.svg")

    return JobWorkspace(
        job_dir=job_dir,
        layout=layout,
        declination=declination,
        mgrs_meta=mgrs_meta,
        osm_gpkg=job_dir / "osm.gpkg",
        terrain_gpkg=job_dir / "terrain.gpkg",
        hillshade_tif=job_dir / "hillshade.tif",
        dem_utm_tif=job_dir / "dem_utm.tif",
        mgrs_gpkg=mgrs_gpkg,
        declination_svg=decl_svg,
        grid_ref_svg=grid_svg,
        contour_interval_display="10 m (index 50 m)",
        attributions=[
            "Map data © OpenStreetMap contributors (ODbL) — openstreetmap.org/copyright",
            "Copernicus WorldDEM-30",
        ],
    )


def _create_synthetic_utm_geotiff(
    out_tif: Path,
    workspace: JobWorkspace,
    width_px: int,
    height_px: int,
) -> Path:
    """Create a fast, colorful synthetic RGB GeoTIFF covering workspace.layout.utm_bbox."""
    gdal.UseExceptions()
    osr.UseExceptions()

    driver = gdal.GetDriverByName("GTiff")
    ds = driver.Create(
        str(out_tif),
        width_px,
        height_px,
        3,
        gdal.GDT_Byte,
        options=["COMPRESS=LZW", "TILED=YES", "PHOTOMETRIC=RGB"],
    )
    bbox = workspace.layout.utm_bbox
    ds.SetGeoTransform(
        (
            bbox.min_x,
            bbox.width / width_px,
            0.0,
            bbox.max_y,
            0.0,
            -(bbox.height / height_px),
        )
    )
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(workspace.layout.utm_epsg)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    ds.SetProjection(srs.ExportToWkt())

    # Build a crisp gradient + grid pattern row-by-row (fast even for large rasters).
    row_r = bytes((x * 255 // max(1, width_px - 1)) for x in range(width_px))
    row_g = bytes(((x // 32) % 2) * 180 + 40 for x in range(width_px))
    row_b = bytes(200 - (x * 150 // max(1, width_px - 1)) for x in range(width_px))
    # Write tiled horizontal stripes in 256-row chunks so huge rasters stay fast & tiny RAM.
    chunk_h = 256
    for y0 in range(0, height_px, chunk_h):
        bh = min(chunk_h, height_px - y0)
        ds.GetRasterBand(1).WriteRaster(0, y0, width_px, bh, row_r * bh)
        ds.GetRasterBand(2).WriteRaster(0, y0, width_px, bh, row_g * bh)
        ds.GetRasterBand(3).WriteRaster(0, y0, width_px, bh, row_b * bh)

    ds.FlushCache()
    ds = None
    return out_tif


class TestRasterMathAndHelpers:
    def test_compute_collarless_dimensions(self) -> None:
        # A4 portrait frame: 186 mm x 235 mm at 300 DPI -> 2197 x 2776 px
        w, h = compute_collarless_dimensions(186.0, 235.0, 300)
        assert w == round(186.0 / 25.4 * 300)
        assert h == round(235.0 / 25.4 * 300)

        # Extreme DPI caps cleanly while preserving aspect ratio
        cw, ch = compute_collarless_dimensions(200.0, 100.0, 10000, max_dim_px=4096)
        assert cw == 4096
        assert ch == 2048

    def test_is_baseline_jpeg_distinguishes_baseline_vs_progressive(self) -> None:
        img = Image.new("RGB", (64, 64), (120, 160, 80))

        base_buf = io.BytesIO()
        img.save(base_buf, format="JPEG", quality=85, progressive=False)
        assert is_baseline_jpeg(base_buf.getvalue()) is True

        prog_buf = io.BytesIO()
        img.save(prog_buf, format="JPEG", quality=85, progressive=True)
        assert is_baseline_jpeg(prog_buf.getvalue()) is False

    def test_compute_optimal_tile_grid_respects_caps(self) -> None:
        # Already fits within 100 tiles: no downscaling
        tw, th, cols, rows = compute_optimal_tile_grid(
            2200, 2800, max_tile_px=1024, tile_cap=100
        )
        assert (tw, th, cols, rows) == (2200, 2800, 3, 3)

        # Huge 12000 x 12000 raster with 100-tile cap -> <= 100 tiles, max 1024 px each
        tw2, th2, cols2, rows2 = compute_optimal_tile_grid(
            12000, 12000, max_tile_px=1024, tile_cap=100
        )
        assert cols2 * rows2 <= 100
        assert tw2 <= cols2 * 1024
        assert th2 <= rows2 * 1024
        assert tw2 == 10240 and th2 == 10240

        # Tight 4-tile cap on portrait 2400 x 3200 -> 2x2 grid
        tw3, th3, cols3, rows3 = compute_optimal_tile_grid(
            2400, 3200, max_tile_px=1024, tile_cap=4
        )
        assert cols3 * rows3 <= 4
        assert (cols3, rows3) == (2, 2)
        assert tw3 <= 2048 and th3 <= 2048


class TestMBTilesExport:
    @pytest.mark.parametrize(
        ("tile_fmt", "expected_meta_fmt", "magic_prefix"),
        [
            (MBTilesFormat.PNG, "png", b"\x89PNG\r\n\x1a\n"),
            (MBTilesFormat.JPG, "jpg", b"\xff\xd8\xff"),
        ],
    )
    def test_export_mbtiles_from_synthetic_utm_geotiff(
        self,
        tmp_path: Path,
        tile_fmt: MBTilesFormat,
        expected_meta_fmt: str,
        magic_prefix: bytes,
    ) -> None:
        workspace = _make_stub_workspace(tmp_path)
        src_tif = _create_synthetic_utm_geotiff(
            tmp_path / "synthetic_utm.tif", workspace, width_px=900, height_px=1100
        )
        out_mbtiles = tmp_path / f"out_{expected_meta_fmt}.mbtiles"

        out_mbtiles.write_bytes(b"previous archive")
        res = export_mbtiles(
            workspace,
            out_mbtiles,
            tile_format=tile_fmt,
            collarless_tif=src_tif,
        )

        assert res.format == ExportFormat.MBTILES
        assert res.output_path == out_mbtiles
        assert out_mbtiles.is_file()
        assert res.file_size_bytes == out_mbtiles.stat().st_size
        assert res.tile_count > 0
        assert res.min_zoom is not None and res.max_zoom is not None
        assert res.min_zoom < res.max_zoom  # Multi-zoom overview pyramid generated!
        assert res.max_tile_px == (256, 256)
        assert res.max_tile_bytes is not None and res.max_tile_bytes > 0

        with sqlite3.connect(str(out_mbtiles)) as conn:
            meta = dict(conn.execute("SELECT name, value FROM metadata").fetchall())
            for req_key in (
                "name",
                "format",
                "bounds",
                "center",
                "minzoom",
                "maxzoom",
                "type",
                "description",
                "attribution",
            ):
                assert req_key in meta and meta[req_key]

            assert meta["format"] == expected_meta_fmt
            assert meta["type"] == "basemap"
            assert int(meta["minzoom"]) == res.min_zoom
            assert int(meta["maxzoom"]) == res.max_zoom
            assert "OpenStreetMap" in meta["attribution"]

            # Verify TMS coordinate ranges and actual image bytes decode cleanly via Pillow.
            rows = conn.execute(
                "SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles"
            ).fetchall()
            assert len(rows) == res.tile_count
            zooms_seen = set()
            for z, col, row, blob in rows:
                zooms_seen.add(z)
                n_tiles = 1 << z
                assert 0 <= col < n_tiles
                assert 0 <= row < n_tiles
                assert blob.startswith(magic_prefix)
            assert len(zooms_seen) >= 2

            # Decode a sample tile at maxzoom
            sample_blob = rows[-1][3]
            with Image.open(io.BytesIO(sample_blob)) as im:
                assert im.size == (256, 256)


class TestGarminKMZExport:
    def _assert_valid_kmz(
        self,
        kmz_path: Path,
        *,
        max_tiles: int,
        max_tile_px: int = 1024,
        max_tile_bytes: int = 3_000_000,
    ) -> list[tuple[float, float, float, float]]:
        assert kmz_path.is_file()
        boxes: list[tuple[float, float, float, float]] = []

        with zipfile.ZipFile(kmz_path, "r") as zf:
            names = set(zf.namelist())
            assert "doc.kml" in names

            root = ET.fromstring(zf.read("doc.kml"))
            assert root.tag == "{http://www.opengis.net/kml/2.2}kml"
            overlays = root.findall(".//kml:GroundOverlay", KML_NS)
            assert 1 <= len(overlays) <= max_tiles

            for ov in overlays:
                draw_order = int(ov.findtext("kml:drawOrder", default="0", namespaces=KML_NS))
                assert 50 <= draw_order <= 75

                href = ov.findtext("kml:Icon/kml:href", default="", namespaces=KML_NS)
                assert href in names
                tile_bytes = zf.read(href)
                assert 0 < len(tile_bytes) <= max_tile_bytes
                assert is_baseline_jpeg(tile_bytes) is True

                with Image.open(io.BytesIO(tile_bytes)) as im:
                    assert im.mode == "RGB"
                    assert im.width <= max_tile_px
                    assert im.height <= max_tile_px

                llbox = ov.find("kml:LatLonBox", KML_NS)
                assert llbox is not None
                assert llbox.find("kml:rotation", KML_NS) is None
                north = float(llbox.findtext("kml:north", namespaces=KML_NS) or "nan")
                south = float(llbox.findtext("kml:south", namespaces=KML_NS) or "nan")
                east = float(llbox.findtext("kml:east", namespaces=KML_NS) or "nan")
                west = float(llbox.findtext("kml:west", namespaces=KML_NS) or "nan")
                assert -90.0 <= south < north <= 90.0
                assert -180.0 <= west < east <= 180.0
                boxes.append((north, south, east, west))

        return boxes

    def test_export_kmz_standard_and_zero_gap_tiling(self, tmp_path: Path) -> None:
        workspace = _make_stub_workspace(tmp_path)
        src_tif = _create_synthetic_utm_geotiff(
            tmp_path / "normal_utm.tif", workspace, width_px=1500, height_px=1800
        )
        out_kmz = tmp_path / "standard.kmz"

        out_kmz.write_bytes(b"previous archive")
        res = export_garmin_kmz(
            workspace,
            out_kmz,
            profile=GarminProfile.STANDARD,
            collarless_tif=src_tif,
        )

        assert res.format == ExportFormat.KMZ
        assert 1 <= res.tile_count <= 100
        assert res.max_tile_px is not None
        assert res.max_tile_px[0] <= 1024 and res.max_tile_px[1] <= 1024
        assert res.max_tile_bytes is not None and res.max_tile_bytes <= 3_000_000

        boxes = self._assert_valid_kmz(out_kmz, max_tiles=100)
        assert len(boxes) == res.tile_count

        # Verify contiguous tiling with zero gaps: unique edges form exact grid
        norths = sorted({round(b[0], 7) for b in boxes}, reverse=True)
        souths = sorted({round(b[1], 7) for b in boxes}, reverse=True)
        wests = sorted({round(b[3], 7) for b in boxes})
        easts = sorted({round(b[2], 7) for b in boxes})
        # Every interior south edge matches the next row's north edge exactly
        assert souths[:-1] == norths[1:]
        # Every interior east edge matches the next col's west edge exactly
        assert easts[:-1] == wests[1:]

    def test_export_kmz_tight_tile_cap_auto_downscales(self, tmp_path: Path) -> None:
        workspace = _make_stub_workspace(tmp_path, color_mode=ColorMode.BW)
        # 3200 x 4000 would naively require 4x4 = 16 tiles at 1024 px; cap strictly to 4!
        src_tif = _create_synthetic_utm_geotiff(
            tmp_path / "large_utm.tif", workspace, width_px=3200, height_px=4000
        )
        out_kmz = tmp_path / "capped_4.kmz"

        res = export_garmin_kmz(
            workspace,
            out_kmz,
            max_tiles=4,
            collarless_tif=src_tif,
        )

        assert res.tile_count <= 4
        assert res.max_tile_px is not None
        assert res.max_tile_px[0] <= 1024 and res.max_tile_px[1] <= 1024
        self._assert_valid_kmz(out_kmz, max_tiles=4)

    def test_export_kmz_huge_raster_enforces_100_tile_standard_cap(
        self, tmp_path: Path
    ) -> None:
        workspace = _make_stub_workspace(tmp_path)
        # 11500 x 11500 raster (> 12x12 = 144 naive tiles) must auto-downscale to <= 100 tiles!
        src_tif = _create_synthetic_utm_geotiff(
            tmp_path / "huge_utm.tif", workspace, width_px=11500, height_px=11500
        )
        out_kmz = tmp_path / "huge_capped_100.kmz"

        res = export_garmin_kmz(
            workspace,
            out_kmz,
            profile=GarminProfile.STANDARD,
            collarless_tif=src_tif,
        )

        assert res.tile_count <= 100
        assert res.max_tile_px is not None
        assert res.max_tile_px[0] <= 1024 and res.max_tile_px[1] <= 1024
        self._assert_valid_kmz(out_kmz, max_tiles=100)


class TestCollarlessPyQGISPipelineExport:
    def test_offline_fixture_collarless_render_to_mbtiles_and_kmz(
        self, tmp_path: Path
    ) -> None:
        """End-to-end collarless PyQGIS render -> MBTiles & KMZ on offline Blue Mountains fixture."""
        from gridforge.pipeline.normalize import normalize_osm_to_gpkg
        from gridforge.pipeline.terrain import build_terrain_products
        from gridforge.sources.dem_local import LocalDEMSource
        from gridforge.sources.osm_pbf import LocalPBFSource

        dem_fixture = FIXTURES_DIR / "synthetic_dem.tif"
        osm_fixture = FIXTURES_DIR / "synthetic_osm.osm"
        assert dem_fixture.is_file() and osm_fixture.is_file()

        spec = SheetSpec(
            center_lat=-33.732,
            center_lon=150.312,
            scale=25000,
            paper=PaperSize.A4,
            orientation=Orientation.PORTRAIT,
            color_mode=ColorMode.COLOR,
            title="OFFLINE EXPORT FIXTURE",
            date_iso="2026-06-15",
        )
        layout = compute_sheet_layout(spec)
        job_dir = tmp_path / "fixture_job"
        job_dir.mkdir(parents=True, exist_ok=True)

        dem_raw = LocalDEMSource(dem_fixture).fetch_dem(layout, job_dir)
        osm_raw = LocalPBFSource(osm_fixture).fetch_osm(layout, job_dir)
        osm_gpkg = normalize_osm_to_gpkg(osm_raw, layout, job_dir / "osm.gpkg")
        terrain = build_terrain_products(dem_raw, layout, job_dir)
        mgrs_gpkg = job_dir / "mgrs_grid.gpkg"
        mgrs_meta = generate_mgrs_grid(layout, mgrs_gpkg)
        declination = compute_declination(layout, elevation_m=terrain.center_elev_m)
        decl_svg = render_declination_svg(declination, job_dir / "declination.svg")
        grid_svg = render_grid_ref_box_svg(mgrs_meta, layout, job_dir / "grid_ref_box.svg")

        workspace = JobWorkspace(
            job_dir=job_dir,
            layout=layout,
            declination=declination,
            mgrs_meta=mgrs_meta,
            osm_gpkg=osm_gpkg,
            terrain_gpkg=terrain.terrain_gpkg,
            hillshade_tif=terrain.hillshade_tif,
            dem_utm_tif=terrain.dem_utm_tif,
            mgrs_gpkg=mgrs_gpkg,
            declination_svg=decl_svg,
            grid_ref_svg=grid_svg,
            contour_interval_display=terrain.contour_interval_display,
            attributions=[
                "Map data © OpenStreetMap contributors (ODbL) — openstreetmap.org/copyright",
                "Synthetic Fixture DEM",
            ],
        )

        # Render collarless GeoTIFF with grid OFF at 150 DPI (fast unit test)
        tif_no_grid = render_collarless_geotiff(
            workspace,
            tmp_path / "collarless_nogrid.tif",
            dpi=150,
            include_grid=False,
        )
        assert tif_no_grid.is_file()

        gdal.UseExceptions()
        ds = gdal.Open(str(tif_no_grid))
        assert ds is not None
        assert ds.RasterCount == 3
        exp_w, exp_h = compute_collarless_dimensions(
            layout.frame_width_mm, layout.frame_height_mm, 150
        )
        assert (ds.RasterXSize, ds.RasterYSize) == (exp_w, exp_h)
        srs = osr.SpatialReference(wkt=ds.GetProjection())
        assert srs.GetAuthorityCode(None) == "32756"
        ds = None

        # Verify grid ON produces a distinct image from grid OFF
        tif_with_grid = render_collarless_geotiff(
            workspace,
            tmp_path / "collarless_grid.tif",
            dpi=150,
            include_grid=True,
        )
        assert tif_with_grid.is_file()
        assert tif_with_grid.read_bytes() != tif_no_grid.read_bytes()

        # Export both MBTiles (PNG) and Garmin KMZ from the shared collarless GeoTIFF
        mbtiles_res = export_mbtiles(
            workspace,
            tmp_path / "fixture.mbtiles",
            tile_format=MBTilesFormat.PNG,
            collarless_tif=tif_with_grid,
        )
        assert mbtiles_res.tile_count > 0
        assert mbtiles_res.min_zoom is not None and mbtiles_res.max_zoom is not None
        assert mbtiles_res.min_zoom < mbtiles_res.max_zoom

        kmz_res = export_garmin_kmz(
            workspace,
            tmp_path / "fixture.kmz",
            profile=GarminProfile.STANDARD,
            collarless_tif=tif_with_grid,
        )
        assert 1 <= kmz_res.tile_count <= 100
        assert kmz_res.max_tile_px is not None
        assert kmz_res.max_tile_px[0] <= 1024 and kmz_res.max_tile_px[1] <= 1024


@pytest.mark.parametrize("existing", [True, False])
@pytest.mark.parametrize("extension", ["kmz", "mbtiles"])
def test_failed_export_does_not_publish_partial_archive(tmp_path, monkeypatch, extension, existing):
    from gridforge.export import kmz, mbtiles

    workspace = _make_stub_workspace(tmp_path)
    src = _create_synthetic_utm_geotiff(tmp_path / "source.tif", workspace, 128, 128)
    out_dir = tmp_path / "output"
    out_dir.mkdir()
    out = out_dir / f"map.{extension}"
    previous = b"previous contents"
    if existing:
        out.write_bytes(previous)

    def fail(*args, **kwargs):
        assert (out.read_bytes() if out.exists() else None) == (previous if existing else None)
        raise OSError("injected late export failure")

    if extension == "kmz":
        # JPEG entries have already been written when KML generation starts.
        monkeypatch.setattr(kmz, "_build_kml", fail)
        export = export_garmin_kmz
    else:
        # Tile generation completed before the overview pyramid is requested.
        monkeypatch.setattr(mbtiles, "_compute_overview_factors", fail)
        export = export_mbtiles

    with pytest.raises(OSError, match="injected late export failure"):
        export(workspace, out, collarless_tif=src)
    assert list(out_dir.iterdir()) == ([out] if existing else [])
    if existing:
        assert out.read_bytes() == previous


def test_mbtiles_preserves_database_with_sidecars(tmp_path):
    workspace = _make_stub_workspace(tmp_path)
    src = _create_synthetic_utm_geotiff(tmp_path / "source.tif", workspace, 128, 128)
    out = tmp_path / "existing.mbtiles"
    out.write_bytes(b"previous archive")
    journal = tmp_path / "existing.mbtiles-journal"
    journal.write_bytes(b"previous journal")

    with pytest.raises(FileExistsError, match=journal.name):
        export_mbtiles(workspace, out, collarless_tif=src)
    assert out.read_bytes() == b"previous archive"
    assert journal.read_bytes() == b"previous journal"
    assert not list(tmp_path.glob(".gridforge-*"))


@pytest.mark.parametrize("extension", ["kmz", "mbtiles"])
def test_scratch_cleanup_failure_precedes_publication(tmp_path, monkeypatch, extension):
    import tempfile

    workspace = _make_stub_workspace(tmp_path)
    src = _create_synthetic_utm_geotiff(tmp_path / "source.tif", workspace, 128, 128)
    out = tmp_path / f"existing.{extension}"
    out.write_bytes(b"previous archive")
    cleanup = tempfile.TemporaryDirectory.cleanup

    def fail_scratch_cleanup(directory):
        cleanup(directory)
        if Path(directory.name).name.startswith(f"gridforge_{extension}_"):
            raise PermissionError("injected scratch cleanup failure")

    monkeypatch.setattr(tempfile.TemporaryDirectory, "cleanup", fail_scratch_cleanup)
    export = export_garmin_kmz if extension == "kmz" else export_mbtiles
    with pytest.raises(PermissionError, match="injected scratch cleanup failure"):
        export(workspace, out, collarless_tif=src)
    assert out.read_bytes() == b"previous archive"
