"""Unit tests for server-backed shareable map snapshots (/api/shares)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from gridforge.web import db, shares as shares_store
from gridforge.web.app import create_app
from gridforge.web.worker import run_pruning_pass

_BASE_URL = "http://localhost:8080"
_SAME_ORIGIN = {"Origin": _BASE_URL, "Sec-Fetch-Site": "same-origin"}


def _sample_state() -> dict:
    return {
        "center": "-36.45590, 148.26360",
        "lat": -36.4559,
        "lon": 148.2636,
        "title": "Kosciuszko Winter Ops",
        "scale": 25000,
        "paper": "a4",
        "orientation": "landscape",
        "color_mode": "alpine",
        "units": "metric",
        "dpi": 300,
        "grid_cols": 2,
        "grid_rows": 1,
        "dem_source": "au-elvis",
        "pack_id": "",
        "basemap": "vector",
        "map_zoom": 13.2,
        "au_overlays": ["au-state-trails", "au-hydro"],
        "carto": {
            "contour_density": "high",
            "hillshade_strength": "strong",
            "show_buildings": False,
            "show_slope_shading": True,
        },
        "outputs": {
            "pdf": True,
            "geopdf": True,
            "mbtiles": True,
            "export_dpi": 450,
        },
        "waypoints": [
            {"label": "Camp 1", "lat": -36.45, "lon": 148.26, "category": "camp"}
        ],
        "viewsheds": [
            {
                "label": "Peak",
                "lat": -36.455,
                "lon": 148.263,
                "observer_height_m": 2,
                "target_height_m": 1.5,
                "max_distance_m": 10000,
            }
        ],
        "rf_links": [],
        "tactical_graphics": [
            {
                "kind": "range_ring",
                "label": "RING 1",
                "points": [[-36.455, 148.263]],
                "radii_km": [1, 3, 5],
                "color": "#38BDF8",
            }
        ],
        "include_route_card": True,
        "include_atlas_index": True,
    }


def test_local_mode_share_crud_sanitization_and_project_snapshot(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path / "data", packs_dir=tmp_path / "packs")
    with TestClient(app, base_url=_BASE_URL) as client:
        # Cross-origin browser writes are rejected by SameOriginWritesMiddleware
        evil = {"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"}
        assert (
            client.post(
                "/api/shares",
                json={"name": "Evil", "state": _sample_state()},
                headers=evil,
            ).status_code
            == 403
        )

        # Create share directly from state (unknown keys stripped)
        payload_state = _sample_state()
        payload_state["secret_extra"] = "should_be_dropped"
        created_res = client.post(
            "/api/shares",
            json={
                "name": "Kosciuszko Shared",
                "notes": "Weekend ski tour",
                "state": payload_state,
            },
            headers=_SAME_ORIGIN,
        )
        assert created_res.status_code == 201
        created = created_res.json()
        sid = created["share_id"]
        assert shares_store.is_valid_share_id(sid)
        assert created["share_path"] == f"/?share={sid}"
        assert created["name"] == "Kosciuszko Shared"
        assert created["notes"] == "Weekend ski tour"
        assert created["expires_at"] is None
        assert "secret_extra" not in created["state"]
        assert "owner_id" not in created
        assert "owner_role" not in created

        # Public GET increments view_count and sets Cache-Control: no-cache
        fetched_res = client.get(f"/api/shares/{sid}")
        assert fetched_res.status_code == 200
        assert fetched_res.headers.get("cache-control") == "no-cache"
        fetched = fetched_res.json()
        assert fetched["view_count"] == 1
        assert fetched["state"]["title"] == "Kosciuszko Winter Ops"
        assert fetched["state"]["basemap"] == "vector"
        assert fetched["state"]["map_zoom"] == 13.2
        assert "owner_id" not in fetched

        # Create a saved project, then mint an immutable share snapshot from project_id
        proj_res = client.post(
            "/api/projects",
            json={
                "name": "Alpine Patrol",
                "notes": "Private notes",
                "state": _sample_state(),
            },
            headers=_SAME_ORIGIN,
        )
        assert proj_res.status_code == 201
        proj_id = proj_res.json()["project_id"]

        proj_share_res = client.post(
            "/api/shares",
            json={"project_id": proj_id},
            headers=_SAME_ORIGIN,
        )
        assert proj_share_res.status_code == 201
        proj_share = proj_share_res.json()
        assert proj_share["name"] == "Alpine Patrol"
        assert proj_share["notes"] == "Private notes"
        assert "source_project_id" not in proj_share
        assert "last_job_id" not in proj_share

        # Editing or deleting the project later does NOT mutate or delete the frozen snapshot
        assert (
            client.patch(
                f"/api/projects/{proj_id}",
                json={"name": "Mutated Private Name", "notes": "Secret update"},
                headers=_SAME_ORIGIN,
            ).status_code
            == 200
        )
        assert (
            client.delete(f"/api/projects/{proj_id}", headers=_SAME_ORIGIN).status_code
            == 200
        )
        still_frozen = client.get(f"/api/shares/{proj_share['share_id']}").json()
        assert still_frozen["name"] == "Alpine Patrol"
        assert still_frozen["notes"] == "Private notes"

        # List & delete
        listed = client.get("/api/shares").json()["shares"]
        assert {s["share_id"] for s in listed} == {sid, proj_share["share_id"]}

        del_res = client.delete(f"/api/shares/{sid}", headers=_SAME_ORIGIN)
        assert del_res.status_code == 200
        assert del_res.json()["deleted"] is True
        assert client.get(f"/api/shares/{sid}").status_code == 404

        # Invalid share ID -> 400; missing valid-format share ID -> 404
        assert client.get("/api/shares/bad!id").status_code == 400
        assert client.get("/api/shares/validMissingShareId123").status_code == 404


def test_share_size_cap_and_validation(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path / "data", packs_dir=tmp_path / "packs")
    with TestClient(app, base_url=_BASE_URL) as client:
        # Empty request fails validation (422)
        assert (
            client.post("/api/shares", json={}, headers=_SAME_ORIGIN).status_code
            == 422
        )

        # Oversized state (> 64 KB) rejected with 413
        huge_state = _sample_state()
        huge_state["waypoints"] = [
            {
                "label": f"WP{i}",
                "lat": -36.4,
                "lon": 148.2,
                "category": "waypoint",
                "notes": "X" * 600,
            }
            for i in range(150)
        ]
        res = client.post(
            "/api/shares",
            json={"name": "Huge", "state": huge_state},
            headers=_SAME_ORIGIN,
        )
        assert res.status_code == 413


def test_share_streaming_body_cap(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path / "data", packs_dir=tmp_path / "packs")
    headers = {**_SAME_ORIGIN, "Content-Type": "application/json"}
    payload = json.dumps({"name": "Map", "state": _sample_state()}).encode()

    async def upload_parts(*, over_limit: bool):
        yield payload
        yield b" " * (256 * 1024 - len(payload))
        if over_limit:
            yield b" "

    async def check() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=_BASE_URL) as client:
            allowed = await client.post("/api/shares", headers=headers, content=upload_parts(over_limit=False))
            assert allowed.status_code == 201
            result = await client.post("/api/shares", headers=headers, content=upload_parts(over_limit=True))
            assert "content-length" not in result.request.headers
            assert result.status_code == 413
            listed = await client.get("/api/shares")
            assert len(listed.json()["shares"]) == 1

    asyncio.run(check())


def test_hosted_mode_share_public_read_tenant_isolation_and_rate_limit(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("GRIDFORGE_AUTH_MODE", "hosted")
    monkeypatch.setenv("GRIDFORGE_SESSION_SECRET", "test-share-secret-0123456789abcdef")
    monkeypatch.setenv("GRIDFORGE_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("GRIDFORGE_ADMIN_PASSWORD", "SuperSecureAdminPass123!")
    monkeypatch.setenv("GRIDFORGE_SHARE_HOURLY_LIMIT", "2")
    monkeypatch.setenv("GRIDFORGE_GUEST_SHARE_TTL_HOURS", "48")

    app = create_app(data_dir=tmp_path / "data", packs_dir=tmp_path / "packs")

    with (
        TestClient(app, base_url=_BASE_URL) as guest_a,
        TestClient(app, base_url=_BASE_URL) as guest_b,
        TestClient(app, base_url=_BASE_URL) as admin,
    ):
        # Admin logs in
        login_res = admin.post(
            "/api/auth/login",
            json={
                "email": "admin@example.com",
                "password": "SuperSecureAdminPass123!",
            },
            headers=_SAME_ORIGIN,
        )
        assert login_res.status_code == 200

        # Guest A creates a private project and shares it
        proj_res = guest_a.post(
            "/api/projects",
            json={"name": "Guest A Hike", "state": _sample_state()},
            headers=_SAME_ORIGIN,
        )
        assert proj_res.status_code == 201
        proj_id = proj_res.json()["project_id"]

        # Guest B CANNOT mint a share from Guest A's private project
        assert (
            guest_b.post(
                "/api/shares",
                json={"project_id": proj_id},
                headers=_SAME_ORIGIN,
            ).status_code
            == 403
        )

        # Guest A creates a share from their project (requesting 9999h gets capped to 48h)
        share_res = guest_a.post(
            "/api/shares",
            json={"project_id": proj_id, "expires_in_hours": 1000},
            headers=_SAME_ORIGIN,
        )
        assert share_res.status_code == 201
        share = share_res.json()
        sid = share["share_id"]
        assert share["expires_at"] is not None
        created_dt = datetime.fromisoformat(share["created_at"])
        expires_dt = datetime.fromisoformat(share["expires_at"])
        assert timedelta(hours=47) <= (expires_dt - created_dt) <= timedelta(hours=49)

        # Guest B (and any unauthenticated viewer) CAN read the public share snapshot
        pub_res = guest_b.get(f"/api/shares/{sid}")
        assert pub_res.status_code == 200
        assert pub_res.json()["name"] == "Guest A Hike"
        assert "owner_id" not in pub_res.json()

        # Guest B does NOT see Guest A's shares in GET /api/shares and CANNOT delete Guest A's share
        assert guest_b.get("/api/shares").json()["shares"] == []
        assert guest_b.delete(f"/api/shares/{sid}", headers=_SAME_ORIGIN).status_code == 403

        # Guest hourly rate limit (2/hr by IP): Guest B already used 1 attempt (403 counted) + Guest A used 1 -> next hits 429!
        limited_res = guest_a.post(
            "/api/shares",
            json={"name": "Third", "state": _sample_state()},
            headers=_SAME_ORIGIN,
        )
        assert limited_res.status_code == 429

        # Admin has no TTL cap and CAN delete/revoke any share snapshot
        admin_share = admin.post(
            "/api/shares",
            json={"name": "Admin Permanent", "state": _sample_state()},
            headers=_SAME_ORIGIN,
        )
        assert admin_share.status_code == 201
        assert admin_share.json()["expires_at"] is None
        assert admin.delete(f"/api/shares/{sid}", headers=_SAME_ORIGIN).status_code == 200
        assert guest_b.get(f"/api/shares/{sid}").status_code == 404


def test_expired_share_returns_404_and_is_pruned_by_worker(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    db_path = data_dir / "gridforge.db"
    db.init_db(db_path)

    past = datetime.now(timezone.utc) - timedelta(hours=5)
    expired = shares_store.create_share(
        db_path,
        name="Old Share",
        notes="",
        state=_sample_state(),
        owner_id="guest:old",
        owner_role="guest",
        ttl_hours=1.0,
        now=past,
    )
    active = shares_store.create_share(
        db_path,
        name="Fresh Share",
        notes="",
        state=_sample_state(),
        owner_id="local",
        owner_role="none",
        ttl_hours=None,
    )

    # Expired snapshot immediately returns None on public lookup
    assert shares_store.get_public_share(db_path, expired.share_id) is None
    assert shares_store.get_public_share(db_path, active.share_id) is not None

    # Worker periodic prune removes expired snapshot while keeping permanent snapshot
    summary = run_pruning_pass(db_path, data_dir=data_dir)
    assert summary["shares"] == 1
    assert shares_store.get_share_owner(db_path, expired.share_id) is None
    assert shares_store.get_share_owner(db_path, active.share_id) == ("local", "none")
