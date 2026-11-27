"""Security hardening and performance regression tests (Waves 1–4)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from gridforge.config import BBox, SheetSpec, TacticalGraphic
from gridforge.geo.crs import compute_sheet_layout
from gridforge.geo.declination import compute_declination, compute_point_declination
from gridforge.geo.strip_atlas import parse_route_bytes, plan_strip_atlas
from gridforge.packs.builder import resolve_pack_asset_path
from gridforge.packs.regions import resolve_region
from gridforge.render.layout import _escape_qgis_label_text
from gridforge.sources.dem_ca_hrdem import extract_stac_cog_hrefs
from gridforge.sources.dem_usgs3dep import select_1m_tile_urls
from gridforge.sources.overlay_user import UserOverlaySource
from gridforge.sources.overlays_au_states import _redact_url_secrets
from gridforge.subprocess_env import build_safe_subprocess_env
from gridforge.web import db, entrypoint
from gridforge.web.app import create_app
from gridforge.web.auth import AuthConfig

_SAME_ORIGIN = {"Origin": "http://localhost", "Sec-Fetch-Site": "same-origin"}
_HOSTED_SECRET = "0123456789abcdef0123456789abcdef"


class TestSubprocessAndInjectionHardening:
    def test_build_safe_subprocess_env_strips_web_secrets(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("GRIDFORGE_SESSION_SECRET", "super-secret-session-value")
        monkeypatch.setenv("GRIDFORGE_ADMIN_PASSWORD", "super-secret-admin-pass")
        monkeypatch.setenv("GRIDFORGE_GOOGLE_CLIENT_SECRET", "super-secret-google")
        monkeypatch.setenv("GRIDFORGE_TURNSTILE_SECRET_KEY", "super-secret-turnstile")
        monkeypatch.setenv("GRIDFORGE_ORIGIN_PROXY_SECRET", "super-secret-origin")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "super-secret-aws")
        monkeypatch.setenv("GDAL_CACHEMAX", "64")

        runtime_dir = tmp_path / "rt"
        env = build_safe_subprocess_env(
            include_linz_key=False,
            allow_network=False,
            private_runtime_dir=runtime_dir,
        )
        for key in (
            "GRIDFORGE_SESSION_SECRET",
            "GRIDFORGE_ADMIN_PASSWORD",
            "GRIDFORGE_GOOGLE_CLIENT_SECRET",
            "GRIDFORGE_TURNSTILE_SECRET_KEY",
            "GRIDFORGE_ORIGIN_PROXY_SECRET",
            "AWS_SECRET_ACCESS_KEY",
        ):
            assert key not in env
        assert env["GDAL_CACHEMAX"] == "64"
        assert env["GDAL_HTTP_TIMEOUT"] == "1"
        assert env["CPL_VSIL_CURL_ALLOWED_EXTENSIONS"] == ".nonexistent"
        assert env["XDG_RUNTIME_DIR"] == str(runtime_dir)
        assert runtime_dir.is_dir()

        net_env = build_safe_subprocess_env(allow_network=True)
        assert net_env["GDAL_HTTP_TIMEOUT"] == "30"
        assert net_env["GDAL_HTTP_CONNECTTIMEOUT"] == "10"

    def test_qgis_label_expression_escaping(self) -> None:
        raw = "Ops [% env('GRIDFORGE_SESSION_SECRET') %] Sector"
        escaped = _escape_qgis_label_text(raw)
        assert "[%" not in escaped
        assert "%]" not in escaped
        assert "Ops" in escaped and "Sector" in escaped

    def test_schema_validation_hardening(self) -> None:
        with pytest.raises(ValueError):
            SheetSpec(center_lat=-33.73, center_lon=150.31, date_iso="not-a-date")
        with pytest.raises(ValueError):
            SheetSpec(center_lat=-33.73, center_lon=150.31, date_iso="2026-01-01;rm")
        with pytest.raises(ValueError):
            SheetSpec(center_lat=-33.73, center_lon=150.31, title="A" * 121)
        with pytest.raises(ValueError):
            TacticalGraphic(kind="bearing_line", points=[(-33.7, 150.3), (-33.8, 150.4)], color="red;background:url(x)")

        good = TacticalGraphic(kind="bearing_line", points=[(-33.7, 150.3), (-33.8, 150.4)], color="#ff00aa")
        assert good.color == "#ff00aa"

    def test_overlay_user_rejects_ogr_vrt_and_gpx_xxe(self, tmp_path: Path) -> None:
        layout = compute_sheet_layout(SheetSpec(center_lat=-33.732, center_lon=150.312))

        vrt_as_geojson = tmp_path / "evil.geojson"
        vrt_as_geojson.write_text(
            '<OGRVRTDataSource><OGRVRTLayer name="x"><SrcDataSource>/etc/passwd</SrcDataSource></OGRVRTLayer></OGRVRTDataSource>',
            encoding="utf-8",
        )
        with pytest.raises(ValueError):
            UserOverlaySource(vrt_as_geojson).fetch_overlay(layout, tmp_path / "out1")

        xxe_gpx = tmp_path / "evil.gpx"
        xxe_gpx.write_text(
            '<?xml version="1.0"?><!DOCTYPE gpx [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><gpx><wpt lat="-33.7" lon="150.3"><name>&xxe;</name></wpt></gpx>',
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="DOCTYPE|ENTITY|DTD"):
            UserOverlaySource(xxe_gpx).fetch_overlay(layout, tmp_path / "out2")

    def test_strip_atlas_guards_gpx_dtd_and_zigzag_oom(self) -> None:
        with pytest.raises(ValueError, match="DOCTYPE|ENTITY"):
            parse_route_bytes(
                b'<?xml version="1.0"?><!DOCTYPE gpx><gpx><trk><trkseg></trkseg></trk></gpx>',
                "evil.gpx",
            )

        # Zigzag route spanning hundreds of km should fail before densifying millions of points
        zigzag = [(150.0 + (0.5 if i % 2 else 0.0), -33.7) for i in range(60)]
        with pytest.raises(ValueError, match="requires more than 12 sheets"):
            plan_strip_atlas(zigzag, scale=25000, max_sheets=12)


class TestOriginProxyAndWebSecurity:
    def test_origin_proxy_secret_blocks_direct_bypass_and_allows_cloudflare(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GRIDFORGE_AUTH_MODE", "hosted")
        monkeypatch.setenv("GRIDFORGE_SESSION_SECRET", _HOSTED_SECRET)
        monkeypatch.setenv("GRIDFORGE_ORIGIN_PROXY_SECRET", "origin-secret-token-abcdef012345")
        monkeypatch.setenv("GRIDFORGE_ALLOWED_HOSTS", "maps.example.com,healthcheck.railway.app")

        app = create_app(data_dir=tmp_path / "data", packs_dir=tmp_path / "packs")
        with TestClient(app, base_url="https://maps.example.com") as client:
            # Missing secret -> 403
            assert client.get("/api/auth/session").status_code == 403
            # Wrong secret -> 403
            assert client.get(
                "/api/auth/session",
                headers={"X-GridForge-Origin-Secret": "wrong-secret"},
            ).status_code == 403
            # Valid secret -> 200 with strict security headers & disabled OpenAPI docs
            ok = client.get(
                "/api/auth/session",
                headers={"X-GridForge-Origin-Secret": "origin-secret-token-abcdef012345"},
            )
            assert ok.status_code == 200
            assert ok.headers.get("x-content-type-options") == "nosniff"
            assert ok.headers.get("x-frame-options") == "DENY"
            csp = ok.headers.get("content-security-policy", "")
            assert "script-src 'self'" in csp
            assert "'unsafe-inline'" not in csp.split("script-src", 1)[1].split(";", 1)[0]
            assert client.get(
                "/docs",
                headers={"X-GridForge-Origin-Secret": "origin-secret-token-abcdef012345"},
            ).status_code == 404

        # Railway internal healthcheck remains reachable on /api/health without Cloudflare secret
        with TestClient(app, base_url="http://healthcheck.railway.app") as hc:
            assert hc.get("/api/health").status_code == 200

    def test_hosted_short_session_secret_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GRIDFORGE_AUTH_MODE", "hosted")
        monkeypatch.setenv("GRIDFORGE_SESSION_SECRET", "too-short")
        with pytest.raises(ValueError, match="at least 32"):
            AuthConfig.from_env()

    def test_railway_public_domain_not_auto_added_when_allowed_hosts_set(self, tmp_path: Path) -> None:
        env, _, _ = entrypoint.configure_environment(
            {
                "RAILWAY_ENVIRONMENT_ID": "env-1",
                "RAILWAY_PUBLIC_DOMAIN": "bypass.up.railway.app",
                "GRIDFORGE_ALLOWED_HOSTS": "maps.example.com",
            },
            volume_root=tmp_path,
            seed_samples_dir=tmp_path / "none",
        )
        hosts = env["GRIDFORGE_ALLOWED_HOSTS"].split(",")
        assert "maps.example.com" in hosts
        assert "healthcheck.railway.app" in hosts
        assert "bypass.up.railway.app" not in hosts

    def test_guest_project_size_cap_and_global_fifo_eviction(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GRIDFORGE_AUTH_MODE", "hosted")
        monkeypatch.setenv("GRIDFORGE_SESSION_SECRET", _HOSTED_SECRET)
        monkeypatch.setenv("GRIDFORGE_MAX_GUEST_PROJECTS", "3")
        monkeypatch.setenv("GRIDFORGE_MAX_PROJECTS_PER_GUEST", "5")
        monkeypatch.setenv("GRIDFORGE_GUEST_PROJECT_HOURLY_LIMIT", "20")

        app = create_app(data_dir=tmp_path / "data", packs_dir=tmp_path / "packs")
        with TestClient(app, base_url="http://localhost") as client:
            # Oversized project state (> 64 KB) rejected with 413
            big = {"blob": "x" * 70_000}
            r_big = client.post(
                "/api/projects",
                json={"name": "TooBig", "state": big},
                headers=_SAME_ORIGIN,
            )
            assert r_big.status_code == 413

            # Create 4 guest projects; global cap of 3 evicts the oldest guest project
            created_ids: list[str] = []
            for i in range(4):
                r = client.post(
                    "/api/projects",
                    json={"name": f"Guest Proj {i}", "state": {"lat": -33.7, "lon": 150.3, "i": i}},
                    headers=_SAME_ORIGIN,
                )
                assert r.status_code == 201
                created_ids.append(r.json()["project_id"])

            listed = client.get("/api/projects").json()["projects"]
            remaining_ids = {p["project_id"] for p in listed}
            assert len(remaining_ids) == 3
            assert created_ids[0] not in remaining_ids
            assert set(created_ids[1:]) == remaining_ids

    def test_google_oauth_emits_pkce_s256(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GRIDFORGE_AUTH_MODE", "hosted")
        monkeypatch.setenv("GRIDFORGE_SESSION_SECRET", _HOSTED_SECRET)
        monkeypatch.setenv("GRIDFORGE_GOOGLE_CLIENT_ID", "cid-123")
        monkeypatch.setenv("GRIDFORGE_GOOGLE_CLIENT_SECRET", "csec-456")

        app = create_app(data_dir=tmp_path / "data", packs_dir=tmp_path / "packs")
        with TestClient(app, base_url="http://localhost") as client:
            r = client.get("/api/auth/google/start", follow_redirects=False)
            assert r.status_code == 302
            qs = parse_qs(urlparse(r.headers["location"]).query)
            assert qs.get("code_challenge_method") == ["S256"]
            assert qs.get("code_challenge") and len(qs["code_challenge"][0]) >= 40


class TestSourceAndPackHardening:
    def test_dem_url_allowlists_reject_untrusted_and_http(self) -> None:
        bbox = BBox(min_x=-105.4, min_y=39.9, max_x=-105.2, max_y=40.1)
        usgs_payload = {
            "items": [
                {"downloadURL": "http://prd-tnm.s3.amazonaws.com/insecure.tif"},
                {"downloadURL": "https://evil.example/ssrf.tif"},
                {"downloadURL": "https://prd-tnm.s3.amazonaws.com/safe.tif"},
            ]
        }
        assert select_1m_tile_urls(usgs_payload, bbox) == [
            "https://prd-tnm.s3.amazonaws.com/safe.tif"
        ]

        ca_payload = {
            "features": [
                {"assets": {"dtm": {"href": "http://canelevation-dem.s3.ca-central-1.amazonaws.com/a.tif"}}},
                {"assets": {"dtm": {"href": "https://evil.example/b.tif"}}},
                {"assets": {"dtm": {"href": "https://canelevation-dem.s3.ca-central-1.amazonaws.com/ok.tif"}}},
            ]
        }
        assert extract_stac_cog_hrefs(ca_payload) == [
            "https://canelevation-dem.s3.ca-central-1.amazonaws.com/ok.tif"
        ]

    def test_pack_asset_traversal_and_custom_span_cap(self, tmp_path: Path) -> None:
        pack_dir = tmp_path / "my_pack"
        pack_dir.mkdir()
        (pack_dir / "dem.tif").write_bytes(b"ok")
        assert resolve_pack_asset_path(pack_dir, "dem.tif") == (pack_dir / "dem.tif").resolve()
        with pytest.raises(ValueError):
            resolve_pack_asset_path(pack_dir, "../../etc/passwd")
        with pytest.raises(ValueError):
            resolve_pack_asset_path(pack_dir, "/etc/passwd")

        # Custom pack bbox > 4 deg rejected
        with pytest.raises(ValueError, match="exceeds maximum"):
            resolve_region("140.0,-38.0,150.0,-30.0")
        # Outside AU/NZ/US/CA does not silently fall back to AU PBF
        europe = resolve_region("7.5,46.0,8.0,46.4")
        assert europe.pbf_url == ""

    def test_wfs_key_redaction(self) -> None:
        raw = "https://services.thelist.tas.gov.au/arcgis/services/x;key=SECRET123/WFSServer?api_key=TOK999"
        redacted = _redact_url_secrets(raw)
        assert "SECRET123" not in redacted
        assert "TOK999" not in redacted
        assert ";key=REDACTED" in redacted


class TestPerformanceOptimizations:
    def test_db_thread_local_pooling_and_usage_report_and_pruning(self, tmp_path: Path) -> None:
        db_path = tmp_path / "perf.sqlite3"
        db.init_db(db_path)

        c1 = db._connect(db_path)
        c1.close()
        c2 = db._connect(db_path)
        assert c1 is c2
        c2.close()

        # Verify usage_events pruning
        now = datetime.now(timezone.utc)
        old_ts = (now - timedelta(days=45)).strftime("%Y-%m-%dT%H:%M:%SZ")
        recent_ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        conn = db._connect(db_path)
        try:
            conn.execute(
                "INSERT INTO usage_events (event_type, actor_id, actor_role, client_ip, created_at) VALUES (?, ?, ?, ?, ?)",
                ("job_render", "guest:old", "guest", "1.2.3.4", old_ts),
            )
            conn.execute(
                "INSERT INTO usage_events (event_type, actor_id, actor_role, client_ip, created_at) VALUES (?, ?, ?, ?, ?)",
                ("job_render", "guest:new", "guest", "1.2.3.4", recent_ts),
            )
            conn.execute(
                "INSERT INTO usage_events (event_type, actor_id, actor_role, client_ip, created_at) VALUES (?, ?, ?, ?, ?)",
                ("viewshed_preview", "usr_1", "friend", "1.2.3.5", recent_ts),
            )
        finally:
            conn.close()

        report = db.get_admin_usage_report(db_path, now=now)
        assert report["summary"]["renders_today"] == 1
        assert report["summary"]["renders_7d"] == 1
        assert report["summary"]["previews_today"] == 1
        assert report["summary"]["global_daily_public_used"] == 1

        pruned = db.prune_expired_usage_events(db_path, retention_days=30.0, now=now)
        assert pruned == 1

    def test_point_declination_matches_layout_declination(self) -> None:
        spec = SheetSpec(center_lat=-33.732, center_lon=150.312, date_iso="2026-06-01")
        layout = compute_sheet_layout(spec)
        d1 = compute_declination(layout, elevation_m=950.0)
        d2 = compute_point_declination(
            spec.center_lat,
            spec.center_lon,
            utm_epsg=layout.utm_epsg,
            date_iso=spec.date_iso,
            elevation_m=950.0,
        )
        assert d1.magnetic_declination_deg == pytest.approx(d2.magnetic_declination_deg, abs=1e-6)
        assert d1.grid_convergence_deg == pytest.approx(d2.grid_convergence_deg, abs=1e-6)
        assert d1.gm_angle_deg == pytest.approx(d2.gm_angle_deg, abs=1e-6)
