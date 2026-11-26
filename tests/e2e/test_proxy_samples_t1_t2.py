"""Opaque-box E2E tests (Tiers 1 & 2) for Proxy/Origin Trust, CF-Connecting-IP & `/samples`.

Covers `PROJECT.md § Feature Inventory`:
  - Feature #1: Proxy, Origin & `CF-Connecting-IP` Trust (6 Tier 1 + 7 Tier 2)
  - Feature #7 (Samples): `GET /samples/{filename}` route & cache headers (5 Tier 1 + 6 Tier 2)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from pypdf import PdfWriter

from tests.e2e.conftest import same_origin_headers

_SAME_ORIGIN_HEADERS = same_origin_headers("http://localhost:8080")


def _write_pdf(path: Path, *, marker_text: bytes = b"") -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    writer.add_blank_page(width=595.28, height=841.89)
    if marker_text:
        writer.add_metadata({"/Title": marker_text.decode("ascii", errors="ignore")})
    with path.open("wb") as fh:
        writer.write(fh)
    return path.read_bytes()


# =============================================================================
# Feature #1: Proxy, Origin, TrustedHost & CF-Connecting-IP (6 T1 + 7 T2 = 13)
# =============================================================================


class TestProxyOriginAndClientIP:
    """Tier 1 & Tier 2 tests for reverse proxy HTTPS, SameOriginWritesMiddleware & CF-Connecting-IP."""

    def test_t1_proxy_https_forwarded_proto_with_cf_trust_or_wildcard_forwarded_allow_ips(
        self, e2e_env
    ) -> None:
        proxy_variants = (
            {"GRIDFORGE_TRUST_CF_CONNECTING_IP": "true"},
            {"FORWARDED_ALLOW_IPS": "*"},
        )
        for proxy_env in proxy_variants:
            app = e2e_env.build_app(
                auth_mode="hosted",
                GRIDFORGE_ALLOWED_HOSTS="gridmaps.net,gridmaps.online",
                **proxy_env,
            )
            client = e2e_env.new_client(app, base_url="http://gridmaps.net")

            login = client.post(
                "/api/auth/login",
                json={"email": e2e_env.admin_email, "password": e2e_env.admin_password},
                headers={
                    "Origin": "https://gridmaps.net",
                    "Sec-Fetch-Site": "same-origin",
                    "X-Forwarded-Proto": "https",
                },
            )
            assert login.status_code == 200, f"Login failed under {proxy_env}: {login.text}"
            set_cookie = login.headers.get("set-cookie", "").lower()
            assert "gf_session=" in set_cookie
            assert "; secure" in set_cookie

            job = client.post(
                "/api/jobs",
                json={"center": "-33.732, 150.312"},
                headers={
                    "Origin": "https://gridmaps.net",
                    "Sec-Fetch-Site": "same-origin",
                    "X-Forwarded-Proto": "https",
                },
            )
            assert job.status_code == 202, job.text
            assert "qgis" not in sys.modules

    def test_t1_proxy_default_port_normalization_443_https_and_80_http(self, e2e_env) -> None:
        app = e2e_env.build_app(
            auth_mode="hosted",
            GRIDFORGE_ALLOWED_HOSTS="gridmaps.net",
            GRIDFORGE_TRUST_CF_CONNECTING_IP="true",
            GRIDFORGE_GUEST_DAILY_RENDERS="10",
        )

        # HTTPS default port :443 stripped from Host or Origin
        c_https = e2e_env.new_client(app, base_url="http://gridmaps.net")
        r1 = c_https.post(
            "/api/jobs",
            json={"center": "-33.732, 150.312"},
            headers={
                "Host": "gridmaps.net:443",
                "Origin": "https://gridmaps.net",
                "X-Forwarded-Proto": "https",
            },
        )
        assert r1.status_code == 202, r1.text

        r2 = c_https.post(
            "/api/jobs",
            json={"center": "-33.732, 150.312"},
            headers={
                "Host": "gridmaps.net",
                "Origin": "https://gridmaps.net:443",
                "X-Forwarded-Proto": "https",
            },
        )
        assert r2.status_code == 202, r2.text

        # HTTP default port :80 stripped, while explicit matching :8080 also passes
        c_http = e2e_env.new_client(app, base_url="http://localhost:80")
        r3 = c_http.post(
            "/api/jobs",
            json={"center": "-33.732, 150.312"},
            headers={"Host": "localhost:80", "Origin": "http://localhost"},
        )
        assert r3.status_code == 202, r3.text

        c_8080 = e2e_env.new_client(app, base_url="http://localhost:8080")
        r4 = c_8080.post(
            "/api/jobs",
            json={"center": "-33.732, 150.312"},
            headers={"Host": "localhost:8080", "Origin": "http://localhost:8080"},
        )
        assert r4.status_code == 202, r4.text

    def test_t1_proxy_sec_fetch_site_same_origin_none_and_case_insensitive_accepted(
        self, e2e_env
    ) -> None:
        app = e2e_env.build_app(auth_mode="none")
        client = e2e_env.new_client(app, base_url="http://localhost:8080")

        for hdrs in (
            {"Origin": "http://localhost:8080", "Sec-Fetch-Site": "same-origin"},
            {"Origin": "http://localhost:8080", "Sec-Fetch-Site": "Same-Origin"},
            {"Origin": "http://localhost:8080", "Sec-Fetch-Site": "none"},
            {"Origin": "http://localhost:8080"},
            {},
        ):
            r = client.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=hdrs)
            assert r.status_code == 202, f"Expected 202 for headers={hdrs}: {r.text}"

    def test_t1_proxy_safe_methods_allowed_without_origin_or_with_cross_site(
        self, e2e_env
    ) -> None:
        app = e2e_env.build_app(
            auth_mode="hosted",
            GRIDFORGE_ALLOWED_HOSTS="gridmaps.net",
        )
        client = e2e_env.new_client(app, base_url="http://gridmaps.net")

        cross_headers = {
            "Origin": "https://external.example",
            "Sec-Fetch-Site": "cross-site",
        }
        assert client.get("/api/health", headers=cross_headers).status_code == 200
        assert client.get("/api/auth/session", headers=cross_headers).status_code == 200
        # HEAD/OPTIONS are bypassed by SameOriginWritesMiddleware (router returns 405, never 403)
        assert client.head("/api/health", headers=cross_headers).status_code == 405
        assert client.options("/api/health", headers=cross_headers).status_code == 405

    def test_t1_cf_connecting_ip_partitions_guest_quotas_ipv4_and_ipv6(
        self, e2e_env
    ) -> None:
        app = e2e_env.build_app(
            auth_mode="hosted",
            GRIDFORGE_TRUST_CF_CONNECTING_IP="true",
            GRIDFORGE_GUEST_DAILY_RENDERS="1",
            GRIDFORGE_ALLOWED_HOSTS="gridmaps.net",
        )
        admin = e2e_env.login_admin(app)

        g_v4 = e2e_env.make_guest_client(app, base_url="http://gridmaps.net")
        g_v6 = e2e_env.make_guest_client(app, base_url="http://gridmaps.net")

        r_v4 = g_v4.post(
            "/api/jobs",
            json={"center": "-33.732, 150.312"},
            headers={
                "Origin": "https://gridmaps.net",
                "X-Forwarded-Proto": "https",
                "CF-Connecting-IP": "198.51.100.10",
            },
        )
        assert r_v4.status_code == 202, r_v4.text

        # Non-canonical IPv6 is normalized via ipaddress to 2001:db8::1
        r_v6 = g_v6.post(
            "/api/jobs",
            json={"center": "-33.732, 150.312"},
            headers={
                "Origin": "https://gridmaps.net",
                "X-Forwarded-Proto": "https",
                "CF-Connecting-IP": "2001:0db8:0000:0000::1",
            },
        )
        assert r_v6.status_code == 202, r_v6.text

        usage = admin.get("/api/admin/usage").json()
        recorded_ips = {ev["client_ip"] for ev in usage["recent_events"]}
        assert "198.51.100.10" in recorded_ips
        assert "2001:db8::1" in recorded_ips

    def test_t1_trusted_hosts_accepts_multiple_csv_hosts_and_google_redirect_uri_uses_https(
        self, e2e_env
    ) -> None:
        app = e2e_env.build_app(
            auth_mode="hosted",
            GRIDFORGE_ALLOWED_HOSTS="gridmaps.net, gridmaps.online",
            GRIDFORGE_TRUST_CF_CONNECTING_IP="true",
            GRIDFORGE_GOOGLE_CLIENT_ID="test-google-client-id",
            GRIDFORGE_GOOGLE_CLIENT_SECRET="test-google-secret",
        )
        for host in ("gridmaps.net", "gridmaps.online"):
            cli = e2e_env.new_client(app, base_url=f"http://{host}")
            assert cli.get("/api/health").status_code == 200

        net_cli = e2e_env.new_client(app, base_url="http://gridmaps.net")
        oauth = net_cli.get(
            "/api/auth/google/start",
            headers={"X-Forwarded-Proto": "https"},
            follow_redirects=False,
        )
        assert oauth.status_code == 302
        qs = parse_qs(urlparse(oauth.headers["location"]).query)
        assert qs["redirect_uri"] == ["https://gridmaps.net/api/auth/google/callback"]

    def test_t2_trusted_host_rejects_unlisted_host_400_before_origin_check(
        self, e2e_env
    ) -> None:
        app = e2e_env.build_app(
            auth_mode="hosted",
            GRIDFORGE_ALLOWED_HOSTS="gridmaps.net",
        )
        client = e2e_env.new_client(app, base_url="http://evil.example")

        get_resp = client.get("/api/health")
        assert get_resp.status_code == 400

        post_resp = client.post(
            "/api/jobs",
            json={"center": "-33.732, 150.312"},
            headers={"Origin": "http://evil.example", "Sec-Fetch-Site": "same-origin"},
        )
        assert post_resp.status_code == 400

    def test_t2_untrusted_x_forwarded_proto_rejected_403(self, e2e_env) -> None:
        for proxy_env in (
            {"GRIDFORGE_TRUST_CF_CONNECTING_IP": "false"},
            {"GRIDFORGE_TRUST_CF_CONNECTING_IP": "false", "FORWARDED_ALLOW_IPS": "127.0.0.1"},
        ):
            app = e2e_env.build_app(
                auth_mode="hosted",
                GRIDFORGE_ALLOWED_HOSTS="gridmaps.net",
                **proxy_env,
            )
            client = e2e_env.new_client(app, base_url="http://gridmaps.net")
            resp = client.post(
                "/api/jobs",
                json={"center": "-33.732, 150.312"},
                headers={
                    "Origin": "https://gridmaps.net",
                    "Sec-Fetch-Site": "same-origin",
                    "X-Forwarded-Proto": "https",
                },
            )
            assert resp.status_code == 403
            assert "cross-origin" in resp.text.lower()

    def test_t2_proxy_origin_scheme_host_or_port_mismatch_returns_403(
        self, e2e_env
    ) -> None:
        app = e2e_env.build_app(
            auth_mode="hosted",
            GRIDFORGE_ALLOWED_HOSTS="gridmaps.net,gridmaps.online",
            GRIDFORGE_TRUST_CF_CONNECTING_IP="true",
        )
        proxy_cli = e2e_env.new_client(app, base_url="http://gridmaps.net")

        # Scheme mismatch: request is https via proxy, Origin is http
        r_scheme = proxy_cli.post(
            "/api/jobs",
            json={"center": "-33.732, 150.312"},
            headers={"Origin": "http://gridmaps.net", "X-Forwarded-Proto": "https"},
        )
        assert r_scheme.status_code == 403

        # Host mismatch: Origin host gridmaps.online != Host gridmaps.net
        r_host = proxy_cli.post(
            "/api/jobs",
            json={"center": "-33.732, 150.312"},
            headers={"Origin": "https://gridmaps.online", "X-Forwarded-Proto": "https"},
        )
        assert r_host.status_code == 403

        # Port mismatch: Origin port 5173 != Host port 8080
        local_cli = e2e_env.new_client(app, base_url="http://localhost:8080")
        r_port = local_cli.post(
            "/api/jobs",
            json={"center": "-33.732, 150.312"},
            headers={"Origin": "http://localhost:5173"},
        )
        assert r_port.status_code == 403

    def test_t2_proxy_sec_fetch_site_cross_site_and_same_site_rejected_403(
        self, e2e_env
    ) -> None:
        app = e2e_env.build_app(auth_mode="hosted")
        client = e2e_env.make_guest_client(app)

        for sfs in ("cross-site", "Cross-Site", "same-site", "SAME-SITE"):
            hdrs = {"Origin": "http://localhost:8080", "Sec-Fetch-Site": sfs}
            assert (
                client.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=hdrs).status_code
                == 403
            )
            assert (
                client.post("/api/projects", json={"name": "X", "state": {}}, headers=hdrs).status_code
                == 403
            )
            assert (
                client.post(
                    "/api/auth/login",
                    json={"email": e2e_env.admin_email, "password": e2e_env.admin_password},
                    headers=hdrs,
                ).status_code
                == 403
            )
            assert client.delete("/api/jobs/nonexistent123", headers=hdrs).status_code == 403

    def test_t2_cf_connecting_ip_ignored_when_trust_disabled_allows_no_quota_bypass(
        self, e2e_env
    ) -> None:
        app = e2e_env.build_app(
            auth_mode="hosted",
            GRIDFORGE_TRUST_CF_CONNECTING_IP="false",
            GRIDFORGE_GUEST_DAILY_RENDERS="1",
        )
        g1 = e2e_env.make_guest_client(app)
        g2 = e2e_env.make_guest_client(app)

        assert (
            g1.post(
                "/api/jobs",
                json={"center": "-33.732, 150.312"},
                headers={**_SAME_ORIGIN_HEADERS, "CF-Connecting-IP": "198.51.100.1"},
            ).status_code
            == 202
        )

        # Spoofing a different CF-Connecting-IP does not bypass quota when trust is disabled
        r2 = g2.post(
            "/api/jobs",
            json={"center": "-33.732, 150.312"},
            headers={**_SAME_ORIGIN_HEADERS, "CF-Connecting-IP": "198.51.100.2"},
        )
        assert r2.status_code == 429

    def test_t2_cf_connecting_ip_malformed_falls_back_cleanly_to_client_host(
        self, e2e_env
    ) -> None:
        app = e2e_env.build_app(
            auth_mode="hosted",
            GRIDFORGE_TRUST_CF_CONNECTING_IP="true",
            GRIDFORGE_GUEST_DAILY_RENDERS="1",
        )
        g1 = e2e_env.make_guest_client(app)
        g2 = e2e_env.make_guest_client(app)

        r1 = g1.post(
            "/api/jobs",
            json={"center": "-33.732, 150.312"},
            headers={**_SAME_ORIGIN_HEADERS, "CF-Connecting-IP": "not-an-ip, 1.2.3.4"},
        )
        assert r1.status_code == 202

        # Second client with different malformed header also falls back to shared request.client.host -> 429
        r2 = g2.post(
            "/api/jobs",
            json={"center": "-33.732, 150.312"},
            headers={**_SAME_ORIGIN_HEADERS, "CF-Connecting-IP": "bogus;drop-table"},
        )
        assert r2.status_code == 429

    def test_t2_proxy_first_x_forwarded_proto_token_used_for_csv_header(
        self, e2e_env
    ) -> None:
        app = e2e_env.build_app(
            auth_mode="hosted",
            GRIDFORGE_ALLOWED_HOSTS="gridmaps.net",
            FORWARDED_ALLOW_IPS="*",
        )
        client = e2e_env.new_client(app, base_url="http://gridmaps.net")

        ok = client.post(
            "/api/jobs",
            json={"center": "-33.732, 150.312"},
            headers={
                "Origin": "https://gridmaps.net",
                "X-Forwarded-Proto": "https, http",
            },
        )
        assert ok.status_code == 202

        bad = client.post(
            "/api/jobs",
            json={"center": "-33.732, 150.312"},
            headers={
                "Origin": "https://gridmaps.net",
                "X-Forwarded-Proto": "http, https",
            },
        )
        assert bad.status_code == 403


# =============================================================================
# Feature #7 (Samples): GET /samples/{filename} & Caching (5 T1 + 6 T2 = 11)
# =============================================================================


class TestSamplesRoute:
    """Tier 1 & Tier 2 tests for `/samples/{filename}`, caching headers, and path guards."""

    def test_t1_samples_serves_pdf_with_cache_control_and_zero_cookies(
        self, e2e_env
    ) -> None:
        pdf_bytes = _write_pdf(e2e_env.samples_dir / "katoomba-demo.pdf")

        for mode in ("none", "hosted"):
            app = e2e_env.build_app(auth_mode=mode)
            client = e2e_env.new_client(app)
            resp = client.get("/samples/katoomba-demo.pdf")
            assert resp.status_code == 200
            assert resp.content == pdf_bytes
            assert resp.headers["content-type"].startswith("application/pdf")
            assert resp.headers["cache-control"] == "public, max-age=604800"
            assert "set-cookie" not in {k.lower() for k in resp.headers.keys()}

    def test_t1_samples_serves_non_pdf_with_octet_stream_and_cache_headers(
        self, e2e_env
    ) -> None:
        blob = b"GRIDFORGE_SAMPLE_MANIFEST_V1\n"
        (e2e_env.samples_dir / "readme.txt").write_bytes(blob)

        app = e2e_env.build_app(auth_mode="hosted")
        client = e2e_env.new_client(app)
        resp = client.get("/samples/readme.txt")
        assert resp.status_code == 200
        assert resp.content == blob
        assert resp.headers["content-type"] == "application/octet-stream"
        assert resp.headers["cache-control"] == "public, max-age=604800"

    def test_t1_samples_accessible_in_hosted_mode_and_with_built_spa_dist(
        self, e2e_env
    ) -> None:
        pdf_bytes = _write_pdf(e2e_env.samples_dir / "demo.pdf")
        app = e2e_env.build_app(auth_mode="hosted", web_dist_with_index=True)
        client = e2e_env.new_client(app)

        sample_resp = client.get("/samples/demo.pdf")
        assert sample_resp.status_code == 200
        assert sample_resp.content == pdf_bytes
        assert sample_resp.content.startswith(b"%PDF-")
        assert "set-cookie" not in {k.lower() for k in sample_resp.headers.keys()}

        asset_resp = client.get("/assets/app.js")
        assert asset_resp.status_code == 200
        assert "gridforge" in asset_resp.text
        assert "set-cookie" not in {k.lower() for k in asset_resp.headers.keys()}

    def test_t1_samples_works_behind_https_proxy_host(self, e2e_env) -> None:
        pdf_bytes = _write_pdf(e2e_env.samples_dir / "alpine.pdf")
        app = e2e_env.build_app(
            auth_mode="hosted",
            GRIDFORGE_ALLOWED_HOSTS="gridmaps.net",
            GRIDFORGE_TRUST_CF_CONNECTING_IP="true",
        )
        client = e2e_env.new_client(app, base_url="http://gridmaps.net")
        resp = client.get(
            "/samples/alpine.pdf",
            headers={"X-Forwarded-Proto": "https", "CF-Connecting-IP": "198.51.100.55"},
        )
        assert resp.status_code == 200
        assert resp.content == pdf_bytes
        assert resp.headers["cache-control"] == "public, max-age=604800"

    def test_t1_samples_multiple_files_in_samples_dir_served_accurately(
        self, e2e_env
    ) -> None:
        b1 = _write_pdf(e2e_env.samples_dir / "sample-one.pdf", marker_text=b"SampleOne")
        b2 = _write_pdf(e2e_env.samples_dir / "sample-two.pdf", marker_text=b"SampleTwo")
        assert b1 != b2

        app = e2e_env.build_app(auth_mode="hosted")
        client = e2e_env.new_client(app)
        r1 = client.get("/samples/sample-one.pdf")
        r2 = client.get("/samples/sample-two.pdf")
        assert r1.status_code == 200 and r1.content == b1
        assert r2.status_code == 200 and r2.content == b2

    def test_t2_samples_missing_file_returns_404_even_with_spa_dist(
        self, e2e_env
    ) -> None:
        app = e2e_env.build_app(auth_mode="hosted", web_dist_with_index=True)
        client = e2e_env.new_client(app)

        for path in ("/samples/does-not-exist.pdf", "/samples", "/samples/sub/file.pdf"):
            resp = client.get(path)
            assert resp.status_code == 404, f"Expected 404 for {path}, got {resp.status_code}"

    def test_t2_samples_missing_samples_dir_returns_404(self, e2e_env) -> None:
        missing_dir = e2e_env.root / "nonexistent-samples-dir"
        app = e2e_env.build_app(
            auth_mode="hosted",
            GRIDFORGE_SAMPLES_DIR=str(missing_dir),
        )
        client = e2e_env.new_client(app)
        resp = client.get("/samples/demo.pdf")
        assert resp.status_code == 404

    def test_t2_samples_path_traversal_probes_blocked_400_or_404(
        self, e2e_env
    ) -> None:
        secret = e2e_env.root / "secret.pdf"
        _write_pdf(secret, marker_text=b"TOPSECRET")
        subdir = e2e_env.samples_dir / "subdir"
        _write_pdf(subdir / "nested.pdf", marker_text=b"NESTED")

        app = e2e_env.build_app(auth_mode="hosted")
        client = e2e_env.new_client(app)

        for probe in (
            "/samples/..%2Fsecret.pdf",
            "/samples/..%5Csecret.pdf",
            "/samples/%2e%2e%2fsecret.pdf",
            "/samples/subdir%2Fnested.pdf",
            "/samples/%2e%2e",
        ):
            resp = client.get(probe)
            assert resp.status_code in (400, 404), f"Probe {probe} leaked: {resp.status_code}"
            assert b"TOPSECRET" not in resp.content
            assert b"NESTED" not in resp.content
            assert "service" not in resp.text

    def test_t2_samples_null_byte_in_filename_rejected_400(self, e2e_env) -> None:
        _write_pdf(e2e_env.samples_dir / "demo.pdf")
        app = e2e_env.build_app(auth_mode="hosted")
        client = e2e_env.new_client(app)

        resp = client.get("/samples/demo.pdf%00.txt")
        assert resp.status_code == 400
        assert "invalid sample filename" in resp.json()["detail"].lower()

    def test_t2_samples_symlink_escaping_samples_dir_rejected_404(
        self, e2e_env
    ) -> None:
        outside = e2e_env.root / "outside-secret.pdf"
        _write_pdf(outside, marker_text=b"OUTSIDE_SECRET")
        link_path = e2e_env.samples_dir / "escape.pdf"
        os.symlink(outside, link_path)

        app = e2e_env.build_app(auth_mode="hosted")
        client = e2e_env.new_client(app)
        resp = client.get("/samples/escape.pdf")
        assert resp.status_code == 404
        assert b"OUTSIDE_SECRET" not in resp.content

    def test_t2_samples_directory_or_write_method_rejected(self, e2e_env) -> None:
        _write_pdf(e2e_env.samples_dir / "demo.pdf")
        (e2e_env.samples_dir / "folder.pdf").mkdir(parents=True, exist_ok=True)

        app = e2e_env.build_app(auth_mode="hosted")
        client = e2e_env.new_client(app)

        assert client.get("/samples/folder.pdf").status_code == 404
        assert client.post("/samples/demo.pdf", headers=_SAME_ORIGIN_HEADERS).status_code == 405
        assert client.delete("/samples/demo.pdf", headers=_SAME_ORIGIN_HEADERS).status_code == 405
