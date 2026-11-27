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
