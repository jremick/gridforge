"""Unit tests for Phase 4 FastAPI backend, SQLite job store, and worker."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader, PdfWriter

from gridforge.config import (
    BBox,
    ColorMode,
    ExportFormat,
    ExportResult,
    Orientation,
    PackManifest,
    PaperSize,
    RenderResult,
)
from gridforge.web import db
from gridforge.web.app import create_app
from gridforge.web.schemas import JobStatus
from gridforge.web.worker import process_single_job


def _seed_fake_pack(packs_dir: Path, pack_id: str = "blue-mountains") -> Path:
    pack_dir = packs_dir / pack_id
    pack_dir.mkdir(parents=True, exist_ok=True)
    (pack_dir / "dem.tif").write_bytes(b"fake-dem")
    (pack_dir / "osm.osm.pbf").write_bytes(b"fake-osm")
    manifest = PackManifest(
        pack_id=pack_id,
        name="Blue Mountains (NSW)",
        region="blue-mountains",
        bbox_wgs84=BBox(min_x=150.0, min_y=-34.05, max_x=150.7, max_y=-33.4),
        utm_zones=["56H"],
        dem_path="dem.tif",
        vertical_datum="AHD",
        osm_pbf_path="osm.osm.pbf",
        overlays={},
        attributions=["OpenStreetMap", "Geoscience Australia"],
        created_at="2026-11-26T08:00:00Z",
        size_bytes=1024,
    )
    (pack_dir / "pack.json").write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    return pack_dir


@pytest.fixture()
def client_and_dirs(tmp_path: Path) -> tuple[TestClient, Path, Path, Path]:
    data_dir = tmp_path / "jobs"
    packs_dir = tmp_path / "packs"
    web_dist = tmp_path / "dist"
    data_dir.mkdir()
    packs_dir.mkdir()
    app = create_app(
        data_dir=data_dir,
        packs_dir=packs_dir,
        web_dist_dir=web_dist,
        memory_budget_mb=3072,
    )
    client = TestClient(app, base_url="http://localhost:8080")
    return (client, data_dir, packs_dir, data_dir / "gridforge_jobs.sqlite3")


class TestStudioAccess:
    def test_same_origin_job_lifecycle(self, client_and_dirs) -> None:
        client, *_ = client_and_dirs
        headers = {"Origin": "http://localhost:8080", "Sec-Fetch-Site": "same-origin"}
        created = client.post(
            "/api/jobs",
            files={"payload": (None, json.dumps({"center": "10,10"}))},
            headers=headers,
        )
        assert created.status_code == 202
        job_url = "/api/jobs/" + created.json()["job_id"]
        assert client.post(job_url + "/cancel", headers=headers).json()["status"] == "cancelled"
        assert client.delete(job_url, headers=headers).status_code == 200

    @pytest.mark.parametrize("headers", [
        {"Origin": "http://localhost:5173"},
        {"Origin": "null"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
    ])
    def test_api_write_requires_matching_browser_origin(self, client_and_dirs, headers) -> None:
        client, *_ = client_and_dirs
        response = client.post("/api/jobs", json={"center": "10,10"}, headers=headers)
        assert response.status_code == 403
        assert client.get("/api/jobs").json()["jobs"] == []

    def test_host_and_cors_defaults(self, client_and_dirs) -> None:
        client, *_ = client_and_dirs
        assert client.get("/api/health", headers={"Host": "[::1]:8080"}).status_code == 200
        assert client.get("/api/health", headers={"Host": "LOCALHOST:8080"}).status_code == 200
        assert client.get("/api/health", headers={"Host": "unlisted.example"}).status_code == 400
        resp = client.get("/api/health", headers={"Origin": "https://other.example"})
        assert "access-control-allow-origin" not in resp.headers

    def test_configured_hostname_works_behind_https_proxy(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("GRIDFORGE_ALLOWED_HOSTS", "studio.example")
        app = create_app(data_dir=tmp_path / "jobs")
        with TestClient(app, base_url="https://studio.example:443") as client:
            created = client.post(
                "/api/jobs", json={"center": "10,10"}, headers={"Origin": "https://studio.example"}
            )
            assert created.status_code == 202


class TestWebMemoryHygieneAndHealth:
    def test_web_app_never_imports_qgis(self) -> None:
        assert "qgis" not in sys.modules
        assert "qgis.core" not in sys.modules

    def test_health_and_packs_endpoints(self, client_and_dirs) -> None:
        client, _, packs_dir, db_path = client_and_dirs

        resp = client.get("/api/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["queue_depth"] == 0
        assert body["worker_alive"] is False
        assert body["memory_budget_mb"] == 3072
        assert body["packs_count"] == 0

        # Touch worker heartbeat & install a pack -> reflected immediately
        db.touch_heartbeat(db_path, worker_id="worker-test", pid=1234)
        _seed_fake_pack(packs_dir, "blue-mountains")

        resp2 = client.get("/api/health")
        assert resp2.status_code == 200
        body2 = resp2.json()
        assert body2["worker_alive"] is True
        assert body2["worker_last_seen"] is not None
        assert body2["packs_count"] == 1

        packs_resp = client.get("/api/packs")
        assert packs_resp.status_code == 200
        packs = packs_resp.json()["packs"]
        assert len(packs) == 1
        assert packs[0]["pack_id"] == "blue-mountains"
        assert packs[0]["vertical_datum"] == "AHD"
        assert packs[0]["utm_zones"] == ["56H"]


class TestFootprintEndpoint:
    @pytest.mark.parametrize(
        ("center", "scale", "paper", "units", "exp_epsg", "exp_gzd"),
        [
            ("-33.732, 150.312", 25000, "a4", "metric", 32756, "56H"),
            ("-25.3444, 131.0369", 25000, "a4", "metric", 32752, "52J"),
            ("40.005, -105.285", 24000, "letter", "imperial", 32613, "13T"),
            ("56H KH 510 640", 25000, "a4", "metric", 32756, "56H"),
        ],
    )
    def test_footprint_blue_mountains_uluru_boulder(
        self,
        client_and_dirs,
        center: str,
        scale: int,
        paper: str,
        units: str,
        exp_epsg: int,
        exp_gzd: str,
    ) -> None:
        client, _, _, _ = client_and_dirs
        resp = client.post(
            "/api/footprint",
            json={
                "center": center,
                "scale": scale,
                "paper": paper,
                "orientation": "portrait",
                "units": units,
                "date_iso": "2026-06-15",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["utm_epsg"] == exp_epsg
        assert data["gzd"] == exp_gzd
        assert data["grid_spacing_mm"] == pytest.approx(1000.0 / scale * 1000.0, abs=0.01)
        assert data["ground_width_km"] > 2.0
        assert data["ground_height_km"] > 3.0
        assert len(data["square_ids_100km"]) >= 1
        assert data["declination"]["model_name"] == "WMM2025"

        # Validate tilted footprint GeoJSON polygon
        fp = data["footprint_geojson"]
        assert fp["type"] == "FeatureCollection"
        assert len(fp["features"]) == 1
        ring = fp["features"][0]["geometry"]["coordinates"][0]
        assert ring[0] == ring[-1]
        assert len(ring) >= 9

        # Validate interior 1 km MGRS preview grid lines
        grid = data["grid_geojson"]
        assert grid["type"] == "FeatureCollection"
        axes = {f["properties"]["axis"] for f in grid["features"]}
        assert axes == {"E", "N"}

    def test_footprint_rejects_polar_coordinates(self, client_and_dirs) -> None:
        client, _, _, _ = client_and_dirs
        resp = client.post("/api/footprint", json={"center": "85.0, 0.0"})
        assert resp.status_code == 422

    def test_footprint_2x2_edge_matched_utm_atlas(self, client_and_dirs) -> None:
        client, _, _, _ = client_and_dirs
        resp = client.post(
            "/api/footprint",
            json={
                "center": "-33.732, 150.312",
                "scale": 25000,
                "paper": "a4",
                "orientation": "portrait",
                "units": "metric",
                "grid_cols": 2,
                "grid_rows": 2,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["grid_cols"] == 2
        assert data["grid_rows"] == 2
        assert data["sheet_count"] == 4
        assert data["total_ground_width_km"] == pytest.approx(data["ground_width_km"] * 2.0, abs=0.01)
        assert data["total_ground_height_km"] == pytest.approx(data["ground_height_km"] * 2.0, abs=0.01)

        feats = data["footprint_geojson"]["features"]
        assert len(feats) == 4
        by_id = {f["properties"]["sheet_id"]: f for f in feats}
        assert set(by_id.keys()) == {"A1", "A2", "B1", "B2"}

        # Edge-match invariant: A1's SE corner matches A2's SW corner and B1's NE corner!
        # Ring layout (edge_steps=8): idx 0=SW, 8=SE, 16=NE, 24=NW
        a1_ring = by_id["A1"]["geometry"]["coordinates"][0]
        a2_ring = by_id["A2"]["geometry"]["coordinates"][0]
        b1_ring = by_id["B1"]["geometry"]["coordinates"][0]
        b2_ring = by_id["B2"]["geometry"]["coordinates"][0]
        assert a1_ring[8] == a2_ring[0]  # A1 SE == A2 SW
        assert a1_ring[16] == a2_ring[24]  # A1 NE == A2 NW
        assert a1_ring[0] == b1_ring[24]  # A1 SW == B1 NW
        assert a1_ring[8] == b1_ring[16] == b2_ring[24]  # Central 4-way corner exact!

        # Combined 1 km MGRS preview grid covers full 2x2 atlas (~9.3 x 11.75 km)
        e_lines = [f for f in data["grid_geojson"]["features"] if f["properties"]["axis"] == "E"]
        n_lines = [f for f in data["grid_geojson"]["features"] if f["properties"]["axis"] == "N"]
        assert len(e_lines) >= 9
        assert len(n_lines) >= 11


class TestGeocodeEndpoint:
    def test_geocode_mock_cache_and_offline_fallback(
        self, client_and_dirs, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from gridforge.web import app as app_mod

        app_mod._geocode_cache.clear()
        client, _, _, _ = client_and_dirs
        calls = {"n": 0}

        class _FakeResp:
            def raise_for_status(self) -> None:
                return None

            def json(self):
                return [
                    {
                        "name": "Mount Kosciuszko",
                        "display_name": "Mount Kosciuszko, Snowy Monaro, New South Wales, Australia",
                        "lat": "-36.4559",
                        "lon": "148.2636",
                        "type": "peak",
                    },
                    {
                        "name": "North Pole",
                        "display_name": "North Pole, Arctic",
                        "lat": "89.9",
                        "lon": "0.0",
                        "type": "place",
                    },
                ]

        def _fake_get(url, **kwargs):
            calls["n"] += 1
            assert "User-Agent" in kwargs.get("headers", {})
            return _FakeResp()

        monkeypatch.setattr("gridforge.web.app.httpx.get", _fake_get)

        r1 = client.get("/api/geocode", params={"q": "Kosciuszko", "limit": 5})
        assert r1.status_code == 200
        results = r1.json()["results"]
        assert len(results) == 1  # Polar result filtered out!
        assert results[0]["short_name"] == "Mount Kosciuszko"
        assert results[0]["lat"] == pytest.approx(-36.4559)
        assert results[0]["lon"] == pytest.approx(148.2636)
        assert results[0]["type"] == "peak"

        # Second identical lookup hits in-memory cache (no second HTTP call)
        r2 = client.get("/api/geocode", params={"q": "kosciuszko", "limit": 5})
        assert r2.status_code == 200
        assert len(r2.json()["results"]) == 1
        assert calls["n"] == 1

        # Offline / network failure returns clean 200 empty results (never 500)
        def _offline_get(url, **kwargs):
            raise ConnectionError("network unreachable")

        monkeypatch.setattr("gridforge.web.app.httpx.get", _offline_get)
        r_off = client.get("/api/geocode", params={"q": "Freycinet"})
        assert r_off.status_code == 200
        assert r_off.json() == {"results": []}

        # Short query returns empty without network call
        r_short = client.get("/api/geocode", params={"q": "a"})
        assert r_short.status_code == 200
        assert r_short.json() == {"results": []}


class TestJobsLifecycleAndSecurity:
    def test_create_list_cancel_delete_job_json(self, client_and_dirs) -> None:
        client, _, _, _ = client_and_dirs
        payload = {
            "center": "-33.732, 150.312",
            "scale": 25000,
            "paper": "a4",
            "orientation": "portrait",
            "color_mode": "color",
            "units": "metric",
            "title": "Katoomba Test",
            "au_overlays": ["au-hydro", "au-parks"],
            "outputs": {"pdf": True, "kmz": True, "include_grid_on_digital": True},
        }
        create_resp = client.post("/api/jobs", json=payload)
        assert create_resp.status_code == 202
        job = create_resp.json()
        job_id = job["job_id"]
        assert job["status"] == "queued"
        assert job["title"] == "Katoomba Test"
        assert job["center_mgrs"].startswith("56H KH ")

        # Appears in list & health queue_depth
        list_resp = client.get("/api/jobs")
        assert list_resp.status_code == 200
        assert any(j["job_id"] == job_id for j in list_resp.json()["jobs"])
        assert client.get("/api/health").json()["queue_depth"] == 1

        # Cancel job
        cancel_resp = client.post(f"/api/jobs/{job_id}/cancel")
        assert cancel_resp.status_code == 200
        assert cancel_resp.json()["status"] == "cancelled"

        # Delete job
        del_resp = client.delete(f"/api/jobs/{job_id}")
        assert del_resp.status_code == 200
        assert client.get(f"/api/jobs/{job_id}").status_code == 404

    def test_create_job_multipart_with_overlay_upload(self, client_and_dirs) -> None:
        client, data_dir, packs_dir, _ = client_and_dirs
        _seed_fake_pack(packs_dir, "blue-mountains")

        payload = {
            "center": "-33.732, 150.312",
            "scale": 25000,
            "paper": "a4",
            "orientation": "portrait",
            "color_mode": "bw",
            "units": "metric",
            "pack_id": "blue-mountains",
            "outputs": {"pdf": True},
        }
        geojson_bytes = json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"name": "LZ Alpha"},
                        "geometry": {"type": "Point", "coordinates": [150.312, -33.732]},
                    }
                ],
            }
        ).encode("utf-8")

        resp = client.post(
            "/api/jobs",
            data={"payload": json.dumps(payload)},
            files={"overlay_file": ("../evil/route.geojson", geojson_bytes, "application/geo+json")},
        )
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]
        saved = data_dir / job_id / "uploads" / "route.geojson"
        assert saved.is_file()
        assert saved.read_bytes() == geojson_bytes

        # Also accept urlencoded form data with 'payload' (no file attached)
        form_resp = client.post("/api/jobs", data={"payload": json.dumps(payload)})
        assert form_resp.status_code == 202
        assert form_resp.json()["status"] == "queued"

        # Reject disallowed upload extension
        bad_ext = client.post(
            "/api/jobs",
            data={"payload": json.dumps(payload)},
            files={"overlay_file": ("script.sh", b"#!/bin/sh", "text/plain")},
        )
        assert bad_ext.status_code == 400

    def test_artifact_download_and_path_traversal_blocked(self, client_and_dirs) -> None:
        client, data_dir, _, _ = client_and_dirs
        create_resp = client.post(
            "/api/jobs",
            json={"center": "-33.732, 150.312", "outputs": {"pdf": True}},
        )
        job_id = create_resp.json()["job_id"]

        art_dir = data_dir / job_id / "artifacts"
        art_dir.mkdir(parents=True, exist_ok=True)
        (art_dir / "sample.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
        (data_dir / "secret.txt").write_text("do-not-leak", encoding="utf-8")

        ok = client.get(f"/api/jobs/{job_id}/artifacts/sample.pdf")
        assert ok.status_code == 200
        assert ok.headers["content-type"].startswith("application/pdf")
        assert ok.content.startswith(b"%PDF-1.4")

        ok_inline = client.get(f"/api/jobs/{job_id}/artifacts/sample.pdf?inline=1")
        assert ok_inline.status_code == 200
        assert "inline" in ok_inline.headers.get("content-disposition", "")

        # Encoded and unencoded path traversal attempts must all be rejected cleanly
        for attack in (
            f"/api/jobs/{job_id}/artifacts/..%2F..%2Fsecret.txt",
            f"/api/jobs/{job_id}/artifacts/..%5Csecret.txt",
            f"/api/jobs/{job_id}/artifacts/subdir/sample.pdf",
            "/api/jobs/../../etc/artifacts/passwd",
        ):
            r = client.get(attack)
            assert r.status_code in (400, 404)
            assert "do-not-leak" not in r.text


class TestSQLiteStoreAndWorker:
    def test_atomic_fifo_claim_and_orphan_recovery(self, tmp_path: Path) -> None:
        db_path = tmp_path / "jobs.sqlite3"
        db.init_db(db_path)

        for idx in range(2):
            db.create_job(
                db_path,
                job_id=f"job0000000{idx}",
                title=f"Job {idx}",
                center="-33.732, 150.312",
                center_mgrs="56H KH 5095 6431",
                scale=25000,
                paper=PaperSize.A4,
                orientation=Orientation.PORTRAIT,
                color_mode=ColorMode.COLOR,
                request_json="{}",
            )

        first = db.claim_next_job(db_path)
        assert first is not None and first.record.job_id == "job00000000"
        assert first.record.status == JobStatus.RUNNING

        second = db.claim_next_job(db_path)
        assert second is not None and second.record.job_id == "job00000001"

        assert db.claim_next_job(db_path) is None

        # Simulate worker restart recovery
        recovered = db.reset_orphaned_running_jobs(db_path)
        assert recovered == 2
        assert db.get_job(db_path, "job00000000").status == JobStatus.FAILED

    def test_process_single_job_produces_artifacts_and_cleans_workspace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        data_dir = tmp_path / "jobs"
        packs_dir = tmp_path / "packs"
        db_path = data_dir / "gridforge_jobs.sqlite3"
        db.init_db(db_path)
        packs_dir.mkdir(parents=True)

        req_json = json.dumps(
            {
                "center": "-33.732, 150.312",
                "scale": 25000,
                "paper": "a4",
                "orientation": "portrait",
                "color_mode": "color",
                "units": "metric",
                "title": "Worker Mock Test",
                "outputs": {"pdf": True, "kmz": True, "mbtiles": False},
            }
        )
        db.create_job(
            db_path,
            job_id="workerjob0001",
            title="Worker Mock Test",
            center="-33.732, 150.312",
            center_mgrs="56H KH 5095 6431",
            scale=25000,
            paper=PaperSize.A4,
            orientation=Orientation.PORTRAIT,
            color_mode=ColorMode.COLOR,
            request_json=req_json,
        )
        claimed = db.claim_next_job(db_path)
        assert claimed is not None

        seen_work_dirs: list[Path] = []

        def _fake_prepare(spec, job_dir, **kwargs):
            seen_work_dirs.append(Path(job_dir))
            if "progress_callback" in kwargs and kwargs["progress_callback"]:
                kwargs["progress_callback"]("terrain", 35)
            return object()

        def _fake_render_pdf(self, workspace, output_pdf: Path):
            output_pdf.write_bytes(b"%PDF-1.4\nfake-pdf\n%%EOF\n")
            return RenderResult(
                output_pdf=output_pdf,
                page_width_mm=210.0,
                page_height_mm=297.0,
                scale=25000,
                utm_epsg=32756,
                elapsed_seconds=0.1,
            )

        def _fake_kmz(workspace, output_kmz: Path, **kwargs):
            output_kmz.write_bytes(b"PK\x03\x04fake-kmz")
            return ExportResult(
                format=ExportFormat.KMZ,
                output_path=output_kmz,
                file_size_bytes=14,
                tile_count=1,
                elapsed_seconds=0.1,
            )

        monkeypatch.setattr("gridforge.web.worker.runner.prepare_job_workspace", _fake_prepare)
        monkeypatch.setattr("gridforge.web.worker.PyQGISRenderer.render_pdf", _fake_render_pdf)
        monkeypatch.setattr("gridforge.web.worker.export_garmin_kmz", _fake_kmz)

        done = process_single_job(
            claimed,
            db_path=db_path,
            data_dir=data_dir,
            packs_dir=packs_dir,
        )
        assert done is not None
        assert done.status == JobStatus.SUCCEEDED
        assert done.progress_pct == 100
        assert done.elapsed_seconds is not None
        assert done.peak_rss_mb is not None and done.peak_rss_mb > 0
        assert {a.kind for a in done.artifacts} == {"pdf", "kmz"}
        # Temp workspace must be reclaimed!
        assert len(seen_work_dirs) == 1
        assert not seen_work_dirs[0].exists()

    def test_process_multi_sheet_2x2_atlas_merges_4_pages(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        data_dir = tmp_path / "jobs"
        packs_dir = tmp_path / "packs"
        db_path = data_dir / "gridforge_jobs.sqlite3"
        db.init_db(db_path)
        packs_dir.mkdir(parents=True)

        req_json = json.dumps(
            {
                "center": "-33.732, 150.312",
                "scale": 25000,
                "paper": "a4",
                "orientation": "portrait",
                "color_mode": "color",
                "units": "metric",
                "title": "Blue Mountains Atlas",
                "grid_cols": 2,
                "grid_rows": 2,
                "outputs": {"pdf": True, "kmz": False, "mbtiles": False},
            }
        )
        db.create_job(
            db_path,
            job_id="atlasjob0001",
            title="Blue Mountains Atlas",
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
        assert claimed.record.grid_cols == 2 and claimed.record.grid_rows == 2

        rendered_titles: list[str] = []

        def _fake_prepare(spec, job_dir, **kwargs):
            rendered_titles.append(spec.title or "")
            return spec

        def _fake_render_pdf(self, workspace, output_pdf: Path):
            # Write a real valid 1-page A4 PDF so pypdf merges cleanly
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

        done = process_single_job(
            claimed,
            db_path=db_path,
            data_dir=data_dir,
            packs_dir=packs_dir,
        )
        assert done is not None
        assert done.status == JobStatus.SUCCEEDED
        assert len(rendered_titles) == 4
        assert "Sheet A1 (1/4)" in rendered_titles[0]
        assert "Sheet A2 (2/4)" in rendered_titles[1]
        assert "Sheet B1 (3/4)" in rendered_titles[2]
        assert "Sheet B2 (4/4)" in rendered_titles[3]
        assert len(done.artifacts) == 1
        assert done.artifacts[0].kind == "pdf"
        assert "atlas_2x2.pdf" in done.artifacts[0].filename

        merged_pdf = data_dir / "atlasjob0001" / "artifacts" / done.artifacts[0].filename
        reader = PdfReader(str(merged_pdf))
        assert len(reader.pages) == 4
