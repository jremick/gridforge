"""Unit tests for GridForge v1.4.0 packs (29 regions, offline basemap) & device exports (GPKG, OsmAnd, Field Bundle)."""

from __future__ import annotations

import io
import json
import sqlite3
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest
from osgeo import gdal, ogr, osr
from PIL import Image

from gridforge.config import (
    BBox,
    ColorMode,
    ExportFormat,
    JobWorkspace,
    MBTilesFormat,
    Orientation,
    PaperSize,
    RFLinkSpec,
    SheetSpec,
    TacticalGraphic,
    ViewshedObserver,
    WaypointCategory,
    WaypointInput,
)
from gridforge.export.bundle import build_field_gpx, export_field_bundle_zip
from gridforge.export.gpkg_tiles import export_gpkg_tiles
from gridforge.export.mbtiles import export_mbtiles
from gridforge.export.osmand import export_osmand_sqlitedb
from gridforge.geo.crs import compute_sheet_layout
from gridforge.geo.declination import (
    compute_declination,
    render_declination_svg,
    render_grid_ref_box_svg,
)
from gridforge.geo.mgrs_grid import generate_mgrs_grid
from gridforge.packs.basemap_builder import (
    build_basemap_for_region,
    build_offline_basemap_mbtiles,
)
from gridforge.packs.builder import build_offline_pack, load_pack_manifest
from gridforge.packs.regions import (
    ALL_REGIONS,
    AUSTRALIA_REGIONS,
    REGION_GROUPS,
    list_regions,
    resolve_region,
)

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"
GPX_NS = {"gpx": "http://www.topografix.com/GPX/1/1"}


