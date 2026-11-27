"""End-to-end integration tests wiring FastAPI + Hosted PAT Auth + /mcp + Real MCP App HTML + Signed PDFs."""

from __future__ import annotations

import base64
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter

from gridforge import __version__
from gridforge.mcp.server import APP_MIME_TYPE, APP_URI
from gridforge.web import db
from gridforge.web.app import create_app
from gridforge.web.schemas import JobArtifact


def _write_pdf(path: Path, num_pages: int = 2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    for _ in range(num_pages):
        writer.add_blank_page(width=595.28, height=841.89)
    with path.open("wb") as fh:
        writer.write(fh)


def test_full_stack_hosted_pat_mcp_app_and_signed_pdf_e2e(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify full integrated AI stack: hosted auth + PAT + /mcp + real bundled MCP App + sync render + signed URLs."""
    monkeypatch.setenv("GRIDFORGE_AUTH_MODE", "hosted")
    monkeypatch.setenv("GRIDFORGE_SESSION_SECRET", "e2e-integration-session-secret-0123456789")
    monkeypatch.setenv("GRIDFORGE_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("GRIDFORGE_ADMIN_PASSWORD", "AdminPassword123!")
    monkeypatch.setenv("GRIDFORGE_TURNSTILE_SITE_KEY", "test-site-key")
    monkeypatch.setenv("GRIDFORGE_TURNSTILE_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("GRIDFORGE_PUBLIC_URL", "https://gridmaps.example")
    monkeypatch.delenv("GRIDFORGE_API_KEY", raising=False)
    monkeypatch.delenv("GRIDFORGE_MCP_APP_HTML", raising=False)

    data_dir = tmp_path / "jobs"
    packs_dir = tmp_path / "packs"
    app = create_app(data_dir=data_dir, packs_dir=packs_dir)
    db_path = data_dir / "gridforge_jobs.sqlite3"

    admin_client = TestClient(app, base_url="http://localhost:8080")
    login = admin_client.post(
        "/api/auth/login",
        json={"email": "admin@example.com", "password": "AdminPassword123!"},
    )
    assert login.status_code == 200

    # Mint a Bearer Personal Access Token
    key_resp = admin_client.post("/api/auth/api-keys", json={"name": "Antigravity MCP Client"})
    assert key_resp.status_code == 201
    pat_token = key_resp.json()["token"]
    assert pat_token.startswith("gf_")

    # Fresh unauthenticated HTTP client acting as an external AI host (Claude / ChatGPT / Antigravity)
    ai_client = TestClient(app, base_url="http://localhost:8080")
    auth_headers = {"Authorization": f"Bearer {pat_token}"}

    # 1. Initialize MCP session over Streamable HTTP /mcp
    init = ai_client.post(
        "/mcp",
        headers=auth_headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        },
    )
    assert init.status_code == 200
    session_id = init.headers["mcp-session-id"]
    mcp_headers = {**auth_headers, "Mcp-Session-Id": session_id}
    init_res = init.json()["result"]
    assert init_res["serverInfo"]["version"] == __version__
    assert "io.modelcontextprotocol/ui" in init_res["capabilities"]["extensions"]

    # 2. Verify real compiled single-file MCP App HTML is served (NOT placeholder fallback)
    res_read = ai_client.post(
        "/mcp",
        headers=mcp_headers,
        json={"jsonrpc": "2.0", "id": 2, "method": "resources/read", "params": {"uri": APP_URI}},
    )
    assert res_read.status_code == 200
    contents = res_read.json()["result"]["contents"]
    assert len(contents) == 1
    assert contents[0]["mimeType"] == APP_MIME_TYPE
    html_text = contents[0]["text"]
    assert len(html_text) > 100_000, "Expected compiled single-file MCP App bundle, not fallback HTML"
    assert "Interactive MCP App bundle placeholder" not in html_text
    assert "<script" in html_text
    assert "localStorage" not in html_text
    assert "IndexedDB" not in html_text

    # 3. Verify tools/list advertises inline UI, Antigravity aux_pane/global entrypoints, and app-only tools
    tools_resp = ai_client.post(
        "/mcp",
        headers=mcp_headers,
        json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
    )
    assert tools_resp.status_code == 200
    tools_by_name = {t["name"]: t for t in tools_resp.json()["result"]["tools"]}
    assert tools_by_name["open_gridforge_studio"]["_meta"]["com.google.antigravity/ui"]["entrypoints"][0]["type"] == "aux_pane"
    assert tools_by_name["answer_location_briefing"]["_meta"]["ui"]["resourceUri"] == APP_URI
    assert tools_by_name["app_get_job_page"]["_meta"]["ui"]["visibility"] == ["app"]

    # 4. Verify Bearer PAT forwarding via /mcp -> /api/capabilities sees admin role
    caps_resp = ai_client.post(
        "/mcp",
        headers=mcp_headers,
        json={
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "get_capabilities", "arguments": {}},
        },
    )
    assert caps_resp.status_code == 200
    caps_result = caps_resp.json()["result"]
    assert not caps_result.get("isError")
    assert caps_result["structuredContent"]["caller_role"] == "admin"
    assert caps_result["structuredContent"]["authenticated"] is True

    # 5. Question-answering briefing returns prose answer + PNG visual asset + interactive briefing structuredContent
    brief_resp = ai_client.post(
        "/mcp",
        headers=mcp_headers,
        json={
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "answer_location_briefing",
                "arguments": {
                    "location": "-33.732, 150.312",
                    "question": "What are the MGRS coordinates and WMM2025 declination?",
                },
            },
        },
    )
    assert brief_resp.status_code == 200
    brief_res = brief_resp.json()["result"]
    assert not brief_res["isError"]
    assert brief_res["structuredContent"]["view"] == "briefing"
    assert brief_res["structuredContent"]["recommendations"]["dem_source"] == "au-elvis"
    img_block = next(c for c in brief_res["content"] if c["type"] == "image")
    assert base64.b64decode(img_block["data"]).startswith(b"\x89PNG\r\n\x1a\n")

    # 6. Synchronous render_map (wait=True) bypasses Turnstile via PAT, completes 2-page PDF,
    #    returns page-1 PNG inline + clickable signed PDF URL + app_get_job_page works for page 2
    stop_evt = threading.Event()

    def _mini_worker() -> None:
        deadline = time.monotonic() + 8.0
        while not stop_evt.is_set() and time.monotonic() < deadline:
            claimed = db.claim_next_job(db_path)
            if claimed is not None:
                jid = claimed.record.job_id
                pdf_file = data_dir / jid / "artifacts" / "katoomba_25k_a4_color.pdf"
                _write_pdf(pdf_file, num_pages=2)
                db.complete_job(
                    db_path,
                    jid,
                    artifacts=[
                        JobArtifact(
                            kind="pdf",
                            filename="katoomba_25k_a4_color.pdf",
                            size_bytes=pdf_file.stat().st_size,
                            download_url=f"/api/jobs/{jid}/artifacts/katoomba_25k_a4_color.pdf",
                        )
                    ],
                    elapsed_seconds=1.1,
                    peak_rss_mb=310.0,
                )
                return
            time.sleep(0.05)

    worker_thread = threading.Thread(target=_mini_worker, daemon=True)
    worker_thread.start()
    try:
        render_resp = ai_client.post(
            "/mcp",
            headers=mcp_headers,
            json={
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {
                    "name": "render_map",
                    "arguments": {
                        "center": "-33.732, 150.312",
                        "scale": 25000,
                        "paper": "a4",
                        "color_mode": "alpine",
                        "wait": True,
                        "timeout_s": 10,
                        "include_preview_image": True,
                    },
                },
            },
        )
    finally:
        stop_evt.set()
        worker_thread.join(timeout=2.0)

    assert render_resp.status_code == 200
    render_res = render_resp.json()["result"]
    assert not render_res["isError"]
    sc_render = render_res["structuredContent"]
    assert sc_render["view"] == "render"
    assert sc_render["job"]["status"] == "succeeded"
    assert sc_render["page_count"] == 2
    assert len(sc_render["pages"]) == 2
    assert any(c["type"] == "image" for c in render_res["content"])

    # Verify clickable signed artifact download works anonymously (no cookies, no Bearer header!)
    signed_url = sc_render["job"]["artifacts"][0]["signed_download_url"]
    assert signed_url.startswith("https://gridmaps.example/api/jobs/")
    rel_signed = signed_url.removeprefix("https://gridmaps.example")
    anon_browser = TestClient(app, base_url="http://localhost:8080")
    dl_pdf = anon_browser.get(rel_signed)
    assert dl_pdf.status_code == 200
    assert dl_pdf.content.startswith(b"%PDF-")

    # Verify Studio share snapshot works anonymously
    share_url = sc_render["share_url"]
    assert share_url.startswith("https://gridmaps.example/?share=")
    share_id = share_url.split("?share=", 1)[1]
    snap_resp = anon_browser.get(f"/api/shares/{share_id}")
    assert snap_resp.status_code == 200
    assert snap_resp.json()["state"]["color_mode"] == "alpine"

    # Verify app-only page loader fetches page 2 PNG cleanly
    job_id = sc_render["job"]["job_id"]
    p2_resp = ai_client.post(
        "/mcp",
        headers=mcp_headers,
        json={
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {
                "name": "app_get_job_page",
                "arguments": {"job_id": job_id, "page": 2, "width": 480},
            },
        },
    )
    assert p2_resp.status_code == 200
    p2_sc = p2_resp.json()["result"]["structuredContent"]
    assert p2_sc["page"] == 2
    assert p2_sc["page_count"] == 2
    assert base64.b64decode(p2_sc["data_base64"]).startswith(b"\x89PNG\r\n\x1a\n")

    # 7. Ensure zero QGIS/PyQt5 imports in web/MCP stack
    assert "qgis" not in sys.modules
    assert "PyQt5" not in sys.modules


def test_origin_proxy_secret_enforced_and_forwarded_through_mcp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify OriginProxyAuthMiddleware blocks direct /mcp bypasses and allows proxied MCP tool sub-requests."""
    origin_secret = "edge-origin-secret-0123456789abcdef"
    monkeypatch.setenv("GRIDFORGE_AUTH_MODE", "hosted")
    monkeypatch.setenv("GRIDFORGE_SESSION_SECRET", "e2e-origin-proxy-session-secret-0123456789")
    monkeypatch.setenv("GRIDFORGE_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("GRIDFORGE_ADMIN_PASSWORD", "AdminPassword123!")
    monkeypatch.setenv("GRIDFORGE_ORIGIN_PROXY_SECRET", origin_secret)
    monkeypatch.setenv("GRIDFORGE_ALLOWED_HOSTS", "maps.example.com")
    monkeypatch.setenv("GRIDFORGE_PUBLIC_URL", "https://maps.example.com")
    monkeypatch.delenv("GRIDFORGE_ENABLE_DOCS", raising=False)

    app = create_app(data_dir=tmp_path / "jobs", packs_dir=tmp_path / "packs")
    proxy_headers = {"X-GridForge-Origin-Secret": origin_secret}

    with TestClient(app, base_url="https://maps.example.com") as client:
        # Direct-to-origin /mcp without proxy secret -> 403 Forbidden
        assert client.get("/mcp").status_code == 403
        assert client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 403
        assert (
            client.post(
                "/mcp",
                headers={"X-GridForge-Origin-Secret": "wrong-secret"},
                json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            ).status_code
            == 403
        )

        # Hosted docs remain disabled by default (404 even through proxy)
        assert client.get("/openapi.json", headers=proxy_headers).status_code == 404
        assert client.get("/docs", headers=proxy_headers).status_code == 404

        # Login & mint PAT through Cloudflare proxy header
        login = client.post(
            "/api/auth/login",
            headers={**proxy_headers, "Origin": "https://maps.example.com", "Sec-Fetch-Site": "same-origin"},
            json={"email": "admin@example.com", "password": "AdminPassword123!"},
        )
        assert login.status_code == 200
        pat = client.post(
            "/api/auth/api-keys",
            headers={**proxy_headers, "Origin": "https://maps.example.com", "Sec-Fetch-Site": "same-origin"},
            json={"name": "Cloudflare MCP"},
        ).json()["token"]

        # Proxied /mcp calls forward X-GridForge-Origin-Secret + Bearer PAT into in-process ASGI calls
        mcp_headers = {**proxy_headers, "Authorization": f"Bearer {pat}"}
        caps = client.post(
            "/mcp",
            headers=mcp_headers,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "get_capabilities", "arguments": {}},
            },
        )
        assert caps.status_code == 200
        caps_res = caps.json()["result"]
        assert not caps_res.get("isError"), caps_res
        assert caps_res["structuredContent"]["caller_role"] == "admin"

        brief = client.post(
            "/mcp",
            headers=mcp_headers,
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "answer_location_briefing",
                    "arguments": {"location": "-33.732, 150.312", "include_image": False},
                },
            },
        )
        assert brief.status_code == 200
        assert not brief.json()["result"].get("isError")
        assert brief.json()["result"]["structuredContent"]["view"] == "briefing"


