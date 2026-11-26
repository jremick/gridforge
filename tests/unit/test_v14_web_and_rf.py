"""Unit tests for GridForge v1.4.0 RF Link Card, Tactical Graphics, Projects CRUD, Basemap & Web API."""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from osgeo import gdal, ogr, osr
from pypdf import PdfReader, PdfWriter

from gridforge.config import (
    BBox,
    CAOverlayKind,
    ColorMode,
    DemSourceKind,
    ExportFormat,
    ExportResult,
    Orientation,
    PackManifest,
    PaperSize,
    RenderResult,
    RFLinkSpec,
    SheetSpec,
    TacticalGraphic,
    USOverlayKind,
)
from gridforge.geo.crs import compute_sheet_layout
from gridforge.geo.supplements import render_rf_link_card_pdf
from gridforge.geo.tactical import compute_ruler_metrics, write_tactical_graphics_gpkg
from gridforge.web import db
from gridforge.web.app import create_app
from gridforge.web.schemas import JobArtifact, JobStatus
from gridforge.web.worker import process_single_job

_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0"
    b"\x00\x00\x03\x01\x01\x00\x18\xdd\x8d\xb0\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _create_mbtiles(path: Path, *, name: str, tile: tuple[int, int, int, bytes]) -> Path:
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
                ("name", name),
                ("format", "png"),
                ("minzoom", "0"),
                ("maxzoom", "10"),
                ("bounds", "150.0,-34.1,150.7,-33.4"),
                ("attribution", "Offline Test Basemap"),
            ],
        )
        conn.execute(
            "INSERT INTO tiles (zoom_level, tile_column, tile_row, tile_data) VALUES (?, ?, ?, ?)",
            tile,
        )
        conn.commit()
    finally:
        conn.close()
    return path


def _create_synthetic_dem(path: Path, lon0: float = 150.15, lat0: float = -33.90) -> Path:
    gdal.UseExceptions()
    osr.UseExceptions()
    w, h = 64, 64
    pix = 0.005
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
        elev = (800.0 + 3.0 * xx + 2.0 * yy).astype(np.float32)
        band = ds.GetRasterBand(1)
        band.SetNoDataValue(-9999.0)
        band.WriteArray(elev)
        band.FlushCache()
        ds.FlushCache()
    finally:
        ds = None
    return path


def _sample_rf_profile(status: str = "CLEAR") -> dict:
    samples = []
    for i in range(21):
        d = i * 0.25
        frac = i / 20.0
        los = 950.0 + 120.0 * frac
        r1 = 18.0 * (4.0 * frac * (1.0 - frac)) ** 0.5
        terr = (920.0 + 100.0 * frac) - (25.0 if status == "CLEAR" else -10.0) * (4.0 * frac * (1.0 - frac))
        samples.append(
            {
                "dist_km": round(d, 3),
                "terrain_m": round(terr, 2),
                "los_m": round(los, 2),
                "fresnel_lo_m": round(los - r1, 2),
                "fresnel_hi_m": round(los + r1, 2),
                "fresnel60_lo_m": round(los - 0.6 * r1, 2),
                "clearance_m": round(los - terr, 2),
            }
        )
    return {
        "status": status,
        "distance_km": 5.0,
        "freq_mhz": 915.0,
        "fspl_db": 105.6,
        "min_clearance_m": 22.4 if status == "CLEAR" else -4.2,
        "min_fresnel_pct": 124.0 if status == "CLEAR" else -23.0,
        "los_clear": status != "OBSTRUCTED",
        "fresnel_60pct_clear": status == "CLEAR",
        "bearing_true_deg": 42.3,
        "bearing_grid_deg": 41.1,
        "bearing_mag_deg": 29.8,
        "bearing_mils": 530.0,
        "from_elev_m": 948.0,
        "to_elev_m": 1068.0,
        "from_mgrs": "56H KH 5095 6431",
        "to_mgrs": "56H KH 5432 6798",
        "from_label": "Echo Point",
        "to_label": "Narrow Neck",
        "from_height_m": 10.0,
        "to_height_m": 5.0,
        "samples": samples,
    }