def _make_stub_workspace(
    tmp_path: Path,
    *,
    color_mode: ColorMode = ColorMode.COLOR,
    title: str = "BLUE MOUNTAINS V14 EXPORT",
    waypoints: list[WaypointInput] | None = None,
    viewsheds: list[ViewshedObserver] | None = None,
    rf_links: list[RFLinkSpec] | None = None,
    tactical_graphics: list[TacticalGraphic] | None = None,
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
        waypoints=waypoints or [],
        viewsheds=viewsheds or [],
        rf_links=rf_links or [],
        tactical_graphics=tactical_graphics or [],
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
    width_px: int = 640,
    height_px: int = 800,
) -> Path:
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

    row_r = bytes((x * 255 // max(1, width_px - 1)) for x in range(width_px))
    row_g = bytes(((x // 32) % 2) * 180 + 40 for x in range(width_px))
    row_b = bytes(200 - (x * 150 // max(1, width_px - 1)) for x in range(width_px))
    chunk_h = 256
    for y0 in range(0, height_px, chunk_h):
        bh = min(chunk_h, height_px - y0)
        ds.GetRasterBand(1).WriteRaster(0, y0, width_px, bh, row_r * bh)
        ds.GetRasterBand(2).WriteRaster(0, y0, width_px, bh, row_g * bh)
        ds.GetRasterBand(3).WriteRaster(0, y0, width_px, bh, row_b * bh)

    ds.FlushCache()
    ds = None
    return out_tif


def _make_mock_overlay_gpkg(path: Path, layer_name: str, lon: float, lat: float) -> Path:
    ogr.UseExceptions()
    osr.UseExceptions()
    driver = ogr.GetDriverByName("GPKG")
    if path.exists():
        path.unlink()
    ds = driver.CreateDataSource(str(path))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    lyr = ds.CreateLayer(layer_name, srs=srs, geom_type=ogr.wkbPoint)
    lyr.CreateField(ogr.FieldDefn("name", ogr.OFTString))
    feat = ogr.Feature(lyr.GetLayerDefn())
    pt = ogr.Geometry(ogr.wkbPoint)
    pt.AddPoint(lon, lat)
    feat.SetGeometry(pt)
    feat.SetField("name", "Test Feature")
    lyr.CreateFeature(feat)
    ds.FlushCache()
    ds = None
    return path


class Test29GroupedRegions:
    def test_29_regions_and_exact_group_counts(self) -> None:
        all_items = list_regions()
        assert len(all_items) == 29
        assert len(ALL_REGIONS) == 29
        assert len(AUSTRALIA_REGIONS) == 15
        assert REGION_GROUPS == ("au_tactical", "au_state", "nz", "na")

        assert len(list_regions("au_tactical")) == 7
        assert len(list_regions("au_state")) == 8
        assert len(list_regions("nz")) == 6
        assert len(list_regions("na")) == 8

        for r in all_items:
            assert r.bbox_wgs84.width > 0.0
            assert r.bbox_wgs84.height > 0.0
            assert len(r.utm_zones) >= 1
            assert r.pbf_url.startswith("https://")

    def test_resolve_nz_us_ca_and_custom_regions(self) -> None:
        nz = resolve_region("NZ_TONGARIRO")
        assert nz.region_id == "nz-tongariro"
        assert nz.group == "nz"
        assert nz.country == "NZ"
        assert nz.default_dem_tier == "nz-linz"
        assert "60H" in nz.utm_zones

        us = resolve_region("us-colorado-front-range")
        assert us.group == "na"
        assert us.country == "US"
        assert us.default_dem_tier == "usgs3dep"
        assert "13T" in us.utm_zones or "13S" in us.utm_zones

        ca = resolve_region("ca-banff-rockies")
        assert ca.group == "na"
        assert ca.country == "CA"
        assert ca.default_dem_tier == "ca-hrdem"
        assert "11U" in ca.utm_zones

        custom_ca = resolve_region("-116.1, 51.1, -115.5, 51.5")
        assert custom_ca.group == "custom"
        assert custom_ca.country == "CA"
        assert custom_ca.default_dem_tier == "ca-hrdem"


class TestOfflineBasemapBuilder:
    @pytest.mark.parametrize(
        ("tile_fmt", "magic_prefix"),
        [("jpg", b"\xff\xd8\xff"), ("png", b"\x89PNG\r\n\x1a\n")],
    )
    def test_build_offline_basemap_mbtiles_zooms_7_to_12(
        self,
        tmp_path: Path,
        tile_fmt: str,
        magic_prefix: bytes,
    ) -> None:
        dem_fixture = FIXTURES_DIR / "synthetic_dem.tif"
        assert dem_fixture.is_file()

        bbox = BBox(min_x=150.22, min_y=-33.80, max_x=150.40, max_y=-33.66)
        out_mbtiles = tmp_path / f"basemap_{tile_fmt}.mbtiles"
        out_mbtiles.write_bytes(b"stale basemap")  # Verify atomic overwrite

        res = build_offline_basemap_mbtiles(
            bbox,
            out_mbtiles,
            source_dem=dem_fixture,
            min_zoom=7,
            max_zoom=12,
            name="Blue Mountains Basemap",
            tile_format=tile_fmt,  # type: ignore[arg-type]
        )
        assert res == out_mbtiles
        assert out_mbtiles.is_file()
        assert not list(tmp_path.glob(".gridforge-*"))

        with sqlite3.connect(str(out_mbtiles)) as conn:
            meta = dict(conn.execute("SELECT name, value FROM metadata").fetchall())
            assert meta["name"] == "Blue Mountains Basemap"
            assert meta["format"] == tile_fmt
            assert meta["type"] == "basemap"
            assert int(meta["minzoom"]) == 7
            assert int(meta["maxzoom"]) == 12
            assert meta["bounds"]
            assert meta["center"]

            zooms = {
                int(r[0])
                for r in conn.execute("SELECT DISTINCT zoom_level FROM tiles").fetchall()
            }
            assert zooms == {7, 8, 9, 10, 11, 12}

            sample = conn.execute(
                "SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles "
                "WHERE zoom_level = 12 LIMIT 1"
            ).fetchone()
            assert sample is not None
            blob = bytes(sample[3])
            assert blob.startswith(magic_prefix)
            with Image.open(io.BytesIO(blob)) as im:
                assert im.size == (256, 256)
                # Ensure hypsometric + hillshade relief actually rendered varied pixels
                extrema = im.convert("RGB").getextrema()
                assert any(hi > lo for lo, hi in extrema)

        # Verify direct compatibility with Studio's read-only MBTilesBasemapStore
        from gridforge.web.basemap import MBTilesBasemapStore

        store = MBTilesBasemapStore(packs_dir=tmp_path, explicit_path=out_mbtiles)
        info = store.get_info()
        assert info.available is True
        assert info.format == tile_fmt
        assert info.minzoom == 7
        assert info.maxzoom == 12
        xyz_y = (1 << int(sample[0])) - 1 - int(sample[2])
        fetched = store.get_tile(int(sample[0]), int(sample[1]), xyz_y)
        assert fetched is not None
        assert fetched[0].startswith(magic_prefix)

    def test_basemap_atomic_sidecar_protection_and_low_ram(self, tmp_path: Path) -> None:
        dem_fixture = FIXTURES_DIR / "synthetic_dem.tif"
        bbox = BBox(min_x=150.22, min_y=-33.80, max_x=150.40, max_y=-33.66)
        out = tmp_path / "protected.mbtiles"
        out.write_bytes(b"original basemap")
        wal = tmp_path / "protected.mbtiles-wal"
        wal.write_bytes(b"active wal")

        with pytest.raises(FileExistsError, match=wal.name):
            build_offline_basemap_mbtiles(bbox, out, source_dem=dem_fixture, min_zoom=8, max_zoom=10)
        assert out.read_bytes() == b"original basemap"
        assert wal.read_bytes() == b"active wal"
        assert not list(tmp_path.glob(".gridforge-*"))

        # Confirm isolated subprocess peak RSS stays strictly < 300 MB
        iso_out = tmp_path / "rss_basemap.mbtiles"
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import json, resource, sys; from pathlib import Path; "
                    "from gridforge.packs.basemap_builder import build_basemap_for_region; "
                    "build_basemap_for_region('150.22,-33.80,150.40,-33.66', Path(sys.argv[1]), "
                    "source_dem=Path(sys.argv[2]), min_zoom=7, max_zoom=12); "
                    "status_p = Path('/proc/self/status'); "
                    "vm_hwm_mb = next((int(l.split()[1]) / 1024.0 for l in status_p.read_text().splitlines() "
                    "if l.startswith('VmHWM:')), None) if status_p.is_file() else None; "
                    "s = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss; "
                    "c = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss; "
                    "div = (1024.0 * 1024.0) if sys.platform == 'darwin' else 1024.0; "
                    "print(json.dumps({'self_mb': vm_hwm_mb if vm_hwm_mb is not None else s / div, 'child_mb': c / div}))"
                ),
                str(iso_out),
                str(dem_fixture),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        stats = json.loads(probe.stdout.strip().splitlines()[-1])
        assert iso_out.is_file()
        assert stats["self_mb"] < 300.0
        assert stats["child_mb"] < 300.0

    def test_build_pack_bundles_basemap_and_nz_na_overlays_and_optional_disable(
        self, tmp_path: Path
    ) -> None:
        dem_fixture = FIXTURES_DIR / "synthetic_dem.tif"
        osm_fixture = FIXTURES_DIR / "synthetic_osm.osm"
        mock_nz = _make_mock_overlay_gpkg(tmp_path / "nz.gpkg", "au_state_trails", 150.312, -33.732)
        mock_us = _make_mock_overlay_gpkg(tmp_path / "us.gpkg", "public_lands", 150.312, -33.732)
        mock_ca = _make_mock_overlay_gpkg(tmp_path / "ca.gpkg", "au_state_pois", 150.312, -33.732)

        pack_with_bm = tmp_path / "pack_with_bm"
        manifest = build_offline_pack(
            "150.22,-33.80,150.40,-33.66",
            pack_with_bm,
            include_osm=True,
            include_dem=True,
            include_overlays=True,
            include_basemap=True,
            basemap_min_zoom=8,
            basemap_max_zoom=11,
            source_pbf=osm_fixture,
            source_dem=dem_fixture,
            local_overlay_overrides={
                "nz-doc-trails": mock_nz,
                "us-public-lands": mock_us,
                "ca-canvec-trails": mock_ca,
            },
        )
        assert manifest.basemap_path == "basemap.mbtiles"
        assert (pack_with_bm / "basemap.mbtiles").is_file()
        assert set(manifest.overlays.keys()) == {
            "nz-doc-trails",
            "us-public-lands",
            "ca-canvec-trails",
        }
        assert load_pack_manifest(pack_with_bm) == manifest

        # Opting out of basemap omits basemap.mbtiles cleanly
        pack_no_bm = tmp_path / "pack_no_bm"
        manifest_no_bm = build_offline_pack(
            "150.22,-33.80,150.40,-33.66",
            pack_no_bm,
            include_osm=True,
            include_dem=True,
            include_overlays=False,
            include_basemap=False,
            source_pbf=osm_fixture,
            source_dem=dem_fixture,
        )
        assert manifest_no_bm.basemap_path is None
        assert not (pack_no_bm / "basemap.mbtiles").exists()

        # Partial OSM-only pack skips basemap gracefully without touching network
        pack_osm_only = tmp_path / "pack_osm_only"
        manifest_osm_only = build_offline_pack(
            "150.22,-33.80,150.40,-33.66",
            pack_osm_only,
            include_osm=True,
            include_dem=False,
            include_overlays=False,
            include_basemap=True,
            source_pbf=osm_fixture,
        )
        assert manifest_osm_only.dem_path is None
        assert manifest_osm_only.basemap_path is None


class TestGpkgTilesExport:
    @pytest.mark.parametrize(
        ("tile_fmt", "color_mode", "magic_prefix"),
        [
            (MBTilesFormat.PNG, ColorMode.NIGHT_RED, b"\x89PNG\r\n\x1a\n"),
            (MBTilesFormat.JPG, ColorMode.ALPINE, b"\xff\xd8\xff"),
        ],
    )
    def test_export_gpkg_tiles_valid_ogc_pyramid_and_atomic_overwrite(
        self,
        tmp_path: Path,
        tile_fmt: MBTilesFormat,
        color_mode: ColorMode,
        magic_prefix: bytes,
    ) -> None:
        workspace = _make_stub_workspace(tmp_path, color_mode=color_mode)
        src_tif = _create_synthetic_utm_geotiff(tmp_path / "utm.tif", workspace, 900, 1100)
        out_gpkg = tmp_path / f"tiles_{tile_fmt.value}.gpkg"
        out_gpkg.write_bytes(b"stale gpkg")

        res = export_gpkg_tiles(
            workspace,
            out_gpkg,
            tile_format=tile_fmt,
            collarless_tif=src_tif,
        )

        assert res.format == ExportFormat.GPKG
        assert res.output_path == out_gpkg
        assert out_gpkg.is_file()
        assert res.file_size_bytes == out_gpkg.stat().st_size
        assert res.tile_count > 0
        assert res.min_zoom is not None and res.max_zoom is not None
        assert res.min_zoom < res.max_zoom
        assert res.max_tile_px == (256, 256)
        assert not list(tmp_path.glob(".gridforge-*"))

        with sqlite3.connect(str(out_gpkg)) as conn:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            for req in ("gpkg_contents", "gpkg_tile_matrix_set", "gpkg_tile_matrix", "gridforge_tiles"):
                assert req in tables

            contents = conn.execute(
                "SELECT table_name, data_type, srs_id FROM gpkg_contents WHERE table_name='gridforge_tiles'"
            ).fetchone()
            assert contents == ("gridforge_tiles", "tiles", 3857)

            matrix_zooms = {
                int(r[0])
                for r in conn.execute(
                    "SELECT zoom_level FROM gpkg_tile_matrix WHERE table_name='gridforge_tiles'"
                ).fetchall()
            }
            assert res.min_zoom in matrix_zooms and res.max_zoom in matrix_zooms

            sample_blob = bytes(
                conn.execute("SELECT tile_data FROM gridforge_tiles LIMIT 1").fetchone()[0]
            )
            assert sample_blob.startswith(magic_prefix)
            with Image.open(io.BytesIO(sample_blob)) as im:
                assert im.size == (256, 256)

        # GDAL must open the GeoPackage cleanly as a Web Mercator raster dataset
        gdal.UseExceptions()
        ds = gdal.Open(str(out_gpkg))
        assert ds is not None
        srs = osr.SpatialReference(wkt=ds.GetProjection())
        assert srs.GetAuthorityCode(None) == "3857"
        ds = None

    def test_gpkg_tiles_preserves_sidecars_and_prior_file_on_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from gridforge.export import gpkg_tiles

        workspace = _make_stub_workspace(tmp_path)
        src_tif = _create_synthetic_utm_geotiff(tmp_path / "utm.tif", workspace, 128, 128)
        out_gpkg = tmp_path / "existing.gpkg"
        out_gpkg.write_bytes(b"previous gpkg")
        journal = tmp_path / "existing.gpkg-journal"
        journal.write_bytes(b"previous journal")

        with pytest.raises(FileExistsError, match=journal.name):
            export_gpkg_tiles(workspace, out_gpkg, collarless_tif=src_tif)
        assert out_gpkg.read_bytes() == b"previous gpkg"
        assert journal.read_bytes() == b"previous journal"

        journal.unlink()
        monkeypatch.setattr(
            gpkg_tiles,
            "_compute_overview_factors",
            lambda *a, **kw: (_ for _ in ()).throw(OSError("injected gpkg failure")),
        )
        with pytest.raises(OSError, match="injected gpkg failure"):
            export_gpkg_tiles(workspace, out_gpkg, collarless_tif=src_tif)
        assert out_gpkg.read_bytes() == b"previous gpkg"
        assert not list(tmp_path.glob(".gridforge-*"))


class TestOsmAndSQLiteDBExport:
    @pytest.mark.parametrize(
        ("tile_fmt", "magic_prefix"),
        [
            (MBTilesFormat.PNG, b"\x89PNG\r\n\x1a\n"),
            (MBTilesFormat.JPG, b"\xff\xd8\xff"),
        ],
    )
    def test_export_osmand_bigplanet_schema_and_exact_tile_mapping(
        self,
        tmp_path: Path,
        tile_fmt: MBTilesFormat,
        magic_prefix: bytes,
    ) -> None:
        workspace = _make_stub_workspace(tmp_path)
        src_tif = _create_synthetic_utm_geotiff(tmp_path / "utm.tif", workspace, 900, 1100)
        ref_mbtiles = tmp_path / f"ref_{tile_fmt.value}.mbtiles"
        out_sqlitedb = tmp_path / f"map_{tile_fmt.value}.sqlitedb"
        out_sqlitedb.write_bytes(b"stale osmand")

        mb_res = export_mbtiles(workspace, ref_mbtiles, tile_format=tile_fmt, collarless_tif=src_tif)
        os_res = export_osmand_sqlitedb(
            workspace, out_sqlitedb, tile_format=tile_fmt, collarless_tif=src_tif
        )

        assert os_res.format == ExportFormat.OSMAND
        assert os_res.output_path == out_sqlitedb
        assert out_sqlitedb.is_file()
        assert os_res.tile_count == mb_res.tile_count
        assert os_res.min_zoom == mb_res.min_zoom
        assert os_res.max_zoom == mb_res.max_zoom
        assert os_res.min_zoom is not None and os_res.max_zoom is not None
        assert os_res.min_zoom < os_res.max_zoom
        assert os_res.max_tile_px == (256, 256)
        assert not list(tmp_path.glob(".gridforge-*"))

        with (
            sqlite3.connect(str(ref_mbtiles)) as mb_conn,
            sqlite3.connect(str(out_sqlitedb)) as os_conn,
        ):
            info = os_conn.execute(
                "SELECT minzoom, maxzoom, tilenumbering, name FROM info"
            ).fetchone()
            assert info is not None
            assert info[0] == 17 - os_res.max_zoom
            assert info[1] == 17 - os_res.min_zoom
            assert info[2] == "BigPlanet"
            assert info[3] == "BLUE MOUNTAINS V14 EXPORT"

            mb_tiles = {
                (int(col), (1 << int(z)) - 1 - int(row), 17 - int(z), 0): bytes(blob)
                for z, col, row, blob in mb_conn.execute(
                    "SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles"
                ).fetchall()
            }
            os_tiles = {
                (int(x), int(y), int(z), int(s)): bytes(img)
                for x, y, z, s, img in os_conn.execute(
                    "SELECT x, y, z, s, image FROM tiles"
                ).fetchall()
            }
            assert set(os_tiles.keys()) == set(mb_tiles.keys())
            for key, img_bytes in os_tiles.items():
                assert img_bytes.startswith(magic_prefix)
                assert img_bytes == mb_tiles[key]

    def test_osmand_preserves_sidecars_and_prior_file_on_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from gridforge.export import osmand

        workspace = _make_stub_workspace(tmp_path)
        src_tif = _create_synthetic_utm_geotiff(tmp_path / "utm.tif", workspace, 128, 128)
        out = tmp_path / "existing.sqlitedb"
        out.write_bytes(b"previous osmand")
        wal = tmp_path / "existing.sqlitedb-wal"
        wal.write_bytes(b"previous wal")

        with pytest.raises(FileExistsError, match=wal.name):
            export_osmand_sqlitedb(workspace, out, collarless_tif=src_tif)
        assert out.read_bytes() == b"previous osmand"
        assert wal.read_bytes() == b"previous wal"

        wal.unlink()
        monkeypatch.setattr(
            osmand,
            "_write_osmand_from_mbtiles",
            lambda *a, **kw: (_ for _ in ()).throw(OSError("injected osmand failure")),
        )
        with pytest.raises(OSError, match="injected osmand failure"):
            export_osmand_sqlitedb(workspace, out, collarless_tif=src_tif)
        assert out.read_bytes() == b"previous osmand"
        assert not list(tmp_path.glob(".gridforge-*"))


class TestFieldBundleZipExport:
    def test_export_field_bundle_zip_with_gpx_11_and_readme(self, tmp_path: Path) -> None:
        workspace = _make_stub_workspace(
            tmp_path,
            title="KATOOMBA FIELD OPS",
            waypoints=[
                WaypointInput(
                    label="Base Camp",
                    lat=-33.730,
                    lon=150.310,
                    category=WaypointCategory.CAMP,
                    notes="Sheltered water & flat tent pads",
                ),
                WaypointInput(
                    label="LZ Alpha",
                    lat=-33.725,
                    lon=150.318,
                    category=WaypointCategory.LZ,
                ),
            ],
            viewsheds=[
                ViewshedObserver(
                    label="Narrow Neck Relay",
                    lat=-33.740,
                    lon=150.305,
                    observer_height_m=10.0,
                    target_height_m=1.5,
                    max_distance_m=8000.0,
                )
            ],
            rf_links=[
                RFLinkSpec(
                    from_lat=-33.740,
                    from_lon=150.305,
                    to_lat=-33.715,
                    to_lon=150.320,
                    from_height_m=10.0,
                    to_height_m=3.0,
                    freq_mhz=915.0,
                    from_label="Relay A",
                    to_label="CP Bravo",
                )
            ],
            tactical_graphics=[
                TacticalGraphic(
                    kind="range_ring",
                    label="CP Rings",
                    points=[(-33.732, 150.312)],
                    radii_km=[1.0, 3.0, 5.0],
                    notes="Comms perimeter",
                ),
                TacticalGraphic(
                    kind="bearing_line",
                    label="Ingress Azimuth",
                    points=[(-33.740, 150.305), (-33.725, 150.318)],
                ),
                TacticalGraphic(
                    kind="sector",
                    label="Search Sector 1",
                    points=[
                        (-33.738, 150.308),
                        (-33.728, 150.308),
                        (-33.728, 150.318),
                    ],
                ),
            ],
        )

        # Create representative artifacts
        pdf_art = tmp_path / "katoomba_25k.pdf"
        pdf_art.write_bytes(b"%PDF-1.7\n%mock pdf\n")
        mb_art = tmp_path / "katoomba_25k.mbtiles"
        mb_art.write_bytes(b"SQLite format 3\x00mock mbtiles")
        gpkg_art = tmp_path / "katoomba_25k.gpkg"
        gpkg_art.write_bytes(b"SQLite format 3\x00mock gpkg")
        os_art = tmp_path / "katoomba_25k.sqlitedb"
        os_art.write_bytes(b"SQLite format 3\x00mock osmand")
        kmz_art = tmp_path / "katoomba_25k.kmz"
        kmz_art.write_bytes(b"PK\x03\x04mock kmz")

        out_zip = tmp_path / "katoomba_field_bundle.zip"
        out_zip.write_bytes(b"stale bundle")

        res = export_field_bundle_zip(
            workspace,
            out_zip,
            artifacts=[pdf_art, mb_art, gpkg_art, os_art, kmz_art],
            extra_notes=["Verify printed scale with 100 mm calibration ruler."],
        )

        assert res.format == ExportFormat.BUNDLE
        assert res.output_path == out_zip
        assert out_zip.is_file()
        assert res.file_size_bytes == out_zip.stat().st_size
        assert res.tile_count == 7  # 5 artifacts + waypoints.gpx + README_FIELD_BUNDLE.txt
        assert not list(tmp_path.glob(".gridforge-*"))

        with zipfile.ZipFile(out_zip, "r") as zf:
            names = set(zf.namelist())
            assert names == {
                "katoomba_25k.pdf",
                "katoomba_25k.mbtiles",
                "katoomba_25k.gpkg",
                "katoomba_25k.sqlitedb",
                "katoomba_25k.kmz",
                "waypoints.gpx",
                "README_FIELD_BUNDLE.txt",
            }
            assert zf.read("katoomba_25k.pdf") == pdf_art.read_bytes()

            gpx_xml = zf.read("waypoints.gpx").decode("utf-8")
            root = ET.fromstring(gpx_xml)
            assert root.tag == "{http://www.topografix.com/GPX/1/1}gpx"
            assert root.attrib["version"] == "1.1"
            assert root.find("gpx:metadata/gpx:bounds", GPX_NS) is not None

            wpt_names = [
                el.findtext("gpx:name", default="", namespaces=GPX_NS)
                for el in root.findall("gpx:wpt", GPX_NS)
            ]
            assert "Base Camp" in wpt_names
            assert "LZ Alpha" in wpt_names
            assert "Narrow Neck Relay" in wpt_names
            assert "Relay A" in wpt_names
            assert "CP Bravo" in wpt_names
            assert "CP Rings" in wpt_names

            rtes = root.findall("gpx:rte", GPX_NS)
            assert len(rtes) == 3  # RF link + bearing_line + closed sector
            sector_rte = next(
                r for r in rtes if r.findtext("gpx:name", default="", namespaces=GPX_NS) == "Search Sector 1"
            )
            sector_pts = sector_rte.findall("gpx:rtept", GPX_NS)
            assert len(sector_pts) == 4  # Closed polygon ring
            assert sector_pts[0].attrib == sector_pts[-1].attrib

            readme = zf.read("README_FIELD_BUNDLE.txt").decode("utf-8")
            assert "KATOOMBA FIELD OPS" in readme
            assert "1:25,000" in readme
            assert "katoomba_25k.gpkg" in readme
            assert "katoomba_25k.sqlitedb" in readme
            assert "waypoints.gpx" in readme
            assert "Verify printed scale" in readme

    def test_empty_markup_gpx_has_sheet_center_and_missing_artifact_fails_cleanly(
        self, tmp_path: Path
    ) -> None:
        workspace = _make_stub_workspace(tmp_path, title="BARE SHEET")
        root = ET.fromstring(build_field_gpx(workspace))
        wpts = root.findall("gpx:wpt", GPX_NS)
        assert len(wpts) == 1
        assert wpts[0].findtext("gpx:type", namespaces=GPX_NS) == "sheet-center"

        out_zip = tmp_path / "missing_art_bundle.zip"
        out_zip.write_bytes(b"original zip")
        with pytest.raises(FileNotFoundError):
            export_field_bundle_zip(workspace, out_zip, artifacts=[tmp_path / "does_not_exist.pdf"])
        assert out_zip.read_bytes() == b"original zip"
        assert not list(tmp_path.glob(".gridforge-*"))
