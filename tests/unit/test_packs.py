"""Unit tests for memory-safe offline Australia pack catalog and builder."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from osgeo import gdal, ogr, osr

from gridforge.config import BBox
from gridforge.packs.builder import (
    _iter_spatial_tiles,
    _stream_download_file,
    build_offline_pack,
    discover_packs,
    load_pack_manifest,
)
from gridforge.packs.catalog import (
    AUSTRALIA_REGIONS,
    compute_utm_zones_for_bbox,
    list_regions,
    resolve_region,
)

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"


def _make_mock_overlay_gpkg(path: Path, layer_name: str, field_name: str) -> Path:
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
    lyr.CreateField(ogr.FieldDefn(field_name, ogr.OFTString))
    feat = ogr.Feature(lyr.GetLayerDefn())
    pt = ogr.Geometry(ogr.wkbPoint)
    pt.AddPoint(150.312, -33.732)
    feat.SetGeometry(pt)
    feat.SetField(field_name, "Katoomba Bore")
    lyr.CreateFeature(feat)
    ds.FlushCache()
    ds = None
    return path


class TestPackCatalog:
    def test_catalog_contains_all_8_states_and_7_tactical_regions(self) -> None:
        regions = list_regions()
        assert len(regions) == 15
        states = {r.region_id for r in regions if r.category == "state"}
        tactical = {r.region_id for r in regions if r.category == "tactical"}
        assert states == {"act", "tas", "vic", "nsw", "sa", "qld", "nt", "wa"}
        assert tactical == {
            "blue-mountains",
            "sydney-basin",
            "australian-alps",
            "tasmania-wilderness",
            "red-centre",
            "perth-hills",
            "far-north-qld",
        }
        assert len(AUSTRALIA_REGIONS) == 15

    def test_resolve_region_catalog_and_custom_bbox(self) -> None:
        bm = resolve_region("BLUE_MOUNTAINS")
        assert bm.region_id == "blue-mountains"
        assert bm.utm_zones == ["56H"]

        tas = resolve_region("tas")
        assert tas.region_id == "tas"
        assert "55G" in tas.utm_zones

        custom = resolve_region("150.2, -33.8, 150.4, -33.6")
        assert custom.category == "custom"
        assert custom.bbox_wgs84.min_x == pytest.approx(150.2)
        assert custom.bbox_wgs84.max_y == pytest.approx(-33.6)
        assert custom.utm_zones == ["56H"]

        # Multi-zone Red Centre check (straddles 132°E zone 52/53 AND -24°S band J/K)
        zones_jk = compute_utm_zones_for_bbox(
            BBox(min_x=130.5, min_y=-25.5, max_x=134.0, max_y=-23.5)
        )
        assert zones_jk == ["52J", "52K", "53J", "53K"]
        zones_j = compute_utm_zones_for_bbox(
            BBox(min_x=130.5, min_y=-25.5, max_x=134.0, max_y=-24.2)
        )
        assert zones_j == ["52J", "53J"]

    @pytest.mark.parametrize(
        "bad",
        ["", "mars", "150.5,-33.8,150.2,-33.6", "150.0,-85.0,151.0,-82.0", "1,2,3"],
    )
    def test_resolve_region_rejects_invalid(self, bad: str) -> None:
        with pytest.raises(ValueError):
            resolve_region(bad)

    def test_iter_spatial_tiles_bounds_chunk_size(self) -> None:
        bbox = BBox(min_x=148.0, min_y=-37.5, max_x=150.5, max_y=-35.0)
        tiles = _iter_spatial_tiles(bbox, max_deg=1.0)
        assert len(tiles) == 9  # 3x3 grid
        for t in tiles:
            assert t.width <= 1.0 + 1e-9
            assert t.height <= 1.0 + 1e-9


class TestOfflinePackBuilder:
    def test_build_discover_and_load_offline_pack_low_ram(self, tmp_path: Path) -> None:
        dem_fixture = FIXTURES_DIR / "synthetic_dem.tif"
        osm_fixture = FIXTURES_DIR / "synthetic_osm.osm"
        assert dem_fixture.is_file() and osm_fixture.is_file()

        mock_hydro = _make_mock_overlay_gpkg(
            tmp_path / "src_hydro.gpkg", "au_hydro_points", "name"
        )
        mock_parks = _make_mock_overlay_gpkg(
            tmp_path / "src_parks.gpkg", "au_protected_areas", "name"
        )

        packs_root = tmp_path / "packs"
        out_dir = packs_root / "blue-mountains"

        manifest = build_offline_pack(
            "150.22,-33.80,150.40,-33.66",
            out_dir,
            name="Blue Mountains Test Pack",
            include_osm=True,
            include_dem=True,
            include_overlays=True,
            source_pbf=osm_fixture,
            source_dem=dem_fixture,
            local_overlay_overrides={
                "au-hydro": mock_hydro,
                "au-parks": mock_parks,
                "au-infra": tmp_path / "missing_infra.gpkg",
                "au-aviation": tmp_path / "missing_aviation.gpkg",
            },
            dem_tier="srtm-1s",
            cache_dir=tmp_path / "cache",
        )

        # Verify manifest & artifacts on disk
        assert (out_dir / "pack.json").is_file()
        assert manifest.pack_id == "blue-mountains"
        assert manifest.name == "Blue Mountains Test Pack"
        assert manifest.vertical_datum == "AHD"
        assert manifest.osm_pbf_path == "osm.osm.pbf"
        assert manifest.dem_path == "dem.tif"
        assert (out_dir / "osm.osm.pbf").is_file()
        assert (out_dir / "dem.tif").is_file()
        assert manifest.size_bytes > 0
        assert set(manifest.overlays.keys()) == {"au-hydro", "au-parks"}

        # Verify DEM is Float32 compressed GeoTIFF
        gdal.UseExceptions()
        ds = gdal.Open(str(out_dir / "dem.tif"))
        assert ds is not None
        assert ds.GetRasterBand(1).DataType == gdal.GDT_Float32
        ds = None

        # Verify round-trip load & pack discovery
        reloaded = load_pack_manifest(out_dir)
        assert reloaded == manifest
        discovered = discover_packs(packs_root)
        assert len(discovered) == 1
        assert discovered[0].pack_id == "blue-mountains"

        # Confirm true isolated peak RSS stays strictly < 300 MB, unaffected by earlier tests
        # in the shared pytest process (note: Linux fork()+execve() copies parent's ru_maxrss
        # into child signal->maxrss, whereas /proc/self/status VmHWM tracks the post-exec mm).
        isolated_dir = tmp_path / "isolated_pack"
        rss_probe = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import json, resource, sys; "
                    "from pathlib import Path; "
                    "from gridforge.packs.builder import build_offline_pack; "
                    "build_offline_pack("
                    "'150.22,-33.80,150.40,-33.66', Path(sys.argv[1]), "
                    "include_osm=True, include_dem=True, include_overlays=True, "
                    "source_pbf=Path(sys.argv[2]), source_dem=Path(sys.argv[3]), "
                    "local_overlay_overrides={'au-hydro': Path(sys.argv[4]), 'au-parks': Path(sys.argv[5]), "
                    "'au-infra': Path(sys.argv[6]), 'au-aviation': Path(sys.argv[7])}"
                    "); "
                    "vm_hwm_mb = None; "
                    "status_p = Path('/proc/self/status'); "
                    "vm_hwm_mb = next((int(l.split()[1]) / 1024.0 for l in status_p.read_text().splitlines() if l.startswith('VmHWM:')), None) if status_p.is_file() else None; "
                    "s = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss; "
                    "c = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss; "
                    "div = (1024.0 * 1024.0) if sys.platform == 'darwin' else 1024.0; "
                    "print(json.dumps({'self_mb': vm_hwm_mb if vm_hwm_mb is not None else s / div, 'child_mb': c / div}))"
                ),
                str(isolated_dir),
                str(osm_fixture),
                str(dem_fixture),
                str(mock_hydro),
                str(mock_parks),
                str(tmp_path / "missing_infra.gpkg"),
                str(tmp_path / "missing_aviation.gpkg"),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        rss_stats = json.loads(rss_probe.stdout.strip().splitlines()[-1])
        assert rss_stats["self_mb"] < 300.0
        assert rss_stats["child_mb"] < 300.0

    def test_stream_download_caches_and_reuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = {"count": 0}

        class _FakeResp:
            def raise_for_status(self) -> None:
                return None

            def iter_bytes(self, chunk_size: int = 1024):
                yield b"chunk1-"
                yield b"chunk2"

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class _FakeClient:
            def __init__(self, **kwargs):
                pass

            def stream(self, method: str, url: str):
                calls["count"] += 1
                return _FakeResp()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        monkeypatch.setattr("gridforge.packs.builder.httpx.Client", _FakeClient)

        dest = tmp_path / "cache" / "australia-latest.osm.pbf"
        out1 = _stream_download_file("https://example.test/australia-latest.osm.pbf", dest)
        out2 = _stream_download_file("https://example.test/australia-latest.osm.pbf", dest)
        assert out1 == out2 == dest
        assert dest.read_bytes() == b"chunk1-chunk2"
        assert calls["count"] == 1  # Second call hit disk cache cleanly!


@pytest.mark.parametrize(
    ("missing", "option"), [("dem_path", "--offline-dem"), ("osm_pbf_path", "--offline-pbf")]
)
def test_partial_pack_requires_missing_input_before_fetch(tmp_path, monkeypatch, missing, option):
    from gridforge.config import PackManifest, SheetSpec
    from gridforge.pipeline import runner

    manifest = PackManifest(
        pack_id="partial", name="Partial pack", region="test", utm_zones=["56H"],
        bbox_wgs84=BBox(min_x=150, min_y=-34, max_x=151, max_y=-33),
        created_at="2026-01-01", dem_path="dem.tif", osm_pbf_path="osm.pbf",
    ).model_copy(update={missing: None})
    (tmp_path / "pack.json").write_text(manifest.model_dump_json())
    (tmp_path / "dem.tif").touch()
    (tmp_path / "osm.pbf").touch()

    def unexpected(*args, **kwargs):
        pytest.fail("Pack validation must finish before selecting or fetching online data")

    monkeypatch.setattr(runner, "_resolve_elevation_source", unexpected)
    monkeypatch.setattr(runner, "OverpassOSMSource", unexpected)
    monkeypatch.setattr(runner.LocalDEMSource, "fetch_dem", unexpected)
    spec = SheetSpec(center_lat=-33.5, center_lon=150.5)
    with pytest.raises(ValueError, match=option):
        runner.prepare_job_workspace(spec, tmp_path / "job", pack_dir=tmp_path)

    # The corresponding explicit local CLI input completes a partial pack.
    dem = runner.LocalDEMSource(tmp_path / "dem.tif") if missing == "dem_path" else None
    osm = runner.LocalPBFSource(tmp_path / "osm.pbf") if missing == "osm_pbf_path" else None
    _, elev_src, _, vec_src, *_ = runner._resolve_pack_sources(
        tmp_path, spec, runner.compute_sheet_layout(spec), elevation_source=dem,
        vector_source=osm, regional_overlay_overrides=None,
    )
    assert isinstance(elev_src, runner.LocalDEMSource)
    assert isinstance(vec_src, runner.LocalPBFSource)
