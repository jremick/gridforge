"""Unit tests for AI-ready Web API layer: PAT API keys, signed URLs, page previews, sync wait, capabilities, /llms.txt, and OpenAPI."""

from __future__ import annotations

import sys
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter

from gridforge import __version__
from gridforge.config import (
    VALID_SCALES,
    AUOverlayKind,
    BBox,
    CAOverlayKind,
    ColorMode,
    DemSourceKind,
    NZOverlayKind,
    Orientation,
    PackManifest,
    PaperSize,
    USOverlayKind,
)
from gridforge.web import db
from gridforge.web.app import create_app
from gridforge.web.auth import sha256_hex, sign_artifact_token, verify_artifact_token
from gridforge.web.schemas import JobArtifact


def _make_hosted_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    extra_env: dict[str, str] | None = None,
    with_dist: bool = False,
):
    env = {
        "GRIDFORGE_AUTH_MODE": "hosted",
        "GRIDFORGE_SESSION_SECRET": "test-agent-api-secret-0123456789abcdef",
        "GRIDFORGE_ADMIN_EMAIL": "admin@example.com",
        "GRIDFORGE_ADMIN_PASSWORD": "AdminPassword123!",
        "GRIDFORGE_ADMIN_NAME": "Trail Admin",
        "GRIDFORGE_ALLOWED_HOSTS": "maps.example.com",
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
        (web_dist / "index.html").write_text(
            "<!doctype html><html><head><title>GridForge</title></head><body>SPA</body></html>",
            encoding="utf-8",
        )

    app = create_app(data_dir=data_dir, packs_dir=packs_dir, web_dist_dir=web_dist)
    return app, data_dir, packs_dir, data_dir / "gridforge_jobs.sqlite3"


def _login(client: TestClient, email: str, password: str) -> dict[str, Any]:
    resp = client.post("/api/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _create_friend(admin_client: TestClient, app, email: str = "friend@example.com") -> tuple[TestClient, dict[str, Any]]:
    inv = admin_client.post(
        "/api/admin/invites",
        json={"email": email, "role": "friend", "expires_hours": 24},
    )
    assert inv.status_code == 201, inv.text
    token = inv.json()["token"]

    friend_client = TestClient(app, base_url="http://localhost:8080")
    accepted = friend_client.post(
        "/api/auth/accept-invite",
        json={"token": token, "email": email, "password": "FriendPassword123!", "display_name": "Field Agent"},
    )
    assert accepted.status_code == 200, accepted.text
    return friend_client, accepted.json()


def _write_pdf(path: Path, *, num_pages: int = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    for _ in range(num_pages):
        writer.add_blank_page(width=595.28, height=841.89)
    with path.open("wb") as fh:
        writer.write(fh)
    writer.close()


def _seed_pack(packs_dir: Path, pack_id: str = "kosciuszko") -> None:
    pdir = packs_dir / pack_id
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "dem.tif").write_bytes(b"dem")
    manifest = PackManifest(
        pack_id=pack_id,
        name="Kosciuszko Alpine",
        region="kosciuszko",
        bbox_wgs84=BBox(min_x=148.0, min_y=-36.7, max_x=148.6, max_y=-36.2),
        utm_zones=["55H"],
        dem_path="dem.tif",
        vertical_datum="AHD",
        created_at="2026-11-26T00:00:00Z",
        size_bytes=512,
    )
    (pdir / "pack.json").write_text(manifest.model_dump_json(indent=2), encoding="utf-8")


class TestApiKeysDBAndHostedAuth:
    def test_db_api_keys_crud_expiry_and_revocation(self, tmp_path: Path) -> None:
        db_path = tmp_path / "keys.sqlite3"
        db.init_db(db_path)
        db.create_user(db_path, user_id="usr_alice", email="alice@example.com", role="friend")

        now = datetime(2026, 11, 26, 12, 0, 0, tzinfo=timezone.utc)
        created = db.create_api_key(
            db_path,
            key_id="key_active0001",
            user_id="usr_alice",
            name="Claude Desktop",
            key_prefix="gf_active123",
            token_hash=sha256_hex("gf_active_secret"),
            scopes=["all"],
            expires_at=db._dt_to_iso(now + timedelta(days=30)),
        )
        assert created["key_id"] == "key_active0001"
        assert created["scopes"] == ["all"]
        assert created["status"] == "active"

        # Active lookup succeeds
        hit = db.get_api_key_and_user_by_token_hash(
            db_path, sha256_hex("gf_active_secret"), now=now
        )
        assert hit is not None
        api_key_row, user_row = hit
        assert api_key_row["key_id"] == "key_active0001"
        assert user_row["email"] == "alice@example.com"

        # Touch last_used_at updates timestamp
        db.touch_api_key_last_used(db_path, "key_active0001", now=now + timedelta(minutes=5))
        listed = db.list_api_keys(db_path, user_id="usr_alice")
        assert len(listed) == 1
        assert listed[0]["last_used_at"] == db._dt_to_iso(now + timedelta(minutes=5))

        # Expired lookup returns None
        assert (
            db.get_api_key_and_user_by_token_hash(
                db_path,
                sha256_hex("gf_active_secret"),
                now=now + timedelta(days=31),
            )
            is None
        )

        # Revoking marks revoked and blocks lookup
        assert db.revoke_api_key(db_path, "key_active0001", user_id="usr_other") is False
        assert db.revoke_api_key(db_path, "key_active0001", user_id="usr_alice") is True
        assert (
            db.get_api_key_and_user_by_token_hash(
                db_path, sha256_hex("gf_active_secret"), now=now
            )
            is None
        )
        assert db.list_api_keys(db_path, user_id="usr_alice")[0]["status"] == "revoked"

    def test_hosted_api_keys_endpoints_turnstile_bypass_quotas_isolation_and_revocation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # In auth_mode="none", /api/auth/api-keys must return 403.
        monkeypatch.delenv("GRIDFORGE_AUTH_MODE", raising=False)
        monkeypatch.delenv("GRIDFORGE_API_KEY", raising=False)
        local_app = create_app(data_dir=tmp_path / "local_j", packs_dir=tmp_path / "local_p")
        local_client = TestClient(local_app, base_url="http://localhost:8080")
        assert local_client.get("/api/auth/api-keys").status_code == 403
        assert local_client.post("/api/auth/api-keys", json={"name": "Nope"}).status_code == 403
        assert local_client.delete("/api/auth/api-keys/key_1234567890").status_code == 403

        # Hosted mode with Turnstile enabled + Friend daily render limit of 1.
        app, _, _, db_path = _make_hosted_app(
            tmp_path,
            monkeypatch,
            extra_env={
                "GRIDFORGE_TURNSTILE_SITE_KEY": "site-key-123",
                "GRIDFORGE_TURNSTILE_SECRET_KEY": "secret-key-123",
                "GRIDFORGE_FRIEND_DAILY_RENDERS": "1",
            },
        )
        guest = TestClient(app, base_url="http://localhost:8080")
        assert guest.get("/api/auth/api-keys").status_code == 403
        assert guest.post("/api/auth/api-keys", json={"name": "Guest"}).status_code == 403

        admin = TestClient(app, base_url="http://localhost:8080")
        _login(admin, "admin@example.com", "AdminPassword123!")
        friend, friend_sess = _create_friend(admin, app, "scout@example.com")
        friend_uid = friend_sess["user"]["user_id"]

        # Admin creates a key; Friend creates a key.
        adm_key_res = admin.post("/api/auth/api-keys", json={"name": "Admin CLI", "expires_days": 30})
        assert adm_key_res.status_code == 201, adm_key_res.text
        adm_key_body = adm_key_res.json()
        adm_token = adm_key_body["token"]
        adm_key_id = adm_key_body["api_key"]["key_id"]
        assert adm_token.startswith("gf_")
        assert len(adm_token) > 40
        assert adm_key_body["api_key"]["key_prefix"] == adm_token[:12]

        fr_key_res = friend.post("/api/auth/api-keys", json={"name": "Codex MCP", "expires_days": 14})
        assert fr_key_res.status_code == 201, fr_key_res.text
        fr_token = fr_key_res.json()["token"]
        fr_key_id = fr_key_res.json()["api_key"]["key_id"]

        # GET /api/auth/api-keys returns only caller's keys and never exposes plaintext/hash.
        fr_list = friend.get("/api/auth/api-keys")
        assert fr_list.status_code == 200
        assert len(fr_list.json()["api_keys"]) == 1
        assert fr_list.json()["api_keys"][0]["key_id"] == fr_key_id
        assert fr_token not in fr_list.text
        assert "token_hash" not in fr_list.text

        # Friend cannot revoke Admin's key (403).
        assert friend.delete(f"/api/auth/api-keys/{adm_key_id}").status_code == 403

        # Fresh stateless API client using Authorization: Bearer bypasses Turnstile & sets NO guest cookie.
        bot = TestClient(app, base_url="http://localhost:8080")
        sess_bearer = bot.get(
            "/api/auth/session", headers={"Authorization": f"Bearer {fr_token}"}
        )
        assert sess_bearer.status_code == 200
        assert sess_bearer.json()["authenticated"] is True
        assert sess_bearer.json()["role"] == "friend"
        assert sess_bearer.json()["user"]["user_id"] == friend_uid
        assert "set-cookie" not in {k.lower() for k in sess_bearer.headers.keys()}

        # last_used_at was populated!
        assert friend.get("/api/auth/api-keys").json()["api_keys"][0]["last_used_at"] is not None

        # X-API-Key header works too, bypasses Turnstile, and respects Friend daily quota (1/day -> 429 on 2nd).
        j1 = bot.post(
            "/api/jobs",
            json={"center": "-33.732, 150.312"},
            headers={"X-API-Key": fr_token},
        )
        assert j1.status_code == 202, j1.text
        fr_job_id = j1.json()["job_id"]
        assert j1.json()["owner_id"] == friend_uid

        j2 = bot.post(
            "/api/jobs",
            json={"center": "-33.732, 150.312"},
            headers={"Authorization": f"Bearer {fr_token}"},
        )
        assert j2.status_code == 429

        # Admin Bearer key creates Admin's own job -> tenant isolation prevents Friend's key from seeing it!
        adm_job = bot.post(
            "/api/jobs",
            json={"center": "-33.732, 150.312"},
            headers={"Authorization": f"Bearer {adm_token}"},
        )
        assert adm_job.status_code == 202
        adm_job_id = adm_job.json()["job_id"]
        assert (
            bot.get(f"/api/jobs/{adm_job_id}", headers={"Authorization": f"Bearer {fr_token}"}).status_code
            == 403
        )
        assert (
            bot.get(f"/api/jobs/{fr_job_id}", headers={"Authorization": f"Bearer {adm_token}"}).status_code
            == 403
        )

        # Header takes precedence BEFORE cookies: even on logged-in admin client, bad Bearer -> 401!
        bad_with_cookie = admin.get(
            "/api/auth/session",
            headers={"Authorization": "Bearer gf_invalid_key_value"},
        )
        assert bad_with_cookie.status_code == 401
        assert bad_with_cookie.json()["detail"] == "Invalid or expired API key."

        # Expired key -> 401
        exp_tok = "gf_expired_secret_token_value"
        db.create_api_key(
            db_path,
            key_id="key_expired001",
            user_id=friend_uid,
            name="Expired Key",
            key_prefix=exp_tok[:12],
            token_hash=sha256_hex(exp_tok),
            expires_at="2020-01-01T00:00:00Z",
        )
        assert (
            bot.get("/api/auth/session", headers={"Authorization": f"Bearer {exp_tok}"}).status_code
            == 401
        )

        # Revoked key -> 401 immediately
        rev = friend.delete(f"/api/auth/api-keys/{fr_key_id}")
        assert rev.status_code == 200
        assert rev.json() == {"revoked": True, "key_id": fr_key_id}
        assert (
            bot.get("/api/auth/session", headers={"Authorization": f"Bearer {fr_token}"}).status_code
            == 401
        )

        # Mint a non-expiring second Friend key, then disable Friend user -> key immediately returns 401!
        fr_key2_res = friend.post(
            "/api/auth/api-keys", json={"name": "Non-Expiring Key", "expires_days": None}
        )
        assert fr_key2_res.status_code == 201
        assert fr_key2_res.json()["api_key"]["expires_at"] is None
        fr_key2 = fr_key2_res.json()["token"]
        assert (
            bot.get("/api/auth/session", headers={"Authorization": f"Bearer {fr_key2}"}).status_code
            == 200
        )
        assert admin.patch(f"/api/admin/users/{friend_uid}", json={"disabled": True}).status_code == 200
        assert (
            bot.get("/api/auth/session", headers={"Authorization": f"Bearer {fr_key2}"}).status_code
            == 401
        )
        # Missing/invalid key_id on revoke returns 400/404
        assert admin.delete("/api/auth/api-keys/bad!").status_code == 400
        assert admin.delete("/api/auth/api-keys/key_missing00001").status_code == 404


class TestStaticApiKeyInNoneMode:
    def test_none_mode_static_gridforge_api_key_enforcement(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GRIDFORGE_AUTH_MODE", "none")
        monkeypatch.setenv("GRIDFORGE_API_KEY", "local-super-secret-key")
        app = create_app(data_dir=tmp_path / "j", packs_dir=tmp_path / "p")
        client = TestClient(app, base_url="http://localhost:8080")

        # Read-only GET endpoints work without a key
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/capabilities").status_code == 200
        assert client.get("/api/jobs").status_code == 200
        assert client.get("/llms.txt").status_code == 200
        assert client.get("/openapi.json").status_code == 200

        # Explicit wrong key on GET /api/* returns 401
        assert (
            client.get("/api/jobs", headers={"Authorization": "Bearer wrong-key"}).status_code
            == 401
        )

        # Mutating /api/* calls require matching Bearer or X-API-Key
        assert client.post("/api/jobs", json={"center": "-33.732, 150.312"}).status_code == 401
        assert (
            client.post(
                "/api/jobs",
                json={"center": "-33.732, 150.312"},
                headers={"Authorization": "Bearer wrong"},
            ).status_code
            == 401
        )
        assert client.post("/api/footprint", json={"center": "-33.732, 150.312"}).status_code == 401

        # Matching Authorization: Bearer succeeds
        ok_bearer = client.post(
            "/api/jobs",
            json={"center": "-33.732, 150.312"},
            headers={"Authorization": "Bearer local-super-secret-key"},
        )
        assert ok_bearer.status_code == 202

        # Matching X-API-Key succeeds
        ok_header = client.post(
            "/api/footprint",
            json={"center": "-33.732, 150.312"},
            headers={"X-API-Key": "local-super-secret-key"},
        )
        assert ok_header.status_code == 200

        # /mcp calls require matching key (401 without or with wrong key; 404 when unmounted with valid key)
        assert client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 401
        assert client.get("/mcp").status_code == 401
        assert (
            client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                headers={"Authorization": "Bearer wrong"},
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                headers={"Authorization": "Bearer local-super-secret-key"},
            ).status_code
            == 404
        )


class TestSignedArtifactAndPagePreviewUrls:
    def test_sign_and_verify_artifact_token_unit(self) -> None:
        secret = b"unit-test-hmac-secret-key"
        now = 1_760_000_000
        tok = sign_artifact_token("job12345678", "artifact:map.pdf", secret, ttl_seconds=60, now_unix=now)
        assert verify_artifact_token(
            tok,
            expected_job_id="job12345678",
            expected_resource="artifact:map.pdf",
            secret=secret,
            now_unix=now + 59,
        )
        # Expired
        assert not verify_artifact_token(
            tok,
            expected_job_id="job12345678",
            expected_resource="artifact:map.pdf",
            secret=secret,
            now_unix=now + 60,
        )
        # Wrong job_id, resource, or secret
        assert not verify_artifact_token(
            tok,
            expected_job_id="otherjob9999",
            expected_resource="artifact:map.pdf",
            secret=secret,
            now_unix=now + 10,
        )
        assert not verify_artifact_token(
            tok,
            expected_job_id="job12345678",
            expected_resource="artifact:other.pdf",
            secret=secret,
            now_unix=now + 10,
        )
        assert not verify_artifact_token(
            tok,
            expected_job_id="job12345678",
            expected_resource="page:1",
            secret=secret,
            now_unix=now + 10,
        )
        assert not verify_artifact_token(
            tok,
            expected_job_id="job12345678",
            expected_resource="artifact:map.pdf",
            secret=b"different-secret",
            now_unix=now + 10,
        )

    def test_signed_artifact_downloads_and_multipage_png_previews(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app, data_dir, _, db_path = _make_hosted_app(
            tmp_path,
            monkeypatch,
            extra_env={"GRIDFORGE_SIGNED_URL_TTL_SECONDS": "1800"},
        )
        admin = TestClient(app, base_url="http://localhost:8080")
        _login(admin, "admin@example.com", "AdminPassword123!")

        created = admin.post("/api/jobs", json={"center": "-33.732, 150.312"})
        assert created.status_code == 202
        job_id = created.json()["job_id"]

        # Complete the job with a real 3-page PDF artifact
        claimed = db.claim_next_job(db_path)
        assert claimed is not None
        pdf_path = data_dir / job_id / "artifacts" / "atlas_3p.pdf"
        _write_pdf(pdf_path, num_pages=3)
        db.complete_job(
            db_path,
            job_id,
            artifacts=[
                JobArtifact(
                    kind="pdf",
                    filename="atlas_3p.pdf",
                    size_bytes=pdf_path.stat().st_size,
                    download_url=f"/api/jobs/{job_id}/artifacts/atlas_3p.pdf",
                )
            ],
            elapsed_seconds=1.25,
            peak_rss_mb=320.0,
        )

        # GET /api/jobs and GET /api/jobs/{job_id} enrich artifacts with signed_download_url
        job_res = admin.get(f"/api/jobs/{job_id}")
        assert job_res.status_code == 200
        art = job_res.json()["artifacts"][0]
        assert art["download_url"] == f"/api/jobs/{job_id}/artifacts/atlas_3p.pdf"
        assert art["signed_download_url"].startswith(f"/api/jobs/{job_id}/artifacts/atlas_3p.pdf?sig=")

        list_art = admin.get("/api/jobs").json()["jobs"][0]["artifacts"][0]
        assert list_art["signed_download_url"].startswith(f"/api/jobs/{job_id}/artifacts/atlas_3p.pdf?sig=")

        # Unauthenticated anonymous client: unsigned -> 403; signed -> 200 PDF without cookies!
        anon = TestClient(app, base_url="http://localhost:8080")
        assert anon.get(art["download_url"]).status_code == 403

        dl = anon.get(art["signed_download_url"])
        assert dl.status_code == 200
        assert dl.headers["content-type"].startswith("application/pdf")
        assert dl.content.startswith(b"%PDF-")
        assert "set-cookie" not in {k.lower() for k in dl.headers.keys()}

        # Tampered or expired sig -> 403
        assert anon.get(f"{art['download_url']}?sig=tampered.token").status_code == 403
        expired_sig = sign_artifact_token(
            job_id,
            "artifact:atlas_3p.pdf",
            app.state.auth_config.session_secret,
            ttl_seconds=1,
            now_unix=1_000_000,
        )
        assert anon.get(f"{art['download_url']}?sig={expired_sig}").status_code == 403

        # GET /api/jobs/{job_id}/pages lists 3 pages with signed preview URLs; anon gets 403
        assert anon.get(f"/api/jobs/{job_id}/pages").status_code == 403
        pages_res = admin.get(f"/api/jobs/{job_id}/pages")
        assert pages_res.status_code == 200
        pages_body = pages_res.json()
        assert pages_body["job_id"] == job_id
        assert pages_body["page_count"] == 3
        assert [p["page"] for p in pages_body["pages"]] == [1, 2, 3]
        p2 = pages_body["pages"][1]
        assert p2["preview_url"] == f"/api/jobs/{job_id}/pages/2.png"
        assert p2["signed_preview_url"].startswith(f"/api/jobs/{job_id}/pages/2.png?sig=")

        # Owner fetches page 2 PNG at custom width -> rasterized & cached atomically
        png_owner = admin.get(f"/api/jobs/{job_id}/pages/2.png?width=360")
        assert png_owner.status_code == 200
        assert png_owner.headers["content-type"] == "image/png"
        assert png_owner.content.startswith(b"\x89PNG\r\n\x1a\n")
        cached_file = data_dir / job_id / "pages" / "page_2_360.png"
        assert cached_file.is_file()

        # Anonymous client fetches signed page 2 PNG (even with custom width!) without cookies/Bearer
        assert anon.get(f"/api/jobs/{job_id}/pages/2.png").status_code == 403
        png_signed = anon.get(f"{p2['signed_preview_url']}&width=320")
        assert png_signed.status_code == 200
        assert png_signed.headers["content-type"] == "image/png"
        assert png_signed.content.startswith(b"\x89PNG\r\n\x1a\n")
        assert (data_dir / job_id / "pages" / "page_2_320.png").is_file()
        assert "set-cookie" not in {k.lower() for k in png_signed.headers.keys()}

        # Cross-page sig (page 2 sig on page 1) or artifact sig on page 2 -> 403
        p2_sig = urllib.parse.parse_qs(urllib.parse.urlsplit(p2["signed_preview_url"]).query)["sig"][0]
        art_sig = urllib.parse.parse_qs(urllib.parse.urlsplit(art["signed_download_url"]).query)["sig"][0]
        assert anon.get(f"/api/jobs/{job_id}/pages/1.png?sig={p2_sig}").status_code == 403
        assert anon.get(f"/api/jobs/{job_id}/pages/2.png?sig={art_sig}").status_code == 403

        # Bounds & validation checks
        assert admin.get(f"/api/jobs/{job_id}/pages/0.png").status_code == 400
        assert admin.get(f"/api/jobs/{job_id}/pages/4.png").status_code == 404
        assert admin.get(f"/api/jobs/{job_id}/pages/1.png?width=100").status_code == 422
        assert admin.get(f"/api/jobs/{job_id}/pages/1.png?width=5000").status_code == 422

        # Job without any PDF artifact returns page_count=0 on /pages and 404 on /pages/1.png
        no_pdf_job = admin.post("/api/jobs", json={"center": "-33.732, 150.312"}).json()["job_id"]
        no_pdf_pages = admin.get(f"/api/jobs/{no_pdf_job}/pages")
        assert no_pdf_pages.status_code == 200
        assert no_pdf_pages.json() == {"job_id": no_pdf_job, "page_count": 0, "pages": []}
        assert admin.get(f"/api/jobs/{no_pdf_job}/pages/1.png").status_code == 404


class TestSyncWaitCreateJob:
    def test_post_jobs_wait_true_completes_200_and_timeout_returns_202(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GRIDFORGE_AUTH_MODE", raising=False)
        monkeypatch.delenv("GRIDFORGE_API_KEY", raising=False)
        data_dir = tmp_path / "jobs"
        packs_dir = tmp_path / "packs"
        app = create_app(data_dir=data_dir, packs_dir=packs_dir)
        db_path = data_dir / "gridforge_jobs.sqlite3"
        client = TestClient(app, base_url="http://localhost:8080")

        # 1. Default wait=False remains 202 Accepted
        r_async = client.post("/api/jobs", json={"center": "-33.732, 150.312"})
        assert r_async.status_code == 202
        assert r_async.json()["status"] == "queued"
        db.cancel_job(db_path, r_async.json()["job_id"])

        # 2. wait=True completes synchronously (200 OK) with enriched signed_download_url
        stop_event = threading.Event()

        def _mini_worker() -> None:
            deadline = time.monotonic() + 5.0
            while not stop_event.is_set() and time.monotonic() < deadline:
                claimed = db.claim_next_job(db_path)
                if claimed is not None:
                    jid = claimed.record.job_id
                    pdf_path = data_dir / jid / "artifacts" / "sync.pdf"
                    _write_pdf(pdf_path, num_pages=1)
                    db.complete_job(
                        db_path,
                        jid,
                        artifacts=[
                            JobArtifact(
                                kind="pdf",
                                filename="sync.pdf",
                                size_bytes=pdf_path.stat().st_size,
                                download_url=f"/api/jobs/{jid}/artifacts/sync.pdf",
                            )
                        ],
                        elapsed_seconds=0.1,
                    )
                    return
                time.sleep(0.05)

        t = threading.Thread(target=_mini_worker, daemon=True)
        t.start()
        try:
            r_sync = client.post(
                "/api/jobs?wait=true&timeout_s=5",
                json={"center": "-33.732, 150.312", "title": "Sync Render"},
            )
        finally:
            stop_event.set()
            t.join(timeout=2.0)

        assert r_sync.status_code == 200, r_sync.text
        sync_body = r_sync.json()
        assert sync_body["status"] == "succeeded"
        assert len(sync_body["artifacts"]) == 1
        assert sync_body["artifacts"][0]["signed_download_url"].startswith(
            f"/api/jobs/{sync_body['job_id']}/artifacts/sync.pdf?sig="
        )

        # 3. wait=True when job fails synchronously returns 200 OK with status="failed"
        stop_fail = threading.Event()

        def _fail_worker() -> None:
            deadline = time.monotonic() + 5.0
            while not stop_fail.is_set() and time.monotonic() < deadline:
                claimed = db.claim_next_job(db_path)
                if claimed is not None:
                    db.fail_job(db_path, claimed.record.job_id, error="Simulated render error")
                    return
                time.sleep(0.05)

        tf = threading.Thread(target=_fail_worker, daemon=True)
        tf.start()
        try:
            r_fail = client.post(
                "/api/jobs?wait=true&timeout_s=5",
                json={"center": "-33.732, 150.312", "title": "Fail Sync"},
            )
        finally:
            stop_fail.set()
            tf.join(timeout=2.0)

        assert r_fail.status_code == 200
        assert r_fail.json()["status"] == "failed"
        assert r_fail.json()["error"] == "Simulated render error"

        # 4. wait=True when job stays queued past timeout_s=1 returns 202 Accepted
        t0 = time.monotonic()
        r_timeout = client.post(
            "/api/jobs?wait=true&timeout_s=1",
            json={"center": "-33.732, 150.312", "title": "Still Pending"},
        )
        elapsed = time.monotonic() - t0
        assert r_timeout.status_code == 202
        assert r_timeout.json()["status"] == "queued"
        assert elapsed >= 0.9


class TestCapabilitiesLlmsTxtOpenAPIAndMcpFallback:
    def test_capabilities_llms_txt_and_spa_mcp_404(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app, _, packs_dir, _ = _make_hosted_app(
            tmp_path,
            monkeypatch,
            extra_env={"GRIDFORGE_PUBLIC_URL": "https://maps.example.com/"},
            with_dist=True,
        )
        _seed_pack(packs_dir, "kosciuszko")

        guest = TestClient(app, base_url="http://localhost:8080")
        admin = TestClient(app, base_url="http://localhost:8080")
        _login(admin, "admin@example.com", "AdminPassword123!")
        adm_key = admin.post("/api/auth/api-keys", json={"name": "Caps Check"}).json()["token"]

        # Guest capabilities (no Set-Cookie minted on discovery endpoint)
        g_caps_res = guest.get("/api/capabilities")
        assert g_caps_res.status_code == 200
        assert "set-cookie" not in {k.lower() for k in g_caps_res.headers.keys()}
        g_caps = g_caps_res.json()

        assert g_caps["version"] == __version__
        assert g_caps["auth_mode"] == "hosted"
        assert g_caps["caller_role"] == "guest"
        assert g_caps["public_url"] == "https://maps.example.com"
        assert g_caps["mcp_endpoint"] == "/mcp"
        assert g_caps["openapi_url"] == "/openapi.json"
        assert g_caps["installed_packs_count"] == 1
        assert g_caps["scales"] == list(VALID_SCALES)
        assert {p["id"] for p in g_caps["paper_sizes"]} == {p.value for p in PaperSize}
        assert set(g_caps["orientations"]) == {o.value for o in Orientation}
        assert {c["id"] for c in g_caps["color_modes"]} == {c.value for c in ColorMode}
        assert all(c["description"] for c in g_caps["color_modes"])
        assert set(g_caps["units"]) == {"metric", "imperial"}
        assert {d["id"] for d in g_caps["dem_sources"]} == {d.value for d in DemSourceKind}
        assert all(d["region"] and d["vertical_datum"] for d in g_caps["dem_sources"])
        assert {o["id"] for o in g_caps["regional_overlays"]["us"]} == {o.value for o in USOverlayKind}
        assert {o["id"] for o in g_caps["regional_overlays"]["au"]} == {o.value for o in AUOverlayKind}
        assert {o["id"] for o in g_caps["regional_overlays"]["nz"]} == {o.value for o in NZOverlayKind}
        assert {o["id"] for o in g_caps["regional_overlays"]["ca"]} == {o.value for o in CAOverlayKind}
        rf_freqs = {r["freq_mhz"] for r in g_caps["rf_band_presets"]}
        assert {915.0, 868.0, 477.0, 462.625, 146.52, 2437.0, 5800.0}.issubset(rf_freqs)
        assert {t["id"] for t in g_caps["tactical_graphics"]} == {
            "range_ring",
            "bearing_line",
            "corridor",
            "sector",
        }
        assert g_caps["caller_capabilities"]["max_grid_cells"] == 1
        assert g_caps["caller_capabilities"]["packs_manage_allowed"] is False

        # Admin via Bearer key sees elevated caller capabilities
        a_caps = guest.get(
            "/api/capabilities", headers={"Authorization": f"Bearer {adm_key}"}
        ).json()
        assert a_caps["caller_role"] == "admin"
        assert a_caps["authenticated"] is True
        assert a_caps["caller_capabilities"]["max_grid_cells"] == 9
        assert a_caps["caller_capabilities"]["packs_manage_allowed"] is True

        # GET /llms.txt returns concise AI instructions (not swallowed by SPA!)
        llms = guest.get("/llms.txt")
        assert llms.status_code == 200
        assert llms.headers["content-type"].startswith("text/plain")
        for needle in ("/mcp", "/api/capabilities", "/openapi.json", "/docs", "--spec-file", "--json"):
            assert needle in llms.text

        # Unmounted /mcp and /mcp/* return 404 rather than SPA HTML
        assert guest.get("/mcp").status_code == 404
        assert guest.get("/mcp/sse").status_code == 404
        # Ordinary SPA client routes still return index.html
        assert guest.get("/studio/share").status_code == 200
        assert "SPA" in guest.get("/studio/share").text

    def test_openapi_schema_completeness_security_and_servers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GRIDFORGE_AUTH_MODE", raising=False)
        monkeypatch.delenv("GRIDFORGE_API_KEY", raising=False)
        monkeypatch.delenv("GRIDFORGE_PUBLIC_URL", raising=False)
        app_no_pub = create_app(data_dir=tmp_path / "j1", packs_dir=tmp_path / "p1")
        schema_no_pub = TestClient(app_no_pub, base_url="http://localhost:8080").get("/openapi.json").json()
        assert "servers" not in schema_no_pub

        monkeypatch.setenv("GRIDFORGE_PUBLIC_URL", "https://gridmaps.example/")
        app = create_app(data_dir=tmp_path / "j2", packs_dir=tmp_path / "p2")
        resp = TestClient(app, base_url="http://localhost:8080").get("/openapi.json")
        assert resp.status_code == 200
        schema = resp.json()

        assert schema["servers"] == [{"url": "https://gridmaps.example"}]

        components = schema["components"]
        schemas = components["schemas"]
        assert "CreateJobRequest" in schemas
        assert "StripPlanRequest" in schemas
        assert "BearerAuth" in components["securitySchemes"]
        assert components["securitySchemes"]["BearerAuth"]["type"] == "http"
        assert components["securitySchemes"]["BearerAuth"]["scheme"] == "bearer"

        # POST /api/jobs has full application/json ($ref) + multipart/form-data + wait/timeout_s + 200/202
        jobs_post = schema["paths"]["/api/jobs"]["post"]
        assert jobs_post["operationId"] == "createJob"
        assert jobs_post["summary"]
        assert jobs_post["tags"] == ["Jobs & Artifacts"]
        assert (
            jobs_post["requestBody"]["content"]["application/json"]["schema"]["$ref"]
            == "#/components/schemas/CreateJobRequest"
        )
        assert "multipart/form-data" in jobs_post["requestBody"]["content"]
        param_names = {p["name"] for p in jobs_post.get("parameters", [])}
        assert {"wait", "timeout_s"}.issubset(param_names)
        assert "200" in jobs_post["responses"]
        assert "202" in jobs_post["responses"]

        # POST /api/strip-plan has full application/json ($ref) + multipart/form-data
        strip_post = schema["paths"]["/api/strip-plan"]["post"]
        assert strip_post["operationId"] == "planStripAtlas"
        assert (
            strip_post["requestBody"]["content"]["application/json"]["schema"]["$ref"]
            == "#/components/schemas/StripPlanRequest"
        )
        assert "multipart/form-data" in strip_post["requestBody"]["content"]

        # Every $ref in the entire OpenAPI schema resolves cleanly inside components.schemas!
        refs: list[str] = []

        def _collect_refs(node: Any) -> None:
            if isinstance(node, dict):
                if "$ref" in node and isinstance(node["$ref"], str):
                    refs.append(node["$ref"])
                for val in node.values():
                    _collect_refs(val)
            elif isinstance(node, list):
                for item in node:
                    _collect_refs(item)

        _collect_refs(schema)
        assert len(refs) > 0
        for ref in refs:
            assert ref.startswith("#/components/schemas/"), f"Unexpected $ref: {ref}"
            target = ref.removeprefix("#/components/schemas/")
            assert target in schemas, f"Dangling OpenAPI $ref: {ref}"

        # Key CreateJobRequest fields carry helpful descriptions for AI clients
        cj_props = schemas["CreateJobRequest"]["properties"]
        assert "description" in cj_props["center"]
        assert "description" in cj_props["scale"]
        assert "description" in cj_props["color_mode"]

        # All API operations have unique operationIds, non-empty summaries, and tags
        op_ids: list[str] = []
        for path, methods in schema["paths"].items():
            if not path.startswith("/api/"):
                continue
            for method, spec in methods.items():
                if isinstance(spec, dict) and method in {"get", "post", "put", "patch", "delete"}:
                    assert spec.get("operationId"), f"Missing operationId on {method.upper()} {path}"
                    assert spec.get("summary"), f"Missing summary on {method.upper()} {path}"
                    assert spec.get("tags"), f"Missing tags on {method.upper()} {path}"
                    op_ids.append(spec["operationId"])
        assert len(op_ids) == len(set(op_ids)), f"Duplicate operationIds: {op_ids}"

        # Ensure zero QGIS/PyQt5 imports across the entire web API test suite
        assert "qgis" not in sys.modules
        assert "PyQt5" not in sys.modules
