"""Opaque-box E2E tests (Tiers 1 & 2) for Turnstile, Quotas, Caps, Rate Limits & Kill Switch.

Covers `PROJECT.md § Feature Inventory`:
  - Feature #4: Turnstile Server Verification (6 Tier 1 + 6 Tier 2)
  - Feature #6: Tiered Quotas, Caps, Rate Limits & Kill Switch (8 Tier 1 + 10 Tier 2)
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.e2e.conftest import same_origin_headers

_SAME_ORIGIN = same_origin_headers("http://localhost:8080")
_TURNSTILE_SITE_KEY = "1x00000000000000000000AA"
_TURNSTILE_SECRET_KEY = "1x0000000000000000000000000000000AA"


def _valid_geojson_bytes(exact_size: int) -> bytes:
    """Build a valid GeoJSON FeatureCollection padded with spaces to ``exact_size`` bytes."""
    base = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"name": "Katoomba Track"},
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[150.310, -33.735], [150.320, -33.725]],
                },
            }
        ],
    }
    raw = json.dumps(base, separators=(",", ":")).encode("utf-8")
    if len(raw) > exact_size:
        raise ValueError(f"Base GeoJSON ({len(raw)}B) exceeds requested {exact_size}B")
    if len(raw) == exact_size:
        return raw
    # Insert trailing spaces before final '}'
    pad = b" " * (exact_size - len(raw))
    return raw[:-1] + pad + b"}"


def _viewshed_payload(*, turnstile_token: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
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
    }
    if turnstile_token is not None:
        payload["turnstile_token"] = turnstile_token
    return payload


def _rf_link_payload() -> dict[str, Any]:
    return {
        "from_lat": -33.732,
        "from_lon": 150.312,
        "to_lat": -33.702,
        "to_lon": 150.342,
        "freq_mhz": 915.0,
    }


def _strip_sheets(count: int) -> list[dict[str, Any]]:
    return [
        {
            "sheet_id": f"S{idx + 1}",
            "center_lat": -33.732 + (idx * 0.01),
            "center_lon": 150.312,
        }
        for idx in range(count)
    ]


def _build_turnstile_hosted_app(e2e_env, **extra_env: str):
    return e2e_env.build_app(
        auth_mode="hosted",
        GRIDFORGE_TURNSTILE_SITE_KEY=_TURNSTILE_SITE_KEY,
        GRIDFORGE_TURNSTILE_SECRET_KEY=_TURNSTILE_SECRET_KEY,
        **extra_env,
    )


def _count_usage_events(db_path: Path, event_type: str) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM usage_events WHERE event_type = ?", (event_type,)
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


# =============================================================================
# Feature #4: Turnstile Server Verification (6 Tier 1 + 6 Tier 2 = 12 tests)
# =============================================================================


class TestTurnstileVerification:
    """Tier 1 & Tier 2 tests for Cloudflare Turnstile verification."""

    def test_t1_turnstile_guest_jobs_json_and_header_valid_token_succeeds(
        self, e2e_env, mock_turnstile
    ) -> None:
        app = _build_turnstile_hosted_app(e2e_env)
        guest = e2e_env.make_guest_client(app)

        sess = guest.get("/api/auth/session").json()
        assert sess["capabilities"]["turnstile_required"] is True
        assert sess["capabilities"]["turnstile_sitekey"] == _TURNSTILE_SITE_KEY

        r_hdr = guest.post(
            "/api/jobs",
            json={"center": "-33.732, 150.312", "outputs": {"pdf": True}},
            headers={**_SAME_ORIGIN, "X-Turnstile-Token": "valid-turnstile-token"},
        )
        assert r_hdr.status_code == 202, r_hdr.text

        r_body = guest.post(
            "/api/jobs",
            json={
                "center": "-33.732, 150.312",
                "outputs": {"pdf": True},
                "turnstile_token": "valid-turnstile-token",
            },
            headers=_SAME_ORIGIN,
        )
        assert r_body.status_code == 202, r_body.text

        assert len(mock_turnstile.turnstile_calls) == 2
        for call in mock_turnstile.turnstile_calls:
            assert call["token"] == "valid-turnstile-token"
            assert call["secret"] == _TURNSTILE_SECRET_KEY
            assert call["remoteip"]
        assert "qgis" not in sys.modules

    def test_t1_turnstile_guest_jobs_multipart_form_field_valid_token_succeeds(
        self, e2e_env, mock_turnstile, synthetic_fixtures
    ) -> None:
        app = _build_turnstile_hosted_app(e2e_env)
        guest = e2e_env.make_guest_client(app)

        overlay_bytes = synthetic_fixtures.overlay_geojson.read_bytes()
        resp = guest.post(
            "/api/jobs",
            data={
                "payload": json.dumps({"center": "-33.732, 150.312", "outputs": {"pdf": True}}),
                "turnstile_token": "valid-turnstile-token",
            },
            files={"overlay_file": ("overlay.geojson", overlay_bytes, "application/geo+json")},
            headers=_SAME_ORIGIN,
        )
        assert resp.status_code == 202, resp.text
        assert len(mock_turnstile.turnstile_calls) == 1
        assert mock_turnstile.turnstile_calls[0]["token"] == "valid-turnstile-token"

    def test_t1_turnstile_guest_viewshed_preview_header_and_body_token_succeeds(
        self, e2e_env, mock_turnstile, mock_preview_dem
    ) -> None:
        _ = mock_preview_dem
        e2e_env.seed_synthetic_pack("blue-mountains")
        app = _build_turnstile_hosted_app(e2e_env)
        guest = e2e_env.make_guest_client(app)

        r_hdr = guest.post(
            "/api/viewshed-preview",
            json=_viewshed_payload(),
            headers={**_SAME_ORIGIN, "X-Turnstile-Token": "valid-turnstile-token"},
        )
        assert r_hdr.status_code == 200, r_hdr.text

        r_body = guest.post(
            "/api/viewshed-preview",
            json=_viewshed_payload(turnstile_token="valid-turnstile-token"),
            headers=_SAME_ORIGIN,
        )
        assert r_body.status_code == 200, r_body.text
        assert len(mock_turnstile.turnstile_calls) == 2

    def test_t1_turnstile_header_precedence_and_cf_remoteip_forwarded(
        self, e2e_env, mock_turnstile
    ) -> None:
        app = _build_turnstile_hosted_app(
            e2e_env,
            GRIDFORGE_TRUST_CF_CONNECTING_IP="true",
        )
        guest = e2e_env.make_guest_client(app)

        resp = guest.post(
            "/api/jobs",
            json={
                "center": "-33.732, 150.312",
                "outputs": {"pdf": True},
                "turnstile_token": "stale-or-invalid-body-token",
            },
            headers={
                **_SAME_ORIGIN,
                "X-Turnstile-Token": "valid-turnstile-token",
                "CF-Connecting-IP": "198.51.100.77",
            },
        )
        assert resp.status_code == 202, resp.text
        assert len(mock_turnstile.turnstile_calls) == 1
        assert mock_turnstile.turnstile_calls[0]["token"] == "valid-turnstile-token"
        assert mock_turnstile.turnstile_calls[0]["remoteip"] == "198.51.100.77"

    def test_t1_turnstile_bypassed_for_friend_admin_and_none_mode(
        self, e2e_env, mock_turnstile, mock_preview_dem
    ) -> None:
        _ = mock_preview_dem
        e2e_env.seed_synthetic_pack("blue-mountains")
        hosted_app = _build_turnstile_hosted_app(e2e_env)
        admin = e2e_env.login_admin(hosted_app)
        friend, _ = e2e_env.invite_and_accept_friend(
            hosted_app, admin, email="friend@example.com"
        )

        assert admin.get("/api/auth/session").json()["capabilities"]["turnstile_required"] is False
        assert friend.get("/api/auth/session").json()["capabilities"]["turnstile_required"] is False

        for role_client in (friend, admin):
            assert (
                role_client.post(
                    "/api/jobs",
                    json={"center": "-33.732, 150.312", "outputs": {"pdf": True}},
                    headers=_SAME_ORIGIN,
                ).status_code
                == 202
            )
            assert (
                role_client.post(
                    "/api/viewshed-preview",
                    json=_viewshed_payload(),
                    headers=_SAME_ORIGIN,
                ).status_code
                == 200
            )

        none_app = e2e_env.build_app(
            auth_mode="none",
            GRIDFORGE_TURNSTILE_SITE_KEY=_TURNSTILE_SITE_KEY,
            GRIDFORGE_TURNSTILE_SECRET_KEY=_TURNSTILE_SECRET_KEY,
        )
        none_cli = e2e_env.new_client(none_app)
        assert (
            none_cli.post(
                "/api/jobs",
                json={"center": "-33.732, 150.312", "outputs": {"pdf": True}},
                headers=_SAME_ORIGIN,
            ).status_code
            == 202
        )
        assert len(mock_turnstile.turnstile_calls) == 0

    def test_t1_turnstile_bypassed_when_unconfigured_or_partial_keys(
        self, e2e_env, mock_turnstile
    ) -> None:
        for env_pair in (
            {},
            {"GRIDFORGE_TURNSTILE_SITE_KEY": _TURNSTILE_SITE_KEY},
            {"GRIDFORGE_TURNSTILE_SECRET_KEY": _TURNSTILE_SECRET_KEY},
        ):
            app = e2e_env.build_app(
                auth_mode="hosted",
                GRIDFORGE_GUEST_DAILY_RENDERS="10",
                **env_pair,
            )
            guest = e2e_env.make_guest_client(app)
            sess = guest.get("/api/auth/session").json()
            assert sess["capabilities"]["turnstile_required"] is False
            assert sess["capabilities"]["turnstile_sitekey"] is None

            r = guest.post(
                "/api/jobs",
                json={"center": "-33.732, 150.312", "outputs": {"pdf": True}},
                headers=_SAME_ORIGIN,
            )
            assert r.status_code == 202, r.text

        assert len(mock_turnstile.turnstile_calls) == 0

    def test_t2_turnstile_guest_missing_or_blank_token_returns_403_no_http_call(
        self, e2e_env, mock_turnstile, mock_preview_dem
    ) -> None:
        _ = mock_preview_dem
        e2e_env.seed_synthetic_pack("blue-mountains")
        app = _build_turnstile_hosted_app(e2e_env)
        guest = e2e_env.make_guest_client(app)

        for hdrs in (_SAME_ORIGIN, {**_SAME_ORIGIN, "X-Turnstile-Token": "   "}):
            rj = guest.post(
                "/api/jobs",
                json={"center": "-33.732, 150.312", "outputs": {"pdf": True}},
                headers=hdrs,
            )
            assert rj.status_code == 403
            assert "turnstile" in rj.json()["detail"].lower()

            rv = guest.post(
                "/api/viewshed-preview",
                json=_viewshed_payload(turnstile_token="   "),
                headers=hdrs,
            )
            assert rv.status_code == 403

        assert len(mock_turnstile.turnstile_calls) == 0

    def test_t2_turnstile_siteverify_rejected_token_returns_403_and_does_not_consume_quota(
        self, e2e_env, mock_turnstile, mock_preview_dem
    ) -> None:
        _ = mock_preview_dem
        e2e_env.seed_synthetic_pack("blue-mountains")
        app = _build_turnstile_hosted_app(
            e2e_env,
            GRIDFORGE_GUEST_DAILY_RENDERS="1",
            GRIDFORGE_GUEST_HOURLY_PREVIEWS="1",
        )
        guest = e2e_env.make_guest_client(app)

        bad_job = guest.post(
            "/api/jobs",
            json={
                "center": "-33.732, 150.312",
                "outputs": {"pdf": True},
                "turnstile_token": "bad-token",
            },
            headers=_SAME_ORIGIN,
        )
        assert bad_job.status_code == 403
        assert "turnstile" in bad_job.json()["detail"].lower()

        bad_vs = guest.post(
            "/api/viewshed-preview",
            json=_viewshed_payload(turnstile_token="bad-token"),
            headers=_SAME_ORIGIN,
        )
        assert bad_vs.status_code == 403

        assert _count_usage_events(e2e_env.db_path, "job_render") == 0
        assert _count_usage_events(e2e_env.db_path, "viewshed_preview") == 0

        # Valid token immediately works because rejected attempts consumed zero quota
        ok_job = guest.post(
            "/api/jobs",
            json={
                "center": "-33.732, 150.312",
                "outputs": {"pdf": True},
                "turnstile_token": "valid-turnstile-token",
            },
            headers=_SAME_ORIGIN,
        )
        assert ok_job.status_code == 202

    def test_t2_turnstile_siteverify_http_500_or_network_error_fails_closed_403(
        self, e2e_env, monkeypatch
    ) -> None:
        app = _build_turnstile_hosted_app(e2e_env)
        guest = e2e_env.make_guest_client(app)

        def _post_500(*_a, **_kw):
            return httpx.Response(500, text="Internal Error")

        monkeypatch.setattr(httpx, "post", _post_500)
        r500 = guest.post(
            "/api/jobs",
            json={"center": "-33.732, 150.312", "turnstile_token": "valid-turnstile-token"},
            headers=_SAME_ORIGIN,
        )
        assert r500.status_code == 403

        def _post_bad_json(*_a, **_kw):
            return httpx.Response(200, content=b"not-json-at-all")

        monkeypatch.setattr(httpx, "post", _post_bad_json)
        r_bad_json = guest.post(
            "/api/jobs",
            json={"center": "-33.732, 150.312", "turnstile_token": "valid-turnstile-token"},
            headers=_SAME_ORIGIN,
        )
        assert r_bad_json.status_code == 403

        def _post_timeout(*_a, **_kw):
            raise httpx.TimeoutException("Timed out connecting to siteverify")

        monkeypatch.setattr(httpx, "post", _post_timeout)
        r_timeout = guest.post(
            "/api/jobs",
            json={"center": "-33.732, 150.312", "turnstile_token": "valid-turnstile-token"},
            headers=_SAME_ORIGIN,
        )
        assert r_timeout.status_code == 403

    def test_t2_turnstile_checked_before_job_capability_or_quota_errors(
        self, e2e_env, mock_turnstile
    ) -> None:
        app = _build_turnstile_hosted_app(e2e_env, GRIDFORGE_GUEST_DAILY_RENDERS="0")
        guest = e2e_env.make_guest_client(app)

        # Guest asks for 2x1 (disallowed) and daily limit is 0, but missing Turnstile fails first
        resp = guest.post(
            "/api/jobs",
            json={"center": "-33.732, 150.312", "grid_cols": 2, "grid_rows": 1},
            headers=_SAME_ORIGIN,
        )
        assert resp.status_code == 403
        assert "turnstile" in resp.json()["detail"].lower()
        assert len(mock_turnstile.turnstile_calls) == 0

    def test_t2_turnstile_forged_cookie_still_enforces_turnstile_as_guest(
        self, e2e_env, mock_turnstile
    ) -> None:
        app = _build_turnstile_hosted_app(e2e_env)
        client = e2e_env.new_client(app)
        client.cookies.set("gf_session", "forged.tampered.session.token")

        resp = client.post(
            "/api/jobs",
            json={"center": "-33.732, 150.312", "outputs": {"pdf": True}},
            headers=_SAME_ORIGIN,
        )
        assert resp.status_code == 403
        assert "turnstile" in resp.json()["detail"].lower()
        assert len(mock_turnstile.turnstile_calls) == 0

    def test_t2_turnstile_not_required_on_geocode_or_geodesic_ruler_but_required_on_rf_link(
        self, e2e_env, mock_turnstile, mock_preview_dem, mock_nominatim
    ) -> None:
        _ = (mock_preview_dem, mock_nominatim)
        e2e_env.seed_synthetic_pack("blue-mountains")
        app = _build_turnstile_hosted_app(e2e_env)
        guest = e2e_env.make_guest_client(app)

        # Geocode & pure geodesic ruler (no dem_source/pack_id) remain unlimited & Turnstile-free
        geo = guest.get("/api/geocode", params={"q": "Katoomba"})
        assert geo.status_code == 200, geo.text
        assert len(geo.json()["results"]) >= 1

        geodesic_ruler = guest.post(
            "/api/ruler-metrics",
            json={"points": [[-33.732, 150.312], [-33.712, 150.322]]},
            headers=_SAME_ORIGIN,
        )
        assert geodesic_ruler.status_code == 200, geodesic_ruler.text
        assert len(mock_turnstile.turnstile_calls) == 0

        # RF link profile requires Turnstile for guests when configured
        rf_denied = guest.post("/api/rf-link-profile", json=_rf_link_payload(), headers=_SAME_ORIGIN)
        assert rf_denied.status_code == 403
        rf_ok = guest.post(
            "/api/rf-link-profile",
            json=_rf_link_payload(),
            headers={**_SAME_ORIGIN, "X-Turnstile-Token": "valid-turnstile-token"},
        )
        assert rf_ok.status_code == 200, rf_ok.text
        assert len(mock_turnstile.turnstile_calls) == 1


# =============================================================================
# Feature #6: Tiered Quotas, Caps, Rate Limits & Kill Switch (8 T1 + 10 T2)
# =============================================================================


class TestQuotasCapsRateLimitsAndKillSwitch:
    """Tier 1 & Tier 2 tests for role capabilities, quotas, rate limits, queue & kill switch."""

    def test_t1_role_caps_guest_1x1_pdf_and_friend_3x3_strip_field_bundle(
        self, e2e_env
    ) -> None:
        app = e2e_env.build_app(auth_mode="hosted")
        admin = e2e_env.login_admin(app)
        friend, _ = e2e_env.invite_and_accept_friend(
            app, admin, email="friend@example.com"
        )
        guest = e2e_env.make_guest_client(app)

        # Guest allowed 1x1 PDF
        g_resp = guest.post(
            "/api/jobs",
            json={
                "center": "-33.732, 150.312",
                "grid_cols": 1,
                "grid_rows": 1,
                "outputs": {"pdf": True},
            },
            headers=_SAME_ORIGIN,
        )
        assert g_resp.status_code == 202, g_resp.text

        # Friend allowed 3x3 atlas + all 7 outputs
        f_atlas = friend.post(
            "/api/jobs",
            json={
                "center": "-33.732, 150.312",
                "grid_cols": 3,
                "grid_rows": 3,
                "outputs": {
                    "pdf": True,
                    "geopdf": True,
                    "mbtiles": True,
                    "kmz": True,
                    "gpkg_tiles": True,
                    "osmand_sqlitedb": True,
                    "field_bundle_zip": True,
                },
            },
            headers=_SAME_ORIGIN,
        )
        assert f_atlas.status_code == 202, f_atlas.text

        # Friend allowed 12-sheet strip atlas
        f_strip = friend.post(
            "/api/jobs",
            json={
                "center": "-33.732, 150.312",
                "strip_sheets": _strip_sheets(12),
                "outputs": {"pdf": True},
            },
            headers=_SAME_ORIGIN,
        )
        assert f_strip.status_code == 202, f_strip.text

    def test_t1_env_configurable_guest_and_friend_caps_override_defaults(
        self, e2e_env
    ) -> None:
        app = e2e_env.build_app(
            auth_mode="hosted",
            GRIDFORGE_GUEST_MAX_GRID_CELLS="4",
            GRIDFORGE_GUEST_STRIP_ALLOWED="true",
            GRIDFORGE_GUEST_MAX_STRIP_SHEETS="3",
            GRIDFORGE_GUEST_ALLOWED_OUTPUTS="pdf,kmz",
        )
        guest = e2e_env.make_guest_client(app)
        sess = guest.get("/api/auth/session").json()
        caps = sess["capabilities"]
        assert caps["max_grid_cells"] == 4
        assert caps["strip_atlas_allowed"] is True
        assert caps["max_strip_sheets"] == 3
        assert caps["allowed_outputs"] == ["pdf", "kmz"]

        r_atlas = guest.post(
            "/api/jobs",
            json={
                "center": "-33.732, 150.312",
                "grid_cols": 2,
                "grid_rows": 2,
                "outputs": {"pdf": True, "kmz": True},
            },
            headers=_SAME_ORIGIN,
        )
        assert r_atlas.status_code == 202, r_atlas.text

        r_strip = guest.post(
            "/api/jobs",
            json={
                "center": "-33.732, 150.312",
                "strip_sheets": _strip_sheets(3),
                "outputs": {"pdf": True},
            },
            headers=_SAME_ORIGIN,
        )
        assert r_strip.status_code == 202, r_strip.text

    def test_t1_upload_bytes_exact_boundary_per_role(self, e2e_env) -> None:
        app = e2e_env.build_app(
            auth_mode="hosted",
            GRIDFORGE_MAX_UPLOAD_BYTES_GUEST="1024",
            GRIDFORGE_MAX_UPLOAD_BYTES_FRIEND="2048",
            GRIDFORGE_MAX_UPLOAD_BYTES_ADMIN="4096",
        )
        admin = e2e_env.login_admin(app)
        friend, _ = e2e_env.invite_and_accept_friend(
            app, admin, email="friend@example.com"
        )
        guest = e2e_env.make_guest_client(app)

        for role_client, exact_limit in ((guest, 1024), (friend, 2048), (admin, 4096)):
            assert (
                role_client.get("/api/auth/session").json()["capabilities"]["max_upload_bytes"]
                == exact_limit
            )
            payload_bytes = _valid_geojson_bytes(exact_limit)
            resp = role_client.post(
                "/api/jobs",
                data={"payload": json.dumps({"center": "-33.732, 150.312"})},
                files={"overlay_file": ("track.geojson", payload_bytes, "application/geo+json")},
                headers=_SAME_ORIGIN,
            )
            assert resp.status_code == 202, f"Expected 202 at {exact_limit}B: {resp.text}"

    def test_t1_daily_render_and_hourly_preview_quotas_up_to_exact_limit(
        self, e2e_env, mock_preview_dem
    ) -> None:
        _ = mock_preview_dem
        e2e_env.seed_synthetic_pack("blue-mountains")
        app = e2e_env.build_app(
            auth_mode="hosted",
            GRIDFORGE_GUEST_DAILY_RENDERS="2",
            GRIDFORGE_GUEST_HOURLY_PREVIEWS="2",
            GRIDFORGE_RF_LINK_HOURLY_LIMIT="2",
        )
        guest = e2e_env.make_guest_client(app)

        for idx in range(2):
            rj = guest.post(
                "/api/jobs",
                json={"center": "-33.732, 150.312"},
                headers=_SAME_ORIGIN,
            )
            assert rj.status_code == 202, f"Job {idx + 1} failed: {rj.text}"

            rv = guest.post(
                "/api/viewshed-preview",
                json=_viewshed_payload(),
                headers=_SAME_ORIGIN,
            )
            assert rv.status_code == 200, f"Viewshed {idx + 1} failed: {rv.text}"

            rr = guest.post(
                "/api/rf-link-profile",
                json=_rf_link_payload(),
                headers=_SAME_ORIGIN,
            )
            assert rr.status_code == 200, f"RF link {idx + 1} failed: {rr.text}"

        quotas = guest.get("/api/auth/session").json()["quotas"]
        assert quotas["daily_renders_limit"] == 2
        assert quotas["daily_renders_used"] == 2
        assert quotas["daily_renders_remaining"] == 0
        assert quotas["hourly_previews_limit"] == 2
        assert quotas["hourly_previews_used"] == 2
        assert quotas["hourly_previews_remaining"] == 0

    def test_t1_rolling_window_and_utc_day_reset_restores_quota(
        self, e2e_env, mock_preview_dem
    ) -> None:
        _ = mock_preview_dem
        e2e_env.seed_synthetic_pack("blue-mountains")
        app = e2e_env.build_app(
            auth_mode="hosted",
            GRIDFORGE_GUEST_DAILY_RENDERS="1",
            GRIDFORGE_GUEST_HOURLY_PREVIEWS="1",
        )
        guest = e2e_env.make_guest_client(app)

        assert (
            guest.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=_SAME_ORIGIN).status_code
            == 202
        )
        assert (
            guest.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=_SAME_ORIGIN).status_code
            == 429
        )
        assert (
            guest.post("/api/viewshed-preview", json=_viewshed_payload(), headers=_SAME_ORIGIN).status_code
            == 200
        )
        assert (
            guest.post("/api/viewshed-preview", json=_viewshed_payload(), headers=_SAME_ORIGIN).status_code
            == 429
        )

        # Age usage_events beyond rolling 1h window (1.1h) -> viewshed restored, daily render still 429
        e2e_env.age_sqlite_timestamps(table="usage_events", age_hours=1.1, columns=("created_at",))
        assert (
            guest.post("/api/viewshed-preview", json=_viewshed_payload(), headers=_SAME_ORIGIN).status_code
            == 200
        )

        # Age usage_events beyond UTC day boundary (25h) -> daily render restored
        e2e_env.age_sqlite_timestamps(table="usage_events", age_hours=25.0, columns=("created_at",))
        assert (
            guest.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=_SAME_ORIGIN).status_code
            == 202
        )

    def test_t1_friend_per_user_quota_override_and_admin_unlimited(
        self, e2e_env, mock_preview_dem
    ) -> None:
        _ = mock_preview_dem
        e2e_env.seed_synthetic_pack("blue-mountains")
        app = e2e_env.build_app(
            auth_mode="hosted",
            GRIDFORGE_FRIEND_DAILY_RENDERS="1",
            GRIDFORGE_FRIEND_HOURLY_PREVIEWS="1",
        )
        admin = e2e_env.login_admin(app)
        friend, friend_sess = e2e_env.invite_and_accept_friend(
            app, admin, email="friend@example.com"
        )
        uid = friend_sess["user"]["user_id"]

        # Exhaust default friend cap of 1
        assert (
            friend.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=_SAME_ORIGIN).status_code
            == 202
        )
        assert (
            friend.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=_SAME_ORIGIN).status_code
            == 429
        )

        # Admin raises override to 3 renders/day and 3 previews/hr
        patch = admin.patch(
            f"/api/admin/users/{uid}",
            json={"daily_render_limit": 3, "hourly_preview_limit": 3},
            headers=_SAME_ORIGIN,
        )
        assert patch.status_code == 200
        assert patch.json()["user"]["daily_render_limit"] == 3

        assert (
            friend.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=_SAME_ORIGIN).status_code
            == 202
        )
        assert (
            friend.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=_SAME_ORIGIN).status_code
            == 202
        )
        assert (
            friend.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=_SAME_ORIGIN).status_code
            == 429
        )

        # Admin has unlimited renders and previews
        admin_quotas = admin.get("/api/auth/session").json()["quotas"]
        assert admin_quotas["daily_renders_limit"] is None
        assert admin_quotas["hourly_previews_limit"] is None
        for _ in range(4):
            assert (
                admin.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=_SAME_ORIGIN).status_code
                == 202
            )
            assert (
                admin.post("/api/viewshed-preview", json=_viewshed_payload(), headers=_SAME_ORIGIN).status_code
                == 200
            )

    def test_t1_queue_depth_recovery_after_job_completion_or_cancel(self, e2e_env) -> None:
        app = e2e_env.build_app(
            auth_mode="hosted",
            GRIDFORGE_MAX_QUEUE_DEPTH="2",
            GRIDFORGE_GUEST_DAILY_RENDERS="10",
        )
        admin = e2e_env.login_admin(app)
        guest = e2e_env.make_guest_client(app)

        j1 = guest.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=_SAME_ORIGIN)
        j2 = guest.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=_SAME_ORIGIN)
        assert j1.status_code == 202
        assert j2.status_code == 202

        blocked = guest.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=_SAME_ORIGIN)
        assert blocked.status_code == 503

        # Cancel j1 -> queue depth drops to 1 -> guest can submit again
        assert (
            guest.post(f"/api/jobs/{j1.json()['job_id']}/cancel", headers=_SAME_ORIGIN).status_code
            == 200
        )
        j3 = guest.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=_SAME_ORIGIN)
        assert j3.status_code == 202

        # Complete j2 -> queue depth drops to 1 -> admin can submit
        e2e_env.seed_completed_job_pdf(j2.json()["job_id"])
        j4 = admin.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=_SAME_ORIGIN)
        assert j4.status_code == 202

    def test_t1_kill_switch_pause_and_resume_and_global_budget_isolation(
        self, e2e_env
    ) -> None:
        app = e2e_env.build_app(
            auth_mode="hosted",
            GRIDFORGE_GLOBAL_DAILY_PUBLIC_RENDERS="1",
            GRIDFORGE_GUEST_DAILY_RENDERS="5",
        )
        admin = e2e_env.login_admin(app)
        friend, _ = e2e_env.invite_and_accept_friend(
            app, admin, email="friend@example.com"
        )
        guest = e2e_env.make_guest_client(app)

        # Pause public renders
        paused = admin.post(
            "/api/admin/settings",
            json={"public_renders_paused": True},
            headers=_SAME_ORIGIN,
        )
        assert paused.status_code == 200
        assert paused.json()["public_renders_paused"] is True
        assert guest.get("/api/auth/session").json()["quotas"]["public_renders_paused"] is True

        assert (
            guest.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=_SAME_ORIGIN).status_code
            == 503
        )
        # Friend and admin bypass kill switch AND do not consume global_daily_public_used
        assert (
            friend.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=_SAME_ORIGIN).status_code
            == 202
        )
        assert (
            admin.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=_SAME_ORIGIN).status_code
            == 202
        )
        assert guest.get("/api/auth/session").json()["quotas"]["global_daily_public_used"] == 0

        # Resume public renders -> guest succeeds for its 1 global budget slot
        resumed = admin.post(
            "/api/admin/settings",
            json={"public_renders_paused": False},
            headers=_SAME_ORIGIN,
        )
        assert resumed.status_code == 200
        assert resumed.json()["public_renders_paused"] is False
        assert (
            guest.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=_SAME_ORIGIN).status_code
            == 202
        )

    def test_t2_guest_exceeding_grid_strip_or_outputs_returns_403(
        self, e2e_env, synthetic_fixtures
    ) -> None:
        app = e2e_env.build_app(auth_mode="hosted")
        guest = e2e_env.make_guest_client(app)

        # 2x1 exceeds guest max_grid_cells=1
        assert (
            guest.post(
                "/api/jobs",
                json={"center": "-33.732, 150.312", "grid_cols": 2, "grid_rows": 1},
                headers=_SAME_ORIGIN,
            ).status_code
            == 403
        )
        # 4x1 exceeds schema max (3) -> 422
        assert (
            guest.post(
                "/api/jobs",
                json={"center": "-33.732, 150.312", "grid_cols": 4, "grid_rows": 1},
                headers=_SAME_ORIGIN,
            ).status_code
            == 422
        )

        # Strip atlas forbidden on both /api/jobs and /api/strip-plan
        assert (
            guest.post(
                "/api/jobs",
                json={"center": "-33.732, 150.312", "strip_sheets": _strip_sheets(2)},
                headers=_SAME_ORIGIN,
            ).status_code
            == 403
        )
        route_json = json.loads(synthetic_fixtures.katoomba_route.read_text(encoding="utf-8"))
        assert (
            guest.post(
                "/api/strip-plan",
                json={"route_geojson": route_json, "max_sheets": 4},
                headers=_SAME_ORIGIN,
            ).status_code
            == 403
        )

        # Every non-PDF output is forbidden for default guest
        for out_key in (
            "geopdf",
            "mbtiles",
            "kmz",
            "gpkg_tiles",
            "osmand_sqlitedb",
            "field_bundle_zip",
        ):
            r = guest.post(
                "/api/jobs",
                json={
                    "center": "-33.732, 150.312",
                    "outputs": {"pdf": True, out_key: True},
                },
                headers=_SAME_ORIGIN,
            )
            assert r.status_code == 403, f"Expected 403 for guest output {out_key}: {r.text}"

    def test_t2_friend_exceeding_max_strip_sheets_or_restricted_outputs_returns_403(
        self, e2e_env, synthetic_fixtures
    ) -> None:
        app = e2e_env.build_app(
            auth_mode="hosted",
            GRIDFORGE_FRIEND_MAX_STRIP_SHEETS="12",
            GRIDFORGE_FRIEND_ALLOWED_OUTPUTS="pdf,kmz",
        )
        admin = e2e_env.login_admin(app)
        friend, _ = e2e_env.invite_and_accept_friend(
            app, admin, email="friend@example.com"
        )

        assert (
            friend.post(
                "/api/jobs",
                json={"center": "-33.732, 150.312", "strip_sheets": _strip_sheets(13)},
                headers=_SAME_ORIGIN,
            ).status_code
            == 403
        )

        route_json = json.loads(synthetic_fixtures.katoomba_route.read_text(encoding="utf-8"))
        assert (
            friend.post(
                "/api/strip-plan",
                json={"route_geojson": route_json, "max_sheets": 13},
                headers=_SAME_ORIGIN,
            ).status_code
            == 403
        )

        assert (
            friend.post(
                "/api/jobs",
                json={
                    "center": "-33.732, 150.312",
                    "outputs": {"pdf": True, "field_bundle_zip": True},
                },
                headers=_SAME_ORIGIN,
            ).status_code
            == 403
        )

    def test_t2_upload_bytes_plus_one_returns_413_and_cleans_disk(self, e2e_env) -> None:
        app = e2e_env.build_app(
            auth_mode="hosted",
            GRIDFORGE_MAX_UPLOAD_BYTES_GUEST="1024",
            GRIDFORGE_MAX_UPLOAD_BYTES_FRIEND="2048",
        )
        admin = e2e_env.login_admin(app)
        friend, _ = e2e_env.invite_and_accept_friend(
            app, admin, email="friend@example.com"
        )
        guest = e2e_env.make_guest_client(app)

        oversized_guest = _valid_geojson_bytes(1025)
        r_guest = guest.post(
            "/api/jobs",
            data={"payload": json.dumps({"center": "-33.732, 150.312"})},
            files={"overlay_file": ("too_big.geojson", oversized_guest, "application/geo+json")},
            headers=_SAME_ORIGIN,
        )
        assert r_guest.status_code == 413
        # Verify partial upload directory was cleaned up
        leftover_uploads = list(e2e_env.data_dir.glob("*/uploads/*"))
        assert leftover_uploads == []

        # Friend strip-plan route_file > 2048 bytes -> 413
        oversized_route = _valid_geojson_bytes(2049)
        r_strip = friend.post(
            "/api/strip-plan",
            data={"scale": "25000", "max_sheets": "6"},
            files={"route_file": ("big_route.geojson", oversized_route, "application/geo+json")},
            headers=_SAME_ORIGIN,
        )
        assert r_strip.status_code == 413

    def test_t2_guest_daily_render_quota_429_persists_across_cookie_reset_same_ip(
        self, e2e_env
    ) -> None:
        app = e2e_env.build_app(auth_mode="hosted", GRIDFORGE_GUEST_DAILY_RENDERS="2")
        guest1 = e2e_env.make_guest_client(app)

        assert (
            guest1.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=_SAME_ORIGIN).status_code
            == 202
        )
        assert (
            guest1.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=_SAME_ORIGIN).status_code
            == 202
        )
        r_429 = guest1.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=_SAME_ORIGIN)
        assert r_429.status_code == 429

        # Fresh client (new guest cookie) from the same IP still hits 429
        guest2 = e2e_env.make_guest_client(app)
        assert (
            guest2.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=_SAME_ORIGIN).status_code
            == 429
        )

    def test_t2_friend_daily_render_quota_429(self, e2e_env) -> None:
        app = e2e_env.build_app(auth_mode="hosted", GRIDFORGE_FRIEND_DAILY_RENDERS="2")
        admin = e2e_env.login_admin(app)
        friend1, _ = e2e_env.invite_and_accept_friend(
            app, admin, email="friend1@example.com"
        )
        friend2, _ = e2e_env.invite_and_accept_friend(
            app, admin, email="friend2@example.com"
        )

        for _ in range(2):
            assert (
                friend1.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=_SAME_ORIGIN).status_code
                == 202
            )
        assert (
            friend1.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=_SAME_ORIGIN).status_code
            == 429
        )
        # Distinct friend has independent quota
        assert (
            friend2.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=_SAME_ORIGIN).status_code
            == 202
        )

    def test_t2_viewshed_and_rf_link_hourly_rate_limits_429(
        self, e2e_env, mock_preview_dem
    ) -> None:
        _ = mock_preview_dem
        e2e_env.seed_synthetic_pack("blue-mountains")
        app = e2e_env.build_app(
            auth_mode="hosted",
            GRIDFORGE_GUEST_HOURLY_PREVIEWS="2",
            GRIDFORGE_FRIEND_HOURLY_PREVIEWS="2",
            GRIDFORGE_RF_LINK_HOURLY_LIMIT="2",
            GRIDFORGE_FRIEND_RF_LINK_HOURLY_LIMIT="2",
        )
        admin = e2e_env.login_admin(app)
        friend, _ = e2e_env.invite_and_accept_friend(
            app, admin, email="friend@example.com"
        )
        guest = e2e_env.make_guest_client(app)

        for role_client in (guest, friend):
            for _ in range(2):
                assert (
                    role_client.post(
                        "/api/viewshed-preview", json=_viewshed_payload(), headers=_SAME_ORIGIN
                    ).status_code
                    == 200
                )
                assert (
                    role_client.post(
                        "/api/rf-link-profile", json=_rf_link_payload(), headers=_SAME_ORIGIN
                    ).status_code
                    == 200
                )
            assert (
                role_client.post(
                    "/api/viewshed-preview", json=_viewshed_payload(), headers=_SAME_ORIGIN
                ).status_code
                == 429
            )
            assert (
                role_client.post(
                    "/api/rf-link-profile", json=_rf_link_payload(), headers=_SAME_ORIGIN
                ).status_code
                == 429
            )

        # Admin remains unblocked
        assert (
            admin.post("/api/viewshed-preview", json=_viewshed_payload(), headers=_SAME_ORIGIN).status_code
            == 200
        )
        assert (
            admin.post("/api/rf-link-profile", json=_rf_link_payload(), headers=_SAME_ORIGIN).status_code
            == 200
        )

    def test_t2_geocode_hourly_rate_limit_429_on_cache_miss_only_and_cache_hit_bypass(
        self, e2e_env, mock_nominatim
    ) -> None:
        app = e2e_env.build_app(auth_mode="hosted", GRIDFORGE_GEOCODE_HOURLY_LIMIT="2")
        admin = e2e_env.login_admin(app)
        friend, _ = e2e_env.invite_and_accept_friend(
            app, admin, email="friend@example.com"
        )
        guest = e2e_env.make_guest_client(app)

        # 2 distinct uncached queries consume the 2/hr per-IP limit
        assert guest.get("/api/geocode", params={"q": "Katoomba"}).status_code == 200
        assert guest.get("/api/geocode", params={"q": "Leura"}).status_code == 200
        assert len(mock_nominatim.nominatim_calls) == 2

        # Cache hit must NOT trigger rate limit even at cap
        hit = guest.get("/api/geocode", params={"q": "Katoomba"})
        assert hit.status_code == 200
        assert len(mock_nominatim.nominatim_calls) == 2

        # 3rd uncached query fails with 429 for both guest and friend on same IP
        assert guest.get("/api/geocode", params={"q": "Blackheath"}).status_code == 429
        assert friend.get("/api/geocode", params={"q": "Wentworth Falls"}).status_code == 429
        assert len(mock_nominatim.nominatim_calls) == 2

        # Admin bypasses geocode rate limit even on uncached miss
        assert admin.get("/api/geocode", params={"q": "Blackheath"}).status_code == 200
        assert len(mock_nominatim.nominatim_calls) == 3

    def test_t2_queue_depth_cap_503_blocks_all_roles_including_admin_and_none(
        self, e2e_env
    ) -> None:
        hosted_app = e2e_env.build_app(
            auth_mode="hosted",
            GRIDFORGE_MAX_QUEUE_DEPTH="2",
            GRIDFORGE_GUEST_DAILY_RENDERS="10",
        )
        admin = e2e_env.login_admin(hosted_app)
        friend, _ = e2e_env.invite_and_accept_friend(
            hosted_app, admin, email="friend@example.com"
        )
        guest = e2e_env.make_guest_client(hosted_app)

        j_g = guest.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=_SAME_ORIGIN)
        j_f = friend.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=_SAME_ORIGIN)
        assert j_g.status_code == 202
        assert j_f.status_code == 202

        for role_client in (guest, friend, admin):
            resp = role_client.post(
                "/api/jobs", json={"center": "-33.732, 150.312"}, headers=_SAME_ORIGIN
            )
            assert resp.status_code == 503

        # Clear the 2 queued hosted jobs before verifying queue cap in `none` mode on same DB
        assert guest.post(f"/api/jobs/{j_g.json()['job_id']}/cancel", headers=_SAME_ORIGIN).status_code == 200
        assert friend.post(f"/api/jobs/{j_f.json()['job_id']}/cancel", headers=_SAME_ORIGIN).status_code == 200

        # Also enforced in `none` mode when GRIDFORGE_MAX_QUEUE_DEPTH is set
        none_app = e2e_env.build_app(auth_mode="none", GRIDFORGE_MAX_QUEUE_DEPTH="2")
        none_cli = e2e_env.new_client(none_app)
        assert (
            none_cli.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=_SAME_ORIGIN).status_code
            == 202
        )
        assert (
            none_cli.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=_SAME_ORIGIN).status_code
            == 202
        )
        assert (
            none_cli.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=_SAME_ORIGIN).status_code
            == 503
        )

    def test_t2_global_daily_public_budget_503_across_distinct_guest_ips(
        self, e2e_env
    ) -> None:
        app = e2e_env.build_app(
            auth_mode="hosted",
            GRIDFORGE_GLOBAL_DAILY_PUBLIC_RENDERS="2",
            GRIDFORGE_GUEST_DAILY_RENDERS="1",
            GRIDFORGE_TRUST_CF_CONNECTING_IP="true",
        )
        admin = e2e_env.login_admin(app)
        friend, _ = e2e_env.invite_and_accept_friend(
            app, admin, email="friend@example.com"
        )

        g1 = e2e_env.make_guest_client(app)
        g2 = e2e_env.make_guest_client(app)
        g3 = e2e_env.make_guest_client(app)

        assert (
            g1.post(
                "/api/jobs",
                json={"center": "-33.732, 150.312"},
                headers={**_SAME_ORIGIN, "CF-Connecting-IP": "198.51.100.1"},
            ).status_code
            == 202
        )
        assert (
            g2.post(
                "/api/jobs",
                json={"center": "-33.732, 150.312"},
                headers={**_SAME_ORIGIN, "CF-Connecting-IP": "198.51.100.2"},
            ).status_code
            == 202
        )
        # 3rd distinct guest IP has not used its personal quota, yet hits global 503 budget
        r3 = g3.post(
            "/api/jobs",
            json={"center": "-33.732, 150.312"},
            headers={**_SAME_ORIGIN, "CF-Connecting-IP": "198.51.100.3"},
        )
        assert r3.status_code == 503

        # Friend is unaffected by global public budget
        assert (
            friend.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=_SAME_ORIGIN).status_code
            == 202
        )

    def test_t2_kill_switch_precedence_over_daily_quota_503(self, e2e_env) -> None:
        app = e2e_env.build_app(
            auth_mode="hosted",
            GRIDFORGE_GUEST_DAILY_RENDERS="1",
        )
        admin = e2e_env.login_admin(app)
        guest = e2e_env.make_guest_client(app)

        assert (
            guest.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=_SAME_ORIGIN).status_code
            == 202
        )
        assert (
            admin.post(
                "/api/admin/settings",
                json={"public_renders_paused": True},
                headers=_SAME_ORIGIN,
            ).status_code
            == 200
        )

        # Guest already exhausted daily quota (would be 429), but kill switch takes precedence (503)
        resp = guest.post(
            "/api/jobs",
            data={"payload": json.dumps({"center": "-33.732, 150.312"})},
            files={
                "overlay_file": (
                    "track.geojson",
                    _valid_geojson_bytes(512),
                    "application/geo+json",
                )
            },
            headers=_SAME_ORIGIN,
        )
        assert resp.status_code == 503
        assert "paused" in resp.json()["detail"].lower()
        assert list(e2e_env.data_dir.glob("*/uploads/*")) == []

    def test_t5_ruler_metrics_requires_rate_limit_and_turnstile_and_rf_link_checks_turnstile(
        self, e2e_env, mock_turnstile, monkeypatch
    ) -> None:
        app = _build_turnstile_hosted_app(
            e2e_env,
            GRIDFORGE_GUEST_HOURLY_PREVIEWS="1",
        )
        fetch_calls: list[Any] = []
        monkeypatch.setattr(
            "gridforge.sources.dem_usgs3dep.AutoDEMSource.fetch_dem",
            lambda self, layout, cache_dir: (
                fetch_calls.append(layout),
                (_ for _ in ()).throw(ValueError("no-net")),
            )[1],
        )
        guest = e2e_env.make_guest_client(app)

        # Without Turnstile token, DEM-backed ruler-metrics & rf-link-profile must fail 403 before AutoDEMSource.fetch_dem
        r_ruler_403 = guest.post(
            "/api/ruler-metrics",
            json={"points": [[-35.30, 149.10], [-35.35, 149.15]], "dem_source": "auto"},
            headers=_SAME_ORIGIN,
        )
        assert r_ruler_403.status_code == 403
        assert len(fetch_calls) == 0

        r_rf_403 = guest.post(
            "/api/rf-link-profile",
            json={"from_lat": -35.3, "from_lon": 149.1, "to_lat": -35.31, "to_lon": 149.11},
            headers=_SAME_ORIGIN,
        )
        assert r_rf_403.status_code == 403
        assert len(fetch_calls) == 0

        # With valid Turnstile token, 1st DEM-backed ruler call executes (and consumes preview quota), 2nd hits 429 before fetch_dem
        r_ruler_ok = guest.post(
            "/api/ruler-metrics",
            json={"points": [[-35.30, 149.10], [-35.35, 149.15]], "dem_source": "auto"},
            headers={**_SAME_ORIGIN, "X-Turnstile-Token": "valid-turnstile-token"},
        )
        assert r_ruler_ok.status_code == 200
        assert len(fetch_calls) == 1

        # M1: DEM-backed ruler_metrics increments shared hourly_previews_used & admin previews_today
        sess_after_ruler = guest.get("/api/auth/session").json()["quotas"]
        assert sess_after_ruler["hourly_previews_used"] == 1
        assert sess_after_ruler["hourly_previews_remaining"] == 0

        admin = e2e_env.login_admin(app)
        usage_after_ruler = admin.get("/api/admin/usage").json()
        assert usage_after_ruler["summary"]["previews_today"] == 1

        r_ruler_429 = guest.post(
            "/api/ruler-metrics",
            json={"points": [[-35.30, 149.10], [-35.35, 149.15]], "dem_source": "auto"},
            headers={**_SAME_ORIGIN, "X-Turnstile-Token": "valid-turnstile-token"},
        )
        assert r_ruler_429.status_code == 429
        assert len(fetch_calls) == 1

        # Pure geodesic ruler (no dem_source/pack_id) stays unlimited without Turnstile and does not increment preview usage
        r_geodesic = guest.post(
            "/api/ruler-metrics",
            json={"points": [[-35.30, 149.10], [-35.35, 149.15]]},
            headers=_SAME_ORIGIN,
        )
        assert r_geodesic.status_code == 200
        assert len(fetch_calls) == 1
        assert guest.get("/api/auth/session").json()["quotas"]["hourly_previews_used"] == 1
        assert admin.get("/api/admin/usage").json()["summary"]["previews_today"] == 1

    def test_t5_multipart_uploads_do_not_spool_disk_before_turnstile_killswitch_or_413(
        self, e2e_env, mock_turnstile, monkeypatch
    ) -> None:
        import tempfile

        rolled: list[int] = []
        orig_write = tempfile.SpooledTemporaryFile.write

        def spy_write(self, data):
            res = orig_write(self, data)
            if getattr(self, "_rolled", False):
                rolled.append(len(data))
            return res

        monkeypatch.setattr(tempfile.SpooledTemporaryFile, "write", spy_write)

        # 1. Missing Turnstile on 2 MiB multipart /api/jobs rejects 403 with zero disk spooling
        app_ts = _build_turnstile_hosted_app(e2e_env)
        guest_ts = e2e_env.make_guest_client(app_ts)
        resp_403 = guest_ts.post(
            "/api/jobs",
            data={"payload": '{"center":"-35.3,149.1","scale":50000}'},
            files={"overlay_file": ("huge.geojson", b"X" * (2 * 1024 * 1024), "application/geo+json")},
            headers=_SAME_ORIGIN,
        )
        assert resp_403.status_code == 403
        assert sum(rolled) == 0

        # 2. Kill switch active on 2 MiB multipart /api/jobs (with valid header Turnstile) rejects 503 with zero disk spooling
        admin_ts = e2e_env.login_admin(app_ts)
        assert (
            admin_ts.post(
                "/api/admin/settings",
                json={"public_renders_paused": True},
                headers=_SAME_ORIGIN,
            ).status_code
            == 200
        )
        resp_503 = guest_ts.post(
            "/api/jobs",
            data={"payload": '{"center":"-35.3,149.1","scale":50000}'},
            files={"overlay_file": ("huge.geojson", b"X" * (2 * 1024 * 1024), "application/geo+json")},
            headers={**_SAME_ORIGIN, "X-Turnstile-Token": "valid-turnstile-token"},
        )
        assert resp_503.status_code == 503
        assert sum(rolled) == 0

        # 3. Oversized /api/strip-plan upload for Friend rejects 413 before spooling
        assert (
            admin_ts.post(
                "/api/admin/settings",
                json={"public_renders_paused": False},
                headers=_SAME_ORIGIN,
            ).status_code
            == 200
        )
        app_cap = e2e_env.build_app(
            auth_mode="hosted",
            GRIDFORGE_MAX_UPLOAD_BYTES_FRIEND=str(2 * 1024 * 1024),
        )
        admin_cap = e2e_env.login_admin(app_cap)
        friend_cap, _ = e2e_env.invite_and_accept_friend(
            app_cap, admin_cap, email="strip-cap@example.com"
        )
        resp_413 = friend_cap.post(
            "/api/strip-plan",
            data={"scale": "25000"},
            files={"route_file": ("big_route.geojson", b"Y" * (3 * 1024 * 1024), "application/geo+json")},
            headers=_SAME_ORIGIN,
        )
        assert resp_413.status_code == 413
        assert sum(rolled) == 0

    def test_t5_oversized_content_length_returns_413_before_turnstile_siteverify(
        self, e2e_env, mock_turnstile, monkeypatch
    ) -> None:
        import tempfile

        rolled: list[int] = []
        orig_write = tempfile.SpooledTemporaryFile.write

        def spy_write(self, data):
            res = orig_write(self, data)
            if getattr(self, "_rolled", False):
                rolled.append(len(data))
            return res

        monkeypatch.setattr(tempfile.SpooledTemporaryFile, "write", spy_write)

        app = _build_turnstile_hosted_app(
            e2e_env,
            GRIDFORGE_MAX_UPLOAD_BYTES_GUEST=str(1024 * 1024),
        )
        guest = e2e_env.make_guest_client(app)
        resp = guest.post(
            "/api/jobs",
            data={"payload": '{"center":"-35.3,149.1","scale":50000}'},
            files={"overlay_file": ("huge.geojson", b"Z" * (3 * 1024 * 1024), "application/geo+json")},
            headers={**_SAME_ORIGIN, "X-Turnstile-Token": "valid-turnstile-token"},
        )
        assert resp.status_code == 413
        assert len(mock_turnstile.turnstile_calls) == 0
        assert sum(rolled) == 0

    def test_t5_chunked_missing_content_length_multipart_aborts_413_without_spooling(
        self, e2e_env, monkeypatch
    ) -> None:
        import asyncio
        import tempfile

        rolled: list[int] = []
        orig_write = tempfile.SpooledTemporaryFile.write

        def spy_write(self, data):
            res = orig_write(self, data)
            if getattr(self, "_rolled", False):
                rolled.append(len(data))
            return res

        monkeypatch.setattr(tempfile.SpooledTemporaryFile, "write", spy_write)

        app = e2e_env.build_app(
            auth_mode="hosted",
            GRIDFORGE_MAX_UPLOAD_BYTES_FRIEND=str(1024 * 1024),
        )
        admin = e2e_env.login_admin(app)
        friend, _ = e2e_env.invite_and_accept_friend(
            app, admin, email="chunked-friend@example.com"
        )
        cookie_hdr = "; ".join(f"{k}={v}" for k, v in friend.cookies.items())

        boundary = "----gfChunkedBoundary77"

        def _build_multipart_bytes(field_name: str, filename: str, extra_fields: dict[str, str], payload_len: int) -> bytes:
            parts: list[bytes] = []
            for k, v in extra_fields.items():
                parts.append(
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{k}"\r\n\r\n'
                    f"{v}\r\n".encode("utf-8")
                )
            parts.append(
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
                f"Content-Type: application/geo+json\r\n\r\n".encode("utf-8")
            )
            parts.append(b"A" * payload_len)
            parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
            return b"".join(parts)

        async def _chunked_stream(raw: bytes, chunk_size: int = 32768):
            for idx in range(0, len(raw), chunk_size):
                yield raw[idx : idx + chunk_size]

        async def _send_chunked(path: str, raw: bytes) -> httpx.Response:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://localhost:8080"
            ) as acli:
                return await acli.post(
                    path,
                    content=_chunked_stream(raw),
                    headers={
                        **_SAME_ORIGIN,
                        "Cookie": cookie_hdr,
                        "Content-Type": f"multipart/form-data; boundary={boundary}",
                    },
                )

        jobs_body = _build_multipart_bytes(
            "overlay_file",
            "huge_chunked.geojson",
            {"payload": '{"center":"-35.3,149.1","scale":50000}'},
            3 * 1024 * 1024,
        )
        resp_jobs = asyncio.run(_send_chunked("/api/jobs", jobs_body))
        assert resp_jobs.status_code == 413
        assert sum(rolled) == 0

        strip_body = _build_multipart_bytes(
            "route_file",
            "huge_route.geojson",
            {"scale": "25000"},
            3 * 1024 * 1024,
        )
        resp_strip = asyncio.run(_send_chunked("/api/strip-plan", strip_body))
        assert resp_strip.status_code == 413
        assert sum(rolled) == 0
        assert list(e2e_env.data_dir.glob("*/uploads/*")) == []
        assert list(e2e_env.data_dir.glob(".tmp_upload_*")) == []
