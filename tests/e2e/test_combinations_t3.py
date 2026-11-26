"""Opaque-box E2E tests (Tier 3): Pairwise Cross-Feature Combinations.

Covers 2-way+ interactions across:
  {auth_mode, role, proxy_scheme, turnstile_state, kill_switch, output_bundle,
   pack_present, rate_limits, ttl_pruning, admin_usage}

All tests run offline inside ``gridforge:dev`` with isolated ``tmp_path`` dirs,
mocked outbound HTTP (Turnstile, Google OAuth, Nominatim), and zero PyQGIS
imports in ``gridforge.web.app``.
"""

from __future__ import annotations

import io
import json
import sqlite3
import sys
import time
import zipfile
from pathlib import Path
from typing import Any

import pytest
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

    state: dict[str, Any] = {
        "prepare_calls": [],
        "render_calls": 0,
        "gpkg_calls": 0,
        "mbtiles_calls": 0,
        "kmz_calls": 0,
        "osmand_calls": 0,
    }

    def _fake_prepare(spec: Any, job_dir: Any, **kwargs: Any) -> JobWorkspace:
        wdir = Path(job_dir)
        wdir.mkdir(parents=True, exist_ok=True)
        state["prepare_calls"].append(
            {"job_dir": wdir, "pack_dir": kwargs.get("pack_dir"), "title": spec.title}
        )
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
        state["mbtiles_calls"] += 1
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
        state["kmz_calls"] += 1
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
        state["osmand_calls"] += 1
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


def _https_headers(host: str, *, ip: str | None = None, turnstile: str | None = None) -> dict[str, str]:
    hdrs = {
        "X-Forwarded-Proto": "https",
        "Origin": f"https://{host}",
        "Sec-Fetch-Site": "same-origin",
    }
    if ip is not None:
        hdrs["CF-Connecting-IP"] = ip
    if turnstile is not None:
        hdrs["X-Turnstile-Token"] = turnstile
    return hdrs


# ---------------------------------------------------------------------------
# Tier 3: 12 Pairwise Cross-Feature Combination Tests
# ---------------------------------------------------------------------------


