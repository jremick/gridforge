"""Opaque-box E2E tests (Tier 4): Real-World Multi-Step User, Worker & Admin Journeys.

Implements all 6 end-to-end journeys defined in ``TEST_INFRA.md § 7``:
  - Journey 1: Homelab Zero-Login (``none`` mode) Full Studio-to-Worker Lifecycle
  - Journey 2: Public Guest ``hosted`` Render, Turnstile, Quota Exhaustion, Isolation & 24h TTL Purge
  - Journey 3: Invite-Only Friend Onboarding, 3x3 + Field Bundle Export, Isolation & 14d Retention
  - Journey 4: Google OAuth Invite Onboarding, Session Persistence Across Restart, Override & Disable
  - Journey 5: Cloudflare Proxy HTTPS, CF-Connecting-IP, /samples, Global Budget, Kill Switch & Usage
  - Journey 6: Single-Container /data Shared WAL Concurrency, Role TTL & LRU Cache Trim
"""

from __future__ import annotations

import io
import json
import sqlite3
import sys
import threading
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader, PdfWriter

from tests.e2e.conftest import E2EEnv, same_origin_headers, set_file_mtime_atime


def _write_minimal_pdf(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    writer.add_blank_page(width=595.28, height=841.89)
    with path.open("wb") as fh:
        writer.write(fh)
    writer.close()


def _write_minimal_sqlite(path: Path, *, table_sql: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(table_sql)
        conn.commit()
    finally:
        conn.close()


def _mock_worker_full_pipeline(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch PyQGIS & raster exporters while keeping PDF merge and Field Bundle real."""
    from gridforge.config import (
        ExportFormat,
        ExportResult,
        JobWorkspace,
        MGRSGridMeta,
        RenderResult,
    )
    from gridforge.geo.crs import compute_sheet_layout
    from gridforge.geo.declination import compute_declination
    from gridforge.geo.mgrs_grid import _format_center_mgrs_10m
    from gridforge.web import worker as worker_mod

    state: dict[str, Any] = {"prepare_calls": 0, "render_calls": 0, "gpkg_calls": 0}

    def _fake_prepare(spec: Any, job_dir: Any, **kwargs: Any) -> JobWorkspace:
        state["prepare_calls"] += 1
        wdir = Path(job_dir)
        wdir.mkdir(parents=True, exist_ok=True)
        layout = compute_sheet_layout(spec)
        decl = compute_declination(layout)
        center_mgrs = _format_center_mgrs_10m(spec.center_lat, spec.center_lon)
        gzd, sq_id, *_ = center_mgrs.split()
        mgrs_meta = MGRSGridMeta(
            gzd=gzd,
            square_ids_100km=[sq_id],
            center_mgrs_10m=center_mgrs,
            grid_spacing_m=1000,
            grid_spacing_mm=1000.0 / (spec.scale / 1000.0),
            crosses_100km_boundary=False,
            crosses_utm_zone_boundary=False,
            ticks=[],
        )
        for rel in (
            "osm.gpkg",
            "terrain.gpkg",
            "hillshade.tif",
            "dem_utm.tif",
            "mgrs.gpkg",
            "decl.svg",
            "grid_ref.svg",
        ):
            p = wdir / rel
            if not p.exists():
                p.write_bytes(b"synthetic-workspace-placeholder")

        return JobWorkspace(
            job_dir=wdir,
            layout=layout,
            declination=decl,
            mgrs_meta=mgrs_meta,
            osm_gpkg=wdir / "osm.gpkg",
            terrain_gpkg=wdir / "terrain.gpkg",
            hillshade_tif=wdir / "hillshade.tif",
            dem_utm_tif=wdir / "dem_utm.tif",
            mgrs_gpkg=wdir / "mgrs.gpkg",
            declination_svg=wdir / "decl.svg",
            grid_ref_svg=wdir / "grid_ref.svg",
            contour_interval_display="10 m",
            attributions=["OpenStreetMap contributors (ODbL)", "Copernicus DEM GLO-30"],
        )

    def _fake_render_pdf(self: Any, workspace: Any, output_pdf: Path) -> RenderResult:
        state["render_calls"] += 1
        _write_minimal_pdf(Path(output_pdf))
        return RenderResult(
            output_pdf=Path(output_pdf),
            page_width_mm=210.0,
            page_height_mm=297.0,
            scale=workspace.layout.spec.scale,
            utm_epsg=workspace.layout.utm_epsg,
            elapsed_seconds=0.05,
        )

    def _fake_collarless(workspace: Any, output_tif: Path, **kwargs: Any) -> Path:
        out = Path(output_tif)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"II*\x00synthetic-geotiff")
        return out

    def _fake_gpkg(workspace: Any, output_path: Path, **kwargs: Any) -> ExportResult:
        state["gpkg_calls"] += 1
        out = Path(output_path)
        _write_minimal_sqlite(
            out,
            table_sql="CREATE TABLE IF NOT EXISTS gpkg_contents (table_name TEXT PRIMARY KEY);",
        )
        return ExportResult(
            format=ExportFormat.GPKG,
            output_path=out,
            file_size_bytes=out.stat().st_size,
            tile_count=4,
            elapsed_seconds=0.02,
        )

    def _fake_mbtiles(workspace: Any, output_path: Path, **kwargs: Any) -> ExportResult:
        out = Path(output_path)
        _write_minimal_sqlite(
            out,
            table_sql="CREATE TABLE IF NOT EXISTS metadata (name TEXT, value TEXT);",
        )
        return ExportResult(
            format=ExportFormat.MBTILES,
            output_path=out,
            file_size_bytes=out.stat().st_size,
            tile_count=4,
            elapsed_seconds=0.02,
        )

    def _fake_kmz(workspace: Any, output_path: Path, **kwargs: Any) -> ExportResult:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(out, "w") as zf:
            zf.writestr("doc.kml", "<kml/>")
        return ExportResult(
            format=ExportFormat.KMZ,
            output_path=out,
            file_size_bytes=out.stat().st_size,
            tile_count=1,
            elapsed_seconds=0.02,
        )

    def _fake_osmand(workspace: Any, output_path: Path, **kwargs: Any) -> ExportResult:
        out = Path(output_path)
        _write_minimal_sqlite(
            out,
            table_sql="CREATE TABLE IF NOT EXISTS info (minzoom INTEGER, maxzoom INTEGER);",
        )
        return ExportResult(
            format=ExportFormat.OSMAND,
            output_path=out,
            file_size_bytes=out.stat().st_size,
            tile_count=4,
            elapsed_seconds=0.02,
        )

    monkeypatch.setattr(worker_mod.runner, "prepare_job_workspace", _fake_prepare)
    monkeypatch.setattr(worker_mod.PyQGISRenderer, "render_pdf", _fake_render_pdf)
    monkeypatch.setattr(worker_mod, "render_collarless_geotiff", _fake_collarless)
    monkeypatch.setattr(worker_mod, "export_gpkg_tiles", _fake_gpkg)
    monkeypatch.setattr(worker_mod, "export_mbtiles", _fake_mbtiles)
    monkeypatch.setattr(worker_mod, "export_garmin_kmz", _fake_kmz)
    monkeypatch.setattr(worker_mod, "export_osmand_sqlitedb", _fake_osmand)
    return state


def _run_worker_once(e2e_env: E2EEnv) -> int:
    from gridforge.web import worker as worker_mod

    return worker_mod.main(
        [
            "--data-dir",
            str(e2e_env.data_dir),
            "--packs-dir",
            str(e2e_env.packs_dir),
            "--cache-dir",
            str(e2e_env.cache_dir),
            "--once",
        ]
    )


# ---------------------------------------------------------------------------
# Journey 1 — Homelab Zero-Login (`none` mode) Full Studio-to-Worker Lifecycle
# ---------------------------------------------------------------------------


def test_t4_journey_1_none_mode_full_studio_to_worker_lifecycle(
    e2e_env: E2EEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Journey 1: none mode -> footprint -> 2x2 atlas + gpkg -> project -> worker -> artifacts + restart."""
    app1 = e2e_env.build_app(auth_mode="none", web_dist_with_index=True)
    client1 = e2e_env.new_client(app1)
    hdrs = same_origin_headers("http://localhost:8080")

    sess = client1.get("/api/auth/session")
    assert sess.status_code == 200
    sjson = sess.json()
    assert sjson["auth_mode"] == "none"
    assert sjson["role"] == "none"
    assert sjson["authenticated"] is False
    assert sjson["capabilities"]["max_grid_cells"] == 9
    assert sjson["capabilities"]["packs_manage_allowed"] is True

    fp = client1.post(
        "/api/footprint",
        json={"center": "-33.732, 150.312", "grid_cols": 2, "grid_rows": 2},
        headers=hdrs,
    )
    assert fp.status_code == 200
    assert fp.json()["sheet_count"] == 4
    assert fp.json()["utm_epsg"] == 32756

    job_resp = client1.post(
        "/api/jobs",
        json={
            "center": "-33.732, 150.312",
            "title": "Homelab Recon",
            "grid_cols": 2,
            "grid_rows": 2,
            "outputs": {"pdf": True, "gpkg_tiles": True},
        },
        headers=hdrs,
    )
    assert job_resp.status_code == 202
    job_id = job_resp.json()["job_id"]

    proj_resp = client1.post(
        "/api/projects",
        json={
            "name": "Katoomba 2x2",
            "notes": "Weekend field atlas",
            "state": {"grid_cols": 2, "grid_rows": 2},
            "last_job_id": job_id,
        },
        headers=hdrs,
    )
    assert proj_resp.status_code == 201
    proj_id = proj_resp.json()["project_id"]
    assert proj_resp.json()["thumbnail_url"] == f"/api/jobs/{job_id}/thumbnail.png"
    assert "qgis" not in sys.modules

    _mock_worker_full_pipeline(monkeypatch)
    assert _run_worker_once(e2e_env) == 0

    done = client1.get(f"/api/jobs/{job_id}").json()
    assert done["status"] == "succeeded"
    assert done["progress_pct"] == 100
    by_kind = {a["kind"]: a for a in done["artifacts"]}
    assert set(by_kind) == {"pdf", "gpkg"}
    assert by_kind["pdf"]["filename"].endswith("_atlas_2x2.pdf")

    pdf_resp = client1.get(by_kind["pdf"]["download_url"])
    assert pdf_resp.status_code == 200
    assert pdf_resp.headers["content-type"].startswith("application/pdf")
    assert pdf_resp.content.startswith(b"%PDF-")
    assert len(PdfReader(io.BytesIO(pdf_resp.content)).pages) == 4

    gpkg_resp = client1.get(by_kind["gpkg"]["download_url"])
    assert gpkg_resp.status_code == 200
    assert gpkg_resp.headers["content-type"].startswith("application/geopackage+sqlite3")
    assert gpkg_resp.content.startswith(b"SQLite format 3\x00")

    thumb_resp = client1.get(f"/api/jobs/{job_id}/thumbnail.png")
    assert thumb_resp.status_code == 200
    assert thumb_resp.headers["content-type"] == "image/png"
    assert thumb_resp.content.startswith(b"\x89PNG\r\n\x1a\n")

    # Restart app and verify SQLite WAL persistence
    app2 = e2e_env.build_app(auth_mode="none", web_dist_with_index=True)
    client2 = e2e_env.new_client(app2)
    persisted_job = client2.get(f"/api/jobs/{job_id}").json()
    assert persisted_job["status"] == "succeeded"
    assert len(persisted_job["artifacts"]) == 2
    persisted_proj = client2.get(f"/api/projects/{proj_id}").json()
    assert persisted_proj["last_job_id"] == job_id


# ---------------------------------------------------------------------------
# Journey 2 — Public Guest `hosted` Render, Turnstile, Quota, Isolation & 24h TTL
# ---------------------------------------------------------------------------


@pytest.mark.m1_contract
def test_t4_journey_2_public_guest_turnstile_quota_isolation_and_24h_ttl(
    e2e_env: E2EEnv,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_fixtures: Any,
) -> None:
    """Journey 2: Guest cookie, 2x2/pack 403, Turnstile 403->202, quota 429, isolation 403, 24h TTL purge."""
    app = e2e_env.build_app(
        auth_mode="hosted",
        GRIDFORGE_TURNSTILE_SITE_KEY="site-key-j2",
        GRIDFORGE_TURNSTILE_SECRET_KEY="secret-key-j2",
        GRIDFORGE_GUEST_DAILY_RENDERS="2",
        GRIDFORGE_GUEST_TTL_HOURS="24",
    )
    hdrs = same_origin_headers("http://localhost:8080")
    guest_a = e2e_env.make_guest_client(app)
    assert guest_a.cookies.get("gf_guest")

    sess_a = guest_a.get("/api/auth/session").json()
    assert sess_a["role"] == "guest"
    assert sess_a["capabilities"]["turnstile_required"] is True
    assert sess_a["quotas"]["daily_renders_limit"] == 2

    # Disallowed 2x2 atlas and pack build
    assert (
        guest_a.post(
            "/api/jobs",
            json={"center": "-33.732, 150.312", "grid_cols": 2, "grid_rows": 2},
            headers={**hdrs, "X-Turnstile-Token": "valid-turnstile-token"},
        ).status_code
        == 403
    )
    assert (
        guest_a.post(
            "/api/packs/build",
            json={"region_or_bbox": "blue-mountains", "pack_id": "blue-mountains"},
            headers=hdrs,
        ).status_code
        == 403
    )

    # Missing or invalid Turnstile token -> 403
    assert guest_a.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=hdrs).status_code == 403
    assert (
        guest_a.post(
            "/api/jobs",
            json={"center": "-33.732, 150.312"},
            headers={**hdrs, "X-Turnstile-Token": "bad-token"},
        ).status_code
        == 403
    )

    # 2 valid 1x1 PDFs (1 JSON + 1 multipart payload + overlay_file)
    j1 = guest_a.post(
        "/api/jobs",
        json={"center": "-33.732, 150.312", "outputs": {"pdf": True}},
        headers={**hdrs, "X-Turnstile-Token": "valid-turnstile-token"},
    )
    assert j1.status_code == 202
    job_a1 = j1.json()["job_id"]

    overlay_bytes = synthetic_fixtures.overlay_geojson.read_bytes()
    j2 = guest_a.post(
        "/api/jobs",
        data={
            "payload": json.dumps({"center": "-33.732, 150.312", "outputs": {"pdf": True}}),
            "turnstile_token": "valid-turnstile-token",
        },
        files={"overlay_file": ("overlay.geojson", overlay_bytes, "application/geo+json")},
        headers=hdrs,
    )
    assert j2.status_code == 202
    job_a2 = j2.json()["job_id"]
    assert len(e2e_env.outbound_http.turnstile_calls) >= 2

    proj_a = guest_a.post(
        "/api/projects",
        json={"name": "Guest Spot", "state": {"scale": 25000}, "last_job_id": job_a1},
        headers=hdrs,
    )
    assert proj_a.status_code == 201
    proj_a_id = proj_a.json()["project_id"]

    # 3rd render exceeds guest daily limit (2) -> 429
    j3 = guest_a.post(
        "/api/jobs",
        json={"center": "-33.732, 150.312", "outputs": {"pdf": True}},
        headers={**hdrs, "X-Turnstile-Token": "valid-turnstile-token"},
    )
    assert j3.status_code == 429

    # Worker renders both queued jobs
    _mock_worker_full_pipeline(monkeypatch)
    assert _run_worker_once(e2e_env) == 0
    assert _run_worker_once(e2e_env) == 0

    done_a1 = guest_a.get(f"/api/jobs/{job_a1}").json()
    assert done_a1["status"] == "succeeded"
    art_resp = guest_a.get(done_a1["artifacts"][0]["download_url"])
    assert art_resp.status_code == 200
    assert art_resp.headers.get("cache-control") == "private, no-store"

    # Second distinct guest cannot list or access guest_a's job or project
    guest_b = e2e_env.make_guest_client(app)
    assert guest_b.get("/api/jobs").json()["jobs"] == []
    assert guest_b.get("/api/projects").json()["projects"] == []
    assert guest_b.get(f"/api/jobs/{job_a1}").status_code == 403
    assert guest_b.get(done_a1["artifacts"][0]["download_url"]).status_code == 403
    assert guest_b.get(f"/api/projects/{proj_a_id}").status_code == 403

    # Age guest_a jobs and project by 25h -> worker TTL pass purges DB rows and disk dirs
    e2e_env.age_sqlite_timestamps(table="jobs", id_col="job_id", row_id=job_a1, age_hours=25.0)
    e2e_env.age_sqlite_timestamps(table="jobs", id_col="job_id", row_id=job_a2, age_hours=25.0)
    e2e_env.age_sqlite_timestamps(table="projects", id_col="project_id", row_id=proj_a_id, age_hours=25.0)

    assert _run_worker_once(e2e_env) == 0
    assert not (e2e_env.data_dir / job_a1).exists()
    assert not (e2e_env.data_dir / job_a2).exists()
    assert guest_a.get(f"/api/jobs/{job_a1}").status_code == 404
    assert guest_a.get(f"/api/jobs/{job_a2}").status_code == 404
    assert guest_a.get(f"/api/projects/{proj_a_id}").status_code == 404


# ---------------------------------------------------------------------------
# Journey 3 — Invite-Only Friend Onboarding, 3x3 + Field Bundle, Isolation & 14d TTL
# ---------------------------------------------------------------------------


@pytest.mark.m1_contract
def test_t4_journey_3_invite_friend_3x3_field_bundle_isolation_and_14d_ttl(
    e2e_env: E2EEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Journey 3: Public signup 403 -> Admin invite -> 3x3 + Field Bundle ZIP -> isolation -> 25h vs 15d TTL."""
    app = e2e_env.build_app(
        auth_mode="hosted",
        GRIDFORGE_GUEST_TTL_HOURS="24",
        GRIDFORGE_FRIEND_TTL_HOURS="336",
    )
    hdrs = same_origin_headers("http://localhost:8080")
    guest = e2e_env.make_guest_client(app)

    assert (
        guest.post(
            "/api/auth/register",
            json={"email": "uninvited@example.com", "password": "Password!123"},
            headers=hdrs,
        ).status_code
        == 403
    )

    admin = e2e_env.login_admin(app)
    friend1, _ = e2e_env.invite_and_accept_friend(app, admin, email="friend1-j3@example.com")
    friend2, _ = e2e_env.invite_and_accept_friend(app, admin, email="friend2-j3@example.com")

    assert friend1.post("/api/packs/build", json={"region_or_bbox": "x", "pack_id": "x"}, headers=hdrs).status_code == 403

    job_resp = friend1.post(
        "/api/jobs",
        json={
            "center": "-33.732, 150.312",
            "title": "Blue Mtns 3x3",
            "grid_cols": 3,
            "grid_rows": 3,
            "waypoints": [{"label": "Summit Cache", "lat": -33.732, "lon": 150.312}],
            "outputs": {"pdf": True, "gpkg_tiles": True, "field_bundle_zip": True},
        },
        headers=hdrs,
    )
    assert job_resp.status_code == 202
    f_job_id = job_resp.json()["job_id"]

    proj_resp = friend1.post(
        "/api/projects",
        json={"name": "Friend Expedition", "state": {"grid_cols": 3}, "last_job_id": f_job_id},
        headers=hdrs,
    )
    assert proj_resp.status_code == 201
    f_proj_id = proj_resp.json()["project_id"]

    _mock_worker_full_pipeline(monkeypatch)
    assert _run_worker_once(e2e_env) == 0

    done = friend1.get(f"/api/jobs/{f_job_id}").json()
    assert done["status"] == "succeeded"
    by_kind = {a["kind"]: a for a in done["artifacts"]}
    assert set(by_kind) == {"pdf", "gpkg", "zip"}
    assert by_kind["pdf"]["filename"].endswith("_atlas_3x3.pdf")
    assert by_kind["zip"]["filename"].endswith("_field_bundle.zip")

    pdf_bytes = friend1.get(by_kind["pdf"]["download_url"]).content
    assert len(PdfReader(io.BytesIO(pdf_bytes)).pages) == 9

    zip_resp = friend1.get(by_kind["zip"]["download_url"])
    assert zip_resp.status_code == 200
    assert zip_resp.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(zip_resp.content)) as zf:
        names = set(zf.namelist())
        assert by_kind["pdf"]["filename"] in names
        assert by_kind["gpkg"]["filename"] in names
        assert "waypoints.gpx" in names
        assert "README_FIELD_BUNDLE.txt" in names
        gpx_text = zf.read("waypoints.gpx").decode("utf-8")
        assert '<gpx version="1.1"' in gpx_text
        assert "Summit Cache" in gpx_text

    # Isolation checks against guest and friend2
    for stranger in (guest, friend2):
        assert stranger.get("/api/jobs").json()["jobs"] == []
        assert stranger.get("/api/projects").json()["projects"] == []
        assert stranger.get(f"/api/jobs/{f_job_id}").status_code == 403
        assert stranger.get(by_kind["zip"]["download_url"]).status_code == 403
        assert stranger.get(f"/api/jobs/{f_job_id}/thumbnail.png").status_code == 403
        assert stranger.get(f"/api/projects/{f_proj_id}").status_code == 403
        assert (
            stranger.post(
                "/api/projects",
                json={"name": "Cross Link", "state": {}, "last_job_id": f_job_id},
                headers=hdrs,
            ).status_code
            == 403
        )

    # Seed guest and admin jobs + projects for retention comparison
    g_job_id = guest.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=hdrs).json()["job_id"]
    e2e_env.seed_completed_job_pdf(g_job_id)
    g_proj_id = guest.post("/api/projects", json={"name": "Guest Proj", "state": {}}, headers=hdrs).json()["project_id"]

    a_job_id = admin.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=hdrs).json()["job_id"]
    e2e_env.seed_completed_job_pdf(a_job_id)
    a_proj_id = admin.post("/api/projects", json={"name": "Admin Proj", "state": {}}, headers=hdrs).json()["project_id"]

    # At +25h: guest purged, friend & admin preserved
    for jid in (g_job_id, f_job_id, a_job_id):
        e2e_env.age_sqlite_timestamps(table="jobs", id_col="job_id", row_id=jid, age_hours=25.0)
    for pid in (g_proj_id, f_proj_id, a_proj_id):
        e2e_env.age_sqlite_timestamps(table="projects", id_col="project_id", row_id=pid, age_hours=25.0)

    assert _run_worker_once(e2e_env) == 0
    assert guest.get(f"/api/jobs/{g_job_id}").status_code == 404
    assert guest.get(f"/api/projects/{g_proj_id}").status_code == 404
    assert friend1.get(f"/api/jobs/{f_job_id}").status_code == 200
    assert friend1.get(f"/api/projects/{f_proj_id}").status_code == 200
    assert admin.get(f"/api/jobs/{a_job_id}").status_code == 200

    # At +360h (15d): friend purged, admin preserved
    for jid in (f_job_id, a_job_id):
        e2e_env.age_sqlite_timestamps(table="jobs", id_col="job_id", row_id=jid, age_hours=360.0)
    for pid in (f_proj_id, a_proj_id):
        e2e_env.age_sqlite_timestamps(table="projects", id_col="project_id", row_id=pid, age_hours=360.0)

    assert _run_worker_once(e2e_env) == 0
    assert friend1.get(f"/api/jobs/{f_job_id}").status_code == 404
    assert not (e2e_env.data_dir / f_job_id).exists()
    assert friend1.get(f"/api/projects/{f_proj_id}").status_code == 404
    assert admin.get(f"/api/jobs/{a_job_id}").status_code == 200
    assert (e2e_env.data_dir / a_job_id).exists()
    assert admin.get(f"/api/projects/{a_proj_id}").status_code == 200

    usage = admin.get("/api/admin/usage").json()
    assert usage["summary"]["renders_7d"] >= 2
    assert usage["summary"]["avg_elapsed_seconds"] > 0
    assert usage["summary"]["max_peak_rss_mb"] > 0
    assert "friend" in usage["by_role"]


# ---------------------------------------------------------------------------
# Journey 4 — Google OAuth Invite Onboarding, Restart Persistence, Override & Disable
# ---------------------------------------------------------------------------


@pytest.mark.m1_contract
def test_t4_journey_4_google_oauth_restart_quota_override_and_disable(
    e2e_env: E2EEnv,
) -> None:
    """Journey 4: OAuth 403 without invite -> invited OAuth 302 -> restart -> override -> disable."""
    oauth_email = "trail.oauth@example.com"
    e2e_env.mock_google_oauth(email=oauth_email, name="OAuth Trail", sub="sub-j4-001")
    oauth_env = {
        "GRIDFORGE_GOOGLE_CLIENT_ID": "google-cid-j4",
        "GRIDFORGE_GOOGLE_CLIENT_SECRET": "google-csec-j4",
        "GRIDFORGE_GOOGLE_REDIRECT_URI": "http://localhost:8080/api/auth/google/callback",
        "GRIDFORGE_FRIEND_DAILY_RENDERS": "1",
    }
    app1 = e2e_env.build_app(auth_mode="hosted", env=oauth_env)
    hdrs = same_origin_headers("http://localhost:8080")
    admin1 = e2e_env.login_admin(app1)

    # Uninvited Google OAuth login is rejected with 403
    uninvited = e2e_env.new_client(app1)
    u_start = uninvited.get("/api/auth/google/start", follow_redirects=False)
    assert u_start.status_code == 302
    u_state = parse_qs(urlparse(u_start.headers["location"]).query)["state"][0]
    u_cb = uninvited.get(f"/api/auth/google/callback?code=no-invite&state={u_state}", follow_redirects=False)
    assert u_cb.status_code == 403

    # Admin creates email-pinned invite and user redeems via Google OAuth
    inv = admin1.post(
        "/api/admin/invites",
        json={"email": oauth_email, "role": "friend"},
        headers=hdrs,
    )
    assert inv.status_code == 201
    invite_token = inv.json()["token"]

    friend1 = e2e_env.new_client(app1)
    start = friend1.get(f"/api/auth/google/start?invite_token={invite_token}", follow_redirects=False)
    assert start.status_code == 302
    assert "gf_oauth_state=" in " ".join(start.headers.get_list("set-cookie"))
    state_val = parse_qs(urlparse(start.headers["location"]).query)["state"][0]

    cb = friend1.get(f"/api/auth/google/callback?code=valid-code&state={state_val}", follow_redirects=False)
    assert cb.status_code == 302
    assert cb.headers["location"] == "/"
    assert "gf_session=" in " ".join(cb.headers.get_list("set-cookie"))

    session_cookie = friend1.cookies.get("gf_session")
    assert session_cookie

    sess1 = friend1.get("/api/auth/session").json()
    assert sess1["authenticated"] is True
    assert sess1["role"] == "friend"
    assert sess1["quotas"]["daily_renders_limit"] == 1
    uid = sess1["user"]["user_id"]

    assert friend1.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=hdrs).status_code == 202
    assert friend1.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=hdrs).status_code == 429
    proj = friend1.post("/api/projects", json={"name": "OAuth Saved", "state": {}}, headers=hdrs)
    assert proj.status_code == 201
    proj_id = proj.json()["project_id"]

    # App restart with same session secret preserves gf_session
    app2 = e2e_env.build_app(auth_mode="hosted", env=oauth_env)
    friend2 = e2e_env.new_client(app2)
    friend2.cookies.set("gf_session", session_cookie)
    assert friend2.get("/api/auth/session").json()["authenticated"] is True
    assert friend2.get(f"/api/projects/{proj_id}").status_code == 200

    # Admin overrides daily render limit -> 3 -> Friend immediately renders again
    admin2 = e2e_env.login_admin(app2)
    assert (
        admin2.patch(
            f"/api/admin/users/{uid}",
            json={"daily_render_limit": 3},
            headers=hdrs,
        ).status_code
        == 200
    )
    assert friend2.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=hdrs).status_code == 202

    # Admin disables Friend -> active cookie immediately degrades to guest & re-login returns 403
    assert admin2.patch(f"/api/admin/users/{uid}", json={"disabled": True}, headers=hdrs).status_code == 200

    degraded = friend2.get("/api/auth/session").json()
    assert degraded["authenticated"] is False
    assert degraded["role"] == "guest"
    assert friend2.get(f"/api/projects/{proj_id}").status_code == 403

    re_start = friend2.get("/api/auth/google/start", follow_redirects=False)
    re_state = parse_qs(urlparse(re_start.headers["location"]).query)["state"][0]
    re_cb = friend2.get(f"/api/auth/google/callback?code=relogin&state={re_state}", follow_redirects=False)
    assert re_cb.status_code == 403


# ---------------------------------------------------------------------------
# Journey 5 — Cloudflare Proxy HTTPS, CF-Connecting-IP, /samples, Global Budget & Kill Switch
# ---------------------------------------------------------------------------


@pytest.mark.m1_contract
def test_t4_journey_5_cloudflare_proxy_cf_ip_samples_budget_kill_switch_usage(
    e2e_env: E2EEnv,
) -> None:
    """Journey 5: gridmaps.online HTTPS + /samples + per-IP guest quota + global budget + kill switch + usage."""
    sample_pdf = e2e_env.samples_dir / "demo.pdf"
    _write_minimal_pdf(sample_pdf)

    app = e2e_env.build_app(
        auth_mode="hosted",
        GRIDFORGE_ALLOWED_HOSTS="gridmaps.net,gridmaps.online",
        GRIDFORGE_TRUST_CF_CONNECTING_IP="true",
        GRIDFORGE_GUEST_DAILY_RENDERS="1",
        GRIDFORGE_GLOBAL_DAILY_PUBLIC_RENDERS="2",
        GRIDFORGE_TURNSTILE_SITE_KEY="site-key-j5",
        GRIDFORGE_TURNSTILE_SECRET_KEY="secret-key-j5",
    )

    https_hdrs = {
        "X-Forwarded-Proto": "https",
        "Origin": "https://gridmaps.online",
        "Sec-Fetch-Site": "same-origin",
        "X-Turnstile-Token": "valid-turnstile-token",
    }

    # 1. Sample PDF caching & zero cookies + host/origin rejections
    anon = e2e_env.new_client(app, base_url="http://gridmaps.online")
    samp = anon.get("/samples/demo.pdf", headers={"X-Forwarded-Proto": "https"})
    assert samp.status_code == 200
    assert samp.headers["content-type"] == "application/pdf"
    assert samp.headers["cache-control"] == "public, max-age=604800"
    assert samp.headers.get_list("set-cookie") == []

    evil = e2e_env.new_client(app, base_url="http://evil.example")
    assert evil.get("/api/health").status_code == 400
    assert (
        anon.post(
            "/api/jobs",
            json={"center": "-33.732, 150.312"},
            headers={
                "X-Forwarded-Proto": "https",
                "Origin": "http://gridmaps.online",
                "Sec-Fetch-Site": "same-origin",
            },
        ).status_code
        == 403
    )

    # 2. Guest A (198.51.100.10): 1st succeeds, 2nd fails 429, fresh cookie same IP also fails 429
    guest_a1 = e2e_env.new_client(app, base_url="http://gridmaps.online")
    sess_a1 = guest_a1.get(
        "/api/auth/session",
        headers={"X-Forwarded-Proto": "https", "CF-Connecting-IP": "198.51.100.10"},
    )
    assert "Secure" in " ".join(sess_a1.headers.get_list("set-cookie"))
    assert (
        guest_a1.post(
            "/api/jobs",
            json={"center": "-33.732, 150.312"},
            headers={**https_hdrs, "CF-Connecting-IP": "198.51.100.10"},
        ).status_code
        == 202
    )
    assert e2e_env.outbound_http.turnstile_calls[-1]["remoteip"] == "198.51.100.10"
    assert (
        guest_a1.post(
            "/api/jobs",
            json={"center": "-33.732, 150.312"},
            headers={**https_hdrs, "CF-Connecting-IP": "198.51.100.10"},
        ).status_code
        == 429
    )

    guest_a2 = e2e_env.new_client(app, base_url="http://gridmaps.online")
    assert (
        guest_a2.post(
            "/api/jobs",
            json={"center": "-33.732, 150.312"},
            headers={**https_hdrs, "CF-Connecting-IP": "198.51.100.10"},
        ).status_code
        == 429
    )

    # 3. Guest B (198.51.100.20) uses slot 2/2 of global public budget; Guest C (198.51.100.30) hits 503
    guest_b = e2e_env.new_client(app, base_url="http://gridmaps.online")
    assert (
        guest_b.post(
            "/api/jobs",
            json={"center": "-33.732, 150.312"},
            headers={**https_hdrs, "CF-Connecting-IP": "198.51.100.20"},
        ).status_code
        == 202
    )
    guest_c = e2e_env.new_client(app, base_url="http://gridmaps.online")
    assert (
        guest_c.post(
            "/api/jobs",
            json={"center": "-33.732, 150.312"},
            headers={**https_hdrs, "CF-Connecting-IP": "198.51.100.30"},
        ).status_code
        == 503
    )

    # 4. Admin + Friend bypass global budget; Admin toggles kill switch (blocks guest 503, allows friend/admin 202)
    admin = e2e_env.login_admin(app, base_url="http://gridmaps.online")
    friend, _ = e2e_env.invite_and_accept_friend(
        app, admin, email="j5-friend@example.com", base_url="http://gridmaps.online"
    )
    assert (
        admin.post(
            "/api/admin/settings",
            json={"public_renders_paused": True},
            headers={
                "X-Forwarded-Proto": "https",
                "Origin": "https://gridmaps.online",
                "Sec-Fetch-Site": "same-origin",
            },
        ).status_code
        == 200
    )

    guest_d = e2e_env.new_client(app, base_url="http://gridmaps.online")
    paused_resp = guest_d.post(
        "/api/jobs",
        json={"center": "-33.732, 150.312"},
        headers={**https_hdrs, "CF-Connecting-IP": "198.51.100.40"},
    )
    assert paused_resp.status_code == 503
    assert "paused" in paused_resp.json()["detail"].lower()

    assert (
        friend.post(
            "/api/jobs",
            json={"center": "-33.732, 150.312", "grid_cols": 2, "grid_rows": 2},
            headers={
                "X-Forwarded-Proto": "https",
                "Origin": "https://gridmaps.online",
                "Sec-Fetch-Site": "same-origin",
            },
        ).status_code
        == 202
    )
    assert (
        admin.post(
            "/api/jobs",
            json={"center": "-33.732, 150.312"},
            headers={
                "X-Forwarded-Proto": "https",
                "Origin": "https://gridmaps.online",
                "Sec-Fetch-Site": "same-origin",
            },
        ).status_code
        == 202
    )

    usage = admin.get("/api/admin/usage").json()
    assert usage["summary"]["global_daily_public_used"] == 2
    assert usage["summary"]["public_renders_paused"] is True
    assert usage["by_role"]["guest"]["renders_today"] >= 2
    assert usage["by_role"]["friend"]["renders_today"] >= 1
    assert usage["by_role"]["admin"]["renders_today"] >= 1
    recorded_ips = {ev["client_ip"] for ev in usage["recent_events"]}
    assert {"198.51.100.10", "198.51.100.20"} <= recorded_ips


# ---------------------------------------------------------------------------
# Journey 6 — Single-Container /data Shared WAL Concurrency, TTL & LRU Cache Trim
# ---------------------------------------------------------------------------


@pytest.mark.m1_contract
def test_t4_journey_6_shared_data_wal_concurrency_ttl_and_lru_cache_trim(
    e2e_env: E2EEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Journey 6: Concurrent API threads + main-thread worker --once + WAL + TTL + LRU cache trim."""
    app = e2e_env.build_app(
        auth_mode="hosted",
        GRIDFORGE_GUEST_TTL_HOURS="24",
        GRIDFORGE_FRIEND_TTL_HOURS="336",
        GRIDFORGE_CACHE_MAX_BYTES="800",
    )
    hdrs = same_origin_headers("http://localhost:8080")
    admin = e2e_env.login_admin(app)
    friend, _ = e2e_env.invite_and_accept_friend(app, admin, email="wal-friend@example.com")
    guest = e2e_env.make_guest_client(app)
    friend_cookie = friend.cookies.get("gf_session")
    assert friend_cookie

    # Seed cache files with LRU timestamps + protected temp/part files
    old_dem = e2e_env.cache_dir / "old_dem.tif"
    mid_osm = e2e_env.cache_dir / "mid_osm.pbf"
    new_dem = e2e_env.cache_dir / "new_dem.tif"
    tmp_lock = e2e_env.cache_dir / "active.tif.tmp"
    part_lock = e2e_env.cache_dir / "stream.osm.part"
    old_dem.write_bytes(b"a" * 700)
    mid_osm.write_bytes(b"b" * 700)
    new_dem.write_bytes(b"c" * 400)
    tmp_lock.write_bytes(b"d" * 500)
    part_lock.write_bytes(b"e" * 500)
    set_file_mtime_atime(old_dem, 48.0)
    set_file_mtime_atime(mid_osm, 12.0)
    set_file_mtime_atime(new_dem, 0.5)
    set_file_mtime_atime(tmp_lock, 96.0)
    set_file_mtime_atime(part_lock, 96.0)

    # Seed 3 active queued friend jobs + 1 aged expired guest job + expired invite & session
    queued_ids = [
        friend.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=hdrs).json()["job_id"]
        for _ in range(3)
    ]
    exp_guest_id = guest.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=hdrs).json()["job_id"]
    e2e_env.seed_completed_job_pdf(exp_guest_id)
    e2e_env.age_sqlite_timestamps(table="jobs", id_col="job_id", row_id=exp_guest_id, age_hours=25.0)

    from gridforge.web import db

    past_iso = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.create_invite(
        e2e_env.db_path,
        invite_id="inv_expired_wal",
        token_hash="expired-hash-wal",
        email="exp@example.com",
        role="friend",
        created_by="admin",
        expires_at=past_iso,
    )
    db.create_session(
        e2e_env.db_path,
        session_id="sess_expired_wal",
        user_id="user_admin",
        token_hash="expired-sess-hash-wal",
        expires_at=past_iso,
    )

    _mock_worker_full_pipeline(monkeypatch)
    errors: list[BaseException] = []
    barrier = threading.Barrier(3)

    def _api_worker(idx: int) -> None:
        try:
            with TestClient(app, base_url="http://localhost:8080") as cli:
                cli.cookies.set("gf_session", friend_cookie)
                barrier.wait(timeout=5.0)
                for step in range(6):
                    assert cli.get("/api/health").status_code == 200
                    assert cli.get("/api/jobs").status_code == 200
                    p = cli.post(
                        "/api/projects",
                        json={"name": f"WAL-{idx}-{step}", "state": {"step": step}},
                        headers=hdrs,
                    )
                    assert p.status_code == 201
                    pid = p.json()["project_id"]
                    assert cli.patch(f"/api/projects/{pid}", json={"notes": "ok"}, headers=hdrs).status_code == 200
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=_api_worker, args=(1,))
    t2 = threading.Thread(target=_api_worker, args=(2,))
    t1.start()
    t2.start()
    barrier.wait(timeout=5.0)

    # Run worker --once on the main thread concurrently with API reader/writer threads
    for _ in range(3):
        assert _run_worker_once(e2e_env) == 0

    t1.join(timeout=15.0)
    t2.join(timeout=15.0)
    assert not t1.is_alive() and not t2.is_alive()
    assert errors == []

    # Verify WAL journal mode, worker health, completed jobs, TTL pruning & LRU trim
    conn = sqlite3.connect(str(e2e_env.db_path))
    try:
        mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        inv_left = conn.execute("SELECT COUNT(*) FROM invites WHERE invite_id = 'inv_expired_wal'").fetchone()[0]
        sess_left = conn.execute("SELECT COUNT(*) FROM sessions WHERE session_id = 'sess_expired_wal'").fetchone()[0]
    finally:
        conn.close()
    assert str(mode).lower() == "wal"
    assert inv_left == 0
    assert sess_left == 0

    health = friend.get("/api/health").json()
    assert health["worker_alive"] is True
    for jid in queued_ids:
        assert friend.get(f"/api/jobs/{jid}").json()["status"] == "succeeded"

    assert guest.get(f"/api/jobs/{exp_guest_id}").status_code == 404
    assert not (e2e_env.data_dir / exp_guest_id).exists()

    assert not old_dem.exists()
    assert not mid_osm.exists()
    assert new_dem.exists()
    assert tmp_lock.exists()
    assert part_lock.exists()
