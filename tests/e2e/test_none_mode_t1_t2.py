"""Opaque-box E2E tests (Tiers 1 & 2) for GridForge `GRIDFORGE_AUTH_MODE=none` & default mode.

Covers zero-login homelab/local Docker compatibility:
  - Baseline tests (runnable pre-M1 and post-M1) verify default unset & explicit
    `GRIDFORGE_AUTH_MODE=none` behavior across jobs, projects, packs, previews,
    geocoding, path traversal, size caps, and same-origin/host guards.
  - `@pytest.mark.m1_contract` tests (`*_m1_contract` suffix) assert the M1
    `/api/auth/*` and `/api/admin/*` contract in `none` mode.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from gridforge.config import BBox, PackManifest
from tests.e2e.conftest import same_origin_headers

_SAME_ORIGIN = same_origin_headers("http://localhost:8080")


# =============================================================================
# Tier 1: Nominal / Happy Paths in `none` mode (7 Baseline + 2 M1-Contract)
# =============================================================================


class TestNoneModeTier1:
    """Tier 1 happy-path tests for `GRIDFORGE_AUTH_MODE=none` and unset default."""

    def test_t1_none_unset_default_health_and_zero_login_jobs_lifecycle(self, e2e_env) -> None:
        app = e2e_env.build_app(auth_mode=None, web_dist_with_index=False)
        client = e2e_env.new_client(app)

        root_resp = client.get("/")
        assert root_resp.status_code == 200
        assert root_resp.json() == {
            "service": "gridforge",
            "status": "ok",
            "frontend_built": False,
            "docs": "/docs",
        }

        health = client.get("/api/health")
        assert health.status_code == 200
        hdata = health.json()
        assert hdata["status"] == "ok"
        assert hdata["queue_depth"] == 0
        assert hdata["worker_alive"] is False
        assert hdata["packs_count"] == 0

        created = client.post(
            "/api/jobs",
            json={
                "center": "-33.732, 150.312",
                "scale": 25000,
                "paper": "a4",
                "orientation": "portrait",
                "color_mode": "color",
                "units": "metric",
                "title": "E2E Katoomba",
                "outputs": {"pdf": True, "kmz": True},
            },
            headers=_SAME_ORIGIN,
        )
        assert created.status_code == 202, created.text
        job = created.json()
        job_id = job["job_id"]
        assert job["status"] == "queued"
        assert job["center_mgrs"].startswith("56H KH ")

        listed = client.get("/api/jobs")
        assert listed.status_code == 200
        assert any(j["job_id"] == job_id for j in listed.json()["jobs"])

        fetched = client.get(f"/api/jobs/{job_id}")
        assert fetched.status_code == 200
        assert fetched.json()["job_id"] == job_id

        cancelled = client.post(f"/api/jobs/{job_id}/cancel", headers=_SAME_ORIGIN)
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"

        deleted = client.delete(f"/api/jobs/{job_id}", headers=_SAME_ORIGIN)
        assert deleted.status_code == 200
        assert deleted.json() == {"deleted": True, "job_id": job_id}
        assert client.get(f"/api/jobs/{job_id}").status_code == 404
        assert "qgis" not in sys.modules

    def test_t1_none_explicit_env_projects_full_crud(self, e2e_env) -> None:
        app = e2e_env.build_app(auth_mode="none")
        client = e2e_env.new_client(app)

        created = client.post(
            "/api/projects",
            json={
                "name": "Alpine Recon",
                "notes": "Day 1",
                "state": {"center": "-33.732, 150.312", "color_mode": "alpine"},
            },
            headers=_SAME_ORIGIN,
        )
        assert created.status_code == 201, created.text
        proj = created.json()
        pid = proj["project_id"]
        assert proj["id"] == pid
        assert proj["description"] == "Day 1"
        assert proj["notes"] == "Day 1"

        listed = client.get("/api/projects")
        assert listed.status_code == 200
        assert any(p["project_id"] == pid for p in listed.json()["projects"])

        fetched = client.get(f"/api/projects/{pid}")
        assert fetched.status_code == 200
        assert fetched.json()["name"] == "Alpine Recon"

        patched = client.patch(
            f"/api/projects/{pid}",
            json={"name": "Alpine Recon v2", "notes": "Updated"},
            headers=_SAME_ORIGIN,
        )
        assert patched.status_code == 200
        assert patched.json()["name"] == "Alpine Recon v2"
        assert patched.json()["notes"] == "Updated"
        assert patched.json()["description"] == "Updated"

        deleted = client.delete(f"/api/projects/{pid}", headers=_SAME_ORIGIN)
        assert deleted.status_code == 200
        assert deleted.json()["deleted"] is True
        assert client.get(f"/api/projects/{pid}").status_code == 404

    def test_t1_none_3x3_atlas_strip_and_digital_job_payloads(self, e2e_env) -> None:
        app = e2e_env.build_app(auth_mode="none")
        client = e2e_env.new_client(app)

        atlas = client.post(
            "/api/jobs",
            json={
                "center": "-33.732, 150.312",
                "grid_cols": 3,
                "grid_rows": 3,
                "include_atlas_index": True,
                "outputs": {"pdf": True},
            },
            headers=_SAME_ORIGIN,
        )
        assert atlas.status_code == 202, atlas.text
        assert atlas.json()["grid_cols"] == 3
        assert atlas.json()["grid_rows"] == 3

        strip = client.post(
            "/api/jobs",
            json={
                "strip_sheets": [
                    {"sheet_id": "S1", "center_lat": -33.720, "center_lon": 150.311},
                    {"sheet_id": "S2", "center_lat": -33.740, "center_lon": 150.311},
                ],
                "outputs": {"pdf": True},
            },
            headers=_SAME_ORIGIN,
        )
        assert strip.status_code == 202, strip.text

        digital = client.post(
            "/api/jobs",
            json={
                "center": "-33.732, 150.312",
                "outputs": {
                    "pdf": False,
                    "mbtiles": True,
                    "kmz": True,
                    "gpkg_tiles": True,
                    "osmand_sqlitedb": True,
                    "field_bundle_zip": True,
                },
            },
            headers=_SAME_ORIGIN,
        )
        assert digital.status_code == 202, digital.text

    def test_t1_none_multipart_overlay_upload_artifact_and_thumbnail(
        self, e2e_env, synthetic_fixtures
    ) -> None:
        app = e2e_env.build_app(auth_mode="none")
        client = e2e_env.new_client(app)

        overlay_bytes = synthetic_fixtures.overlay_geojson.read_bytes()
        created = client.post(
            "/api/jobs",
            data={"payload": json.dumps({"center": "-33.732, 150.312", "outputs": {"pdf": True}})},
            files={"overlay_file": ("overlay.geojson", overlay_bytes, "application/geo+json")},
            headers=_SAME_ORIGIN,
        )
        assert created.status_code == 202, created.text
        job_id = created.json()["job_id"]

        uploaded_path = e2e_env.data_dir / job_id / "uploads" / "overlay.geojson"
        assert uploaded_path.is_file()
        assert uploaded_path.read_bytes() == overlay_bytes

        e2e_env.seed_completed_job_pdf(job_id, filename="map.pdf")

        dl = client.get(f"/api/jobs/{job_id}/artifacts/map.pdf")
        assert dl.status_code == 200
        assert dl.headers["content-type"].startswith("application/pdf")
        assert dl.content.startswith(b"%PDF-")

        inline = client.get(f"/api/jobs/{job_id}/artifacts/map.pdf?inline=1")
        assert inline.status_code == 200
        assert "inline" in inline.headers.get("content-disposition", "").lower()

        thumb = client.get(f"/api/jobs/{job_id}/thumbnail.png")
        assert thumb.status_code == 200
        assert thumb.headers["content-type"].startswith("image/png")
        assert thumb.content.startswith(b"\x89PNG\r\n\x1a\n")

    def test_t1_none_packs_build_regions_and_delete(self, e2e_env) -> None:
        def _stub_pack_builder(region_or_bbox: str, out_dir: Path, **kwargs: Any) -> PackManifest:
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
            return manifest

        app = e2e_env.build_app(auth_mode="none", pack_builder=_stub_pack_builder)
        client = e2e_env.new_client(app)

        regions = client.get("/api/packs/regions")
        assert regions.status_code == 200
        assert len(regions.json()["regions"]) == 29

        build = client.post(
            "/api/packs/build",
            json={"region_or_bbox": "act", "dem_tier": "srtm-1s"},
            headers=_SAME_ORIGIN,
        )
        assert build.status_code == 202, build.text
        task_id = build.json()["task_id"]

        deadline = time.monotonic() + 5.0
        final: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            poll = client.get(f"/api/packs/tasks/{task_id}")
            assert poll.status_code == 200
            final = poll.json()
            if final["status"] in ("succeeded", "failed"):
                break
            time.sleep(0.02)
        assert final is not None and final["status"] == "succeeded"

        packs = client.get("/api/packs")
        assert packs.status_code == 200
        assert any(p["pack_id"] == "act" for p in packs.json()["packs"])

        deleted = client.delete("/api/packs/act", headers=_SAME_ORIGIN)
        assert deleted.status_code == 200
        assert deleted.json() == {"deleted": True, "pack_id": "act"}

    def test_t1_none_footprint_route_metrics_viewshed_and_rf_link(
        self, e2e_env, mock_preview_dem
    ) -> None:
        _ = mock_preview_dem
        app = e2e_env.build_app(auth_mode="none")
        client = e2e_env.new_client(app)

        fp = client.post(
            "/api/footprint",
            json={
                "center": "-33.732, 150.312",
                "scale": 25000,
                "paper": "a4",
                "grid_cols": 2,
                "grid_rows": 2,
            },
            headers=_SAME_ORIGIN,
        )
        assert fp.status_code == 200, fp.text
        fp_data = fp.json()
        assert fp_data["utm_epsg"] == 32756
        assert fp_data["sheet_count"] == 4
        assert fp_data["declination"]["model_name"] == "WMM2025"

        route = client.post(
            "/api/route-metrics",
            json={
                "waypoints": [
                    {"label": "Echo Point", "lat": -33.732, "lon": 150.312},
                    {"label": "Narrow Neck", "lat": -33.745, "lon": 150.295},
                ]
            },
            headers=_SAME_ORIGIN,
        )
        assert route.status_code == 200, route.text
        assert len(route.json()["legs"]) == 1
        assert route.json()["total_distance_km"] > 0

        vs = client.post(
            "/api/viewshed-preview",
            json={
                "observer": {
                    "label": "Obs",
                    "lat": -33.732,
                    "lon": 150.312,
                    "max_distance_m": 5000.0,
                }
            },
            headers=_SAME_ORIGIN,
        )
        assert vs.status_code == 200, vs.text
        vs_data = vs.json()
        assert vs_data["utm_epsg"] == 32756
        assert "viewshed_geojson" in vs_data
        assert "range_ring_geojson" in vs_data

        rf = client.post(
            "/api/rf-link-profile",
            json={
                "from_lat": -33.732,
                "from_lon": 150.312,
                "to_lat": -33.702,
                "to_lon": 150.342,
                "freq_mhz": 915.0,
            },
            headers=_SAME_ORIGIN,
        )
        assert rf.status_code == 200, rf.text
        assert rf.json()["status"] in {"CLEAR", "MARGINAL", "OBSTRUCTED"}

    def test_t1_none_geocode_uses_mocked_nominatim_and_caches(
        self, e2e_env, mock_nominatim
    ) -> None:
        app = e2e_env.build_app(auth_mode="none")
        client = e2e_env.new_client(app)

        r1 = client.get("/api/geocode", params={"q": "Katoomba"})
        assert r1.status_code == 200
        results = r1.json()["results"]
        assert len(results) == 1
        assert results[0]["short_name"] == "Katoomba"
        assert len(mock_nominatim.nominatim_calls) == 1

        # Second identical query served from _geocode_cache without outbound HTTP
        r2 = client.get("/api/geocode", params={"q": "Katoomba"})
        assert r2.status_code == 200
        assert len(mock_nominatim.nominatim_calls) == 1

    @pytest.mark.m1_contract
    def test_t1_none_auth_session_unset_returns_unrestricted_none_role_m1_contract(
        self, e2e_env
    ) -> None:
        app = e2e_env.build_app(auth_mode=None)
        client = e2e_env.new_client(app)
        resp = client.get("/api/auth/session")
        assert resp.status_code == 200
        body = resp.json()
        assert body["auth_mode"] == "none"
        assert body["authenticated"] is False
        assert body["user"] is None
        assert body["role"] == "none"
        assert body["capabilities"]["max_grid_cells"] == 9
        assert body["capabilities"]["strip_atlas_allowed"] is True
        assert body["capabilities"]["packs_manage_allowed"] is True
        assert body["capabilities"]["turnstile_required"] is False

    @pytest.mark.m1_contract
    def test_t1_none_auth_session_explicit_none_returns_unrestricted_none_role_m1_contract(
        self, e2e_env
    ) -> None:
        app = e2e_env.build_app(auth_mode="none")
        client = e2e_env.new_client(app)
        resp = client.get("/api/auth/session")
        assert resp.status_code == 200
        body = resp.json()
        assert body["auth_mode"] == "none"
        assert body["role"] == "none"
        assert body["quotas"]["daily_renders_limit"] is None
        assert body["quotas"]["hourly_previews_limit"] is None


# =============================================================================
# Tier 2: Boundaries, Validation, Traversal, Size Caps & Error Paths (6 Baseline + 1 M1-Contract)
# =============================================================================


class TestNoneModeTier2:
    """Tier 2 validation, security, and error-handling tests in `none` mode."""

    def test_t2_none_invalid_payloads_return_422(self, e2e_env) -> None:
        app = e2e_env.build_app(auth_mode="none")
        client = e2e_env.new_client(app)

        assert client.post("/api/jobs", json={}, headers=_SAME_ORIGIN).status_code == 422
        assert (
            client.post(
                "/api/jobs",
                json={"center": "-33.732, 150.312", "scale": 12345},
                headers=_SAME_ORIGIN,
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/jobs",
                json={"center": "-33.732, 150.312", "grid_cols": 4},
                headers=_SAME_ORIGIN,
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/jobs",
                json={"center": "-33.732, 150.312", "outputs": {"pdf": False}},
                headers=_SAME_ORIGIN,
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/jobs",
                json={"center": "85.0, 0.0"},
                headers=_SAME_ORIGIN,
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/jobs",
                json={"center": "45,6", "grid_cols": 2, "grid_rows": 1},
                headers=_SAME_ORIGIN,
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/footprint",
                json={"center": "85.0, 0.0"},
                headers=_SAME_ORIGIN,
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/projects",
                json={"name": "", "state": {}},
                headers=_SAME_ORIGIN,
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/packs/build",
                json={"region_or_bbox": "atlantis"},
                headers=_SAME_ORIGIN,
            ).status_code
            == 422
        )

    def test_t2_none_missing_and_malformed_ids_return_404_and_400(self, e2e_env) -> None:
        app = e2e_env.build_app(auth_mode="none", web_dist_with_index=True)
        client = e2e_env.new_client(app)

        assert client.get("/api/jobs/nonexistent1234").status_code == 404
        assert client.post("/api/jobs/nonexistent1234/cancel", headers=_SAME_ORIGIN).status_code == 404
        assert client.delete("/api/jobs/nonexistent1234", headers=_SAME_ORIGIN).status_code == 404
        assert client.get("/api/jobs/nonexistent1234/thumbnail.png").status_code == 404

        assert client.get("/api/projects/nonexistent1234").status_code == 404
        assert (
            client.patch(
                "/api/projects/nonexistent1234",
                json={"name": "Nope"},
                headers=_SAME_ORIGIN,
            ).status_code
            == 404
        )
        assert client.delete("/api/projects/nonexistent1234", headers=_SAME_ORIGIN).status_code == 404

        assert client.get("/api/packs/tasks/nonexistent1234").status_code == 404
        assert client.delete("/api/packs/nonexistent", headers=_SAME_ORIGIN).status_code == 404

        assert client.get("/api/jobs/bad!id").status_code == 400
        assert client.get("/api/projects/bad!id").status_code == 400
        assert client.delete("/api/packs/bad..id", headers=_SAME_ORIGIN).status_code == 400

        # Built SPA fallback MUST NOT swallow unknown /api/* routes with 200 index.html
        unknown_api = client.get("/api/does-not-exist")
        assert unknown_api.status_code == 404

    def test_t2_none_path_traversal_blocked_on_artifacts_and_packs(self, e2e_env) -> None:
        app = e2e_env.build_app(auth_mode="none")
        client = e2e_env.new_client(app)

        created = client.post(
            "/api/jobs",
            json={"center": "-33.732, 150.312"},
            headers=_SAME_ORIGIN,
        )
        assert created.status_code == 202
        job_id = created.json()["job_id"]

        (e2e_env.data_dir / "secret.txt").write_text("do-not-leak", encoding="utf-8")
        outside_dir = e2e_env.root / "outside_dir"
        outside_dir.mkdir(parents=True, exist_ok=True)
        (outside_dir / "keep.txt").write_text("safe", encoding="utf-8")

        for probe in ("..%2F..%2Fsecret.txt", "..%5Csecret.txt", "subdir/map.pdf"):
            resp = client.get(f"/api/jobs/{job_id}/artifacts/{probe}")
            assert resp.status_code in (400, 404)
            assert "do-not-leak" not in resp.text

        for probe in ("..%2Foutside_dir", "bad..id", "a/b"):
            resp = client.delete(f"/api/packs/{probe}", headers=_SAME_ORIGIN)
            assert resp.status_code in (400, 404, 405)

        assert (outside_dir / "keep.txt").is_file()

    def test_t2_none_upload_exceeding_max_upload_bytes_returns_413_and_bad_ext_400(
        self, e2e_env, monkeypatch
    ) -> None:
        app = e2e_env.build_app(auth_mode="none", GRIDFORGE_MAX_UPLOAD_BYTES="1024")
        from gridforge.web import app as app_mod

        monkeypatch.setattr(app_mod, "MAX_UPLOAD_BYTES", 1024, raising=False)
        client = e2e_env.new_client(app)

        oversized = b"x" * 1025
        too_big_job = client.post(
            "/api/jobs",
            data={"payload": json.dumps({"center": "-33.732, 150.312"})},
            files={"overlay_file": ("big.geojson", oversized, "application/geo+json")},
            headers=_SAME_ORIGIN,
        )
        assert too_big_job.status_code == 413

        # Ensure partial oversized upload was cleaned up on disk
        assert list(e2e_env.data_dir.glob("*/uploads/big.geojson")) == []

        too_big_strip = client.post(
            "/api/strip-plan",
            files={"route_file": ("big.geojson", oversized, "application/geo+json")},
            headers=_SAME_ORIGIN,
        )
        assert too_big_strip.status_code == 413

        bad_ext = client.post(
            "/api/jobs",
            data={"payload": json.dumps({"center": "-33.732, 150.312"})},
            files={"overlay_file": ("evil.sh", b"#!/bin/sh\necho hi\n", "text/plain")},
            headers=_SAME_ORIGIN,
        )
        assert bad_ext.status_code == 400

    def test_t2_none_same_origin_and_trusted_host_enforcement(self, e2e_env) -> None:
        app = e2e_env.build_app(auth_mode="none")
        client = e2e_env.new_client(app)

        for bad_headers in (
            {"Origin": "https://evil.example"},
            {"Origin": "http://localhost:5173"},
            {"Sec-Fetch-Site": "cross-site"},
            {"Sec-Fetch-Site": "same-site"},
        ):
            assert (
                client.post(
                    "/api/jobs", json={"center": "-33.732, 150.312"}, headers=bad_headers
                ).status_code
                == 403
            )
            assert (
                client.post(
                    "/api/projects",
                    json={"name": "Bad Origin", "state": {}},
                    headers=bad_headers,
                ).status_code
                == 403
            )
            assert (
                client.delete("/api/packs/blue-mountains", headers=bad_headers).status_code == 403
            )

        bad_host = client.get("/api/health", headers={"Host": "evil.example"})
        assert bad_host.status_code == 400

    def test_t2_none_pack_delete_while_building_returns_409(self, e2e_env) -> None:
        gate = threading.Event()
        started = threading.Event()

        def _blocking_builder(region_or_bbox: str, out_dir: Path, **kwargs: Any) -> PackManifest:
            started.set()
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
            return manifest

        e2e_env.seed_synthetic_pack("act")
        app = e2e_env.build_app(auth_mode="none", pack_builder=_blocking_builder)
        client = e2e_env.new_client(app)

        build = client.post(
            "/api/packs/build",
            json={"region_or_bbox": "act", "dem_tier": "srtm-1s"},
            headers=_SAME_ORIGIN,
        )
        assert build.status_code == 202
        task_id = build.json()["task_id"]
        assert started.wait(timeout=2.0)

        try:
            conflict = client.delete("/api/packs/act", headers=_SAME_ORIGIN)
            assert conflict.status_code == 409
        finally:
            gate.set()

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            r = client.get(f"/api/packs/tasks/{task_id}")
            if r.json()["status"] in ("succeeded", "failed"):
                break
            time.sleep(0.02)

    @pytest.mark.m1_contract
    def test_t2_none_mode_register_and_admin_endpoints_m1_contract(self, e2e_env) -> None:
        app = e2e_env.build_app(auth_mode="none")
        client = e2e_env.new_client(app)

        reg = client.post(
            "/api/auth/register",
            json={"email": "stranger@example.com", "password": "Password1234!"},
            headers=_SAME_ORIGIN,
        )
        assert reg.status_code == 403

        # Admin management routes must return 403 when hosted auth is off (role="none")
        assert client.get("/api/admin/usage").status_code == 403
        assert client.get("/api/admin/users").status_code == 403
        assert (
            client.post(
                "/api/admin/settings",
                json={"public_renders_paused": True},
                headers=_SAME_ORIGIN,
            ).status_code
            == 403
        )