@pytest.mark.m1_contract
def test_t3_01_none_mode_https_proxy_turnstile_ignored_pack_3x3_bundle_worker(
    e2e_env: E2EEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """none + https proxy (FORWARDED_ALLOW_IPS=*) + Turnstile keys ignored + pack + 3x3 & bundle."""
    e2e_env.seed_synthetic_pack("blue-mountains")
    app = e2e_env.build_app(
        auth_mode="none",
        FORWARDED_ALLOW_IPS="*",
        GRIDFORGE_ALLOWED_HOSTS="homelab.local",
        GRIDFORGE_TURNSTILE_SITE_KEY="1x00000000000000000000AA",
        GRIDFORGE_TURNSTILE_SECRET_KEY="1x0000000000000000000000000000000AA",
    )
    client = e2e_env.new_client(app, base_url="http://homelab.local")

    sess = client.get("/api/auth/session")
    assert sess.status_code == 200
    assert sess.json()["role"] == "none"
    assert sess.json()["capabilities"]["turnstile_required"] is False

    resp = client.post(
        "/api/jobs",
        json={
            "center": "-33.732, 150.312",
            "title": "Homelab 3x3 Bundle",
            "grid_cols": 3,
            "grid_rows": 3,
            "pack_id": "blue-mountains",
            "waypoints": [{"label": "Base Camp", "lat": -33.732, "lon": 150.312}],
            "outputs": {"pdf": True, "gpkg_tiles": True, "field_bundle_zip": True},
        },
        headers=_https_headers("homelab.local"),
    )
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["job_id"]
    assert e2e_env.outbound_http.turnstile_calls == []
    assert "qgis" not in sys.modules

    worker_state = _mock_worker_full_pipeline(monkeypatch)
    from gridforge.web import worker as worker_mod

    rc = worker_mod.main(
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
    assert rc == 0
    assert any(
        c["pack_dir"] == e2e_env.packs_dir / "blue-mountains"
        for c in worker_state["prepare_calls"]
    )

    done = client.get(f"/api/jobs/{job_id}").json()
    assert done["status"] == "succeeded"
    by_kind = {a["kind"]: a for a in done["artifacts"]}
    assert set(by_kind) == {"pdf", "gpkg", "zip"}

    pdf_bytes = client.get(by_kind["pdf"]["download_url"]).content
    assert len(PdfReader(io.BytesIO(pdf_bytes)).pages) == 9

    zip_bytes = client.get(by_kind["zip"]["download_url"]).content
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = set(zf.namelist())
        assert by_kind["pdf"]["filename"] in names
        assert by_kind["gpkg"]["filename"] in names
        assert "waypoints.gpx" in names
        assert "README_FIELD_BUNDLE.txt" in names
        assert "Base Camp" in zf.read("waypoints.gpx").decode("utf-8")


@pytest.mark.m1_contract
def test_t3_02_hosted_guest_https_cf_ip_turnstile_pack_1x1_worker_and_usage(
    e2e_env: E2EEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """hosted + guest + https CF-Connecting-IP (IPv6) + Turnstile + installed pack + 1x1 PDF."""
    e2e_env.seed_synthetic_pack("blue-mountains")
    app = e2e_env.build_app(
        auth_mode="hosted",
        GRIDFORGE_ALLOWED_HOSTS="gridmaps.online",
        GRIDFORGE_TRUST_CF_CONNECTING_IP="true",
        GRIDFORGE_TURNSTILE_SITE_KEY="site-key-test",
        GRIDFORGE_TURNSTILE_SECRET_KEY="secret-key-test",
    )
    guest = e2e_env.new_client(app, base_url="https://gridmaps.online")
    sess = guest.get(
        "/api/auth/session",
        headers={"X-Forwarded-Proto": "https", "CF-Connecting-IP": "2001:0db8:0000:0000::42"},
    )
    assert sess.status_code == 200
    assert sess.json()["role"] == "guest"
    set_cookies = " ".join(sess.headers.get_list("set-cookie"))
    assert "gf_guest=" in set_cookies
    assert "Secure" in set_cookies
    assert "HttpOnly" in set_cookies

    resp = guest.post(
        "/api/jobs",
        json={"center": "-33.732, 150.312", "pack_id": "blue-mountains", "outputs": {"pdf": True}},
        headers=_https_headers(
            "gridmaps.online",
            ip="2001:0db8:0000:0000::42",
            turnstile="valid-turnstile-token",
        ),
    )
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["job_id"]
    assert len(e2e_env.outbound_http.turnstile_calls) == 1
    assert e2e_env.outbound_http.turnstile_calls[0]["remoteip"] == "2001:db8::42"

    _mock_worker_full_pipeline(monkeypatch)
    from gridforge.web import worker as worker_mod

    assert (
        worker_mod.main(
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
        == 0
    )

    done = guest.get(f"/api/jobs/{job_id}").json()
    assert done["status"] == "succeeded"
    art_resp = guest.get(done["artifacts"][0]["download_url"])
    assert art_resp.status_code == 200
    assert art_resp.headers.get("cache-control") == "private, no-store"
    assert art_resp.content.startswith(b"%PDF-")

    admin = e2e_env.login_admin(app)
    usage = admin.get("/api/admin/usage").json()
    assert usage["summary"]["renders_today"] == 1
    assert any(
        ev["client_ip"] == "2001:db8::42" and ev["actor_role"] == "guest"
        for ev in usage["recent_events"]
    )


@pytest.mark.m1_contract
def test_t3_03_hosted_guest_bad_turnstile_preserves_render_preview_and_public_quotas(
    e2e_env: E2EEnv,
    mock_preview_dem: None,
) -> None:
    """hosted + guest + missing/invalid Turnstile (403) does not consume render, preview, or public quota."""
    _ = mock_preview_dem
    app = e2e_env.build_app(
        auth_mode="hosted",
        GRIDFORGE_TURNSTILE_SITE_KEY="site-key-test",
        GRIDFORGE_TURNSTILE_SECRET_KEY="secret-key-test",
        GRIDFORGE_GUEST_DAILY_RENDERS="1",
        GRIDFORGE_GUEST_HOURLY_PREVIEWS="1",
        GRIDFORGE_GLOBAL_DAILY_PUBLIC_RENDERS="1",
    )
    guest = e2e_env.make_guest_client(app)
    hdrs = same_origin_headers("http://localhost:8080")

    bad_job_missing = guest.post(
        "/api/jobs",
        json={"center": "-33.732, 150.312", "outputs": {"pdf": True}},
        headers=hdrs,
    )
    assert bad_job_missing.status_code == 403

    bad_job_token = guest.post(
        "/api/jobs",
        json={"center": "-33.732, 150.312", "outputs": {"pdf": True}},
        headers={**hdrs, "X-Turnstile-Token": "bad-token"},
    )
    assert bad_job_token.status_code == 403

    bad_preview = guest.post(
        "/api/viewshed-preview",
        json={"observer": {"label": "OBS", "lat": -33.732, "lon": 150.312}},
        headers={**hdrs, "X-Turnstile-Token": "bad-token"},
    )
    assert bad_preview.status_code == 403

    sess_before = guest.get("/api/auth/session").json()
    assert sess_before["quotas"]["daily_renders_used"] == 0
    assert sess_before["quotas"]["hourly_previews_used"] == 0
    assert sess_before["quotas"]["global_daily_public_used"] == 0

    good_preview = guest.post(
        "/api/viewshed-preview",
        json={"observer": {"label": "OBS", "lat": -33.732, "lon": 150.312}},
        headers={**hdrs, "X-Turnstile-Token": "valid-turnstile-token"},
    )
    assert good_preview.status_code == 200

    good_job = guest.post(
        "/api/jobs",
        json={"center": "-33.732, 150.312", "outputs": {"pdf": True}},
        headers={**hdrs, "X-Turnstile-Token": "valid-turnstile-token"},
    )
    assert good_job.status_code == 202


@pytest.mark.m1_contract
def test_t3_04_hosted_guest_capability_403_and_missing_pack_404_before_quota(
    e2e_env: E2EEnv,
) -> None:
    """hosted + guest + valid Turnstile + blocked capability (403) or missing pack (404) leaves quota intact."""
    app = e2e_env.build_app(
        auth_mode="hosted",
        GRIDFORGE_TURNSTILE_SITE_KEY="site-key-test",
        GRIDFORGE_TURNSTILE_SECRET_KEY="secret-key-test",
        GRIDFORGE_GUEST_DAILY_RENDERS="1",
    )
    guest = e2e_env.make_guest_client(app)
    hdrs = {**same_origin_headers("http://localhost:8080"), "X-Turnstile-Token": "valid-turnstile-token"}

    assert (
        guest.post(
            "/api/jobs",
            json={"center": "-33.732, 150.312", "outputs": {"pdf": True, "gpkg_tiles": True}},
            headers=hdrs,
        ).status_code
        == 403
    )
    assert (
        guest.post(
            "/api/jobs",
            json={"center": "-33.732, 150.312", "outputs": {"pdf": True, "field_bundle_zip": True}},
            headers=hdrs,
        ).status_code
        == 403
    )
    assert (
        guest.post(
            "/api/jobs",
            json={"center": "-33.732, 150.312", "pack_id": "missing-pack", "outputs": {"pdf": True}},
            headers=hdrs,
        ).status_code
        == 404
    )

    conn = sqlite3.connect(str(e2e_env.db_path))
    try:
        ev_count = conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
    finally:
        conn.close()
    assert ev_count == 0
    assert guest.get("/api/auth/session").json()["quotas"]["daily_renders_used"] == 0

    assert (
        guest.post(
            "/api/jobs",
            json={"center": "-33.732, 150.312", "outputs": {"pdf": True}},
            headers=hdrs,
        ).status_code
        == 202
    )


@pytest.mark.m1_contract
def test_t3_05_hosted_guest_turnstile_403_and_kill_switch_503_precedence_over_429(
    e2e_env: E2EEnv,
) -> None:
    """hosted + guest: Turnstile (403) precedes kill switch (503), which precedes daily quota (429)."""
    app = e2e_env.build_app(
        auth_mode="hosted",
        GRIDFORGE_TURNSTILE_SITE_KEY="site-key-test",
        GRIDFORGE_TURNSTILE_SECRET_KEY="secret-key-test",
        GRIDFORGE_GUEST_DAILY_RENDERS="1",
    )
    guest = e2e_env.make_guest_client(app)
    admin = e2e_env.login_admin(app)
    hdrs = same_origin_headers("http://localhost:8080")

    first = guest.post(
        "/api/jobs",
        json={"center": "-33.732, 150.312", "outputs": {"pdf": True}},
        headers={**hdrs, "X-Turnstile-Token": "valid-turnstile-token"},
    )
    assert first.status_code == 202

    assert (
        admin.post(
            "/api/admin/settings",
            json={"public_renders_paused": True},
            headers=hdrs,
        ).status_code
        == 200
    )

    bad_ts = guest.post(
        "/api/jobs",
        json={"center": "-33.732, 150.312", "outputs": {"pdf": True}},
        headers={**hdrs, "X-Turnstile-Token": "invalid-token"},
    )
    assert bad_ts.status_code == 403

    paused_over_quota = guest.post(
        "/api/jobs",
        json={"center": "-33.732, 150.312", "outputs": {"pdf": True}},
        headers={**hdrs, "X-Turnstile-Token": "valid-turnstile-token"},
    )
    assert paused_over_quota.status_code == 503


@pytest.mark.m1_contract
def test_t3_06_hosted_kill_switch_role_matrix_and_app_restart_persistence(
    e2e_env: E2EEnv,
) -> None:
    """hosted + kill_switch=True blocks guest (503), allows friend & admin (202), and persists restart."""
    app1 = e2e_env.build_app(auth_mode="hosted")
    hdrs = same_origin_headers("http://localhost:8080")
    admin1 = e2e_env.login_admin(app1)
    friend1, _ = e2e_env.invite_and_accept_friend(app1, admin1, email="ks-friend@example.com")
    guest1 = e2e_env.make_guest_client(app1)

    assert (
        admin1.post(
            "/api/admin/settings",
            json={"public_renders_paused": True},
            headers=hdrs,
        ).status_code
        == 200
    )

    assert guest1.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=hdrs).status_code == 503
    assert friend1.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=hdrs).status_code == 202
    assert admin1.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=hdrs).status_code == 202

    # Restart app with same DB + session secret
    app2 = e2e_env.build_app(auth_mode="hosted")
    guest2 = e2e_env.make_guest_client(app2)
    admin2 = e2e_env.login_admin(app2)
    friend2 = e2e_env.new_client(app2)
    assert (
        friend2.post(
            "/api/auth/login",
            json={"email": "ks-friend@example.com", "password": "FriendPass!234"},
            headers=hdrs,
        ).status_code
        == 200
    )

    assert guest2.get("/api/auth/session").json()["quotas"]["public_renders_paused"] is True
    assert guest2.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=hdrs).status_code == 503
    assert friend2.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=hdrs).status_code == 202
    assert admin2.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=hdrs).status_code == 202

    assert (
        admin2.post(
            "/api/admin/settings",
            json={"public_renders_paused": False},
            headers=hdrs,
        ).status_code
        == 200
    )
    assert guest2.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=hdrs).status_code == 202