class TestTacticalGraphicsAndRuler:
    def test_compute_ruler_metrics_with_and_without_dem(self, tmp_path: Path) -> None:
        pts = [(-33.732, 150.312), (-33.712, 150.322), (-33.702, 150.342)]
        res = compute_ruler_metrics(pts, date_iso="2026-06-15")
        assert len(res.segments) == 2
        assert res.total_distance_m > 4000.0
        for seg in res.segments:
            assert seg.distance_m > 1000.0
            assert 0.0 <= seg.bearing_true_deg < 360.0
            assert 0.0 <= seg.bearing_grid_deg < 360.0
            assert 0.0 <= seg.bearing_mag_deg < 360.0
            assert 0.0 <= seg.bearing_mils < 6400.0
            assert seg.elev_start_m is None and seg.grade_pct is None

        dem = _create_synthetic_dem(tmp_path / "dem.tif")
        res_dem = compute_ruler_metrics(pts, dem_path=dem, date_iso="2026-06-15")
        assert res_dem.segments[0].elev_start_m is not None
        assert res_dem.segments[0].elev_end_m is not None
        assert res_dem.segments[0].grade_pct is not None

    def test_write_tactical_graphics_gpkg_all_three_layers_and_rf_links(self, tmp_path: Path) -> None:
        graphics = [
            TacticalGraphic(
                kind="range_ring",
                label="LZ Alpha",
                points=[(-33.732, 150.312)],
                radii_km=[1.0, 2.5],
            ),
            TacticalGraphic(
                kind="sector",
                label="AOI North",
                points=[(-33.72, 150.30), (-33.72, 150.33), (-33.74, 150.33), (-33.74, 150.30)],
            ),
            TacticalGraphic(
                kind="bearing_line",
                label="Azimuth 1",
                points=[(-33.732, 150.312), (-33.710, 150.325)],
            ),
            TacticalGraphic(
                kind="corridor",
                label="Ingress",
                points=[(-33.735, 150.305), (-33.725, 150.325)],
                radii_km=[0.25],
            ),
        ]
        rf_links = [
            RFLinkSpec(
                from_lat=-33.732,
                from_lon=150.312,
                to_lat=-33.710,
                to_lon=150.335,
                freq_mhz=915.0,
                from_label="Base",
                to_label="Relay",
            )
        ]
        out = write_tactical_graphics_gpkg(
            graphics,
            32756,
            tmp_path / "tactical.gpkg",
            rf_links=rf_links,
            date_iso="2026-06-15",
        )
        assert out is not None and out.is_file()

        ogr.UseExceptions()
        ds = ogr.Open(str(out))
        assert ds is not None
        try:
            names = {ds.GetLayerByIndex(i).GetName(): ds.GetLayerByIndex(i) for i in range(ds.GetLayerCount())}
            assert set(names.keys()) == {"tactical_sectors", "tactical_rings", "tactical_lines"}
            assert names["tactical_sectors"].GetFeatureCount() == 2  # 1 sector + 1 buffered corridor
            assert names["tactical_rings"].GetFeatureCount() == 2  # 1.0 km + 2.5 km
            assert names["tactical_lines"].GetFeatureCount() == 3  # bearing_line + corridor + rf_link
            srs = names["tactical_rings"].GetSpatialRef()
            assert srs is not None and str(srs.GetAuthorityCode(None)) == "32756"
        finally:
            ds = None