def test_mcp_cross_tenant_pat_isolation_api_key_cap_and_secret_stripping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify cross-tenant PAT isolation over /mcp, per-user API key cap, and GRIDFORGE_API_KEY env stripping."""
    from gridforge.subprocess_env import build_safe_subprocess_env
    from gridforge.web.auth import sign_token, verify_artifact_token

    monkeypatch.setenv("GRIDFORGE_API_KEY", "top-secret-static-api-key")
    safe_env = build_safe_subprocess_env(allow_network=False)
    assert "GRIDFORGE_API_KEY" not in safe_env

    # Forged boolean exp must be rejected
    secret = b"0123456789abcdef0123456789abcdef"
    bool_tok = sign_token('{"j":"job12345678","r":"artifact:a.pdf","exp":true}', secret)
    assert not verify_artifact_token(
        bool_tok,
        expected_job_id="job12345678",
        expected_resource="artifact:a.pdf",
        secret=secret,
    )

    monkeypatch.delenv("GRIDFORGE_API_KEY", raising=False)
    monkeypatch.setenv("GRIDFORGE_AUTH_MODE", "hosted")
    monkeypatch.setenv("GRIDFORGE_SESSION_SECRET", "e2e-isolation-session-secret-0123456789")
    monkeypatch.setenv("GRIDFORGE_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("GRIDFORGE_ADMIN_PASSWORD", "AdminPassword123!")
    monkeypatch.setenv("GRIDFORGE_ENABLE_DOCS", "true")

    data_dir = tmp_path / "jobs"
    app = create_app(data_dir=data_dir, packs_dir=tmp_path / "packs")
    db_path = data_dir / "gridforge_jobs.sqlite3"

    # Opt-in GRIDFORGE_ENABLE_DOCS=true exposes complete OpenAPI in hosted mode
    anon = TestClient(app, base_url="http://localhost:8080")
    oa = anon.get("/openapi.json")
    assert oa.status_code == 200
    assert "CreateJobRequest" in oa.json()["components"]["schemas"]

    # Create two invited Friends via Admin
    admin = TestClient(app, base_url="http://localhost:8080")
    assert admin.post("/api/auth/login", json={"email": "admin@example.com", "password": "AdminPassword123!"}).status_code == 200
    inv_a = admin.post("/api/admin/invites", json={"email": "a@example.com", "role": "friend"}).json()["token"]
    inv_b = admin.post("/api/admin/invites", json={"email": "b@example.com", "role": "friend"}).json()["token"]

    friend_a = TestClient(app, base_url="http://localhost:8080")
    assert friend_a.post("/api/auth/accept-invite", json={"token": inv_a, "email": "a@example.com", "password": "PasswordA123!"}).status_code == 200
    pat_a = friend_a.post("/api/auth/api-keys", json={"name": "Friend A MCP"}).json()["token"]

    friend_b = TestClient(app, base_url="http://localhost:8080")
    assert friend_b.post("/api/auth/accept-invite", json={"token": inv_b, "email": "b@example.com", "password": "PasswordB123!"}).status_code == 200
    pat_b = friend_b.post("/api/auth/api-keys", json={"name": "Friend B MCP"}).json()["token"]

    # Friend A queues and completes a PDF job
    job_id = friend_a.post("/api/jobs", json={"center": "-33.732, 150.312"}).json()["job_id"]
    claimed = db.claim_next_job(db_path)
    assert claimed is not None and claimed.record.job_id == job_id
    pdf_path = data_dir / job_id / "artifacts" / "secret_a.pdf"
    _write_pdf(pdf_path, num_pages=2)
    db.complete_job(
        db_path,
        job_id,
        artifacts=[
            JobArtifact(
                kind="pdf",
                filename="secret_a.pdf",
                size_bytes=pdf_path.stat().st_size,
                download_url=f"/api/jobs/{job_id}/artifacts/secret_a.pdf",
            )
        ],
        elapsed_seconds=1.0,
        peak_rss_mb=200.0,
    )

    # Friend B cannot read Friend A's job or page previews over /mcp
    for tool_name, args in (
        ("get_job_status", {"job_id": job_id}),
        ("get_job_page_preview", {"job_id": job_id, "page": 1}),
        ("app_get_job_status", {"job_id": job_id}),
        ("app_get_job_page", {"job_id": job_id, "page": 1}),
    ):
        resp = anon.post(
            "/mcp",
            headers={"Authorization": f"Bearer {pat_b}"},
            json={
                "jsonrpc": "2.0",
                "id": tool_name,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": args},
            },
        )
        assert resp.status_code == 200
        assert resp.json()["result"]["isError"] is True

    # Friend A can read their own job over /mcp
    ok_a = anon.post(
        "/mcp",
        headers={"Authorization": f"Bearer {pat_a}"},
        json={
            "jsonrpc": "2.0",
            "id": "ok-a",
            "method": "tools/call",
            "params": {"name": "app_get_job_page", "arguments": {"job_id": job_id, "page": 1, "width": 360}},
        },
    )
    assert ok_a.status_code == 200
    assert not ok_a.json()["result"].get("isError")

    # Per-user active API key cap (25 max; Friend B already has 1 active key)
    created_key_ids: list[str] = []
    for i in range(24):
        r = friend_b.post("/api/auth/api-keys", json={"name": f"Cap Key {i}"})
        assert r.status_code == 201
        created_key_ids.append(r.json()["api_key"]["key_id"])
    assert friend_b.post("/api/auth/api-keys", json={"name": "Over Cap"}).status_code == 400
    # Revoking one frees a slot
    assert friend_b.delete(f"/api/auth/api-keys/{created_key_ids[0]}").status_code == 200
    assert friend_b.post("/api/auth/api-keys", json={"name": "After Revoke"}).status_code == 201

    # Oversized JSON-RPC batch (>20 items) is rejected to prevent DoS amplification
    oversized = [{"jsonrpc": "2.0", "id": i, "method": "ping"} for i in range(21)]
    batch_resp = anon.post("/mcp", headers={"Authorization": f"Bearer {pat_a}"}, json=oversized)
    assert batch_resp.status_code == 200
    assert batch_resp.json()["error"]["code"] == -32600
    assert "exceeds maximum" in batch_resp.json()["error"]["message"]