@pytest.mark.m1_contract
def test_t3_07_hosted_admin_queue_depth_503_recovery_and_pack_lifecycle(
    e2e_env: E2EEnv,
) -> None:
    """hosted + kill_switch + queue depth=1 blocks even admin (503) until cancel; admin builds/deletes pack."""
    app = e2e_env.build_app(auth_mode="hosted", GRIDFORGE_MAX_QUEUE_DEPTH="1")
    hdrs = same_origin_headers("http://localhost:8080")
    admin = e2e_env.login_admin(app)
    friend, _ = e2e_env.invite_and_accept_friend(app, admin, email="pack-friend@example.com")

    assert admin.post("/api/admin/settings", json={"public_renders_paused": True}, headers=hdrs).status_code == 200

    build_resp = admin.post(
        "/api/packs/build",
        json={"region_or_bbox": "act", "pack_id": "act", "name": "ACT Test"},
        headers=hdrs,
    )
    assert build_resp.status_code == 202, build_resp.text
    task_id = build_resp.json()["task_id"]
    task_status: dict[str, Any] = {}
    for _ in range(100):
        task_status = admin.get(f"/api/packs/tasks/{task_id}").json()
        if task_status["status"] in ("succeeded", "failed"):
            break
        time.sleep(0.02)
    assert task_status["status"] == "succeeded", task_status

    friend_job = friend.post(
        "/api/jobs",
        json={"center": "-35.30, 149.12", "pack_id": "act"},
        headers=hdrs,
    )
    assert friend_job.status_code == 202
    friend_job_id = friend_job.json()["job_id"]

    # Queue depth 1 reached -> even admin gets 503
    assert admin.post("/api/jobs", json={"center": "-35.30, 149.12"}, headers=hdrs).status_code == 503

    assert friend.post(f"/api/jobs/{friend_job_id}/cancel", headers=hdrs).status_code == 200
    assert admin.post("/api/jobs", json={"center": "-35.30, 149.12"}, headers=hdrs).status_code == 202

    assert friend.delete("/api/packs/act", headers=hdrs).status_code == 403
    assert admin.delete("/api/packs/act", headers=hdrs).status_code == 200