class TestRFLinkCardPDF:
    def test_render_rf_link_card_pdf_exact_mediabox_and_content(self, tmp_path: Path) -> None:
        layout = compute_sheet_layout(
            SheetSpec(
                center_lat=-33.732,
                center_lon=150.312,
                scale=25000,
                paper=PaperSize.A4,
                orientation=Orientation.PORTRAIT,
                title="Kosciuszko RF Survey",
            )
        )
        p1 = {
            **_sample_rf_profile("CLEAR"),
            "from_label": "Kosciuszko Relay",
            "to_label": "Seamans Hut",
            "bearing_true_deg": 83.0,
            "bearing_grid_deg": 83.7,
            "bearing_mag_deg": 70.2,
        }
        p2 = _sample_rf_profile("OBSTRUCTED")
        out = render_rf_link_card_pdf(
            [p1, p2],
            layout,
            tmp_path / "rf_card.pdf",
            bw=False,
        )
        assert out.is_file()
        reader = PdfReader(str(out))
        assert len(reader.pages) == 1
        mb = reader.pages[0].mediabox
        assert float(mb.width) * 25.4 / 72.0 == pytest.approx(210.0, abs=0.01)
        assert float(mb.height) * 25.4 / 72.0 == pytest.approx(297.0, abs=0.01)

        txt = re.sub(r"\s+", " ", reader.pages[0].extract_text() or "")
        assert "RF / LINE-OF-SIGHT LINK PROFILE" in txt
        assert "CLEAR" in txt
        assert "OBSTRUCTED" in txt
        assert "915 MHz" in txt
        assert "105.6 dB" in txt
        assert "Kosciuszko Relay → Seamans Hut" in txt
        assert "070.2°M)" in txt
        assert "PyQt5" not in sys.modules


