"""Unit tests for M1 hosted auth, proxy/CF-Connecting-IP, multi-tenancy, quotas, and admin API."""

from __future__ import annotations

import json
import sys
import urllib.parse
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter

from gridforge.web import db
from gridforge.web.app import create_app


def _make_hosted_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    extra_env: dict[str, str] | None = None,
    with_dist: bool = False,
):
    env = {
        "GRIDFORGE_AUTH_MODE": "hosted",
        "GRIDFORGE_SESSION_SECRET": "test-hosted-secret-0123456789abcdef",
        "GRIDFORGE_ADMIN_EMAIL": "admin@example.com",
        "GRIDFORGE_ADMIN_PASSWORD": "AdminPassword123!",
        "GRIDFORGE_ADMIN_NAME": "Trail Admin",
        "GRIDFORGE_ALLOWED_HOSTS": "gridmaps.example,studio.example",
    }
    if extra_env:
        env.update(extra_env)
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    data_dir = tmp_path / "jobs"
    packs_dir = tmp_path / "packs"
    web_dist = tmp_path / "dist"
    data_dir.mkdir(parents=True, exist_ok=True)
    packs_dir.mkdir(parents=True, exist_ok=True)
    if with_dist:
        (web_dist / "assets").mkdir(parents=True, exist_ok=True)
        (web_dist / "index.html").write_text("<html><body>GridForge</body></html>", encoding="utf-8")
        (web_dist / "assets" / "app.js").write_text("console.log('ok');", encoding="utf-8")

    app = create_app(data_dir=data_dir, packs_dir=packs_dir, web_dist_dir=web_dist)
    return app, data_dir, packs_dir, data_dir / "gridforge_jobs.sqlite3"


