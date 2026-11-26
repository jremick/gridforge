"""Unit tests for GridForge v1.3.0 Atlas Index Cover, MBTiles Basemap, Pack Manager, and Viewshed API."""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from osgeo import gdal, osr
from pypdf import PdfReader, PdfWriter

from gridforge.config import (
    BBox,
    ColorMode,
    DemSourceKind,
    NZOverlayKind,
    Orientation,
    PackManifest,
    PaperSize,
    RenderResult,
)
from gridforge.geo.strip_atlas import StripSheetPlan
from gridforge.geo.supplements import AtlasIndexSheet, render_atlas_index_pdf
from gridforge.web import db
from gridforge.web.app import create_app
from gridforge.web.schemas import JobStatus
from gridforge.web.worker import process_single_job

_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0"
    b"\x00\x00\x03\x01\x01\x00\x18\xdd\x8d\xb0\x00\x00\x00\x00IEND\xaeB`\x82"
)
_TINY_JPG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"


def _create_synthetic_mbtiles(
    path: Path,
    *,
    fmt: str = "png",
    tiles: list[tuple[int, int, int, bytes]] | None = None,
    bounds: str = "112.0,-44.0,154.0,-10.0",
) -> Path:
    """Create a valid MBTiles SQLite archive storing TMS-row tiles."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
        conn.execute(
            "CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB)"
        )
        conn.executemany(
            "INSERT INTO metadata (name, value) VALUES (?, ?)",
            [
                ("name", "Australia Offline Overview"),
                ("format", fmt),
                ("minzoom", "0"),
                ("maxzoom", "6"),
                ("bounds", bounds),
                ("attribution", "Offline Test Tiles"),
            ],
        )
        rows = tiles if tiles is not None else [(4, 14, 6, _TINY_PNG)]
        conn.executemany(
            "INSERT INTO tiles (zoom_level, tile_column, tile_row, tile_data) VALUES (?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    return path


def _create_synthetic_dem_wgs84(path: Path, lon0: float = 150.15, lat0: float = -33.90) -> Path:
    """Create a small synthetic WGS84 Float32 GeoTIFF with a clear hill peak at Blue Mountains."""
    gdal.UseExceptions()
    osr.UseExceptions()
    w, h = 64, 64
    pix = 0.005  # ~500 m pixels spanning 0.32 deg (~35 km)
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(str(path), w, h, 1, gdal.GDT_Float32, options=["COMPRESS=DEFLATE"])
    assert ds is not None
    try:
        ds.SetGeoTransform((lon0, pix, 0.0, lat0 + h * pix, 0.0, -pix))
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(4326)
        ds.SetProjection(srs.ExportToWkt())

        import numpy as np

        yy, xx = np.mgrid[0:h, 0:w]
        cy, cx = h / 2.0, w / 2.0
        elev = (950.0 - 12.0 * np.hypot(xx - cx, yy - cy)).astype(np.float32)
        band = ds.GetRasterBand(1)
        band.SetNoDataValue(-9999.0)
        band.WriteArray(elev)
        band.FlushCache()
        ds.FlushCache()
    finally:
        ds = None
    return path


def _seed_pack_dir(packs_dir: Path, dir_name: str, pack_id: str, name: str = "Test Pack") -> Path:
    pdir = packs_dir / dir_name
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "dem.tif").write_bytes(b"fake-dem")
    (pdir / "osm.osm.pbf").write_bytes(b"fake-osm")
    manifest = PackManifest(
        pack_id=pack_id,
        name=name,
        region=pack_id,
        bbox_wgs84=BBox(min_x=150.0, min_y=-34.05, max_x=150.7, max_y=-33.4),
        utm_zones=["56H"],
        dem_path="dem.tif",
        vertical_datum="AHD",
        osm_pbf_path="osm.osm.pbf",
        overlays={},
        attributions=["OpenStreetMap"],
        created_at="2026-11-26T12:00:00Z",
        size_bytes=2048,
    )
    (pdir / "pack.json").write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    return pdir


class TestAtlasOverviewIndexCoverPDF:
    def test_render_grid_and_strip_atlas_index_exact_mediabox(self, tmp_path: Path) -> None:
        # 1. 2x2 Grid Atlas (A4 Portrait Color + B&W)
        grid_sheets = [
            AtlasIndexSheet(
                sheet_id="A1",
                page_number=2,
                center_lat=-33.705,
                center_lon=150.287,
                center_mgrs_10m="56H KH 4862 6732",
                utm_bbox=BBox(min_x=246300, min_y=6264400, max_x=250950, max_y=6270275),
            ),
            AtlasIndexSheet(
                sheet_id="A2",
                page_number=3,
                center_lat=-33.705,
                center_lon=150.337,
                center_mgrs_10m="56H KH 5327 6732",
                utm_bbox=BBox(min_x=250950, min_y=6264400, max_x=255600, max_y=6270275),
            ),
            AtlasIndexSheet(
                sheet_id="B1",
                page_number=4,
                center_lat=-33.758,
                center_lon=150.287,
                center_mgrs_10m="56H KH 4862 6145",
                utm_bbox=BBox(min_x=246300, min_y=6258525, max_x=250950, max_y=6264400),
            ),
            AtlasIndexSheet(
                sheet_id="B2",
                page_number=5,
                center_lat=-33.758,
                center_lon=150.337,
                center_mgrs_10m="56H KH 5327 6145",
                utm_bbox=BBox(min_x=250950, min_y=6258525, max_x=255600, max_y=6264400),
            ),
        ]
        out_grid = render_atlas_index_pdf(
            grid_sheets,
            page_width_mm=210.0,
            page_height_mm=297.0,
            scale=25000,
            utm_epsg=32756,
            title="Blue Mountains 2x2",
            output_pdf=tmp_path / "grid_index_a4p.pdf",
            bw=False,
        )
        assert out_grid.is_file()
        r_grid = PdfReader(str(out_grid))
        assert len(r_grid.pages) == 1
        mb = r_grid.pages[0].mediabox
        assert float(mb.width) * 25.4 / 72.0 == pytest.approx(210.0, abs=0.01)
        assert float(mb.height) * 25.4 / 72.0 == pytest.approx(297.0, abs=0.01)

        txt = re.sub(r"\s+", " ", r_grid.pages[0].extract_text() or "")
        assert "ATLAS OVERVIEW & SHEET INDEX" in txt
        for sid in ("A1", "A2", "B1", "B2"):
            assert sid in txt
        assert "56H KH 4862 6732" in txt
        assert "EPSG:32756" in txt

        # 2. Strip Atlas accepting StripSheetPlan directly + route polyline + overlap hatching (A4 Landscape B&W)
        strip_sheets = [
            StripSheetPlan(
                sheet_id="S1",
                index=1,
                center_lat=-33.710,
                center_lon=150.290,
                center_mgrs_10m="56H KH 4890 6670",
                utm_bbox=BBox(min_x=246000, min_y=6263500, max_x=251800, max_y=6268150),
            ),
            StripSheetPlan(
                sheet_id="S2",
                index=2,
                center_lat=-33.735,
                center_lon=150.335,
                center_mgrs_10m="56H KH 5310 6390",
                utm_bbox=BBox(min_x=249800, min_y=6261200, max_x=255600, max_y=6265850),
            ),
        ]
        out_strip = render_atlas_index_pdf(
            strip_sheets,
            page_width_mm=297.0,
            page_height_mm=210.0,
            scale=25000,
            utm_epsg=32756,
            title="Katoomba Corridor",
            overlap_pct=12.0,
            route_utm=[(247000.0, 6267000.0), (254000.0, 6262500.0)],
            output_pdf=tmp_path / "strip_index_a4l_bw.pdf",
            bw=True,
        )
        r_strip = PdfReader(str(out_strip))
        assert len(r_strip.pages) == 1
        mb2 = r_strip.pages[0].mediabox
        assert float(mb2.width) * 25.4 / 72.0 == pytest.approx(297.0, abs=0.01)
        assert float(mb2.height) * 25.4 / 72.0 == pytest.approx(210.0, abs=0.01)
        txt2 = re.sub(r"\s+", " ", r_strip.pages[0].extract_text() or "")
        assert "S1" in txt2 and "S2" in txt2 and "OVERLAP: 12%" in txt2
        assert "PyQt5" not in sys.modules

    def test_worker_prepends_atlas_index_on_grid_and_strip_jobs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        data_dir = tmp_path / "jobs"
        packs_dir = tmp_path / "packs"
        db_path = data_dir / "gridforge_jobs.sqlite3"
        db.init_db(db_path)
        packs_dir.mkdir(parents=True)

        seen_specs = []

        def _fake_prepare(spec, job_dir, **kwargs):
            seen_specs.append(spec)
            return spec

        def _fake_render_pdf(self, workspace, output_pdf: Path):
            w = PdfWriter()
            w.add_blank_page(width=595.28, height=841.89)
            with output_pdf.open("wb") as fh:
                w.write(fh)
            w.close()
            return RenderResult(
                output_pdf=output_pdf,
                page_width_mm=210.0,
                page_height_mm=297.0,
                scale=25000,
                utm_epsg=32756,
                elapsed_seconds=0.05,
            )

        monkeypatch.setattr("gridforge.web.worker.runner.prepare_job_workspace", _fake_prepare)
        monkeypatch.setattr("gridforge.web.worker.PyQGISRenderer.render_pdf", _fake_render_pdf)

        # 2x2 Grid Atlas with include_atlas_index + show_fold_marks + viewsheds + nz_overlays
        req_json = json.dumps(
            {
                "center": "-33.732, 150.312",
                "scale": 25000,
                "paper": "a4",
                "orientation": "portrait",
                "color_mode": "color",
                "units": "metric",
                "title": "Indexed 2x2 Atlas",
                "grid_cols": 2,
                "grid_rows": 2,
                "include_atlas_index": True,
                "show_fold_marks": True,
                "dem_source": "nz-linz",
                "nz_overlays": ["nz-doc-trails"],
                "viewsheds": [
                    {
                        "label": "Repeater 1",
                        "lat": -33.732,
                        "lon": 150.312,
                        "observer_height_m": 10.0,
                        "target_height_m": 2.0,
                        "max_distance_m": 5000.0,
                    }
                ],
                "outputs": {"pdf": True},
            }
        )
        db.create_job(
            db_path,
            job_id="idxatlas0001",
            title="Indexed 2x2 Atlas",
            center="-33.732, 150.312",
            center_mgrs="56H KH 5095 6431",
            scale=25000,
            paper=PaperSize.A4,
            orientation=Orientation.PORTRAIT,
            color_mode=ColorMode.COLOR,
            grid_cols=2,
            grid_rows=2,
            request_json=req_json,
        )
        claimed = db.claim_next_job(db_path)
        assert claimed is not None
        done = process_single_job(claimed, db_path=db_path, data_dir=data_dir, packs_dir=packs_dir)
        assert done is not None and done.status == JobStatus.SUCCEEDED
        assert len(seen_specs) == 4
        assert seen_specs[0].show_fold_marks is True
        assert seen_specs[0].include_atlas_index is True
        assert seen_specs[0].dem_source == DemSourceKind.NZ_LINZ
        assert seen_specs[0].nz_overlays == [NZOverlayKind.NZ_DOC_TRAILS]
        assert len(seen_specs[0].viewsheds) == 1
        assert seen_specs[0].viewsheds[0].label == "Repeater 1"

        merged = data_dir / "idxatlas0001" / "artifacts" / done.artifacts[0].filename
        reader = PdfReader(str(merged))
        assert len(reader.pages) == 5  # Page 1 = Atlas Index Cover + 4 map sheets
        cover_txt = re.sub(r"\s+", " ", reader.pages[0].extract_text() or "")
        assert "ATLAS OVERVIEW & SHEET INDEX" in cover_txt
        assert "A1" in cover_txt and "B2" in cover_txt
        assert "Page 2" in cover_txt and "Page 5" in cover_txt


class TestAirGappedMBTilesBasemapAPI:
    def test_basemap_unavailable_returns_clean_false_and_204(self, tmp_path: Path) -> None:
        app = create_app(data_dir=tmp_path / "jobs", packs_dir=tmp_path / "packs")
        client = TestClient(app, base_url="http://localhost:8080")

        info = client.get("/api/basemap/info")
        assert info.status_code == 200
        body = info.json()
        assert body["available"] is False
        assert body["format"] is None

        tile = client.get("/api/basemap/4/14/9.png")
        assert tile.status_code == 204

    def test_basemap_auto_discovery_and_xyz_to_tms_flip(self, tmp_path: Path) -> None:
        packs_dir = tmp_path / "packs"
        # Store tile at z=4, x=14, tms_y=6 -> corresponds to XYZ y = (2^4 - 1) - 6 = 9!
        _create_synthetic_mbtiles(
            packs_dir / "basemap.mbtiles",
            fmt="png",
            tiles=[(4, 14, 6, _TINY_PNG)],
        )
        app = create_app(data_dir=tmp_path / "jobs", packs_dir=packs_dir)
        client = TestClient(app, base_url="http://localhost:8080")

        info = client.get("/api/basemap/info")
        assert info.status_code == 200
        meta = info.json()
        assert meta["available"] is True
        assert meta["name"] == "Australia Offline Overview"
        assert meta["format"] == "png"
        assert meta["minzoom"] == 0 and meta["maxzoom"] == 6
        assert meta["bounds"] == [112.0, -44.0, 154.0, -10.0]
        assert meta["tile_url"] == "/api/basemap/{z}/{x}/{y}.png"

        # Request XYZ y=9 -> flips to TMS y=6 -> 200 OK with exact PNG bytes!
        hit = client.get("/api/basemap/4/14/9.png")
        assert hit.status_code == 200
        assert hit.headers["content-type"].startswith("image/png")
        assert "max-age=86400" in hit.headers.get("cache-control", "")
        assert hit.content == _TINY_PNG

        # Request unflipped y=6 -> flips to TMS y=9 (absent) -> 204 No Content!
        miss = client.get("/api/basemap/4/14/6.png")
        assert miss.status_code == 204

        # Out-of-range coordinates return 204
        assert client.get("/api/basemap/4/99/9.png").status_code == 204
        assert client.get("/api/basemap/4/14/9.exe").status_code == 400

    def test_basemap_env_var_and_jpeg_format(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mbt = _create_synthetic_mbtiles(
            tmp_path / "custom" / "world.mbtiles",
            fmt="jpeg",
            tiles=[(2, 3, 1, _TINY_JPG)],  # XYZ y = (4 - 1) - 1 = 2
        )
        monkeypatch.setenv("GRIDFORGE_BASEMAP_MBTILES", str(mbt))
        app = create_app(data_dir=tmp_path / "jobs", packs_dir=tmp_path / "packs")
        client = TestClient(app, base_url="http://localhost:8080")

        meta = client.get("/api/basemap/info").json()
        assert meta["available"] is True
        assert meta["format"] == "jpg"
        hit = client.get("/api/basemap/2/3/2.jpg")
        assert hit.status_code == 200
        assert hit.headers["content-type"].startswith("image/jpeg")
        assert hit.content == _TINY_JPG


class TestPackManagerAPI:
    def test_regions_catalog_build_lifecycle_single_concurrency_and_delete(
        self, tmp_path: Path
    ) -> None:
        packs_dir = tmp_path / "packs"
        packs_dir.mkdir(parents=True)
        outside_secret = tmp_path / "outside_dir"
        outside_secret.mkdir()
        (outside_secret / "keep.txt").write_text("safe", encoding="utf-8")

        active = 0
        max_concurrent = 0
        gate = threading.Event()
        started_first = threading.Event()

        def _fake_pack_builder(region_or_bbox, out_dir, **kwargs):
            nonlocal active, max_concurrent
            active += 1
            max_concurrent = max(max_concurrent, active)
            started_first.set()
            gate.wait(timeout=3.0)
            pdir = Path(out_dir)
            pdir.mkdir(parents=True, exist_ok=True)
            manifest = PackManifest(
                pack_id=pdir.name,
                name=kwargs.get("name") or f"Built {region_or_bbox}",
                region=region_or_bbox,
                bbox_wgs84=BBox(min_x=148.74, min_y=-35.94, max_x=149.42, max_y=-35.10),
                utm_zones=["55H"],
                dem_path="dem.tif",
                vertical_datum="AHD",
                osm_pbf_path="osm.osm.pbf",
                overlays={},
                attributions=["OpenStreetMap"],
                created_at="2026-11-26T12:00:00Z",
                size_bytes=4096,
            )
            (pdir / "dem.tif").write_bytes(b"dem")
            (pdir / "osm.osm.pbf").write_bytes(b"osm")
            (pdir / "pack.json").write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
            active -= 1
            return manifest

        app = create_app(
            data_dir=tmp_path / "jobs",
            packs_dir=packs_dir,
            pack_builder=_fake_pack_builder,
        )
        client = TestClient(app, base_url="http://localhost:8080")

        # 1. All 29 curated grouped regions listed (7 AU tactical + 8 AU state + 6 NZ + 8 NA)
        reg_resp = client.get("/api/packs/regions")
        assert reg_resp.status_code == 200
        regions = reg_resp.json()["regions"]
        assert len(regions) == 29
        assert sum(1 for r in regions if r["group"] == "au_state") == 8
        assert sum(1 for r in regions if r["group"] == "au_tactical") == 7
        assert sum(1 for r in regions if r["group"] == "nz") == 6
        assert sum(1 for r in regions if r["group"] == "na") == 8
        assert all(r["id"] == r["region_id"] for r in regions)

        # 2. Invalid region rejected with 422
        assert client.post("/api/packs/build", json={"region_or_bbox": "atlantis"}).status_code == 422

        # 3. Enqueue 2 builds -> 1st starts running, 2nd queues cleanly (strictly 1 concurrent!)
        b1 = client.post("/api/packs/build", json={"region_or_bbox": "act", "dem_tier": "srtm-1s"})
        assert b1.status_code == 202
        t1 = b1.json()
        assert t1["status"] == "running"
        assert t1["region"] == "act"

        started_first.wait(timeout=2.0)

        b2 = client.post(
            "/api/packs/build",
            json={"region_or_bbox": "blue-mountains", "name": "BM Custom"},
        )
        assert b2.status_code == 202
        t2 = b2.json()
        assert t2["status"] == "queued"

        # Cannot delete a pack while its build is in progress -> 409 Conflict
        _seed_pack_dir(packs_dir, "act", "act")
        assert client.delete("/api/packs/act").status_code == 409

        # Release builder and poll both tasks to completion
        gate.set()
        for tid in (t1["task_id"], t2["task_id"]):
            deadline = time.monotonic() + 5.0
            final = None
            while time.monotonic() < deadline:
                r = client.get(f"/api/packs/tasks/{tid}")
                assert r.status_code == 200
                final = r.json()
                if final["status"] in ("succeeded", "failed"):
                    break
                time.sleep(0.02)
            assert final is not None and final["status"] == "succeeded"
            assert final["manifest"] is not None

        assert max_concurrent == 1
        assert len(client.get("/api/packs/tasks").json()["tasks"]) == 2
        assert client.get("/api/packs/tasks/nonexistent1234").status_code == 404

        # 4. Delete installed pack (including a renamed directory matched by manifest pack_id!)
        _seed_pack_dir(packs_dir, "renamed-tas-folder", "tas", "Tasmania")
        del_tas = client.delete("/api/packs/tas")
        assert del_tas.status_code == 200
        assert del_tas.json() == {"deleted": True, "pack_id": "tas"}
        assert not (packs_dir / "renamed-tas-folder").exists()
        assert client.delete("/api/packs/tas").status_code == 404

        # 5. Path traversal strictly blocked on DELETE (both HTTP and PackTaskManager layer)
        assert client.delete("/api/packs/bad..id").status_code == 400
        for attack in ("../outside_dir", "..%2Foutside_dir", "a/b"):
            resp = client.delete(f"/api/packs/{attack}")
            assert resp.status_code in (400, 404, 405)
        with pytest.raises(ValueError, match="Invalid pack_id"):
            app.state.pack_manager.delete_pack("../outside_dir")
        assert (outside_secret / "keep.txt").is_file()


class TestViewshedPreviewAndV13JobsAPI:
    def test_viewshed_preview_endpoint_returns_mgrs_elev_area_and_geojson(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dem_path = _create_synthetic_dem_wgs84(tmp_path / "bm_dem.tif")

        class _FakeElevSource:
            def fetch_dem(self, layout, work_dir: Path) -> Path:
                return dem_path

        def _fake_compute_viewshed_geojson(dem_utm_tif: Path, observer, utm_epsg: int, work_dir: Path):
            assert dem_utm_tif.is_file()
            assert utm_epsg == 32756
            return {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"label": observer.label, "visible": 1},
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [
                                [
                                    [150.300, -33.740],
                                    [150.324, -33.740],
                                    [150.324, -33.724],
                                    [150.300, -33.724],
                                    [150.300, -33.740],
                                ]
                            ],
                        },
                    }
                ],
            }

        monkeypatch.setattr(
            "gridforge.web.viewshed._resolve_preview_elevation_source",
            lambda *a, **kw: _FakeElevSource(),
        )
        monkeypatch.setattr(
            "gridforge.pipeline.terrain.compute_viewshed_geojson",
            _fake_compute_viewshed_geojson,
            raising=False,
        )

        app = create_app(data_dir=tmp_path / "jobs", packs_dir=tmp_path / "packs")
        client = TestClient(app, base_url="http://localhost:8080")

        resp = client.post(
            "/api/viewshed-preview",
            json={
                "observer": {
                    "label": "Echo Point Tx",
                    "lat": -33.732,
                    "lon": 150.312,
                    "observer_height_m": 10.0,
                    "target_height_m": 1.5,
                    "max_distance_m": 5000.0,
                },
                "dem_source": "auto",
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["observer_mgrs_10m"].startswith("56H KH ")
        assert data["utm_epsg"] == 32756
        assert data["max_distance_m"] == 5000.0
        assert data["observer_elev_m"] > 500.0
        assert data["visible_area_km2"] > 3.0
        assert data["viewshed_geojson"]["type"] == "FeatureCollection"
        assert len(data["viewshed_geojson"]["features"]) == 1
        ring_fc = data["range_ring_geojson"]
        assert ring_fc["type"] == "FeatureCollection"
        ring_coords = ring_fc["features"][0]["geometry"]["coordinates"][0]
        assert len(ring_coords) == 73
        assert ring_coords[0] == ring_coords[-1]
        assert "qgis" not in sys.modules
        assert "PyQt5" not in sys.modules

    def test_create_job_accepts_v13_viewsheds_nz_fold_and_atlas_index(self, tmp_path: Path) -> None:
        app = create_app(data_dir=tmp_path / "jobs", packs_dir=tmp_path / "packs")
        client = TestClient(app, base_url="http://localhost:8080")

        resp = client.post(
            "/api/jobs",
            json={
                "center": "-39.133, 175.642",
                "scale": 25000,
                "paper": "a4",
                "dem_source": "nz-linz",
                "nz_overlays": ["nz-doc-trails"],
                "viewsheds": [
                    {
                        "label": "Red Crater Repeater",
                        "lat": -39.133,
                        "lon": 175.642,
                        "observer_height_m": 5.0,
                        "target_height_m": 1.5,
                        "max_distance_m": 8000.0,
                    }
                ],
                "grid_cols": 2,
                "grid_rows": 2,
                "include_atlas_index": True,
                "show_fold_marks": True,
                "outputs": {"pdf": True},
            },
        )
        assert resp.status_code == 202, resp.text
        job = resp.json()
        assert job["status"] == "queued"
        assert job["center_mgrs"].startswith("60H ")