@pytest.mark.m1_contract
def test_t3_08_hosted_https_cf_ip_trust_geocode_cache_and_rf_link_limits(
    e2e_env: E2EEnv,
    mock_nominatim: Any,
    mock_preview_dem: None,
) -> None:
    """hosted + https CF-Connecting-IP trusted vs untrusted + geocode cache hit/miss + rf-link-profile."""
    _ = mock_preview_dem

    # Part A: GRIDFORGE_TRUST_CF_CONNECTING_IP=false ignores CF-Connecting-IP -> both guests share IP
    app_untrusted = e2e_env.build_app(
        auth_mode="hosted",
        FORWARDED_ALLOW_IPS="*",
        GRIDFORGE_ALLOWED_HOSTS="gridmaps.net",
        GRIDFORGE_TRUST_CF_CONNECTING_IP="false",
        GRIDFORGE_GEOCODE_HOURLY_LIMIT="2",
    )
    g1 = e2e_env.make_guest_client(app_untrusted, base_url="http://gridmaps.net")
    g2 = e2e_env.make_guest_client(app_untrusted, base_url="http://gridmaps.net")
    assert g1.get("/api/geocode", params={"q": "Katoomba"}, headers={"CF-Connecting-IP": "198.51.100.1"}).status_code == 200
    assert g2.get("/api/geocode", params={"q": "Leura"}, headers={"CF-Connecting-IP": "198.51.100.2"}).status_code == 200
    assert g2.get("/api/geocode", params={"q": "Blackheath"}, headers={"CF-Connecting-IP": "198.51.100.2"}).status_code == 429

    # Part B: GRIDFORGE_TRUST_CF_CONNECTING_IP=true isolates IPs, shares IP across guest+friend, bypasses on cache hit
    app_trusted = e2e_env.build_app(
        auth_mode="hosted",
        GRIDFORGE_ALLOWED_HOSTS="gridmaps.net",
        GRIDFORGE_TRUST_CF_CONNECTING_IP="true",
        GRIDFORGE_GEOCODE_HOURLY_LIMIT="2",
        GRIDFORGE_RF_LINK_HOURLY_LIMIT="1",
        GRIDFORGE_FRIEND_RF_LINK_HOURLY_LIMIT="2",
    )
    admin = e2e_env.login_admin(app_trusted, base_url="http://gridmaps.net")
    friend, _ = e2e_env.invite_and_accept_friend(
        app_trusted, admin, email="geo-friend@example.com", base_url="http://gridmaps.net"
    )
    guest_ip1 = e2e_env.make_guest_client(app_trusted, base_url="http://gridmaps.net")
    guest_ip2 = e2e_env.make_guest_client(app_trusted, base_url="http://gridmaps.net")

    calls_before = len(mock_nominatim.nominatim_calls)
    assert guest_ip1.get("/api/geocode", params={"q": "Katoomba"}, headers={"CF-Connecting-IP": "203.0.113.55"}).status_code == 200
    assert friend.get("/api/geocode", params={"q": "Leura"}, headers={"CF-Connecting-IP": "203.0.113.55"}).status_code == 200
    # 3rd unique miss from same IP fails even for friend
    assert friend.get("/api/geocode", params={"q": "Medlow"}, headers={"CF-Connecting-IP": "203.0.113.55"}).status_code == 429
    # Cache hit on "Katoomba" bypasses geocode rate limit
    assert guest_ip1.get("/api/geocode", params={"q": "Katoomba"}, headers={"CF-Connecting-IP": "203.0.113.55"}).status_code == 200
    assert len(mock_nominatim.nominatim_calls) - calls_before == 2
    # Distinct IP and Admin remain unblocked
    assert guest_ip2.get("/api/geocode", params={"q": "Medlow"}, headers={"CF-Connecting-IP": "203.0.113.99"}).status_code == 200
    assert admin.get("/api/geocode", params={"q": "Lithgow"}, headers={"CF-Connecting-IP": "203.0.113.55"}).status_code == 200

    # RF Link rate limits across roles
    rf_payload = {
        "from_lat": -33.732,
        "from_lon": 150.312,
        "to_lat": -33.700,
        "to_lon": 150.340,
        "freq_mhz": 915.0,
    }
    hdr_ip1 = _https_headers("gridmaps.net", ip="203.0.113.55")
    assert guest_ip1.post("/api/rf-link-profile", json=rf_payload, headers=hdr_ip1).status_code == 200
    assert guest_ip1.post("/api/rf-link-profile", json=rf_payload, headers=hdr_ip1).status_code == 429
    assert friend.post("/api/rf-link-profile", json=rf_payload, headers=hdr_ip1).status_code == 200
    assert admin.post("/api/rf-link-profile", json=rf_payload, headers=_https_headers("gridmaps.net")).status_code == 200