def _login(client: TestClient, email: str, password: str) -> dict:
    resp = client.post("/api/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestAuthModesAndProxyHardening:
    def test_invalid_auth_mode_raises_value_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GRIDFORGE_AUTH_MODE", "invalid_mode")
        with pytest.raises(ValueError, match="GRIDFORGE_AUTH_MODE"):
            create_app(data_dir=tmp_path / "j", packs_dir=tmp_path / "p")

    def test_none_mode_preserves_zero_login_and_no_qgis_imports(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GRIDFORGE_AUTH_MODE", raising=False)
        app = create_app(data_dir=tmp_path / "j", packs_dir=tmp_path / "p")
        client = TestClient(app, base_url="http://localhost:8080")

        sess = client.get("/api/auth/session")
        assert sess.status_code == 200
        body = sess.json()
        assert body["auth_mode"] == "none"
        assert body["authenticated"] is False
        assert body["role"] == "none"
        assert body["capabilities"]["packs_manage_allowed"] is True
        assert body["capabilities"]["turnstile_required"] is False
        assert "set-cookie" not in sess.headers

        assert client.post("/api/auth/login", json={"email": "a@b.c", "password": "pw"}).status_code == 403
        assert client.post("/api/auth/register", json={"email": "a@b.c", "password": "pw"}).status_code == 403
        assert client.get("/api/admin/usage").status_code == 403
        assert "qgis" not in sys.modules
        assert "PyQt5" not in sys.modules

    def test_proxy_origin_default_port_and_cf_connecting_ip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app, _, _, _ = _make_hosted_app(
            tmp_path,
            monkeypatch,
            extra_env={
                "GRIDFORGE_TRUST_CF_CONNECTING_IP": "true",
                "GRIDFORGE_GUEST_DAILY_RENDERS": "1",
            },
        )
        # Simulate HTTP hop behind Railway/Cloudflare sending X-Forwarded-Proto: https
        with TestClient(app, base_url="http://gridmaps.example") as client:
            # Case-normalized Sec-Fetch-Site and mismatched scheme rejected
            assert (
                client.post(
                    "/api/jobs",
                    json={"center": "-33.732, 150.312"},
                    headers={"Origin": "https://gridmaps.example", "Sec-Fetch-Site": "Cross-Site"},
                ).status_code
                == 403
            )
            assert (
                client.post(
                    "/api/jobs",
                    json={"center": "-33.732, 150.312"},
                    headers={"Origin": "http://gridmaps.example", "X-Forwarded-Proto": "https"},
                ).status_code
                == 403
            )

            # Matching https:// Origin + default :443 port stripping succeeds and sets Secure guest cookie
            ok = client.post(
                "/api/jobs",
                json={"center": "-33.732, 150.312"},
                headers={
                    "Host": "gridmaps.example:443",
                    "Origin": "https://gridmaps.example",
                    "X-Forwarded-Proto": "https",
                    "Sec-Fetch-Site": "Same-Origin",
                    "CF-Connecting-IP": "198.51.100.24",
                },
            )
            assert ok.status_code == 202, ok.text
            assert "Secure" in ok.headers.get("set-cookie", "")

            # Clearing cookies from SAME CF-Connecting-IP hits daily limit (1/day -> 429)
            client.cookies.clear()
            blocked = client.post(
                "/api/jobs",
                json={"center": "-33.732, 150.312"},
                headers={
                    "Origin": "https://gridmaps.example:443",
                    "X-Forwarded-Proto": "https",
                    "CF-Connecting-IP": "198.51.100.24",
                },
            )
            assert blocked.status_code == 429

            # Different valid IPv6 CF-Connecting-IP succeeds
            ok2 = client.post(
                "/api/jobs",
                json={"center": "-33.732, 150.312"},
                headers={
                    "Origin": "https://gridmaps.example",
                    "X-Forwarded-Proto": "https",
                    "CF-Connecting-IP": "2001:db8::42",
                },
            )
            assert ok2.status_code == 202

        # When proxy trust is OFF, spoofed X-Forwarded-Proto must NOT make http:// scope match https:// origin
        monkeypatch.setenv("GRIDFORGE_TRUST_CF_CONNECTING_IP", "false")
        monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
        with TestClient(app, base_url="http://gridmaps.example") as untrusted_client:
            spoofed = untrusted_client.post(
                "/api/jobs",
                json={"center": "-33.732, 150.312"},
                headers={"Origin": "https://gridmaps.example", "X-Forwarded-Proto": "https"},
            )
            assert spoofed.status_code == 403


class TestAuthFlowsAndSamples:
    def test_guest_cookies_and_cacheable_routes_never_set_cookies(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        samples_dir = tmp_path / "samples"
        samples_dir.mkdir()
        (samples_dir / "katoomba.pdf").write_bytes(b"%PDF-1.4\nsample\n%%EOF\n")
        app, _, _, _ = _make_hosted_app(
            tmp_path,
            monkeypatch,
            extra_env={"GRIDFORGE_SAMPLES_DIR": str(samples_dir)},
            with_dist=True,
        )
        client = TestClient(app, base_url="http://localhost:8080")

        # Cacheable static/sample/basemap/health routes MUST NOT set cookies
        for route in ("/assets/app.js", "/samples/katoomba.pdf", "/api/basemap/info", "/api/health"):
            r = client.get(route)
            assert r.status_code == 200
            assert "set-cookie" not in {k.lower() for k in r.headers.keys()}, route

        sample = client.get("/samples/katoomba.pdf")
        assert sample.headers["content-type"].startswith("application/pdf")
        assert "max-age=604800" in sample.headers["cache-control"]
        assert client.get("/samples/missing.pdf").status_code == 404
        assert client.get("/samples/..%2Fsecret.pdf").status_code in (400, 404)

        # Dynamic session route issues HttpOnly SameSite=Lax gf_guest cookie
        sess = client.get("/api/auth/session")
        assert sess.status_code == 200
        assert sess.json()["role"] == "guest"
        set_cookie = sess.headers.get("set-cookie", "")
        assert "gf_guest=" in set_cookie
        assert "HttpOnly" in set_cookie
        assert "SameSite=lax" in set_cookie

    def test_invite_only_friend_onboarding_login_logout_and_disable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app, _, _, _ = _make_hosted_app(tmp_path, monkeypatch)
        admin_client = TestClient(app, base_url="http://localhost:8080")
        guest_client = TestClient(app, base_url="http://localhost:8080")

        # Public signup disabled
        assert guest_client.post("/api/auth/register", json={"email": "x@y.z"}).status_code == 403

        # Admin login (invalid password -> 401; valid -> 200)
        assert (
            admin_client.post(
                "/api/auth/login",
                json={"email": "admin@example.com", "password": "wrong-password"},
            ).status_code
            == 401
        )
        admin_sess = _login(admin_client, "admin@example.com", "AdminPassword123!")
        assert admin_sess["authenticated"] is True
        assert admin_sess["role"] == "admin"
        assert admin_sess["capabilities"]["packs_manage_allowed"] is True

        # Admin creates two invites; revokes one
        inv_rev = admin_client.post("/api/admin/invites", json={"role": "friend"})
        assert inv_rev.status_code == 201
        inv_rev_data = inv_rev.json()
        assert (
            admin_client.delete(f"/api/admin/invites/{inv_rev_data['invite_id']}").status_code
            == 200
        )
        assert (
            guest_client.post(
                "/api/auth/accept-invite",
                json={
                    "token": inv_rev_data["token"],
                    "email": "revoked@example.com",
                    "password": "Password123!",
                },
            ).status_code
            == 403
        )

        # Admin creates email-pinned invite for Friend
        inv_ok = admin_client.post(
            "/api/admin/invites",
            json={"email": "friend@example.com", "role": "friend", "expires_hours": 24},
        ).json()

        # Email mismatch rejected
        assert (
            guest_client.post(
                "/api/auth/accept-invite",
                json={
                    "token": inv_ok["token"],
                    "email": "wrong@example.com",
                    "password": "FriendPassword123!",
                },
            ).status_code
            == 403
        )

        # Valid acceptance creates Friend user and sets gf_session
        accepted = guest_client.post(
            "/api/auth/accept-invite",
            json={
                "token": inv_ok["token"],
                "email": "friend@example.com",
                "password": "FriendPassword123!",
                "display_name": "Scout One",
            },
        )
        assert accepted.status_code == 200, accepted.text
        f_body = accepted.json()
        assert f_body["authenticated"] is True
        assert f_body["role"] == "friend"
        assert f_body["user"]["display_name"] == "Scout One"
        friend_user_id = f_body["user"]["user_id"]

        # Logout clears gf_session and reverts to guest
        logged_out = guest_client.post("/api/auth/logout")
        assert logged_out.status_code == 200
        assert logged_out.json()["authenticated"] is False
        assert logged_out.json()["role"] == "guest"

        # Friend logs back in
        assert _login(guest_client, "friend@example.com", "FriendPassword123!")["role"] == "friend"

        # Admin disables Friend -> active session invalidated & login rejected (403)
        patched = admin_client.patch(
            f"/api/admin/users/{friend_user_id}",
            json={"disabled": True},
        )
        assert patched.status_code == 200
        assert patched.json()["user"]["disabled"] is True
        assert guest_client.get("/api/auth/session").json()["role"] == "guest"
        assert (
            guest_client.post(
                "/api/auth/login",
                json={"email": "friend@example.com", "password": "FriendPassword123!"},
            ).status_code
            == 403
        )

    def test_google_oauth_flow_mocked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app, _, _, _ = _make_hosted_app(
            tmp_path,
            monkeypatch,
            extra_env={
                "GRIDFORGE_GOOGLE_CLIENT_ID": "test-client-id",
                "GRIDFORGE_GOOGLE_CLIENT_SECRET": "test-client-secret",
            },
        )
        admin_client = TestClient(app, base_url="http://localhost:8080")
        _login(admin_client, "admin@example.com", "AdminPassword123!")
        invite = admin_client.post(
            "/api/admin/invites",
            json={"email": "oauthfriend@example.com", "role": "friend"},
        ).json()

        mock_email = {"value": "stranger@example.com"}

        class _MockResp:
            def __init__(self, payload: dict, status_code: int = 200) -> None:
                self._payload = payload
                self.status_code = status_code

            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return self._payload

        monkeypatch.setattr(
            "gridforge.web.auth.httpx.post",
            lambda url, **kw: _MockResp({"access_token": "tok-abc"}),
        )
        monkeypatch.setattr(
            "gridforge.web.auth.httpx.get",
            lambda url, **kw: _MockResp(
                {
                    "email": mock_email["value"],
                    "email_verified": True,
                    "sub": "google-sub-123",
                    "name": "OAuth Scout",
                }
            ),
        )

        # 1. Uninvited user fails with 403
        c1 = TestClient(app, base_url="http://localhost:8080")
        start1 = c1.get("/api/auth/google/start", follow_redirects=False)
        assert start1.status_code == 302
        state1 = urllib.parse.parse_qs(
            urllib.parse.urlsplit(start1.headers["location"]).query
        )["state"][0]
        assert c1.get(f"/api/auth/google/callback?code=c1&state={state1}").status_code == 403

        # 2. Bad state rejected
        assert c1.get("/api/auth/google/callback?code=c1&state=wrong").status_code == 403

        # 3. Invited user succeeds and gets gf_session
        mock_email["value"] = "oauthfriend@example.com"
        c2 = TestClient(app, base_url="http://localhost:8080")
        start2 = c2.get(
            f"/api/auth/google/start?invite_token={invite['token']}",
            follow_redirects=False,
        )
        assert start2.status_code == 302
        state2 = urllib.parse.parse_qs(
            urllib.parse.urlsplit(start2.headers["location"]).query
        )["state"][0]
        cb2 = c2.get(
            f"/api/auth/google/callback?code=c2&state={state2}",
            follow_redirects=False,
        )
        assert cb2.status_code == 302
        assert cb2.headers["location"] == "/"
        assert c2.get("/api/auth/session").json()["role"] == "friend"


class TestMultiTenancyTurnstileQuotasAndAdmin:
    def test_turnstile_verification_for_guest_and_bypass_for_friend(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app, _, _, _ = _make_hosted_app(
            tmp_path,
            monkeypatch,
            extra_env={
                "GRIDFORGE_TURNSTILE_SITE_KEY": "site-key-123",
                "GRIDFORGE_TURNSTILE_SECRET_KEY": "secret-key-123",
            },
        )
        seen_tokens: list[str] = []

        class _TurnstileResp:
            status_code = 200

            def __init__(self, ok: bool) -> None:
                self._ok = ok

            def json(self) -> dict:
                return {"success": self._ok}

        def _mock_siteverify(url, data=None, **kw):
            tok = (data or {}).get("response", "")
            seen_tokens.append(tok)
            return _TurnstileResp(tok == "valid-turnstile-tok")

        monkeypatch.setattr("gridforge.web.auth.httpx.post", _mock_siteverify)

        guest = TestClient(app, base_url="http://localhost:8080")
        assert guest.get("/api/auth/session").json()["capabilities"]["turnstile_required"] is True

        # Missing token -> 403
        assert guest.post("/api/jobs", json={"center": "-33.732, 150.312"}).status_code == 403
        assert (
            guest.post(
                "/api/viewshed-preview",
                json={"observer": {"lat": -33.732, "lon": 150.312}},
            ).status_code
            == 403
        )

        # Invalid token -> 403
        assert (
            guest.post(
                "/api/jobs",
                json={"center": "-33.732, 150.312", "turnstile_token": "bad-tok"},
            ).status_code
            == 403
        )

        # Valid JSON token -> 202, and token is NOT persisted in request_json
        ok = guest.post(
            "/api/jobs",
            json={"center": "-33.732, 150.312", "turnstile_token": "valid-turnstile-tok"},
        )
        assert ok.status_code == 202
        assert "valid-turnstile-tok" in seen_tokens
        conn = db._connect(tmp_path / "jobs" / "gridforge_jobs.sqlite3")
        try:
            row = conn.execute(
                "SELECT request_json FROM jobs WHERE job_id = ?", (ok.json()["job_id"],)
            ).fetchone()
            assert "turnstile_token" not in str(row["request_json"])
        finally:
            conn.close()

        # Admin / Friend bypass Turnstile without token
        admin = TestClient(app, base_url="http://localhost:8080")
        _login(admin, "admin@example.com", "AdminPassword123!")
        assert admin.post("/api/jobs", json={"center": "-33.732, 150.312"}).status_code == 202

    def test_multi_tenant_isolation_and_pack_lockdown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app, data_dir, _, db_path = _make_hosted_app(tmp_path, monkeypatch)
        admin = TestClient(app, base_url="http://localhost:8080")
        _login(admin, "admin@example.com", "AdminPassword123!")
        inv = admin.post("/api/admin/invites", json={"role": "friend"}).json()

        friend = TestClient(app, base_url="http://localhost:8080")
        friend.post(
            "/api/auth/accept-invite",
            json={"token": inv["token"], "email": "f@example.com", "password": "Password123!"},
        )
        guest_a = TestClient(app, base_url="http://localhost:8080")
        guest_b = TestClient(app, base_url="http://localhost:8080")

        # Pack build/tasks/delete locked to admin only
        for non_admin in (guest_a, friend):
            assert (
                non_admin.post("/api/packs/build", json={"region_or_bbox": "act"}).status_code
                == 403
            )
            assert non_admin.get("/api/packs/tasks").status_code == 403
            assert non_admin.delete("/api/packs/act").status_code == 403
        assert admin.get("/api/packs/tasks").status_code == 200

        # Guest A creates a job + artifact + PDF thumbnail + project
        job_a = guest_a.post("/api/jobs", json={"center": "-33.732, 150.312"}).json()
        jid = job_a["job_id"]
        art_dir = data_dir / jid / "artifacts"
        art_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = art_dir / "map.pdf"
        w = PdfWriter()
        w.add_blank_page(width=595.28, height=841.89)
        with pdf_path.open("wb") as fh:
            w.write(fh)
        w.close()

        proj_a = guest_a.post(
            "/api/projects",
            json={"name": "Guest A Trip", "state": {"scale": 25000}, "last_job_id": jid},
        ).json()
        pid = proj_a["project_id"]

        # Guest B and Friend cannot see, download, cancel, patch, or delete Guest A's resources
        assert guest_b.get("/api/jobs").json()["jobs"] == []
        assert guest_b.get("/api/projects").json()["projects"] == []
        for other in (guest_b, friend):
            assert other.get(f"/api/jobs/{jid}").status_code == 403
            assert other.post(f"/api/jobs/{jid}/cancel").status_code == 403
            assert other.delete(f"/api/jobs/{jid}").status_code == 403
            assert other.get(f"/api/jobs/{jid}/artifacts/map.pdf").status_code == 403
            assert other.get(f"/api/jobs/{jid}/thumbnail.png").status_code == 403
            assert other.get(f"/api/projects/{pid}").status_code == 403
            assert other.patch(f"/api/projects/{pid}", json={"name": "Hijack"}).status_code == 403
            assert other.delete(f"/api/projects/{pid}").status_code == 403
            assert (
                other.post(
                    "/api/projects",
                    json={"name": "Steal Thumb", "last_job_id": jid},
                ).status_code
                == 403
            )

        # Nonexistent job/project returns 404
        assert guest_b.get("/api/jobs/missingjob12").status_code == 404
        assert guest_b.get("/api/jobs/missingjob12/artifacts/map.pdf").status_code == 404
        assert guest_b.get("/api/projects/missingproj1").status_code == 404

        # Owner downloads with private cache headers
        art_resp = guest_a.get(f"/api/jobs/{jid}/artifacts/map.pdf")
        assert art_resp.status_code == 200
        assert "private" in art_resp.headers.get("cache-control", "")
        thumb_resp = guest_a.get(f"/api/jobs/{jid}/thumbnail.png")
        assert thumb_resp.status_code == 200
        assert "private" in thumb_resp.headers.get("cache-control", "")

    def test_role_caps_upload_413_queue_503_budget_503_kill_switch_and_rate_limits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app, _, _, db_path = _make_hosted_app(
            tmp_path,
            monkeypatch,
            extra_env={
                "GRIDFORGE_TRUST_CF_CONNECTING_IP": "1",
                "GRIDFORGE_MAX_UPLOAD_BYTES_GUEST": "1024",
                "GRIDFORGE_GUEST_DAILY_RENDERS": "2",
                "GRIDFORGE_GLOBAL_DAILY_PUBLIC_RENDERS": "3",
                "GRIDFORGE_MAX_QUEUE_DEPTH": "4",
                "GRIDFORGE_GUEST_HOURLY_PREVIEWS": "2",
                "GRIDFORGE_RF_LINK_HOURLY_LIMIT": "2",
                "GRIDFORGE_GEOCODE_HOURLY_LIMIT": "2",
            },
        )
        admin = TestClient(app, base_url="http://localhost:8080")
        _login(admin, "admin@example.com", "AdminPassword123!")
        inv = admin.post("/api/admin/invites", json={"role": "friend"}).json()
        friend = TestClient(app, base_url="http://localhost:8080")
        f_sess = friend.post(
            "/api/auth/accept-invite",
            json={"token": inv["token"], "email": "scout@example.com", "password": "Password123!"},
        ).json()
        friend_id = f_sess["user"]["user_id"]

        guest = TestClient(app, base_url="http://localhost:8080")

        # 1. Guest capability restrictions (403)
        assert guest.post("/api/jobs", json={"center": "-33.732, 150.312", "grid_cols": 2}).status_code == 403
        assert (
            guest.post(
                "/api/jobs",
                json={"center": "-33.732, 150.312", "outputs": {"pdf": True, "kmz": True}},
            ).status_code
            == 403
        )
        assert (
            guest.post(
                "/api/jobs",
                json={"center": "-33.732, 150.312", "outputs": {"pdf": True, "geopdf": True}},
            ).status_code
            == 403
        )
        assert (
            guest.post(
                "/api/jobs",
                json={
                    "center": "-33.732, 150.312",
                    "strip_sheets": [{"sheet_id": "S1", "center_lat": -33.732, "center_lon": 150.312}],
                },
            ).status_code
            == 403
        )
        assert guest.post("/api/strip-plan", json={"route_geojson": {}}).status_code == 403

        # 2. Guest upload cap 413
        big_upload = guest.post(
            "/api/jobs",
            data={"payload": json.dumps({"center": "-33.732, 150.312"})},
            files={"overlay_file": ("big.geojson", b"x" * 2048, "application/geo+json")},
        )
        assert big_upload.status_code == 413

        # 3. Guest daily render quota (2/day -> 429) & global daily public budget (3/day -> 503)
        assert (
            guest.post(
                "/api/jobs",
                json={"center": "-33.732, 150.312"},
                headers={"CF-Connecting-IP": "203.0.113.10"},
            ).status_code
            == 202
        )
        assert (
            guest.post(
                "/api/jobs",
                json={"center": "-33.732, 150.312"},
                headers={"CF-Connecting-IP": "203.0.113.10"},
            ).status_code
            == 202
        )
        assert (
            guest.post(
                "/api/jobs",
                json={"center": "-33.732, 150.312"},
                headers={"CF-Connecting-IP": "203.0.113.10"},
            ).status_code
            == 429
        )

        # Second IP uses 3rd global public slot; 3rd IP hits global budget 503
        guest2 = TestClient(app, base_url="http://localhost:8080")
        assert (
            guest2.post(
                "/api/jobs",
                json={"center": "-33.732, 150.312"},
                headers={"CF-Connecting-IP": "203.0.113.11"},
            ).status_code
            == 202
        )
        guest3 = TestClient(app, base_url="http://localhost:8080")
        assert (
            guest3.post(
                "/api/jobs",
                json={"center": "-33.732, 150.312"},
                headers={"CF-Connecting-IP": "203.0.113.12"},
            ).status_code
            == 503
        )

        # 4. Friend is unaffected by global public budget and can submit 2x2 + KMZ, hitting queue depth 503 at 5th job!
        f_job = friend.post(
            "/api/jobs",
            json={
                "center": "-33.732, 150.312",
                "grid_cols": 2,
                "grid_rows": 2,
                "outputs": {"pdf": True, "kmz": True},
            },
        )
        assert f_job.status_code == 202
        # Queue depth is now 4 (max_queue_depth=4) -> next job returns 503
        assert friend.post("/api/jobs", json={"center": "-33.732, 150.312"}).status_code == 503

        # 5. Kill switch pauses guest renders (503) even with fresh IP and cleared queue
        for queued_job in db.list_jobs(db_path):
            db.cancel_job(db_path, queued_job.job_id)
        assert (
            admin.post("/api/admin/settings", json={"public_renders_paused": True}).status_code
            == 200
        )
        guest4 = TestClient(app, base_url="http://localhost:8080")
        assert (
            guest4.post(
                "/api/jobs",
                json={"center": "-33.732, 150.312"},
                headers={"CF-Connecting-IP": "203.0.113.99"},
            ).status_code
            == 503
        )
        # Friend can still render with kill switch on (and respects per-user daily_render_limit override)
        admin.patch(f"/api/admin/users/{friend_id}", json={"daily_render_limit": 2})
        assert friend.post("/api/jobs", json={"center": "-33.732, 150.312"}).status_code == 202
        assert friend.post("/api/jobs", json={"center": "-33.732, 150.312"}).status_code == 429

        # 6. Geocode rate limit applies ONLY to cache misses (hits do not consume quota)
        from gridforge.web import app as app_mod

        app_mod._geocode_cache.clear()
        nominatim_calls = {"n": 0}

        class _GeoResp:
            def raise_for_status(self) -> None:
                return None

            def json(self):
                return [{"name": "Test", "display_name": "Test Peak", "lat": "-33.7", "lon": "150.3"}]

        def _fake_geo_get(url, **kw):
            nominatim_calls["n"] += 1
            return _GeoResp()

        monkeypatch.setattr("gridforge.web.app.httpx.get", _fake_geo_get)
        assert guest.get("/api/geocode?q=Katoomba", headers={"CF-Connecting-IP": "198.51.100.5"}).status_code == 200
        assert guest.get("/api/geocode?q=Katoomba", headers={"CF-Connecting-IP": "198.51.100.5"}).status_code == 200
        assert guest.get("/api/geocode?q=Leura", headers={"CF-Connecting-IP": "198.51.100.5"}).status_code == 200
        assert nominatim_calls["n"] == 2
        # 3rd uncached query returns 429 without hitting Nominatim
        assert guest.get("/api/geocode?q=Blackheath", headers={"CF-Connecting-IP": "198.51.100.5"}).status_code == 429
        assert nominatim_calls["n"] == 2

        # 7. Viewshed preview & RF link profile hourly rate limits (429)
        monkeypatch.setattr(
            "gridforge.web.app.compute_viewshed_preview",
            lambda req, **kw: {
                "observer_mgrs_10m": "56H KH 5095 6431",
                "observer_elev_m": 1000.0,
                "utm_epsg": 32756,
                "max_distance_m": 3000.0,
                "visible_area_km2": 2.5,
                "viewshed_geojson": {"type": "FeatureCollection", "features": []},
                "range_ring_geojson": {"type": "FeatureCollection", "features": []},
            },
        )
        vs_payload = {"observer": {"lat": -33.732, "lon": 150.312}}
        assert guest.post("/api/viewshed-preview", json=vs_payload, headers={"CF-Connecting-IP": "198.51.100.7"}).status_code == 200
        assert guest.post("/api/viewshed-preview", json=vs_payload, headers={"CF-Connecting-IP": "198.51.100.7"}).status_code == 200
        assert guest.post("/api/viewshed-preview", json=vs_payload, headers={"CF-Connecting-IP": "198.51.100.7"}).status_code == 429

        monkeypatch.setattr(
            "gridforge.web.app.compute_rf_link_preview",
            lambda req, **kw: {
                "status": "CLEAR",
                "distance_km": 2.0,
                "freq_mhz": 915.0,
                "fspl_db": 97.7,
                "min_clearance_m": 25.0,
                "min_fresnel_pct": 120.0,
                "los_clear": True,
                "fresnel_60pct_clear": True,
                "bearing_true_deg": 45.0,
                "bearing_grid_deg": 45.0,
                "bearing_mag_deg": 33.0,
                "bearing_mils": 800.0,
                "from_elev_m": 950.0,
                "to_elev_m": 980.0,
                "from_mgrs": "56H KH 500 640",
                "to_mgrs": "56H KH 510 650",
                "samples": [],
            },
        )
        rf_payload = {"from_lat": -33.732, "from_lon": 150.312, "to_lat": -33.712, "to_lon": 150.322}
        assert guest.post("/api/rf-link-profile", json=rf_payload, headers={"CF-Connecting-IP": "198.51.100.7"}).status_code == 200
        assert guest.post("/api/rf-link-profile", json=rf_payload, headers={"CF-Connecting-IP": "198.51.100.7"}).status_code == 200
        assert guest.post("/api/rf-link-profile", json=rf_payload, headers={"CF-Connecting-IP": "198.51.100.7"}).status_code == 429

        # 8. Admin usage report & users list
        assert guest.get("/api/admin/usage").status_code == 403
        assert friend.get("/api/admin/users").status_code == 403

        usage = admin.get("/api/admin/usage")
        assert usage.status_code == 200
        u_body = usage.json()
        assert u_body["summary"]["renders_today"] >= 5
        assert u_body["summary"]["public_renders_paused"] is True
        assert u_body["summary"]["global_daily_public_used"] == 3
        assert "guest" in u_body["by_role"]
        assert "friend" in u_body["by_role"]
        assert len(u_body["recent_events"]) > 0

        users_resp = admin.get("/api/admin/users")
        assert users_resp.status_code == 200
        assert any(u["email"] == "admin@example.com" for u in users_resp.json()["users"])
        assert any(u["email"] == "scout@example.com" for u in users_resp.json()["users"])

    def test_validation_error_returns_clean_422_and_admin_cannot_self_lockout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app, _, _, _ = _make_hosted_app(tmp_path, monkeypatch)
        admin = TestClient(app, base_url="http://localhost:8080")
        admin_sess = _login(admin, "admin@example.com", "AdminPassword123!")
        admin_id = admin_sess["user"]["user_id"]

        # Invalid scale raises ValueError inside Pydantic validator -> must return clean JSON 422 (never 500)
        bad_json = admin.post("/api/jobs", json={"center": "-33.732, 150.312", "scale": 12345})
        assert bad_json.status_code == 422
        assert isinstance(bad_json.json()["detail"], list)

        bad_form = admin.post(
            "/api/jobs",
            data={"payload": json.dumps({"center": "-33.732, 150.312", "outputs": {"pdf": False}})},
        )
        assert bad_form.status_code == 422
        assert isinstance(bad_form.json()["detail"], list)

        # Admin cannot disable or demote their own account (400)
        self_disable = admin.patch(f"/api/admin/users/{admin_id}", json={"disabled": True})
        assert self_disable.status_code == 400
        assert "own account" in self_disable.json()["detail"]

        self_demote = admin.patch(f"/api/admin/users/{admin_id}", json={"role": "friend"})
        assert self_demote.status_code == 400
        assert "own account" in self_demote.json()["detail"]

        # Admin session remains valid
        assert admin.get("/api/auth/session").json()["role"] == "admin"


class TestTier5SecurityHardening:
    def test_t5_google_oauth_rejects_string_false_email_verified(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app, _, _, _ = _make_hosted_app(
            tmp_path,
            monkeypatch,
            extra_env={
                "GRIDFORGE_GOOGLE_CLIENT_ID": "test-client-id",
                "GRIDFORGE_GOOGLE_CLIENT_SECRET": "test-client-secret",
            },
        )

        class _MockResp:
            def __init__(self, payload: dict) -> None:
                self._payload = payload
                self.status_code = 200

            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return self._payload

        monkeypatch.setattr(
            "gridforge.web.auth.httpx.post",
            lambda url, **kw: _MockResp({"access_token": "tok-abc"}),
        )

        for raw_ev in ("false", "False", False, None, 0, ""):
            monkeypatch.setattr(
                "gridforge.web.auth.httpx.get",
                lambda url, _ev=raw_ev, **kw: _MockResp(
                    {
                        "email": "admin@example.com",
                        "email_verified": _ev,
                        "sub": "google-sub-unverified",
                        "name": "Unverified",
                    }
                ),
            )
            client = TestClient(app, base_url="http://localhost:8080")
            start = client.get("/api/auth/google/start", follow_redirects=False)
            state = urllib.parse.parse_qs(
                urllib.parse.urlsplit(start.headers["location"]).query
            )["state"][0]
            cb = client.get(
                f"/api/auth/google/callback?code=c1&state={state}",
                follow_redirects=False,
            )
            assert cb.status_code == 403
            assert "gf_session=" not in cb.headers.get("set-cookie", "")

    def test_t5_whitespace_turnstile_header_falls_back_to_body_token(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app, _, _, _ = _make_hosted_app(
            tmp_path,
            monkeypatch,
            extra_env={
                "GRIDFORGE_TURNSTILE_SITE_KEY": "site-key-123",
                "GRIDFORGE_TURNSTILE_SECRET_KEY": "secret-key-123",
            },
        )
        seen_tokens: list[str] = []

        class _TurnstileResp:
            status_code = 200

            def json(self) -> dict:
                return {"success": True}

        def _mock_siteverify(url, data=None, **kw):
            seen_tokens.append((data or {}).get("response", ""))
            return _TurnstileResp()

        monkeypatch.setattr("gridforge.web.auth.httpx.post", _mock_siteverify)

        guest = TestClient(app, base_url="http://localhost:8080")
        ok = guest.post(
            "/api/jobs",
            json={"center": "-33.732, 150.312", "turnstile_token": "valid-body-token"},
            headers={"X-Turnstile-Token": "   "},
        )
        assert ok.status_code == 202
        assert seen_tokens == ["valid-body-token"]

    def test_t5_login_constant_time_unknown_user_and_auth_rate_limit_429(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from gridforge.web import auth as auth_mod

        app, _, _, _ = _make_hosted_app(
            tmp_path,
            monkeypatch,
            extra_env={"GRIDFORGE_AUTH_RATE_LIMIT_PER_MINUTE": "4"},
        )

        verify_calls: list[tuple[str, str | None]] = []
        orig_verify = auth_mod.verify_password

        def _spy_verify(password: str, stored_hash: str | None) -> bool:
            verify_calls.append((password, stored_hash))
            return orig_verify(password, stored_hash)

        monkeypatch.setattr(auth_mod, "verify_password", _spy_verify)

        client = TestClient(app, base_url="http://localhost:8080")
        # Unknown user still runs full PBKDF2 dummy verification (eliminating timing oracle)
        r1 = client.post(
            "/api/auth/login",
            json={"email": "nonexistent@example.com", "password": "SomePassword123!"},
        )
        assert r1.status_code == 401
        assert len(verify_calls) == 1
        assert verify_calls[0][1] is not None and verify_calls[0][1].startswith("pbkdf2_sha256$310000$")

        # Remaining 3 attempts within rate limit return 401; 5th attempt hits 429
        for _ in range(3):
            assert (
                client.post(
                    "/api/auth/login",
                    json={"email": "admin@example.com", "password": "wrong"},
                ).status_code
                == 401
            )
        r5 = client.post(
            "/api/auth/login",
            json={"email": "admin@example.com", "password": "AdminPassword123!"},
        )
        assert r5.status_code == 429

        # accept-invite also enforces per-IP auth rate limit
        for _ in range(4):
            assert (
                client.post(
                    "/api/auth/accept-invite",
                    json={"token": "bad-invite-token", "email": "x@example.com", "password": "Password123!"},
                ).status_code
                == 403
            )
        assert (
            client.post(
                "/api/auth/accept-invite",
                json={"token": "bad-invite-token", "email": "x@example.com", "password": "Password123!"},
            ).status_code
            == 429
        )

    def test_t5_forwarded_allow_ips_cidr_trusts_matching_peer_and_rejects_untrusted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import anyio
        import httpx

        app, _, _, _ = _make_hosted_app(
            tmp_path,
            monkeypatch,
            extra_env={
                "GRIDFORGE_ALLOWED_HOSTS": "example.com",
                "GRIDFORGE_TRUST_CF_CONNECTING_IP": "false",
                "FORWARDED_ALLOW_IPS": "10.0.0.0/8, 192.0.2.50",
            },
        )

        async def _probe() -> tuple[int, str, int]:
            trusted_transport = httpx.ASGITransport(app=app, client=("10.0.0.5", 44321))
            async with httpx.AsyncClient(
                transport=trusted_transport, base_url="http://example.com"
            ) as trusted:
                ok = await trusted.post(
                    "/api/auth/login",
                    json={"email": "admin@example.com", "password": "AdminPassword123!"},
                    headers={"Origin": "https://example.com", "X-Forwarded-Proto": "https"},
                )

            untrusted_transport = httpx.ASGITransport(app=app, client=("198.51.100.9", 44321))
            async with httpx.AsyncClient(
                transport=untrusted_transport, base_url="http://example.com"
            ) as untrusted:
                bad = await untrusted.post(
                    "/api/auth/login",
                    json={"email": "admin@example.com", "password": "AdminPassword123!"},
                    headers={"Origin": "https://example.com", "X-Forwarded-Proto": "https"},
                )
            return ok.status_code, ok.headers.get("set-cookie", ""), bad.status_code

        ok_status, set_cookie, bad_status = anyio.run(_probe)
        assert ok_status == 200
        assert "Secure" in set_cookie
        assert bad_status == 403


class TestLiveBrandingConfig:
    def test_custom_brand_renders_ssr_html_and_session_safely(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dist_dir = tmp_path / "web_dist"
        dist_dir.mkdir(parents=True)
        (dist_dir / "index.html").write_text(
            '<!doctype html><html><head>'
            '<title>GridForge — Tactical Topographic Print Studio</title>'
            '<meta name="description" content="GridForge — True-scale UTM print studio." />'
            '<meta property="og:title" content="GridForge — Tactical Topographic Print Studio" />'
            '</head><body><div id="app"></div></body></html>',
            encoding="utf-8",
        )
        monkeypatch.setenv("GRIDFORGE_AUTH_MODE", "hosted")
        monkeypatch.setenv("GRIDFORGE_SESSION_SECRET", "brand-test-secret-0123456789abcdef")
        monkeypatch.setenv("GRIDFORGE_BRAND_NAME", "GridMaps")
        monkeypatch.setenv("GRIDFORGE_BRAND_SUBTITLE", "Studio")
        monkeypatch.setenv("GRIDFORGE_BRAND_TAGLINE", "Tactical Topographic Print Studio")

        app = create_app(
            data_dir=tmp_path / "jobs",
            packs_dir=tmp_path / "packs",
            web_dist_dir=dist_dir,
        )
        with TestClient(app, base_url="http://localhost") as client:
            root_resp = client.get("/")
            assert root_resp.status_code == 200
            assert "<title>GridMaps — Tactical Topographic Print Studio</title>" in root_resp.text
            assert 'property="og:title" content="GridMaps — Tactical Topographic Print Studio"' in root_resp.text
            assert 'name="description" content="GridMaps — True-scale UTM print studio."' in root_resp.text
            assert '<script id="gf-brand" type="application/json">{"name":"GridMaps","subtitle":"Studio","tagline":"Tactical Topographic Print Studio"}</script>' in root_resp.text

            sess = client.get("/api/auth/session")
            assert sess.status_code == 200
            body = sess.json()
            assert body["brand_name"] == "GridMaps"
            assert body["brand_subtitle"] == "Studio"
            assert body["brand_tagline"] == "Tactical Topographic Print Studio"

    def test_custom_brand_escapes_html_and_script_injection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dist_dir = tmp_path / "web_dist"
        dist_dir.mkdir(parents=True)
        (dist_dir / "index.html").write_text(
            '<!doctype html><html><head>'
            '<title>GridForge — Studio</title>'
            '</head><body></body></html>',
            encoding="utf-8",
        )
        monkeypatch.setenv("GRIDFORGE_AUTH_MODE", "none")
        monkeypatch.setenv("GRIDFORGE_BRAND_NAME", '</script><script>alert(1)</script>')

        app = create_app(
            data_dir=tmp_path / "jobs",
            packs_dir=tmp_path / "packs",
            web_dist_dir=dist_dir,
        )
        with TestClient(app, base_url="http://localhost") as client:
            resp = client.get("/")
            assert resp.status_code == 200
            assert "<script>alert(1)</script>" not in resp.text
            assert "\\u003c/script\\u003e" in resp.text


class TestResolutionAndDetailCapabilities:
    def test_hosted_dpi_caps_and_fine_scales(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app, _, _, _ = _make_hosted_app(tmp_path, monkeypatch)
        guest = TestClient(app, base_url="http://localhost:8080")
        admin = TestClient(app, base_url="http://localhost:8080")
        _login(admin, "admin@example.com", "AdminPassword123!")

        guest_sess = guest.get("/api/auth/session").json()
        assert guest_sess["capabilities"]["max_dpi"] == 300
        assert guest_sess["capabilities"]["max_export_dpi"] == 300

        admin_sess = admin.get("/api/auth/session").json()
        assert admin_sess["capabilities"]["max_dpi"] == 600
        assert admin_sess["capabilities"]["max_export_dpi"] == 600

        # Guest cannot exceed 300 DPI print or export resolution
        too_high_print = guest.post(
            "/api/jobs",
            json={"center": "-33.732, 150.312", "scale": 10000, "dpi": 600},
        )
        assert too_high_print.status_code == 403
        assert "300 DPI" in too_high_print.json()["detail"]

        too_high_export = guest.post(
            "/api/jobs",
            json={
                "center": "-33.732, 150.312",
                "scale": 12500,
                "dpi": 300,
                "outputs": {"pdf": True, "export_dpi": 450},
            },
        )
        assert too_high_export.status_code == 403
        assert "300 DPI" in too_high_export.json()["detail"]

        # Guest CAN request 1:10,000 at 300 DPI
        ok_guest = guest.post(
            "/api/jobs",
            json={"center": "-33.732, 150.312", "scale": 10000, "dpi": 300},
        )
        assert ok_guest.status_code == 202

        # Admin / Friend CAN request 1:12,500 at 600 DPI print + 450 DPI digital export
        ok_admin = admin.post(
            "/api/jobs",
            json={
                "center": "-33.732, 150.312",
                "scale": 12500,
                "dpi": 600,
                "outputs": {"pdf": True, "mbtiles": True, "export_dpi": 450},
            },
        )
        assert ok_admin.status_code == 202
        assert ok_admin.json()["scale"] == 12500


def test_hosted_session_emits_distinct_opaque_workspace_scope_per_guest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _, _, _ = _make_hosted_app(tmp_path, monkeypatch)
    g1 = TestClient(app, base_url="http://localhost:8080")
    g2 = TestClient(app, base_url="http://localhost:8080")

    s1 = g1.get("/api/auth/session")
    s2 = g2.get("/api/auth/session")
    assert s1.status_code == 200 and s2.status_code == 200
    scope1 = s1.json()["workspace_scope"]
    scope2 = s2.json()["workspace_scope"]
    assert isinstance(scope1, str) and len(scope1) == 24
    assert isinstance(scope2, str) and len(scope2) == 24
    assert scope1 != scope2
    # Must remain stable for the same guest cookie and never expose raw cookie value
    assert g1.get("/api/auth/session").json()["workspace_scope"] == scope1
    raw_cookie = g1.cookies.get("gf_guest", "")
    assert raw_cookie and raw_cookie not in scope1
    # Signing in changes workspace_scope to the authenticated account's scope
    signed_in = _login(g1, "admin@example.com", "AdminPassword123!")
    assert signed_in["workspace_scope"] != scope1
    assert signed_in["user"]["user_id"] not in signed_in["workspace_scope"]


def test_limits_default_geocode_budgets_and_resolve_auth_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gridforge.web.auth import resolve_auth_mode
    from gridforge.web.limits import LimitsConfig

    monkeypatch.delenv("GRIDFORGE_GEOCODE_HOURLY_LIMIT", raising=False)
    monkeypatch.delenv("GRIDFORGE_GEOCODE_GLOBAL_HOURLY_LIMIT", raising=False)
    monkeypatch.delenv("GRIDFORGE_AUTH_MODE", raising=False)

    cfg = LimitsConfig.from_env(auth_mode="hosted")
    assert cfg.geocode_hourly_limit == 30
    assert cfg.geocode_global_hourly_limit == 300
    assert resolve_auth_mode() == "none"
    assert resolve_auth_mode("  ") == "none"
    assert resolve_auth_mode("HOSTED") == "hosted"
    with pytest.raises(ValueError, match="Invalid GRIDFORGE_AUTH_MODE"):
        resolve_auth_mode("bogus")
