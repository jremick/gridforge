"""Opaque-box E2E tests (Tiers 1 & 2) for GridForge Auth, Isolation, Admin & Session Contracts.

Covers `PROJECT.md § Feature Inventory`:
  - Feature #2: SQLite Multi-Tenant & Auth Schema
  - Feature #3: Opt-In Auth (`none` vs `hosted`) + Invite-Only Onboarding
  - Feature #5: Multi-Tenant Ownership & Pack Lockdown
  - Feature #7 (Admin): `/api/admin/{usage,users,invites,settings}`
  - Feature #10: `GET /api/auth/session` & `web/src/api.ts` Contract Alignment
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient
from pypdf import PdfWriter

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SAME_ORIGIN = {"Origin": "http://localhost:8080", "Sec-Fetch-Site": "same-origin"}
_HTTPS_SAME_ORIGIN = {
    "Origin": "https://studio.example",
    "Sec-Fetch-Site": "same-origin",
    "X-Forwarded-Proto": "https",
}


def _write_minimal_pdf(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    with path.open("wb") as fh:
        writer.write(fh)
    return path


def _create_completed_job(
    client: TestClient,
    data_dir: Path,
    db_path: Path,
    *,
    center: str = "-33.732, 150.312",
    title: str = "Test Sheet",
    filename: str = "map.pdf",
) -> str:
    from gridforge.web import db
    from gridforge.web.schemas import JobArtifact

    resp = client.post("/api/jobs", json={"center": center, "title": title}, headers=_SAME_ORIGIN)
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["job_id"]

    claimed = db.claim_next_job(db_path)
    assert claimed is not None
    # Claim the matching job if multiple exist in queue; complete it cleanly.
    while claimed.record.job_id != job_id:
        db.complete_job(db_path, claimed.record.job_id, artifacts=[], elapsed_seconds=0.1, peak_rss_mb=64.0)
        claimed = db.claim_next_job(db_path)
        assert claimed is not None

    pdf_path = _write_minimal_pdf(data_dir / job_id / "artifacts" / filename)
    artifact = JobArtifact(
        kind="pdf",
        filename=filename,
        size_bytes=pdf_path.stat().st_size,
        download_url=f"/api/jobs/{job_id}/artifacts/{filename}",
    )
    completed = db.complete_job(
        db_path,
        job_id,
        artifacts=[artifact],
        elapsed_seconds=1.25,
        peak_rss_mb=256.0,
    )
    assert completed is not None
    return job_id


def _create_project(client: TestClient, name: str = "Trip Alpha") -> dict[str, Any]:
    resp = client.post(
        "/api/projects",
        json={"name": name, "description": "Weekend nav", "state": {"scale": 25000}},
        headers=_SAME_ORIGIN,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# =============================================================================
# Feature #2: SQLite Multi-Tenant & Auth Schema (5 Tier 1 + 5 Tier 2 = 10)
# =============================================================================


class TestSchemaAndPersistence:
    """Tier 1 & Tier 2 tests for SQLite multi-tenant/auth schema & WAL persistence."""

    def test_t1_schema_creates_all_required_tables(self, e2e_env) -> None:
        app = e2e_env.build_app(auth_mode="hosted")
        assert app is not None
        conn = sqlite3.connect(str(e2e_env.db_path))
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        finally:
            conn.close()

        expected = {
            "jobs",
            "projects",
            "worker_heartbeat",
            "users",
            "invites",
            "sessions",
            "usage_events",
            "app_settings",
        }
        assert expected.issubset(tables)

    def test_t1_schema_adds_owner_columns_and_enables_wal(self, e2e_env) -> None:
        e2e_env.build_app(auth_mode="hosted")
        conn = sqlite3.connect(str(e2e_env.db_path))
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            job_cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
            proj_cols = {row[1] for row in conn.execute("PRAGMA table_info(projects)").fetchall()}
        finally:
            conn.close()

        assert str(mode).lower() == "wal"
        assert {"owner_id", "owner_role"}.issubset(job_cols)
        assert {"owner_id", "owner_role"}.issubset(proj_cols)

    def test_t1_schema_persists_users_invites_sessions_across_restarts(self, e2e_env) -> None:
        app1 = e2e_env.build_app(auth_mode="hosted")
        admin1 = e2e_env.login_admin(app1)
        friend1, _ = e2e_env.invite_and_accept_friend(
            app1, admin1, email="persist@example.com", password="CorrectHorseBattery99!"
        )
        # Create second unused invite to verify invites persist too
        inv_resp = admin1.post(
            "/api/admin/invites",
            json={"email": "pending@example.com", "role": "friend"},
            headers=_SAME_ORIGIN,
        )
        assert inv_resp.status_code == 201
        friend_cookies = dict(friend1.cookies)

        # Restart: build a completely fresh FastAPI instance against the same DB
        app2 = e2e_env.build_app(auth_mode="hosted")
        friend2 = e2e_env.new_client(app2)
        friend2.cookies.update(friend_cookies)
        sess = friend2.get("/api/auth/session")
        assert sess.status_code == 200
        body = sess.json()
        assert body["authenticated"] is True
        assert body["role"] == "friend"
        assert body["user"]["email"] == "persist@example.com"

        admin2 = e2e_env.login_admin(app2)
        users_resp = admin2.get("/api/admin/users")
        assert users_resp.status_code == 200
        emails = {u["email"] for u in users_resp.json()["users"]}
        invite_emails = {i.get("email") for i in users_resp.json()["invites"]}
        assert "persist@example.com" in emails
        assert "pending@example.com" in invite_emails

    def test_t1_schema_records_usage_events_for_renders_and_previews(self, e2e_env) -> None:
        e2e_env.seed_synthetic_pack("blue-mountains")
        app = e2e_env.build_app(auth_mode="hosted")
        admin = e2e_env.login_admin(app)
        friend, _ = e2e_env.invite_and_accept_friend(
            app, admin, email="usage@example.com", password="StrongPassword123!"
        )

        job_resp = friend.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=_SAME_ORIGIN)
        assert job_resp.status_code == 202
        vs_resp = friend.post(
            "/api/viewshed-preview",
            json={
                "observer": {
                    "label": "OP1",
                    "lat": -33.732,
                    "lon": 150.312,
                    "observer_height_m": 2.0,
                    "target_height_m": 1.5,
                    "max_distance_m": 3000.0,
                },
                "dem_source": "auto",
                "pack_id": "blue-mountains",
            },
            headers=_SAME_ORIGIN,
        )
        assert vs_resp.status_code == 200

        usage = admin.get("/api/admin/usage")
        assert usage.status_code == 200
        data = usage.json()
        assert data["summary"]["renders_today"] >= 1
        assert data["summary"]["previews_today"] >= 1
        assert "friend" in data["by_role"]
        assert len(data["recent_events"]) >= 2

    def test_t1_schema_backward_compatible_migration_on_legacy_v14_db(self, e2e_env) -> None:
        # Seed a pre-M1 SQLite DB without owner_id/owner_role or auth tables
        e2e_env.data_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(e2e_env.db_path))
        try:
            conn.executescript(
                """
                CREATE TABLE jobs (
                    job_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    progress_pct INTEGER NOT NULL DEFAULT 0,
                    title TEXT,
                    center TEXT NOT NULL,
                    center_mgrs TEXT,
                    scale INTEGER NOT NULL,
                    paper TEXT NOT NULL,
                    orientation TEXT NOT NULL,
                    color_mode TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    overlay_path TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    elapsed_seconds REAL,
                    peak_rss_mb REAL,
                    error TEXT,
                    artifacts_json TEXT NOT NULL DEFAULT '[]'
                );
                CREATE TABLE projects (
                    project_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    state_json TEXT NOT NULL,
                    last_job_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                INSERT INTO jobs (
                    job_id, status, stage, progress_pct, title, center, scale,
                    paper, orientation, color_mode, request_json, created_at
                ) VALUES (
                    'legacyjob001', 'succeeded', 'done', 100, 'Legacy Map',
                    '-33.732, 150.312', 25000, 'a4', 'portrait', 'color',
                    '{"center":"-33.732, 150.312"}', '2026-11-25T10:00:00Z'
                );
                INSERT INTO projects (
                    project_id, name, description, state_json, last_job_id, created_at, updated_at
                ) VALUES (
                    'legacyproj01', 'Legacy Trip', 'Saved pre-auth', '{}',
                    'legacyjob001', '2026-11-25T10:00:00Z', '2026-11-25T10:00:00Z'
                );
                """
            )
            conn.commit()
        finally:
            conn.close()

        app = e2e_env.build_app(auth_mode="none")
        client = e2e_env.new_client(app)
        jobs = client.get("/api/jobs").json()["jobs"]
        projects = client.get("/api/projects").json()["projects"]
        assert any(j["job_id"] == "legacyjob001" for j in jobs)
        assert any(p["project_id"] == "legacyproj01" for p in projects)

    def test_t2_schema_legacy_rows_not_leaked_to_hosted_guest_after_migration(self, e2e_env) -> None:
        # Create row in none mode first
        none_app = e2e_env.build_app(auth_mode="none")
        none_client = e2e_env.new_client(none_app)
        job_resp = none_client.post("/api/jobs", json={"center": "10,10"}, headers=_SAME_ORIGIN)
        assert job_resp.status_code == 202
        legacy_job_id = job_resp.json()["job_id"]
        legacy_proj = _create_project(none_client, "Local Homelab Trip")

        # Switch deployment to hosted mode on same DB
        hosted_app = e2e_env.build_app(auth_mode="hosted")
        guest = e2e_env.make_guest_client(hosted_app)
        assert guest.get("/api/jobs").json()["jobs"] == []
        assert guest.get("/api/projects").json()["projects"] == []
        assert guest.get(f"/api/jobs/{legacy_job_id}").status_code == 403
        assert guest.get(f"/api/projects/{legacy_proj['project_id']}").status_code == 403

    def test_t2_schema_unique_email_prevents_duplicate_users(self, e2e_env) -> None:
        app = e2e_env.build_app(auth_mode="hosted")
        admin = e2e_env.login_admin(app)
        e2e_env.invite_and_accept_friend(
            app, admin, email="dupe@example.com", password="Password1234!"
        )
        inv2 = admin.post(
            "/api/admin/invites",
            json={"email": "DUPE@example.com", "role": "friend"},
            headers=_SAME_ORIGIN,
        )
        if inv2.status_code == 201:
            token = inv2.json()["token"]
            client2 = e2e_env.new_client(app)
            accept2 = client2.post(
                "/api/auth/accept-invite",
                json={"token": token, "email": "dupe@example.com", "password": "Password1234!"},
                headers=_SAME_ORIGIN,
            )
            assert accept2.status_code in (400, 403, 409)
        else:
            assert inv2.status_code in (400, 409)

    def test_t2_schema_app_settings_persists_kill_switch_across_restarts(self, e2e_env) -> None:
        app1 = e2e_env.build_app(auth_mode="hosted")
        admin1 = e2e_env.login_admin(app1)
        res = admin1.post(
            "/api/admin/settings",
            json={"public_renders_paused": True},
            headers=_SAME_ORIGIN,
        )
        assert res.status_code == 200
        assert res.json()["public_renders_paused"] is True

        app2 = e2e_env.build_app(auth_mode="hosted")
        guest2 = e2e_env.make_guest_client(app2)
        sess = guest2.get("/api/auth/session").json()
        assert sess["quotas"]["public_renders_paused"] is True

    def test_t2_schema_wal_concurrent_read_write_no_lock_errors(self, e2e_env) -> None:
        app = e2e_env.build_app(auth_mode="hosted")
        admin = e2e_env.login_admin(app)

        conn = sqlite3.connect(str(e2e_env.db_path), timeout=1.0)
        try:
            conn.execute("BEGIN")
            conn.execute("SELECT COUNT(*) FROM jobs").fetchone()
            # Concurrent HTTP writes/reads while another WAL reader is open
            for idx in range(5):
                resp = admin.post(
                    "/api/projects",
                    json={"name": f"WAL Proj {idx}", "state": {}},
                    headers=_SAME_ORIGIN,
                )
                assert resp.status_code == 201
            conn.commit()
        finally:
            conn.close()

        assert len(admin.get("/api/projects").json()["projects"]) == 5

    def test_t2_schema_expired_session_falls_back_cleanly_to_guest(self, e2e_env) -> None:
        app = e2e_env.build_app(auth_mode="hosted")
        admin = e2e_env.login_admin(app)
        friend, _ = e2e_env.invite_and_accept_friend(
            app, admin, email="expiring@example.com", password="Password1234!"
        )
        assert friend.get("/api/auth/session").json()["authenticated"] is True

        # Age all session expiry timestamps into the past
        past_iso = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn = sqlite3.connect(str(e2e_env.db_path))
        try:
            conn.execute("UPDATE sessions SET expires_at = ?", (past_iso,))
            conn.commit()
        finally:
            conn.close()

        sess = friend.get("/api/auth/session")
        assert sess.status_code == 200
        assert sess.json()["authenticated"] is False
        assert sess.json()["role"] == "guest"
        assert sess.json()["user"] is None


# =============================================================================
# Feature #3: Opt-In Auth (`none` vs `hosted`) + Invite-Only Onboarding (6 T1 + 8 T2 = 14)
# =============================================================================


class TestHostedAuthAndOnboarding:
    """Tier 1 & Tier 2 tests for hosted authentication, invites, and mocked Google OAuth."""

    def test_t1_hosted_admin_seed_login_sets_httponly_cookie(self, e2e_env) -> None:
        app = e2e_env.build_app(auth_mode="hosted")
        client = e2e_env.new_client(app)
        resp = client.post(
            "/api/auth/login",
            json={
                "email": e2e_env.admin_email,
                "password": e2e_env.admin_password,
            },
            headers=_SAME_ORIGIN,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["auth_mode"] == "hosted"
        assert body["authenticated"] is True
        assert body["role"] == "admin"
        assert body["user"]["email"] == e2e_env.admin_email
        set_cookie = resp.headers.get("set-cookie", "")
        assert "gf_session=" in set_cookie
        assert "httponly" in set_cookie.lower()

    def test_t1_hosted_public_register_always_returns_403(self, e2e_env) -> None:
        app = e2e_env.build_app(auth_mode="hosted")
        client = e2e_env.new_client(app)
        resp = client.post(
            "/api/auth/register",
            json={"email": "stranger@example.com", "password": "Password1234!"},
            headers=_SAME_ORIGIN,
        )
        assert resp.status_code == 403
        assert "invite" in resp.json()["detail"].lower()

    def test_t1_hosted_invite_create_and_accept_onboards_friend(self, e2e_env) -> None:
        app = e2e_env.build_app(auth_mode="hosted")
        admin = e2e_env.login_admin(app)
        inv = admin.post(
            "/api/admin/invites",
            json={"email": "mate@example.com", "role": "friend", "expires_hours": 48},
            headers=_SAME_ORIGIN,
        )
        assert inv.status_code == 201
        inv_data = inv.json()
        assert inv_data["invite_id"]
        assert inv_data["token"]
        assert inv_data["invite_path"].startswith("/")
        assert inv_data["expires_at"]

        friend = e2e_env.new_client(app)
        acc = friend.post(
            "/api/auth/accept-invite",
            json={
                "token": inv_data["token"],
                "email": "mate@example.com",
                "password": "TrailPassword42!",
                "display_name": "Mate",
            },
            headers=_SAME_ORIGIN,
        )
        assert acc.status_code == 200
        body = acc.json()
        assert body["authenticated"] is True
        assert body["role"] == "friend"
        assert body["user"]["email"] == "mate@example.com"
        assert body["user"]["display_name"] == "Mate"
        assert "gf_session=" in acc.headers.get("set-cookie", "")

        # Verify password persisted for subsequent explicit login
        fresh = e2e_env.new_client(app)
        login2 = fresh.post(
            "/api/auth/login",
            json={"email": "mate@example.com", "password": "TrailPassword42!"},
            headers=_SAME_ORIGIN,
        )
        assert login2.status_code == 200
        assert login2.json()["user"]["user_id"] == body["user"]["user_id"]

    def test_t1_hosted_logout_clears_session_and_reverts_to_guest(self, e2e_env) -> None:
        app = e2e_env.build_app(auth_mode="hosted")
        admin = e2e_env.login_admin(app)
        assert admin.get("/api/admin/usage").status_code == 200

        out = admin.post("/api/auth/logout", headers=_SAME_ORIGIN)
        assert out.status_code == 200
        body = out.json()
        assert body["authenticated"] is False
        assert body["role"] == "guest"
        assert body["user"] is None

        assert admin.get("/api/admin/usage").status_code == 403
        assert admin.get("/api/auth/session").json()["role"] == "guest"

    def test_t1_hosted_google_oauth_invite_onboarding_flow(self, e2e_env, monkeypatch) -> None:
        e2e_env.mock_google_oauth(
            monkeypatch,
            email="oauth-friend@example.com",
            name="OAuth Friend",
            sub="sub-oauth-001",
        )
        app = e2e_env.build_app(
            auth_mode="hosted",
            GRIDFORGE_GOOGLE_CLIENT_ID="test-google-client-id",
            GRIDFORGE_GOOGLE_CLIENT_SECRET="test-google-secret",
        )
        admin = e2e_env.login_admin(app)
        inv = admin.post(
            "/api/admin/invites",
            json={"email": "oauth-friend@example.com", "role": "friend"},
            headers=_SAME_ORIGIN,
        )
        assert inv.status_code == 201
        token = inv.json()["token"]

        browser = e2e_env.new_client(app)
        start = browser.get(
            "/api/auth/google/start",
            params={"invite_token": token},
            follow_redirects=False,
        )
        assert start.status_code == 302
        location = start.headers["location"]
        assert "accounts.google.com" in location
        assert "state=" in location
        state = parse_qs(urlparse(location).query)["state"][0]

        cb = browser.get(
            "/api/auth/google/callback",
            params={"code": "auth-code-123", "state": state},
            follow_redirects=False,
        )
        assert cb.status_code == 302
        assert cb.headers["location"] == "/"

        sess = browser.get("/api/auth/session").json()
        assert sess["authenticated"] is True
        assert sess["role"] == "friend"
        assert sess["user"]["email"] == "oauth-friend@example.com"

    def test_t1_hosted_google_oauth_login_existing_user(self, e2e_env, monkeypatch) -> None:
        e2e_env.mock_google_oauth(
            monkeypatch,
            email="existing-oauth@example.com",
            name="Existing OAuth",
            sub="sub-existing-002",
        )
        app = e2e_env.build_app(
            auth_mode="hosted",
            GRIDFORGE_GOOGLE_CLIENT_ID="test-google-client-id",
            GRIDFORGE_GOOGLE_CLIENT_SECRET="test-google-secret",
        )
        admin = e2e_env.login_admin(app)
        _, seeded_sess = e2e_env.invite_and_accept_friend(
            app, admin, email="existing-oauth@example.com", password="Password1234!"
        )

        browser = e2e_env.new_client(app)
        start = browser.get("/api/auth/google/start", follow_redirects=False)
        assert start.status_code == 302
        state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]

        cb = browser.get(
            "/api/auth/google/callback",
            params={"code": "auth-code-xyz", "state": state},
            follow_redirects=False,
        )
        assert cb.status_code == 302
        sess = browser.get("/api/auth/session").json()
        assert sess["authenticated"] is True
        assert sess["user"]["user_id"] == seeded_sess["user"]["user_id"]

    def test_t2_hosted_google_oauth_start_returns_503_when_unconfigured(self, e2e_env) -> None:
        app = e2e_env.build_app(
            auth_mode="hosted",
            GRIDFORGE_GOOGLE_CLIENT_ID="",
            GRIDFORGE_GOOGLE_CLIENT_SECRET="",
        )
        client = e2e_env.new_client(app)
        resp = client.get("/api/auth/google/start", follow_redirects=False)
        assert resp.status_code == 503

    def test_t2_hosted_google_oauth_callback_rejects_mismatched_state(self, e2e_env, monkeypatch) -> None:
        e2e_env.mock_google_oauth(monkeypatch, email="attacker@example.com")
        app = e2e_env.build_app(
            auth_mode="hosted",
            GRIDFORGE_GOOGLE_CLIENT_ID="test-google-client-id",
            GRIDFORGE_GOOGLE_CLIENT_SECRET="test-google-secret",
        )
        browser = e2e_env.new_client(app)
        start = browser.get("/api/auth/google/start", follow_redirects=False)
        assert start.status_code == 302

        bad_cb = browser.get(
            "/api/auth/google/callback",
            params={"code": "auth-code", "state": "forged-state-value"},
            follow_redirects=False,
        )
        assert bad_cb.status_code in (400, 403)
        assert browser.get("/api/auth/session").json()["authenticated"] is False

    def test_t2_hosted_login_invalid_credentials_returns_401(self, e2e_env) -> None:
        app = e2e_env.build_app(auth_mode="hosted")
        client = e2e_env.new_client(app)
        bad_pw = client.post(
            "/api/auth/login",
            json={"email": e2e_env.admin_email, "password": "wrong-password"},
            headers=_SAME_ORIGIN,
        )
        assert bad_pw.status_code == 401
        assert "gf_session=" not in bad_pw.headers.get("set-cookie", "")

        unknown = client.post(
            "/api/auth/login",
            json={"email": "nobody@example.com", "password": "Password1234!"},
            headers=_SAME_ORIGIN,
        )
        assert unknown.status_code == 401

    def test_t2_hosted_disabled_user_login_403_and_session_invalidated(self, e2e_env) -> None:
        app = e2e_env.build_app(auth_mode="hosted")
        admin = e2e_env.login_admin(app)
        friend, friend_sess = e2e_env.invite_and_accept_friend(
            app, admin, email="disableme@example.com", password="Password1234!"
        )
        uid = friend_sess["user"]["user_id"]

        patch = admin.patch(
            f"/api/admin/users/{uid}",
            json={"disabled": True},
            headers=_SAME_ORIGIN,
        )
        assert patch.status_code == 200
        assert patch.json()["user"]["disabled"] is True

        # Active cookie must immediately lose authenticated role
        sess_after = friend.get("/api/auth/session").json()
        assert sess_after["authenticated"] is False
        assert sess_after["role"] == "guest"

        # Fresh login must fail with 403
        fresh = e2e_env.new_client(app)
        relogin = fresh.post(
            "/api/auth/login",
            json={"email": "disableme@example.com", "password": "Password1234!"},
            headers=_SAME_ORIGIN,
        )
        assert relogin.status_code == 403

    def test_t2_hosted_accept_invite_rejects_expired_token(self, e2e_env) -> None:
        app = e2e_env.build_app(auth_mode="hosted")
        admin = e2e_env.login_admin(app)
        inv = admin.post(
            "/api/admin/invites",
            json={"email": "late@example.com", "role": "friend"},
            headers=_SAME_ORIGIN,
        ).json()

        past_iso = (datetime.now(timezone.utc) - timedelta(seconds=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn = sqlite3.connect(str(e2e_env.db_path))
        try:
            conn.execute("UPDATE invites SET expires_at = ? WHERE invite_id = ?", (past_iso, inv["invite_id"]))
            conn.commit()
        finally:
            conn.close()

        client = e2e_env.new_client(app)
        resp = client.post(
            "/api/auth/accept-invite",
            json={"token": inv["token"], "email": "late@example.com", "password": "Password1234!"},
            headers=_SAME_ORIGIN,
        )
        assert resp.status_code in (400, 403, 410)

    def test_t2_hosted_accept_invite_rejects_revoked_and_reused_tokens(self, e2e_env) -> None:
        app = e2e_env.build_app(auth_mode="hosted")
        admin = e2e_env.login_admin(app)

        # Revoked token
        inv1 = admin.post(
            "/api/admin/invites",
            json={"email": "revoked@example.com", "role": "friend"},
            headers=_SAME_ORIGIN,
        ).json()
        assert admin.delete(f"/api/admin/invites/{inv1['invite_id']}", headers=_SAME_ORIGIN).status_code == 200
        c1 = e2e_env.new_client(app)
        r1 = c1.post(
            "/api/auth/accept-invite",
            json={"token": inv1["token"], "email": "revoked@example.com", "password": "Password1234!"},
            headers=_SAME_ORIGIN,
        )
        assert r1.status_code in (400, 403, 410)

        # Reused token
        inv2 = admin.post(
            "/api/admin/invites",
            json={"email": "once@example.com", "role": "friend"},
            headers=_SAME_ORIGIN,
        ).json()
        c2 = e2e_env.new_client(app)
        ok = c2.post(
            "/api/auth/accept-invite",
            json={"token": inv2["token"], "email": "once@example.com", "password": "Password1234!"},
            headers=_SAME_ORIGIN,
        )
        assert ok.status_code == 200
        c3 = e2e_env.new_client(app)
        reuse = c3.post(
            "/api/auth/accept-invite",
            json={"token": inv2["token"], "email": "second@example.com", "password": "Password1234!"},
            headers=_SAME_ORIGIN,
        )
        assert reuse.status_code in (400, 403, 409, 410)

    def test_t2_hosted_accept_invite_rejects_wrong_email_and_empty_password(self, e2e_env) -> None:
        app = e2e_env.build_app(auth_mode="hosted")
        admin = e2e_env.login_admin(app)
        inv = admin.post(
            "/api/admin/invites",
            json={"email": "intended@example.com", "role": "friend"},
            headers=_SAME_ORIGIN,
        ).json()

        c_wrong = e2e_env.new_client(app)
        wrong_email = c_wrong.post(
            "/api/auth/accept-invite",
            json={"token": inv["token"], "email": "other@example.com", "password": "Password1234!"},
            headers=_SAME_ORIGIN,
        )
        assert wrong_email.status_code in (400, 403)

        empty_pw = c_wrong.post(
            "/api/auth/accept-invite",
            json={"token": inv["token"], "email": "intended@example.com", "password": ""},
            headers=_SAME_ORIGIN,
        )
        assert empty_pw.status_code in (400, 422)

    def test_t2_hosted_admin_invite_rejects_invalid_role_422(self, e2e_env) -> None:
        app = e2e_env.build_app(auth_mode="hosted")
        admin = e2e_env.login_admin(app)
        resp = admin.post(
            "/api/admin/invites",
            json={"email": "badrole@example.com", "role": "superadmin"},
            headers=_SAME_ORIGIN,
        )
        assert resp.status_code == 422


# =============================================================================
# Feature #5: Multi-Tenant Ownership & Pack Lockdown (5 Tier 1 + 7 Tier 2 = 12)
# =============================================================================


class TestMultiTenantIsolationAndPackLockdown:
    """Tier 1 & Tier 2 tests for owner_id isolation across jobs/projects and pack admin gating."""

    def test_t1_isolation_jobs_list_filtered_by_owner(self, e2e_env) -> None:
        app = e2e_env.build_app(auth_mode="hosted")
        admin = e2e_env.login_admin(app)
        friend, _ = e2e_env.invite_and_accept_friend(
            app, admin, email="f1@example.com", password="Password1234!"
        )
        guest = e2e_env.make_guest_client(app)

        j_admin = admin.post("/api/jobs", json={"center": "10,10", "title": "AdminJob"}, headers=_SAME_ORIGIN).json()["job_id"]
        j_friend = friend.post("/api/jobs", json={"center": "11,11", "title": "FriendJob"}, headers=_SAME_ORIGIN).json()["job_id"]
        j_guest = guest.post("/api/jobs", json={"center": "12,12", "title": "GuestJob"}, headers=_SAME_ORIGIN).json()["job_id"]

        assert [j["job_id"] for j in admin.get("/api/jobs").json()["jobs"]] == [j_admin]
        assert [j["job_id"] for j in friend.get("/api/jobs").json()["jobs"]] == [j_friend]
        assert [j["job_id"] for j in guest.get("/api/jobs").json()["jobs"]] == [j_guest]

    def test_t1_isolation_projects_list_filtered_by_owner(self, e2e_env) -> None:
        app = e2e_env.build_app(auth_mode="hosted")
        admin = e2e_env.login_admin(app)
        friend, _ = e2e_env.invite_and_accept_friend(
            app, admin, email="pf1@example.com", password="Password1234!"
        )
        guest = e2e_env.make_guest_client(app)

        p_admin = _create_project(admin, "Admin Proj")["project_id"]
        p_friend = _create_project(friend, "Friend Proj")["project_id"]
        p_guest = _create_project(guest, "Guest Proj")["project_id"]

        assert [p["project_id"] for p in admin.get("/api/projects").json()["projects"]] == [p_admin]
        assert [p["project_id"] for p in friend.get("/api/projects").json()["projects"]] == [p_friend]
        assert [p["project_id"] for p in guest.get("/api/projects").json()["projects"]] == [p_guest]

    def test_t1_isolation_owner_full_job_lifecycle_artifacts_and_thumbnail(self, e2e_env) -> None:
        app = e2e_env.build_app(auth_mode="hosted")
        admin = e2e_env.login_admin(app)
        friend, _ = e2e_env.invite_and_accept_friend(
            app, admin, email="owner-job@example.com", password="Password1234!"
        )

        # Active job cancel
        queued = friend.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=_SAME_ORIGIN)
        assert queued.status_code == 202
        qid = queued.json()["job_id"]
        assert friend.get(f"/api/jobs/{qid}").status_code == 200
        assert friend.post(f"/api/jobs/{qid}/cancel", headers=_SAME_ORIGIN).json()["status"] == "cancelled"

        # Completed job artifact & thumbnail & delete
        cid = _create_completed_job(friend, e2e_env.data_dir, e2e_env.db_path, filename="sheet.pdf")
        art = friend.get(f"/api/jobs/{cid}/artifacts/sheet.pdf")
        assert art.status_code == 200
        assert art.content.startswith(b"%PDF")
        thumb = friend.get(f"/api/jobs/{cid}/thumbnail.png")
        assert thumb.status_code == 200
        assert thumb.headers["content-type"] == "image/png"
        assert friend.delete(f"/api/jobs/{cid}", headers=_SAME_ORIGIN).status_code == 200

    def test_t1_isolation_owner_full_project_crud(self, e2e_env) -> None:
        app = e2e_env.build_app(auth_mode="hosted")
        guest = e2e_env.make_guest_client(app)
        proj = _create_project(guest, "Guest Nav Plan")
        pid = proj["project_id"]

        assert guest.get(f"/api/projects/{pid}").status_code == 200
        patched = guest.patch(
            f"/api/projects/{pid}",
            json={"name": "Updated Plan", "notes": "Day 2"},
            headers=_SAME_ORIGIN,
        )
        assert patched.status_code == 200
        assert patched.json()["name"] == "Updated Plan"
        assert guest.delete(f"/api/projects/{pid}", headers=_SAME_ORIGIN).status_code == 200

    def test_t1_packs_admin_can_build_list_tasks_and_delete_while_catalog_is_readable(self, e2e_env) -> None:
        e2e_env.seed_synthetic_pack("blue-mountains")
        app = e2e_env.build_app(auth_mode="hosted")
        admin = e2e_env.login_admin(app)
        guest = e2e_env.make_guest_client(app)

        # Catalog read routes remain open in hosted mode
        assert guest.get("/api/packs").status_code == 200
        assert guest.get("/api/packs/regions").status_code == 200

        build = admin.post(
            "/api/packs/build",
            json={"region_or_bbox": "act", "include_osm": False, "include_dem": False},
            headers=_SAME_ORIGIN,
        )
        assert build.status_code == 202
        tid = build.json()["task_id"]
        assert admin.get("/api/packs/tasks").status_code == 200
        assert admin.get(f"/api/packs/tasks/{tid}").status_code == 200
        assert admin.delete("/api/packs/blue-mountains", headers=_SAME_ORIGIN).status_code == 200

    def test_t2_isolation_cross_tenant_job_actions_return_403(self, e2e_env) -> None:
        app = e2e_env.build_app(auth_mode="hosted")
        admin = e2e_env.login_admin(app)
        friend_a, _ = e2e_env.invite_and_accept_friend(
            app, admin, email="fa@example.com", password="Password1234!"
        )
        friend_b, _ = e2e_env.invite_and_accept_friend(
            app, admin, email="fb@example.com", password="Password1234!"
        )

        job_id = friend_a.post("/api/jobs", json={"center": "10,10"}, headers=_SAME_ORIGIN).json()["job_id"]

        assert friend_b.get(f"/api/jobs/{job_id}").status_code == 403
        assert friend_b.post(f"/api/jobs/{job_id}/cancel", headers=_SAME_ORIGIN).status_code == 403
        assert friend_b.delete(f"/api/jobs/{job_id}", headers=_SAME_ORIGIN).status_code == 403
        assert friend_a.get(f"/api/jobs/{job_id}").json()["status"] == "queued"

    def test_t2_isolation_cross_tenant_job_artifact_and_thumbnail_return_403(self, e2e_env) -> None:
        app = e2e_env.build_app(auth_mode="hosted")
        admin = e2e_env.login_admin(app)
        friend_a, _ = e2e_env.invite_and_accept_friend(
            app, admin, email="arta@example.com", password="Password1234!"
        )
        friend_b, _ = e2e_env.invite_and_accept_friend(
            app, admin, email="artb@example.com", password="Password1234!"
        )
        job_id = _create_completed_job(friend_a, e2e_env.data_dir, e2e_env.db_path, filename="secret.pdf")

        assert friend_b.get(f"/api/jobs/{job_id}/artifacts/secret.pdf").status_code == 403
        assert friend_b.get(f"/api/jobs/{job_id}/thumbnail.png").status_code == 403

    def test_t2_isolation_cross_tenant_project_actions_return_403(self, e2e_env) -> None:
        app = e2e_env.build_app(auth_mode="hosted")
        admin = e2e_env.login_admin(app)
        friend_a, _ = e2e_env.invite_and_accept_friend(
            app, admin, email="pa@example.com", password="Password1234!"
        )
        friend_b, _ = e2e_env.invite_and_accept_friend(
            app, admin, email="pb@example.com", password="Password1234!"
        )
        pid = _create_project(friend_a, "Private Route")["project_id"]

        assert friend_b.get(f"/api/projects/{pid}").status_code == 403
        assert friend_b.patch(f"/api/projects/{pid}", json={"name": "Hijacked"}, headers=_SAME_ORIGIN).status_code == 403
        assert friend_b.delete(f"/api/projects/{pid}", headers=_SAME_ORIGIN).status_code == 403
        assert friend_a.get(f"/api/projects/{pid}").json()["name"] == "Private Route"

    def test_t2_isolation_nonexistent_ids_return_404_not_403(self, e2e_env) -> None:
        app = e2e_env.build_app(auth_mode="hosted")
        guest = e2e_env.make_guest_client(app)

        assert guest.get("/api/jobs/nonexistent99").status_code == 404
        assert guest.post("/api/jobs/nonexistent99/cancel", headers=_SAME_ORIGIN).status_code == 404
        assert guest.delete("/api/jobs/nonexistent99", headers=_SAME_ORIGIN).status_code == 404
        assert guest.get("/api/jobs/nonexistent99/artifacts/map.pdf").status_code == 404
        assert guest.get("/api/jobs/nonexistent99/thumbnail.png").status_code == 404
        assert guest.get("/api/projects/nonexistent99").status_code == 404
        assert guest.patch("/api/projects/nonexistent99", json={"name": "x"}, headers=_SAME_ORIGIN).status_code == 404
        assert guest.delete("/api/projects/nonexistent99", headers=_SAME_ORIGIN).status_code == 404

    def test_t2_isolation_two_distinct_guests_cannot_access_each_others_resources(self, e2e_env) -> None:
        app = e2e_env.build_app(auth_mode="hosted")
        guest1 = e2e_env.make_guest_client(app)
        guest2 = e2e_env.make_guest_client(app)

        g1_job = guest1.post("/api/jobs", json={"center": "10,10"}, headers=_SAME_ORIGIN).json()["job_id"]
        g1_proj = _create_project(guest1, "Guest 1 Secret")["project_id"]

        assert guest2.get("/api/jobs").json()["jobs"] == []
        assert guest2.get("/api/projects").json()["projects"] == []
        assert guest2.get(f"/api/jobs/{g1_job}").status_code == 403
        assert guest2.delete(f"/api/jobs/{g1_job}", headers=_SAME_ORIGIN).status_code == 403
        assert guest2.get(f"/api/projects/{g1_proj}").status_code == 403
        assert guest2.delete(f"/api/projects/{g1_proj}", headers=_SAME_ORIGIN).status_code == 403

    def test_t2_packs_lockdown_returns_403_for_guest_and_friend(self, e2e_env) -> None:
        pack_dir = e2e_env.seed_synthetic_pack("blue-mountains")
        app = e2e_env.build_app(auth_mode="hosted")
        admin = e2e_env.login_admin(app)
        task_id = admin.post(
            "/api/packs/build",
            json={"region_or_bbox": "act", "include_osm": False, "include_dem": False},
            headers=_SAME_ORIGIN,
        ).json()["task_id"]

        friend, _ = e2e_env.invite_and_accept_friend(
            app, admin, email="nopacks@example.com", password="Password1234!"
        )
        guest = e2e_env.make_guest_client(app)

        for actor in (guest, friend):
            assert actor.post(
                "/api/packs/build",
                json={"region_or_bbox": "act"},
                headers=_SAME_ORIGIN,
            ).status_code == 403
            assert actor.get("/api/packs/tasks").status_code == 403
            assert actor.get(f"/api/packs/tasks/{task_id}").status_code == 403
            assert actor.delete("/api/packs/blue-mountains", headers=_SAME_ORIGIN).status_code == 403

        assert (pack_dir / "pack.json").is_file()

    def test_t2_isolation_non_admin_cannot_access_admin_resources_or_jobs(self, e2e_env) -> None:
        app = e2e_env.build_app(auth_mode="hosted")
        admin = e2e_env.login_admin(app)
        friend, _ = e2e_env.invite_and_accept_friend(
            app, admin, email="viewer@example.com", password="Password1234!"
        )
        guest = e2e_env.make_guest_client(app)

        admin_job = admin.post("/api/jobs", json={"center": "10,10"}, headers=_SAME_ORIGIN).json()["job_id"]
        admin_proj = _create_project(admin, "Admin Only")["project_id"]

        for actor in (guest, friend):
            assert actor.get(f"/api/jobs/{admin_job}").status_code == 403
            assert actor.get(f"/api/projects/{admin_proj}").status_code == 403


# =============================================================================
# Feature #7 (Admin API): Usage, Users, Invites & Settings (5 Tier 1 + 5 Tier 2 = 10)
# =============================================================================


class TestAdminApi:
    """Tier 1 & Tier 2 tests for `/api/admin/{usage,users,invites,settings}`."""

    def test_t1_admin_usage_returns_exact_contract_schema(self, e2e_env) -> None:
        app = e2e_env.build_app(auth_mode="hosted")
        admin = e2e_env.login_admin(app)
        resp = admin.get("/api/admin/usage")
        assert resp.status_code == 200
        body = resp.json()
        assert set(["summary", "by_role", "recent_events"]).issubset(body.keys())
        summary = body["summary"]
        for key in (
            "renders_today",
            "renders_7d",
            "previews_today",
            "active_queue_depth",
            "global_daily_public_used",
            "global_daily_public_limit",
            "public_renders_paused",
            "avg_elapsed_seconds",
            "max_peak_rss_mb",
        ):
            assert key in summary, f"Missing summary key: {key}"

    def test_t1_admin_users_lists_users_and_invites(self, e2e_env) -> None:
        app = e2e_env.build_app(auth_mode="hosted")
        admin = e2e_env.login_admin(app)
        e2e_env.invite_and_accept_friend(
            app, admin, email="listed-friend@example.com", password="Password1234!"
        )
        admin.post(
            "/api/admin/invites",
            json={"email": "open-invite@example.com", "role": "friend"},
            headers=_SAME_ORIGIN,
        )

        resp = admin.get("/api/admin/users")
        assert resp.status_code == 200
        body = resp.json()
        assert "users" in body and "invites" in body
        user_emails = {u["email"] for u in body["users"]}
        assert e2e_env.admin_email in user_emails
        assert "listed-friend@example.com" in user_emails
        assert len(body["invites"]) >= 2

    def test_t1_admin_invites_create_and_revoke(self, e2e_env) -> None:
        app = e2e_env.build_app(auth_mode="hosted")
        admin = e2e_env.login_admin(app)
        created = admin.post(
            "/api/admin/invites",
            json={"email": "temp@example.com", "role": "friend", "expires_hours": 24},
            headers=_SAME_ORIGIN,
        )
        assert created.status_code == 201
        cdata = created.json()
        assert set(["invite_id", "token", "invite_path", "expires_at"]).issubset(cdata.keys())

        revoked = admin.delete(f"/api/admin/invites/{cdata['invite_id']}", headers=_SAME_ORIGIN)
        assert revoked.status_code == 200
        assert revoked.json() == {"revoked": True, "invite_id": cdata["invite_id"]}

    def test_t1_admin_patch_user_quota_overrides_and_role(self, e2e_env) -> None:
        app = e2e_env.build_app(auth_mode="hosted")
        admin = e2e_env.login_admin(app)
        friend, fsess = e2e_env.invite_and_accept_friend(
            app, admin, email="quotabump@example.com", password="Password1234!"
        )
        uid = fsess["user"]["user_id"]

        patched = admin.patch(
            f"/api/admin/users/{uid}",
            json={"daily_render_limit": 45, "hourly_preview_limit": 90, "role": "admin"},
            headers=_SAME_ORIGIN,
        )
        assert patched.status_code == 200
        user_obj = patched.json()["user"]
        assert user_obj["role"] == "admin"
        assert user_obj["daily_render_limit"] == 45
        assert user_obj["hourly_preview_limit"] == 90

        # Demote back to friend and verify quota override reflected in friend session
        demoted = admin.patch(
            f"/api/admin/users/{uid}",
            json={"role": "friend"},
            headers=_SAME_ORIGIN,
        )
        assert demoted.status_code == 200
        sess = friend.get("/api/auth/session").json()
        assert sess["role"] == "friend"
        assert sess["quotas"]["daily_renders_limit"] == 45
        assert sess["quotas"]["hourly_previews_limit"] == 90

    def test_t1_admin_settings_toggle_kill_switch(self, e2e_env) -> None:
        app = e2e_env.build_app(auth_mode="hosted")
        admin = e2e_env.login_admin(app)

        paused = admin.post(
            "/api/admin/settings",
            json={"public_renders_paused": True},
            headers=_SAME_ORIGIN,
        )
        assert paused.status_code == 200
        assert paused.json() == {"public_renders_paused": True}

        resumed = admin.post(
            "/api/admin/settings",
            json={"public_renders_paused": False},
            headers=_SAME_ORIGIN,
        )
        assert resumed.status_code == 200
        assert resumed.json() == {"public_renders_paused": False}

    def test_t2_admin_endpoints_return_403_for_guest_and_friend(self, e2e_env) -> None:
        app = e2e_env.build_app(auth_mode="hosted")
        admin = e2e_env.login_admin(app)
        friend, fsess = e2e_env.invite_and_accept_friend(
            app, admin, email="nonadmin@example.com", password="Password1234!"
        )
        inv_id = admin.post(
            "/api/admin/invites",
            json={"email": "x@example.com", "role": "friend"},
            headers=_SAME_ORIGIN,
        ).json()["invite_id"]
        uid = fsess["user"]["user_id"]
        guest = e2e_env.make_guest_client(app)

        for actor in (guest, friend):
            assert actor.get("/api/admin/usage").status_code == 403
            assert actor.get("/api/admin/users").status_code == 403
            assert actor.post(
                "/api/admin/invites", json={"role": "friend"}, headers=_SAME_ORIGIN
            ).status_code == 403
            assert actor.delete(f"/api/admin/invites/{inv_id}", headers=_SAME_ORIGIN).status_code == 403
            assert actor.patch(
                f"/api/admin/users/{uid}", json={"disabled": True}, headers=_SAME_ORIGIN
            ).status_code == 403
            assert actor.post(
                "/api/admin/settings", json={"public_renders_paused": True}, headers=_SAME_ORIGIN
            ).status_code == 403

    def test_t2_admin_patch_nonexistent_user_returns_404(self, e2e_env) -> None:
        app = e2e_env.build_app(auth_mode="hosted")
        admin = e2e_env.login_admin(app)
        resp = admin.patch(
            "/api/admin/users/missing-user-id-999",
            json={"disabled": True},
            headers=_SAME_ORIGIN,
        )
        assert resp.status_code == 404

    def test_t2_admin_delete_nonexistent_invite_returns_404(self, e2e_env) -> None:
        app = e2e_env.build_app(auth_mode="hosted")
        admin = e2e_env.login_admin(app)
        resp = admin.delete("/api/admin/invites/missing-invite-999", headers=_SAME_ORIGIN)
        assert resp.status_code == 404

    def test_t2_admin_patch_user_invalid_role_422_and_self_lockout_guard(self, e2e_env) -> None:
        app = e2e_env.build_app(auth_mode="hosted")
        admin = e2e_env.login_admin(app)
        admin_uid = admin.get("/api/auth/session").json()["user"]["user_id"]

        bad_role = admin.patch(
            f"/api/admin/users/{admin_uid}",
            json={"role": "root"},
            headers=_SAME_ORIGIN,
        )
        assert bad_role.status_code == 422

        # Guard against sole-admin disabling themselves
        self_disable = admin.patch(
            f"/api/admin/users/{admin_uid}",
            json={"disabled": True},
            headers=_SAME_ORIGIN,
        )
        assert self_disable.status_code in (400, 403, 422)
        assert admin.get("/api/auth/session").json()["authenticated"] is True

    def test_t2_admin_usage_aggregates_elapsed_seconds_and_peak_rss_mb(self, e2e_env) -> None:
        app = e2e_env.build_app(auth_mode="hosted")
        admin = e2e_env.login_admin(app)
        _create_completed_job(admin, e2e_env.data_dir, e2e_env.db_path)

        usage = admin.get("/api/admin/usage")
        assert usage.status_code == 200
        summary = usage.json()["summary"]
        assert summary["avg_elapsed_seconds"] is not None
        assert summary["avg_elapsed_seconds"] >= 1.0
        assert summary["max_peak_rss_mb"] is not None
        assert summary["max_peak_rss_mb"] >= 256.0


# =============================================================================
# Feature #10: Session & Capability Contract + api.ts Alignment (5 Tier 1 + 5 Tier 2 = 10)
# =============================================================================


class TestSessionAndCapabilityContract:
    """Tier 1 & Tier 2 tests for `GET /api/auth/session`, cookies, and `web/src/api.ts` contract."""

    def test_t1_session_contract_none_mode_exact_schema(self, e2e_env) -> None:
        app = e2e_env.build_app(auth_mode="none")
        client = e2e_env.new_client(app)
        resp = client.get("/api/auth/session")
        assert resp.status_code == 200
        body = resp.json()

        assert body["auth_mode"] == "none"
        assert body["authenticated"] is False
        assert body["user"] is None
        assert body["role"] == "none"

        caps = body["capabilities"]
        assert caps["max_grid_cells"] == 9
        assert caps["max_grid_cols"] == 3
        assert caps["max_grid_rows"] == 3
        assert caps["strip_atlas_allowed"] is True
        assert caps["packs_manage_allowed"] is True
        assert caps["turnstile_required"] is False
        assert caps["max_upload_bytes"] > 0
        assert set(["pdf", "mbtiles", "kmz", "gpkg_tiles", "osmand_sqlitedb", "field_bundle_zip"]).issubset(
            set(caps["allowed_outputs"])
        )

        quotas = body["quotas"]
        assert quotas["daily_renders_limit"] is None
        assert quotas["hourly_previews_limit"] is None
        assert quotas["public_renders_paused"] is False

    def test_t1_session_contract_hosted_guest_exact_schema_and_cookie(self, e2e_env) -> None:
        app = e2e_env.build_app(auth_mode="hosted")
        client = e2e_env.new_client(app)
        resp = client.get("/api/auth/session")
        assert resp.status_code == 200
        body = resp.json()

        assert body["auth_mode"] == "hosted"
        assert body["authenticated"] is False
        assert body["user"] is None
        assert body["role"] == "guest"

        caps = body["capabilities"]
        assert caps["max_grid_cells"] == 1
        assert caps["max_grid_cols"] == 1
        assert caps["max_grid_rows"] == 1
        assert caps["strip_atlas_allowed"] is False
        assert caps["max_strip_sheets"] == 0
        assert caps["allowed_outputs"] == ["pdf"]
        assert caps["packs_manage_allowed"] is False
        assert caps["max_upload_bytes"] > 0

        quotas = body["quotas"]
        assert quotas["daily_renders_limit"] == 2
        assert quotas["daily_renders_used"] == 0
        assert quotas["daily_renders_remaining"] == 2
        assert quotas["hourly_previews_limit"] == 5
        assert quotas["hourly_previews_used"] == 0
        assert quotas["hourly_previews_remaining"] == 5
        assert "public_renders_paused" in quotas
        assert "global_daily_public_limit" in quotas
        assert "global_daily_public_used" in quotas

        # Signed guest cookie is HttpOnly
        assert "httponly" in resp.headers.get("set-cookie", "").lower()

    def test_t1_session_contract_hosted_friend_exact_schema(self, e2e_env) -> None:
        app = e2e_env.build_app(auth_mode="hosted")
        admin = e2e_env.login_admin(app)
        _, body = e2e_env.invite_and_accept_friend(
            app, admin, email="friend-caps@example.com", password="Password1234!", display_name="Scout"
        )

        assert body["auth_mode"] == "hosted"
        assert body["authenticated"] is True
        assert body["role"] == "friend"
        assert body["user"]["email"] == "friend-caps@example.com"
        assert body["user"]["display_name"] == "Scout"
        assert body["user"]["role"] == "friend"

        caps = body["capabilities"]
        assert caps["max_grid_cells"] == 9
        assert caps["max_grid_cols"] == 3
        assert caps["max_grid_rows"] == 3
        assert caps["strip_atlas_allowed"] is True
        assert caps["max_strip_sheets"] >= 12
        assert caps["packs_manage_allowed"] is False
        assert caps["turnstile_required"] is False
        assert set(["pdf", "mbtiles", "kmz", "gpkg_tiles", "osmand_sqlitedb", "field_bundle_zip"]).issubset(
            set(caps["allowed_outputs"])
        )

        quotas = body["quotas"]
        assert quotas["daily_renders_limit"] == 20
        assert quotas["hourly_previews_limit"] == 60

    def test_t1_session_contract_hosted_admin_exact_schema(self, e2e_env) -> None:
        app = e2e_env.build_app(auth_mode="hosted")
        admin = e2e_env.login_admin(app)
        body = admin.get("/api/auth/session").json()

        assert body["auth_mode"] == "hosted"
        assert body["authenticated"] is True
        assert body["role"] == "admin"
        assert body["user"]["role"] == "admin"

        caps = body["capabilities"]
        assert caps["max_grid_cells"] >= 9
        assert caps["strip_atlas_allowed"] is True
        assert caps["packs_manage_allowed"] is True
        assert caps["turnstile_required"] is False
        assert set(["pdf", "mbtiles", "kmz", "gpkg_tiles", "osmand_sqlitedb", "field_bundle_zip"]).issubset(
            set(caps["allowed_outputs"])
        )

        quotas = body["quotas"]
        assert quotas["daily_renders_limit"] is None
        assert quotas["daily_renders_remaining"] is None
        assert quotas["hourly_previews_limit"] is None
        assert quotas["hourly_previews_remaining"] is None

    def test_t1_session_contract_dynamic_flags_turnstile_oauth_and_upload_bytes(self, e2e_env) -> None:
        app = e2e_env.build_app(
            auth_mode="hosted",
            GRIDFORGE_TURNSTILE_SITE_KEY="0x4AAAAAAATestSiteKey",
            GRIDFORGE_TURNSTILE_SECRET_KEY="0x4AAAAAAATestSecret",
            GRIDFORGE_GOOGLE_CLIENT_ID="google-id",
            GRIDFORGE_GOOGLE_CLIENT_SECRET="google-secret",
            GRIDFORGE_MAX_UPLOAD_BYTES_GUEST=str(2 * 1024 * 1024),
            GRIDFORGE_MAX_UPLOAD_BYTES_FRIEND=str(10 * 1024 * 1024),
            GRIDFORGE_MAX_UPLOAD_BYTES_ADMIN=str(20 * 1024 * 1024),
        )
        admin = e2e_env.login_admin(app)
        friend, _ = e2e_env.invite_and_accept_friend(
            app, admin, email="dyn@example.com", password="Password1234!"
        )
        guest = e2e_env.make_guest_client(app)

        g_caps = guest.get("/api/auth/session").json()["capabilities"]
        f_caps = friend.get("/api/auth/session").json()["capabilities"]
        a_caps = admin.get("/api/auth/session").json()["capabilities"]

        assert g_caps["turnstile_required"] is True
        assert g_caps["turnstile_sitekey"] == "0x4AAAAAAATestSiteKey"
        assert g_caps["google_oauth_enabled"] is True
        assert g_caps["max_upload_bytes"] == 2 * 1024 * 1024

        assert f_caps["turnstile_required"] is False
        assert f_caps["google_oauth_enabled"] is True
        assert f_caps["max_upload_bytes"] == 10 * 1024 * 1024

        assert a_caps["turnstile_required"] is False
        assert a_caps["max_upload_bytes"] == 20 * 1024 * 1024

    def test_t2_session_tampered_gf_session_cookie_rejected(self, e2e_env) -> None:
        app = e2e_env.build_app(auth_mode="hosted")
        admin = e2e_env.login_admin(app)
        real_cookie = admin.cookies.get("gf_session")
        assert real_cookie

        tampered_cookie = real_cookie[:-4] + ("AAAA" if real_cookie[-4:] != "AAAA" else "BBBB")
        attacker = e2e_env.new_client(app)
        attacker.cookies.set("gf_session", tampered_cookie)

        sess = attacker.get("/api/auth/session")
        assert sess.status_code == 200
        assert sess.json()["authenticated"] is False
        assert sess.json()["role"] == "guest"
        assert sess.json()["user"] is None
        assert attacker.get("/api/admin/usage").status_code == 403

    def test_t2_session_tampered_guest_cookie_rotated_and_cannot_hijack_owner(self, e2e_env) -> None:
        app = e2e_env.build_app(auth_mode="hosted")
        guest1 = e2e_env.make_guest_client(app)
        g1_job = guest1.post("/api/jobs", json={"center": "10,10"}, headers=_SAME_ORIGIN).json()["job_id"]

        guest_cookie_obj = next(c for c in guest1.cookies.jar if c.name == "gf_guest")
        orig_guest_cookie = guest_cookie_obj.value
        assert orig_guest_cookie
        tampered_guest = orig_guest_cookie[:-3] + ("xyz" if orig_guest_cookie[-3:] != "xyz" else "abc")

        attacker = e2e_env.new_client(app)
        attacker.cookies.clear()
        attacker.cookies.set(
            "gf_guest",
            tampered_guest,
            domain=guest_cookie_obj.domain,
            path=guest_cookie_obj.path,
        )

        sess = attacker.get("/api/auth/session")
        assert sess.status_code == 200
        assert sess.json()["role"] == "guest"
        assert "gf_guest=" in sess.headers.get("set-cookie", "")
        rotated_cookie = attacker.cookies.get("gf_guest")
        assert rotated_cookie is not None
        assert rotated_cookie != tampered_guest
        assert rotated_cookie != orig_guest_cookie
        assert attacker.get(f"/api/jobs/{g1_job}").status_code == 403

    def test_t2_session_cookie_security_attributes_on_https_proxy(self, e2e_env) -> None:
        app = e2e_env.build_app(
            auth_mode="hosted",
            GRIDFORGE_ALLOWED_HOSTS="studio.example",
            FORWARDED_ALLOW_IPS="*",
        )
        client = e2e_env.new_client(app, base_url="https://studio.example")
        resp = client.post(
            "/api/auth/login",
            json={"email": e2e_env.admin_email, "password": e2e_env.admin_password},
            headers=_HTTPS_SAME_ORIGIN,
        )
        assert resp.status_code == 200
        set_cookie = resp.headers.get("set-cookie", "").lower()
        assert "httponly" in set_cookie
        assert "samesite=lax" in set_cookie or "samesite=strict" in set_cookie
        assert "secure" in set_cookie

    def test_t2_session_quotas_decrement_after_render_and_preview(self, e2e_env) -> None:
        e2e_env.seed_synthetic_pack("blue-mountains")
        app = e2e_env.build_app(auth_mode="hosted")
        guest = e2e_env.make_guest_client(app)

        before = guest.get("/api/auth/session").json()["quotas"]
        assert before["daily_renders_used"] == 0
        assert before["hourly_previews_used"] == 0

        assert guest.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=_SAME_ORIGIN).status_code == 202
        assert (
            guest.post(
                "/api/viewshed-preview",
                json={
                    "observer": {
                        "label": "G1",
                        "lat": -33.732,
                        "lon": 150.312,
                        "observer_height_m": 2.0,
                        "target_height_m": 1.5,
                        "max_distance_m": 3000.0,
                    },
                    "dem_source": "auto",
                    "pack_id": "blue-mountains",
                },
                headers=_SAME_ORIGIN,
            ).status_code
            == 200
        )

        after = guest.get("/api/auth/session").json()["quotas"]
        assert after["daily_renders_used"] == 1
        assert after["daily_renders_remaining"] == before["daily_renders_limit"] - 1
        assert after["hourly_previews_used"] == 1
        assert after["hourly_previews_remaining"] == before["hourly_previews_limit"] - 1
        assert after["global_daily_public_used"] >= before["global_daily_public_used"] + 1

    def test_t2_web_api_ts_aligns_with_backend_auth_and_admin_contracts(self) -> None:
        raw_api_ts = (_REPO_ROOT / "web" / "src" / "api.ts").read_text(encoding="utf-8")
        raw_types_ts = (_REPO_ROOT / "web" / "src" / "types.ts").read_text(encoding="utf-8")

        def _strip_ts_comments(source: str) -> str:
            no_block = re.sub(r"/\*[\s\S]*?\*/", "", source)
            return re.sub(r"//[^\n]*", "", no_block)

        api_ts = _strip_ts_comments(raw_api_ts)
        types_ts = _strip_ts_comments(raw_types_ts)

        expected_api_calls: list[tuple[str, str, str | None]] = [
            ("fetchSession", r'"/api/auth/session"', None),
            ("loginWithPassword", r'"/api/auth/login"', "POST"),
            ("logoutSession", r'"/api/auth/logout"', "POST"),
            ("acceptInvite", r'"/api/auth/accept-invite"', "POST"),
            ("googleAuthStartUrl", r'"/api/auth/google/start"', None),
            ("fetchAdminUsage", r'"/api/admin/usage"', None),
            ("fetchAdminUsers", r'"/api/admin/users"', None),
            ("createAdminInvite", r'"/api/admin/invites"', "POST"),
            ("revokeAdminInvite", r"`/api/admin/invites/\$\{encodeURIComponent\(", "DELETE"),
            ("updateAdminUser", r"`/api/admin/users/\$\{encodeURIComponent\(", "PATCH"),
            ("updateAdminSettings", r'"/api/admin/settings"', "POST"),
        ]
        for fn_name, url_pattern, method in expected_api_calls:
            fn_match = re.search(
                rf"export\s+(?:async\s+)?function\s+{fn_name}\b[\s\S]*?(?=\nexport\s+|\Z)",
                api_ts,
            )
            assert fn_match is not None, f"Missing exported function {fn_name} in web/src/api.ts"
            fn_body = fn_match.group(0)
            assert re.search(url_pattern, fn_body), (
                f"{fn_name} missing expected endpoint pattern {url_pattern}"
            )
            if method is not None:
                assert re.search(rf'method:\s*"{method}"', fn_body), (
                    f"{fn_name} missing HTTP method {method}"
                )

        # Turnstile header wiring in submitJob & fetchViewshedPreview
        for fn_name in ("submitJob", "fetchViewshedPreview"):
            fn_match = re.search(
                rf"export\s+(?:async\s+)?function\s+{fn_name}\b[\s\S]*?(?=\nexport\s+|\Z)",
                api_ts,
            )
            assert fn_match is not None and '"X-Turnstile-Token"' in fn_match.group(0), (
                f"{fn_name} missing X-Turnstile-Token header wiring"
            )

        # Verify actual exported TypeScript interfaces & typed property declarations
        interface_fields: dict[str, tuple[str, ...]] = {
            "SessionResponse": ("auth_mode", "authenticated", "user", "role", "capabilities", "quotas"),
            "SessionCapabilities": (
                "max_grid_cells",
                "max_grid_cols",
                "max_grid_rows",
                "strip_atlas_allowed",
                "max_strip_sheets",
                "allowed_outputs",
                "packs_manage_allowed",
                "max_upload_bytes",
                "turnstile_required",
                "turnstile_sitekey",
                "google_oauth_enabled",
            ),
            "SessionQuotas": (
                "daily_renders_limit",
                "daily_renders_used",
                "daily_renders_remaining",
                "hourly_previews_limit",
                "hourly_previews_used",
                "hourly_previews_remaining",
                "public_renders_paused",
                "global_daily_public_limit",
                "global_daily_public_used",
            ),
            "AdminUsageSummary": (
                "renders_today",
                "renders_7d",
                "previews_today",
                "active_queue_depth",
                "global_daily_public_used",
                "global_daily_public_limit",
                "public_renders_paused",
                "avg_elapsed_seconds",
                "max_peak_rss_mb",
            ),
            "AdminSettingsResponse": ("public_renders_paused",),
        }
        for iface_name, fields in interface_fields.items():
            iface_match = re.search(
                rf"export\s+interface\s+{iface_name}\s*\{{([\s\S]*?)\}}",
                types_ts,
            )
            assert iface_match is not None, f"Missing exported interface {iface_name} in web/src/types.ts"
            iface_body = iface_match.group(1)
            for field in fields:
                assert re.search(rf"^\s*{field}\??\s*:", iface_body, re.MULTILINE), (
                    f"Interface {iface_name} missing property declaration '{field}'"
                )