@pytest.mark.m1_contract
def test_t3_09_hosted_friend_quota_override_strip_bundle_and_upload_413_clean(
    e2e_env: E2EEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """hosted + friend override + strip(3) + field_bundle + MAX_UPLOAD_BYTES (413 guest vs 202 friend)."""
    app = e2e_env.build_app(
        auth_mode="hosted",
        GRIDFORGE_FRIEND_DAILY_RENDERS="1",
        GRIDFORGE_MAX_UPLOAD_BYTES_GUEST="1024",
        GRIDFORGE_MAX_UPLOAD_BYTES_FRIEND="4096",
    )
    hdrs = same_origin_headers("http://localhost:8080")
    admin = e2e_env.login_admin(app)
    friend, f_sess = e2e_env.invite_and_accept_friend(app, admin, email="strip-friend@example.com")
    friend_id = f_sess["user"]["user_id"]
    guest = e2e_env.make_guest_client(app)

    patch_resp = admin.patch(
        f"/api/admin/users/{friend_id}",
        json={"daily_render_limit": 2},
        headers=hdrs,
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["user"]["daily_render_limit"] == 2

    geojson_bytes = json.dumps(
        {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"name": "Long Ridge", "notes": "Trail corridor " + ("x" * 1300)},
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [[150.30, -33.74], [150.32, -33.72], [150.34, -33.70]],
                    },
                }
            ],
        }
    ).encode("utf-8")
    assert 1024 < len(geojson_bytes) < 4096

    guest_payload = json.dumps({"center": "-33.732, 150.312", "outputs": {"pdf": True}})
    guest_resp = guest.post(
        "/api/jobs",
        data={"payload": guest_payload},
        files={"overlay_file": ("route.geojson", geojson_bytes, "application/geo+json")},
        headers=hdrs,
    )
    assert guest_resp.status_code == 413
    # Ensure 413 left zero job directories on disk and zero usage events
    assert [p for p in e2e_env.data_dir.iterdir() if p.name.startswith("job_")] == []
    assert guest.get("/api/auth/session").json()["quotas"]["daily_renders_used"] == 0

    strip_payload = json.dumps(
        {
            "title": "Strip Recon",
            "strip_sheets": [
                {"sheet_id": "S1", "center_lat": -33.74, "center_lon": 150.30},
                {"sheet_id": "S2", "center_lat": -33.72, "center_lon": 150.32},
                {"sheet_id": "S3", "center_lat": -33.70, "center_lon": 150.34},
            ],
            "waypoints": [{"label": "Checkpoint Alpha", "lat": -33.72, "lon": 150.32}],
            "outputs": {"pdf": True, "field_bundle_zip": True},
        }
    )
    f_job1 = friend.post(
        "/api/jobs",
        data={"payload": strip_payload},
        files={"overlay_file": ("route.geojson", geojson_bytes, "application/geo+json")},
        headers=hdrs,
    )
    assert f_job1.status_code == 202, f_job1.text
    f_job1_id = f_job1.json()["job_id"]

    # Override=2 allows a 2nd job; 3rd returns 429
    assert friend.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=hdrs).status_code == 202
    assert friend.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=hdrs).status_code == 429

    _mock_worker_full_pipeline(monkeypatch)
    from gridforge.web import worker as worker_mod

    assert (
        worker_mod.main(
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
        == 0
    )

    done = friend.get(f"/api/jobs/{f_job1_id}").json()
    assert done["status"] == "succeeded"
    by_kind = {a["kind"]: a for a in done["artifacts"]}
    assert by_kind["pdf"]["filename"].endswith("_strip_3sheets.pdf")
    assert len(PdfReader(io.BytesIO(friend.get(by_kind["pdf"]["download_url"]).content)).pages) == 3

    with zipfile.ZipFile(io.BytesIO(friend.get(by_kind["zip"]["download_url"]).content)) as zf:
        names = set(zf.namelist())
        assert by_kind["pdf"]["filename"] in names
        assert "waypoints.gpx" in names
        assert "README_FIELD_BUNDLE.txt" in names


@pytest.mark.m1_contract
def test_t3_10_hosted_friend_gpkg_no_pdf_thumbnail_404_and_project_link_isolation(
    e2e_env: E2EEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """hosted + friend + outputs={pdf: False, gpkg_tiles: True} -> thumbnail 404 + project last_job_id isolation."""
    app = e2e_env.build_app(auth_mode="hosted")
    hdrs = same_origin_headers("http://localhost:8080")
    admin = e2e_env.login_admin(app)
    friend1, _ = e2e_env.invite_and_accept_friend(app, admin, email="f1-iso@example.com")
    friend2, _ = e2e_env.invite_and_accept_friend(app, admin, email="f2-iso@example.com")

    job_resp = friend1.post(
        "/api/jobs",
        json={"center": "-33.732, 150.312", "outputs": {"pdf": False, "gpkg_tiles": True}},
        headers=hdrs,
    )
    assert job_resp.status_code == 202
    job_id = job_resp.json()["job_id"]

    _mock_worker_full_pipeline(monkeypatch)
    from gridforge.web import worker as worker_mod

    assert (
        worker_mod.main(
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
        == 0
    )

    done = friend1.get(f"/api/jobs/{job_id}").json()
    assert done["status"] == "succeeded"
    assert [a["kind"] for a in done["artifacts"]] == ["gpkg"]
    assert friend1.get(f"/api/jobs/{job_id}/thumbnail.png").status_code == 404

    assert (
        friend2.post(
            "/api/projects",
            json={"name": "Hijack Attempt", "state": {}, "last_job_id": job_id},
            headers=hdrs,
        ).status_code
        == 403
    )
    own_proj = friend1.post(
        "/api/projects",
        json={"name": "Own GPKG Project", "state": {}, "last_job_id": job_id},
        headers=hdrs,
    )
    assert own_proj.status_code == 201


@pytest.mark.m1_contract
def test_t3_11_hosted_active_vs_terminal_ttl_cache_trim_and_admin_usage(
    e2e_env: E2EEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """hosted + worker --once prunes 25h terminal guest (sparing queued guest & friend), trims cache, runs queued."""
    app = e2e_env.build_app(
        auth_mode="hosted",
        GRIDFORGE_GUEST_TTL_HOURS="24",
        GRIDFORGE_FRIEND_TTL_HOURS="336",
        GRIDFORGE_CACHE_MAX_BYTES="1000",
    )
    hdrs = same_origin_headers("http://localhost:8080")
    admin = e2e_env.login_admin(app)
    friend, _ = e2e_env.invite_and_accept_friend(app, admin, email="ttl-friend@example.com")
    guest = e2e_env.make_guest_client(app)

    q_guest_id = guest.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=hdrs).json()["job_id"]
    c_guest_id = guest.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=hdrs).json()["job_id"]
    assert guest.post(f"/api/jobs/{c_guest_id}/cancel", headers=hdrs).status_code == 200
    (e2e_env.data_dir / c_guest_id / "artifacts").mkdir(parents=True, exist_ok=True)
    _write_minimal_pdf(e2e_env.data_dir / c_guest_id / "artifacts" / "cancelled.pdf")

    f_job_id = friend.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=hdrs).json()["job_id"]
    e2e_env.seed_completed_job_pdf(f_job_id)

    # Age all three jobs to 25h ago
    e2e_env.age_sqlite_timestamps(table="jobs", id_col="job_id", row_id=q_guest_id, age_hours=25.0)
    e2e_env.age_sqlite_timestamps(table="jobs", id_col="job_id", row_id=c_guest_id, age_hours=25.0)
    e2e_env.age_sqlite_timestamps(table="jobs", id_col="job_id", row_id=f_job_id, age_hours=25.0)

    # Seed cache_dir with old + new + active lock
    old_cache = e2e_env.cache_dir / "old_dem.tif"
    new_cache = e2e_env.cache_dir / "new_dem.tif"
    tmp_lock = e2e_env.cache_dir / "active.tif.tmp"
    old_cache.write_bytes(b"o" * 700)
    new_cache.write_bytes(b"n" * 700)
    tmp_lock.write_bytes(b"t" * 500)
    set_file_mtime_atime(old_cache, 48.0)
    set_file_mtime_atime(new_cache, 1.0)
    set_file_mtime_atime(tmp_lock, 96.0)

    _mock_worker_full_pipeline(monkeypatch)
    from gridforge.web import worker as worker_mod

    assert (
        worker_mod.main(
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
        == 0
    )

    assert guest.get(f"/api/jobs/{c_guest_id}").status_code == 404
    assert not (e2e_env.data_dir / c_guest_id).exists()
    assert guest.get(f"/api/jobs/{q_guest_id}").json()["status"] == "succeeded"
    assert friend.get(f"/api/jobs/{f_job_id}").status_code == 200

    assert not old_cache.exists()
    assert new_cache.exists()
    assert tmp_lock.exists()

    usage = admin.get("/api/admin/usage").json()
    assert usage["summary"]["renders_today"] >= 2
    assert usage["summary"]["avg_elapsed_seconds"] > 0
    assert usage["summary"]["max_peak_rss_mb"] > 0


@pytest.mark.m1_contract
def test_t3_12_hosted_https_google_oauth_restart_and_mid_session_disable(
    e2e_env: E2EEnv,
) -> None:
    """hosted + https proxy + Google OAuth + stable secret restart + mid-session user disable."""
    oauth_email = "oauth-t3@example.com"
    e2e_env.mock_google_oauth(email=oauth_email, name="OAuth T3", sub="sub-t3-12")
    env_vars = {
        "GRIDFORGE_ALLOWED_HOSTS": "gridmaps.net",
        "FORWARDED_ALLOW_IPS": "*",
        "GRIDFORGE_GOOGLE_CLIENT_ID": "test-client-id",
        "GRIDFORGE_GOOGLE_CLIENT_SECRET": "test-client-secret",
        "GRIDFORGE_GOOGLE_REDIRECT_URI": "https://gridmaps.net/api/auth/google/callback",
    }
    app1 = e2e_env.build_app(auth_mode="hosted", env=env_vars)
    admin1 = e2e_env.login_admin(app1, base_url="http://gridmaps.net")
    inv = admin1.post(
        "/api/admin/invites",
        json={"email": oauth_email, "role": "friend"},
        headers=_https_headers("gridmaps.net"),
    )
    assert inv.status_code == 201
    invite_token = inv.json()["token"]

    browser1 = e2e_env.new_client(app1, base_url="https://gridmaps.net")
    start = browser1.get(
        f"/api/auth/google/start?invite_token={invite_token}",
        headers={"X-Forwarded-Proto": "https"},
        follow_redirects=False,
    )
    assert start.status_code == 302
    assert "Secure" in " ".join(start.headers.get_list("set-cookie"))
    from urllib.parse import parse_qs, urlparse

    oauth_state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    cb = browser1.get(
        f"/api/auth/google/callback?code=auth-code-1&state={oauth_state}",
        headers={"X-Forwarded-Proto": "https"},
        follow_redirects=False,
    )
    assert cb.status_code == 302
    assert cb.headers["location"] == "/"
    assert "gf_session=" in " ".join(cb.headers.get_list("set-cookie"))
    assert "Secure" in " ".join(cb.headers.get_list("set-cookie"))

    session_cookie = browser1.cookies.get("gf_session")
    assert session_cookie
    sess1 = browser1.get("/api/auth/session", headers={"X-Forwarded-Proto": "https"}).json()
    assert sess1["authenticated"] is True
    assert sess1["role"] == "friend"
    user_id = sess1["user"]["user_id"]

    # Restart app with identical GRIDFORGE_SESSION_SECRET
    app2 = e2e_env.build_app(auth_mode="hosted", env=env_vars)
    browser2 = e2e_env.new_client(app2, base_url="https://gridmaps.net")
    browser2.cookies.set("gf_session", session_cookie)
    sess2 = browser2.get("/api/auth/session", headers={"X-Forwarded-Proto": "https"}).json()
    assert sess2["authenticated"] is True
    assert sess2["user"]["user_id"] == user_id

    # Admin disables user mid-session -> active cookie degrades immediately & re-login returns 403
    admin2 = e2e_env.login_admin(app2, base_url="http://gridmaps.net")
    dis = admin2.patch(
        f"/api/admin/users/{user_id}",
        json={"disabled": True},
        headers=_https_headers("gridmaps.net"),
    )
    assert dis.status_code == 200
    assert dis.json()["user"]["disabled"] is True

    after_dis = browser2.get("/api/auth/session", headers={"X-Forwarded-Proto": "https"})
    assert after_dis.status_code == 200
    assert after_dis.json()["authenticated"] is False
    assert after_dis.json()["role"] == "guest"

    re_start = browser2.get(
        "/api/auth/google/start",
        headers={"X-Forwarded-Proto": "https"},
        follow_redirects=False,
    )
    re_state = parse_qs(urlparse(re_start.headers["location"]).query)["state"][0]
    re_cb = browser2.get(
        f"/api/auth/google/callback?code=auth-code-2&state={re_state}",
        headers={"X-Forwarded-Proto": "https"},
        follow_redirects=False,
    )
    assert re_cb.status_code == 403