class TestV14WebAPIsAndSecurity:
    def test_projects_crud_csrf_and_job_thumbnail(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "jobs"
        packs_dir = tmp_path / "packs"
        app = create_app(data_dir=data_dir, packs_dir=packs_dir, web_dist_dir=tmp_path / "dist")
        client = TestClient(app, base_url="http://localhost:8080")

        # 1. Cross-origin writes rejected by SameOriginWritesMiddleware on all new v1.4 routes
        evil = {"Origin": "https://evil.example"}
        assert client.post("/api/projects", json={"name": "X", "state": {}}, headers=evil).status_code == 403
        assert (
            client.post(
                "/api/ruler-metrics",
                json={"points": [{"lat": -33.73, "lon": 150.31}, {"lat": -33.72, "lon": 150.32}]},
                headers=evil,
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/rf-link-profile",
                json={"from_lat": -33.73, "from_lon": 150.31, "to_lat": -33.71, "to_lon": 150.33},
                headers=evil,
            ).status_code
            == 403
        )

        # 2. Projects CRUD (POST 201 -> GET list/detail -> PATCH -> DELETE)
        created = client.post(
            "/api/projects",
            json={
                "name": "Blue Mountains Patrol",
                "notes": "Winter ridgeline comms plan",
                "state": {"center": "-33.732, 150.312", "color_mode": "alpine"},
            },
        )
        assert created.status_code == 201, created.text
        proj = created.json()
        pid = proj["project_id"]
        assert proj["id"] == pid
        assert proj["notes"] == "Winter ridgeline comms plan"
        assert proj["description"] == "Winter ridgeline comms plan"
        assert proj["state"]["color_mode"] == "alpine"

        listed = client.get("/api/projects").json()["projects"]
        assert len(listed) == 1 and listed[0]["project_id"] == pid
        assert client.get(f"/api/projects/{pid}").json()["name"] == "Blue Mountains Patrol"

        patched = client.patch(
            f"/api/projects/{pid}",
            json={"name": "Blue Mountains Patrol v2", "notes": "Updated plan"},
        )
        assert patched.status_code == 200
        assert patched.json()["name"] == "Blue Mountains Patrol v2"
        assert patched.json()["notes"] == "Updated plan"

        # Blank/null optional description & notes must coerce cleanly to "" (HTTP 201)
        blank_resp = client.post(
            "/api/projects",
            json={
                "name": "Blank Notes",
                "description": None,
                "notes": None,
                "state": {"center": "-33.732, 150.312"},
            },
        )
        assert blank_resp.status_code == 201, blank_resp.text
        blank_proj = blank_resp.json()
        assert blank_proj["description"] == ""
        assert blank_proj["notes"] == ""
        assert client.patch(f"/api/projects/{blank_proj['project_id']}", json={"notes": None}).status_code == 200
        assert client.delete(f"/api/projects/{blank_proj['project_id']}").status_code == 200

        # 3. Job thumbnail rasterized lazily via pdftoppm and cached
        job_resp = client.post("/api/jobs", json={"center": "-33.732, 150.312", "outputs": {"pdf": True}})
        job_id = job_resp.json()["job_id"]
        # Before PDF exists -> 404
        assert client.get(f"/api/jobs/{job_id}/thumbnail.png").status_code == 404

        art_dir = data_dir / job_id / "artifacts"
        art_dir.mkdir(parents=True, exist_ok=True)
        pdf_file = art_dir / "map.pdf"
        w = PdfWriter()
        w.add_blank_page(width=595.28, height=841.89)
        with pdf_file.open("wb") as fh:
            w.write(fh)
        w.close()
        db.complete_job(
            data_dir / "gridforge_jobs.sqlite3",
            job_id,
            artifacts=[
                JobArtifact(
                    kind="pdf",
                    filename="map.pdf",
                    size_bytes=pdf_file.stat().st_size,
                    download_url=f"/api/jobs/{job_id}/artifacts/map.pdf",
                )
            ],
            elapsed_seconds=0.1,
        )

        thumb = client.get(f"/api/jobs/{job_id}/thumbnail.png")
        assert thumb.status_code == 200
        assert thumb.headers["content-type"].startswith("image/png")
        assert thumb.content.startswith(b"\x89PNG\r\n\x1a\n")
        assert (data_dir / job_id / "thumbnail.png").is_file()

        assert client.delete(f"/api/projects/{pid}").status_code == 200
        assert client.get(f"/api/projects/{pid}").status_code == 404

    def test_per_pack_basemap_discovery_and_query_param(self, tmp_path: Path) -> None:
        packs_dir = tmp_path / "packs"
        pdir = packs_dir / "blue-mountains"
        pdir.mkdir(parents=True)
        (pdir / "dem.tif").write_bytes(b"dem")
        (pdir / "osm.osm.pbf").write_bytes(b"osm")
        # z=8, x=234, tms_y=102 -> XYZ y = (256 - 1) - 102 = 153
        _create_mbtiles(
            pdir / "basemap.mbtiles",
            name="Blue Mountains Relief",
            tile=(8, 234, 102, _TINY_PNG),
        )
        manifest = PackManifest(
            pack_id="blue-mountains",
            name="Blue Mountains (NSW)",
            region="blue-mountains",
            bbox_wgs84=BBox(min_x=150.0, min_y=-34.05, max_x=150.7, max_y=-33.4),
            utm_zones=["56H"],
            dem_path="dem.tif",
            vertical_datum="AHD",
            osm_pbf_path="osm.osm.pbf",
            basemap_path="basemap.mbtiles",
            overlays={},
            attributions=["OpenStreetMap"],
            created_at="2026-11-26T14:00:00Z",
            size_bytes=4096,
        )
        (pdir / "pack.json").write_text(manifest.model_dump_json(indent=2), encoding="utf-8")

        app = create_app(data_dir=tmp_path / "jobs", packs_dir=packs_dir, web_dist_dir=tmp_path / "dist")
        client = TestClient(app, base_url="http://localhost:8080")

        # Pack summary reports has_basemap=True
        packs = client.get("/api/packs").json()["packs"]
        assert len(packs) == 1 and packs[0]["has_basemap"] is True

        # Auto-discovered even without ?pack_id=, AND explicitly selectable with ?pack_id=
        for qs in ("", "?pack_id=blue-mountains"):
            info = client.get(f"/api/basemap/info{qs}").json()
            assert info["available"] is True
            assert info["name"] == "Blue Mountains Relief"
            assert info["pack_id"] == "blue-mountains"

        hit = client.get("/api/basemap/8/234/153.png?pack_id=blue-mountains")
        assert hit.status_code == 200
        assert hit.content == _TINY_PNG
        assert client.get("/api/basemap/8/234/102.png?pack_id=blue-mountains").status_code == 204

    def test_rf_link_profile_and_ruler_metrics_endpoints(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dem_path = _create_synthetic_dem(tmp_path / "dem.tif")

        class _FakeElev:
            def fetch_dem(self, layout, work_dir: Path) -> Path:
                return dem_path

        monkeypatch.setattr(
            "gridforge.web.viewshed._resolve_preview_elevation_source",
            lambda *a, **kw: _FakeElev(),
        )
        monkeypatch.setattr(
            "gridforge.pipeline.terrain.compute_rf_link_profile",
            lambda dem_tif, link, epsg, **kw: _sample_rf_profile("CLEAR"),
            raising=False,
        )

        app = create_app(data_dir=tmp_path / "jobs", packs_dir=tmp_path / "packs", web_dist_dir=tmp_path / "dist")
        client = TestClient(app, base_url="http://localhost:8080")

        rf_resp = client.post(
            "/api/rf-link-profile",
            json={
                "from_lat": -33.732,
                "from_lon": 150.312,
                "to_lat": -33.702,
                "to_lon": 150.342,
                "freq_mhz": 915.0,
                "from_label": "Echo Point",
                "to_label": "Narrow Neck",
                "dem_source": "auto",
            },
        )
        assert rf_resp.status_code == 200, rf_resp.text
        body = rf_resp.json()
        assert body["status"] == "CLEAR"
        assert body["fspl_db"] == pytest.approx(105.6)
        assert len(body["samples"]) == 21

        ruler_resp = client.post(
            "/api/ruler-metrics",
            json={
                "points": [{"lat": -33.732, "lon": 150.312}, {"lat": -33.712, "lon": 150.322}],
                "dem_source": "auto",
            },
        )
        assert ruler_resp.status_code == 200, ruler_resp.text
        rb = ruler_resp.json()
        assert rb["total_distance_m"] > 2000.0
        assert len(rb["segments"]) == 1
        assert rb["segments"][0]["grade_pct"] is not None
        assert "qgis" not in sys.modules
        assert "PyQt5" not in sys.modules

    def test_worker_v14_exports_gpkg_osmand_bundle_and_rf_link_card(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        data_dir = tmp_path / "jobs"
        packs_dir = tmp_path / "packs"
        db_path = data_dir / "gridforge_jobs.sqlite3"
        db.init_db(db_path)
        packs_dir.mkdir(parents=True)

        dem_path = _create_synthetic_dem(tmp_path / "dem_utm.tif")
        seen_specs: list[SheetSpec] = []

        def _fake_prepare(spec: SheetSpec, job_dir: Path, **kwargs):
            seen_specs.append(spec)
            layout = compute_sheet_layout(spec)

            class _FakeWS:
                pass

            ws = _FakeWS()
            ws.layout = layout
            from gridforge.geo.declination import compute_declination

            ws.declination = compute_declination(layout)
            ws.dem_utm_tif = dem_path
            return ws

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
                utm_epsg=32611,
                elapsed_seconds=0.05,
            )

        def _fake_collarless(ws, out_tif: Path, **kw):
            out_tif.write_bytes(b"tif")
            return out_tif

        def _fake_gpkg(ws, out_path: Path, **kw):
            out_path.write_bytes(b"SQLite format 3\x00gpkg")
            return ExportResult(
                format=ExportFormat.GPKG,
                output_path=out_path,
                file_size_bytes=20,
                tile_count=4,
                elapsed_seconds=0.05,
            )

        def _fake_osmand(ws, out_path: Path, **kw):
            out_path.write_bytes(b"SQLite format 3\x00osmand")
            return ExportResult(
                format=ExportFormat.OSMAND,
                output_path=out_path,
                file_size_bytes=22,
                tile_count=4,
                elapsed_seconds=0.05,
            )

        def _fake_bundle(ws, out_path: Path, *, artifacts=(), **kw):
            assert len(artifacts) == 3  # pdf + gpkg + sqlitedb!
            out_path.write_bytes(b"PK\x03\x04bundle")
            return ExportResult(
                format=ExportFormat.BUNDLE,
                output_path=out_path,
                file_size_bytes=10,
                tile_count=5,
                elapsed_seconds=0.05,
            )

        monkeypatch.setattr("gridforge.web.worker.runner.prepare_job_workspace", _fake_prepare)
        monkeypatch.setattr("gridforge.web.worker.PyQGISRenderer.render_pdf", _fake_render_pdf)
        monkeypatch.setattr("gridforge.web.worker.render_collarless_geotiff", _fake_collarless)
        monkeypatch.setattr("gridforge.web.worker.export_gpkg_tiles", _fake_gpkg)
        monkeypatch.setattr("gridforge.web.worker.export_osmand_sqlitedb", _fake_osmand)
        monkeypatch.setattr("gridforge.web.worker.export_field_bundle_zip", _fake_bundle)
        monkeypatch.setattr(
            "gridforge.pipeline.terrain.compute_rf_link_profile",
            lambda dem_tif, link, epsg, **kw: _sample_rf_profile("CLEAR"),
            raising=False,
        )

        req_json = json.dumps(
            {
                "center": "51.1784, -115.5708",
                "scale": 25000,
                "paper": "a4",
                "orientation": "portrait",
                "color_mode": "night-red",
                "units": "metric",
                "title": "Banff Night Comms",
                "dem_source": "ca-hrdem",
                "us_overlays": ["us-trails", "us-public-lands"],
                "ca_overlays": ["ca-canvec-trails"],
                "rf_links": [
                    {
                        "from_lat": 51.1784,
                        "from_lon": -115.5708,
                        "to_lat": 51.1950,
                        "to_lon": -115.5450,
                        "freq_mhz": 915.0,
                        "from_label": "Sulphur",
                        "to_label": "Norquay",
                    }
                ],
                "tactical_graphics": [
                    {
                        "kind": "range_ring",
                        "label": "Camp",
                        "points": [[51.1784, -115.5708]],
                        "radii_km": [1.0, 3.0],
                    }
                ],
                "include_rf_link_card": True,
                "outputs": {
                    "pdf": True,
                    "gpkg_tiles": True,
                    "osmand_sqlitedb": True,
                    "field_bundle_zip": True,
                },
            }
        )
        db.create_job(
            db_path,
            job_id="v14job000001",
            title="Banff Night Comms",
            center="51.1784, -115.5708",
            center_mgrs="11U PG 0000 7200",
            scale=25000,
            paper=PaperSize.A4,
            orientation=Orientation.PORTRAIT,
            color_mode=ColorMode.NIGHT_RED,
            request_json=req_json,
        )
        claimed = db.claim_next_job(db_path)
        assert claimed is not None

        done = process_single_job(claimed, db_path=db_path, data_dir=data_dir, packs_dir=packs_dir)
        assert done is not None and done.status == JobStatus.SUCCEEDED, getattr(done, "error", None)
        assert len(seen_specs) == 1
        sp = seen_specs[0]
        assert sp.color_mode == ColorMode.NIGHT_RED
        assert sp.dem_source == DemSourceKind.CA_HRDEM
        assert sp.us_overlays == [USOverlayKind.US_TRAILS, USOverlayKind.US_PUBLIC_LANDS]
        assert sp.ca_overlays == [CAOverlayKind.CA_CANVEC_TRAILS]
        assert len(sp.rf_links) == 1 and sp.include_rf_link_card is True
        assert len(sp.tactical_graphics) == 1

        assert {a.kind for a in done.artifacts} == {"pdf", "gpkg", "sqlitedb", "zip"}
        pdf_art = next(a for a in done.artifacts if a.kind == "pdf")
        reader = PdfReader(str(data_dir / "v14job000001" / "artifacts" / pdf_art.filename))
        assert len(reader.pages) == 2  # Map sheet + RF Link Card!
        card_txt = re.sub(r"\s+", " ", reader.pages[1].extract_text() or "")
        assert "RF / LINE-OF-SIGHT LINK PROFILE" in card_txt
