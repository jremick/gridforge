"""Unit tests for Phase 4 FastAPI backend, SQLite job store, and worker."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader, PdfWriter
from pyproj import Transformer

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
from gridforge.geo.crs import compute_sheet_layout
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

    @pytest.mark.parametrize(
        "headers",
        [
            {"Origin": "http://localhost:5173"},
            {"Origin": "null"},
            {"Sec-Fetch-Site": "cross-site"},
            {"Sec-Fetch-Site": "same-site"},
        ],
    )
    def test_api_write_requires_matching_browser_origin(self, client_and_dirs, headers) -> None:
        client, *_ = client_and_dirs
        response = client.post("/api/jobs", json={"center": "10,10"}, headers=headers)
        assert response.status_code == 403
        assert client.get("/api/jobs").json()["jobs"] == []

        # New v1.3.0 write routes are equally protected by SameOriginWritesMiddleware
        assert (
            client.post("/api/packs/build", json={"region_or_bbox": "act"}, headers=headers).status_code
            == 403
        )
        assert (
            client.post(
                "/api/viewshed-preview",
                json={"observer": {"lat": -33.732, "lon": 150.312}},
                headers=headers,
            ).status_code
            == 403
        )
        assert client.delete("/api/packs/blue-mountains", headers=headers).status_code == 403

    def test_host_and_cors_defaults(self, client_and_dirs) -> None:
        client, *_ = client_and_dirs
        assert client.get("/api/health", headers={"Host": "[::1]:8080"}).status_code == 200
        assert client.get("/api/health", headers={"Host": "LOCALHOST:8080"}).status_code == 200
        assert client.get("/api/health", headers={"Host": "unlisted.example"}).status_code == 400
        resp = client.get("/api/health", headers={"Origin": "https://other.example"})
        assert "access-control-allow-origin" not in resp.headers

    def test_configured_hostname_works_behind_https_proxy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GRIDFORGE_ALLOWED_HOSTS", "studio.example")
        app = create_app(data_dir=tmp_path / "jobs", packs_dir=tmp_path / "packs")
        with TestClient(app, base_url="https://studio.example:443") as client:
            created = client.post(
                "/api/jobs",
                json={"center": "10,10"},
                headers={"Origin": "https://studio.example"},
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


class TestV11StripPlanAndRouteMetricsAPI:
    def test_strip_plan_multipart_and_json_endpoints(self, client_and_dirs) -> None:
        client, _, _, _ = client_and_dirs
        route_path = Path(__file__).resolve().parents[2] / "examples" / "katoomba_route.geojson"
        route_bytes = route_path.read_bytes()

        # 1. Multipart upload (Web Studio drag-and-drop flow)
        r_mp = client.post(
            "/api/strip-plan",
            data={"scale": "25000", "paper": "a4", "orientation": "portrait", "units": "metric"},
            files={"route_file": ("katoomba_route.geojson", route_bytes, "application/geo+json")},
        )
        assert r_mp.status_code == 200, r_mp.text
        body = r_mp.json()
        assert body["utm_epsg"] == 32756
        assert body["gzd"] == "56H"
        assert body["sheet_count"] >= 1
        assert body["sheets"][0]["sheet_id"] == "S1"
        assert body["route_geojson"]["type"] == "FeatureCollection"
        assert body["footprint_geojson"]["type"] == "FeatureCollection"
        assert body["grid_geojson"]["type"] == "FeatureCollection"

        # 2. Direct JSON body
        r_json = client.post(
            "/api/strip-plan",
            json={
                "route_geojson": json.loads(route_bytes.decode("utf-8")),
                "scale": 25000,
                "paper": "a4",
                "orientation": "portrait",
            },
        )
        assert r_json.status_code == 200
        assert r_json.json()["sheet_count"] == body["sheet_count"]

    def test_route_metrics_endpoint_and_no_qt_import(self, client_and_dirs) -> None:
        client, _, _, _ = client_and_dirs
        resp = client.post(
            "/api/route-metrics",
            json={
                "waypoints": [
                    {"label": "Katoomba", "lat": -33.7133, "lon": 150.3113, "category": "waypoint"},
                    {"label": "Echo Point", "lat": -33.7322, "lon": 150.3117, "category": "camp"},
                ],
                "units": "metric",
                "date_iso": "2026-06-15",
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert len(data["waypoints"]) == 2
        assert len(data["legs"]) == 1
        assert data["waypoints"][0]["code"] == "WP1"
        assert data["waypoints"][0]["mgrs_10m"].startswith("56H KH ")
        assert data["total_distance_km"] > 2.0
        assert 0.0 <= data["legs"][0]["mag_az_deg"] < 360.0
        assert "PyQt5" not in sys.modules
        assert "qgis" not in sys.modules


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
        self, client_and_dirs, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, data_dir, packs_dir, db_path = client_and_dirs

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

        rendered_layouts = []

        def _fake_prepare(spec, job_dir, **kwargs):
            return compute_sheet_layout(spec)

        def _fake_render_pdf(self, workspace, output_pdf: Path):
            rendered_layouts.append(workspace)
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
        preview = client.post("/api/footprint", json=json.loads(req_json))
        assert preview.status_code == 200
        features = preview.json()["footprint_geojson"]["features"]
        assert len(rendered_layouts) == len(features) == 4
        for layout, feature in zip(rendered_layouts, features, strict=True):
            assert layout.utm_epsg == feature["properties"]["utm_epsg"]
            project = Transformer.from_crs(4326, layout.utm_epsg, always_xy=True)
            ring = feature["geometry"]["coordinates"][0]
            xs, ys = project.transform(*zip(*ring))
            assert layout.utm_bbox.as_tuple() == pytest.approx(
                (min(xs), min(ys), max(xs), max(ys)), abs=0.02, rel=0
            )
        rendered_titles = [layout.spec.title or "" for layout in rendered_layouts]
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

    def test_process_strip_atlas_with_route_card_and_calibration(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from gridforge import __version__

        assert create_app(data_dir=tmp_path / "v_chk", packs_dir=tmp_path / "p_chk").version == __version__

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
                "title": "Strip With Supplements",
                "date_iso": "2026-06-15",
                "strip_sheets": [
                    {"sheet_id": "S1", "index": 1, "center_lat": -33.720, "center_lon": 150.311},
                    {"sheet_id": "S2", "index": 2, "center_lat": -33.740, "center_lon": 150.311},
                ],
                "waypoints": [
                    {"label": "Start", "lat": -33.720, "lon": 150.311, "category": "waypoint"},
                    {"label": "Finish", "lat": -33.740, "lon": 150.311, "category": "camp"},
                ],
                "include_route_card": True,
                "include_calibration_page": True,
                "outputs": {"pdf": True, "kmz": False, "mbtiles": False},
            }
        )
        db.create_job(
            db_path,
            job_id="stripjob0001",
            title="Strip With Supplements",
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

        def _fake_prepare(spec, job_dir, **kwargs):
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

        done = process_single_job(
            claimed,
            db_path=db_path,
            data_dir=data_dir,
            packs_dir=packs_dir,
        )
        assert done is not None
        assert done.status == JobStatus.SUCCEEDED
        assert len(done.artifacts) == 1
        assert "strip_2sheets.pdf" in done.artifacts[0].filename

        merged_pdf = data_dir / "stripjob0001" / "artifacts" / done.artifacts[0].filename
        reader = PdfReader(str(merged_pdf))
        # 2 strip map sheets + 1 Route Card + 1 100 mm Calibration page = 4 pages!
        assert len(reader.pages) == 4
        import re

        rc_text = re.sub(r"\s+", " ", reader.pages[2].extract_text() or "")
        cal_text = re.sub(r"\s+", " ", reader.pages[3].extract_text() or "")
        assert "TACTICAL ROUTE CARD" in rc_text
        assert "PRINTER SCALE VERIFICATION" in cal_text


@pytest.mark.parametrize(
    ("center", "cols", "rows"),
    [("45,6", 2, 1), ("0,3", 1, 2), ("60,3", 2, 1)],
)
def test_incompatible_atlas_projections_rejected_before_enqueue(
    client_and_dirs, center: str, cols: int, rows: int
) -> None:
    client, _, _, _ = client_and_dirs
    request = {"center": center, "grid_cols": cols, "grid_rows": rows}
    preview = client.post("/api/footprint", json=request)
    job = client.post("/api/jobs", json=request)
    assert preview.status_code == job.status_code == 422
    assert "Atlas sheet" in preview.json()["detail"]
    assert preview.json()["detail"] == job.json()["detail"]
    assert client.get("/api/jobs").json()["jobs"] == []

    # Device-only exports use the center sheet, regardless of the PDF grid setting.
    digital = client.post("/api/jobs", json={**request, "outputs": {"pdf": False, "kmz": True}})
    assert digital.status_code == 202


def test_atlas_across_latitude_band_keeps_native_page_labels(client_and_dirs) -> None:
    client, _, _, _ = client_and_dirs
    preview = client.post("/api/footprint", json={"center": "48,3", "grid_rows": 2})
    assert preview.status_code == 200
    pages = preview.json()["footprint_geojson"]["features"]
    assert [page["properties"]["gzd"] for page in pages] == ["31U", "31T"]
    assert all(page["properties"]["utm_epsg"] == 32631 for page in pages)


def test_worker_rechecks_previously_queued_incompatible_atlas(
    client_and_dirs, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, data_dir, packs_dir, db_path = client_and_dirs
    center = "45,6"
    db.create_job(
        db_path,
        job_id="atlaszone0001",
        title=None,
        center=center,
        center_mgrs=None,
        scale=25000,
        paper=PaperSize.A4,
        orientation=Orientation.PORTRAIT,
        color_mode=ColorMode.COLOR,
        grid_cols=2,
        request_json=json.dumps({"center": center, "grid_cols": 2}),
    )
    claimed = db.claim_next_job(db_path)
    assert claimed is not None
    monkeypatch.setattr(
        "gridforge.web.worker.runner.prepare_job_workspace",
        lambda *a, **kw: pytest.fail("An incompatible atlas must fail before preparing any page"),
    )
    job = process_single_job(claimed, db_path=db_path, data_dir=data_dir, packs_dir=packs_dir)
    assert job is not None and job.status == JobStatus.FAILED
    assert job.error is not None and "Atlas sheet" in job.error
    assert job.artifacts == []


@pytest.mark.parametrize("sign", [-1, 1])
def test_antimeridian_extents_refused_before_fetch(client_and_dirs, sign: int) -> None:
    client, _, _, _ = client_and_dirs
    for lon, cols in [(179.98, 1), (179.94, 3)]:
        # In the three-column case the center is safe but an outer page wraps.
        payload = {"center": f"45,{sign * lon}", "grid_cols": cols}
        for endpoint in ("/api/footprint", "/api/jobs"):
            response = client.post(endpoint, json=payload)
            assert response.status_code == 422
            assert "antimeridian" in response.json()["detail"]
    assert client.get("/api/jobs").json()["jobs"] == []
    nearby = client.post("/api/footprint", json={"center": f"45,{sign * 179.8}", "grid_cols": 3})
    assert nearby.status_code == 200
