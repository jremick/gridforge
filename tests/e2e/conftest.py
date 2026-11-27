"""Shared E2E test infrastructure, isolated app factories, HTTP mocks, and role helpers.

All E2E tests run offline against isolated temporary directories and independent
``TestClient`` instances, without ever using the module-level ``gridforge.web.app.app``
singleton or making real network requests.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pypdf import PdfWriter

from gridforge.config import BBox, PackManifest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FIXTURES_DIR = _REPO_ROOT / "tests" / "fixtures"
_EXAMPLES_DIR = _REPO_ROOT / "examples"

_SAFE_SQL_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_DEFAULT_ADMIN_EMAIL = "admin@gridmaps.test"
_DEFAULT_ADMIN_PASSWORD = "AdminPass!234"
_DEFAULT_SESSION_SECRET = "e2e-test-session-secret-32bytes!!"

_GRIDFORGE_ENV_KEYS_TO_CLEAR = (
    # Core paths, runtime & proxy
    "GRIDFORGE_DATA_DIR",
    "GRIDFORGE_PACKS_DIR",
    "GRIDFORGE_CACHE_DIR",
    "GRIDFORGE_CACHE_MAX_BYTES",
    "GRIDFORGE_SAMPLES_DIR",
    "GRIDFORGE_WEB_DIST",
    "GRIDFORGE_MEMORY_BUDGET_MB",
    "GRIDFORGE_ALLOWED_HOSTS",
    "GRIDFORGE_BASEMAP_MBTILES",
    "GRIDFORGE_NOMINATIM_URL",
    "GRIDFORGE_TRUST_CF_CONNECTING_IP",
    "GRIDFORGE_WORKER_ID",
    "GRIDFORGE_LOG_LEVEL",
    "GRIDFORGE_JOB_TTL_HOURS",
    "GRIDFORGE_GUEST_TTL_HOURS",
    "GRIDFORGE_FRIEND_TTL_HOURS",
    # Auth (auth.py)
    "GRIDFORGE_AUTH_MODE",
    "GRIDFORGE_SESSION_SECRET",
    "GRIDFORGE_SESSION_TTL_HOURS",
    "GRIDFORGE_GUEST_COOKIE_TTL_HOURS",
    "GRIDFORGE_COOKIE_SECURE",
    "GRIDFORGE_TURNSTILE_SITE_KEY",
    "GRIDFORGE_TURNSTILE_SECRET_KEY",
    "GRIDFORGE_GOOGLE_CLIENT_ID",
    "GRIDFORGE_GOOGLE_CLIENT_SECRET",
    "GRIDFORGE_GOOGLE_REDIRECT_URI",
    "GRIDFORGE_ADMIN_EMAIL",
    "GRIDFORGE_ADMIN_PASSWORD",
    "GRIDFORGE_ADMIN_NAME",
    # Limits & quotas (limits.py)
    "GRIDFORGE_GUEST_MAX_GRID_CELLS",
    "GRIDFORGE_FRIEND_MAX_GRID_CELLS",
    "GRIDFORGE_GUEST_STRIP_ALLOWED",
    "GRIDFORGE_FRIEND_STRIP_ALLOWED",
    "GRIDFORGE_GUEST_MAX_STRIP_SHEETS",
    "GRIDFORGE_FRIEND_MAX_STRIP_SHEETS",
    "GRIDFORGE_GUEST_ALLOWED_OUTPUTS",
    "GRIDFORGE_FRIEND_ALLOWED_OUTPUTS",
    "GRIDFORGE_MAX_UPLOAD_BYTES",
    "GRIDFORGE_MAX_UPLOAD_BYTES_GUEST",
    "GRIDFORGE_MAX_UPLOAD_BYTES_FRIEND",
    "GRIDFORGE_MAX_UPLOAD_BYTES_ADMIN",
    "GRIDFORGE_GUEST_DAILY_RENDERS",
    "GRIDFORGE_FRIEND_DAILY_RENDERS",
    "GRIDFORGE_GUEST_HOURLY_PREVIEWS",
    "GRIDFORGE_FRIEND_HOURLY_PREVIEWS",
    "GRIDFORGE_RF_LINK_HOURLY_LIMIT",
    "GRIDFORGE_FRIEND_RF_LINK_HOURLY_LIMIT",
    "GRIDFORGE_GEOCODE_HOURLY_LIMIT",
    "GRIDFORGE_GEOCODE_GLOBAL_HOURLY_LIMIT",
    "GRIDFORGE_MAX_QUEUE_DEPTH",
    "GRIDFORGE_GLOBAL_DAILY_PUBLIC_RENDERS",
    # Uvicorn proxy trust (access.py)
    "FORWARDED_ALLOW_IPS",
)

# Valid 1x1 transparent PNG for fake basemap MBTiles or asset stubs
_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\rIDATx\x9cc``\xf8\x0f\x00"
    b"\x01\x04\x01\x00\x18\xdd\x8d\xb1\x00\x00\x00\x00IEND\xaeB`\x82"
)


def pytest_configure(config: pytest.Config) -> None:
    """Register custom E2E test markers."""
    config.addinivalue_line(
        "markers",
        "m1_contract: Tests asserting M1/M2/M3 interface contracts",
    )


def _clear_gridforge_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear all ``GRIDFORGE_*`` and ``FORWARDED_ALLOW_IPS`` env vars via ``monkeypatch``."""
    for key in _GRIDFORGE_ENV_KEYS_TO_CLEAR:
        monkeypatch.delenv(key, raising=False)
    for key in list(os.environ.keys()):
        if key.startswith("GRIDFORGE_"):
            monkeypatch.delenv(key, raising=False)


def _reset_optional_web_singletons() -> None:
    """Clear in-memory caches and optional M1 rate-limiter buckets without importing app early."""
    app_mod = sys.modules.get("gridforge.web.app")
    if app_mod is not None and hasattr(app_mod, "_geocode_cache"):
        app_mod._geocode_cache.clear()
    for mod_name in ("gridforge.web.limits", "gridforge.web.auth"):
        mod = sys.modules.get(mod_name)
        if mod is None:
            continue
        for hook_name in ("reset_rate_limiters", "reset_limiters", "clear_rate_limiters"):
            hook = getattr(mod, hook_name, None)
            if callable(hook):
                hook()
        for attr_name in ("_RATE_BUCKETS", "_rate_buckets", "_limiters", "_LIMITERS"):
            bucket = getattr(mod, attr_name, None)
            if hasattr(bucket, "clear"):
                bucket.clear()


@pytest.fixture(autouse=True)
def clean_gridforge_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Ensure zero environment variable or in-memory cache leakage between E2E tests."""
    _clear_gridforge_env(monkeypatch)
    _reset_optional_web_singletons()
    yield
    _reset_optional_web_singletons()


@dataclass(frozen=True)
class E2EDirs:
    """Isolated per-test filesystem directories mirroring `/data` and `/app/web/dist`."""

    root: Path
    data_dir: Path
    packs_dir: Path
    cache_dir: Path
    samples_dir: Path
    web_dist_dir: Path
    db_path: Path


@dataclass(frozen=True)
class SyntheticFixtures:
    """Paths to deterministic offline fixtures under ``tests/fixtures/`` and ``examples/``."""

    dem_tif: Path
    osm: Path
    overlay_geojson: Path
    overlay_gpx: Path
    overlay_csv: Path
    katoomba_route: Path


@pytest.fixture()
def e2e_dirs(tmp_path: Path) -> E2EDirs:
    """Provision isolated data, packs, cache, samples, and web_dist directories."""
    data_root = tmp_path / "data"
    data_dir = data_root / "jobs"
    packs_dir = data_root / "packs"
    cache_dir = data_root / "cache"
    samples_dir = tmp_path / "samples"
    web_dist_dir = tmp_path / "web_dist"
    for d in (data_dir, packs_dir, cache_dir, samples_dir, web_dist_dir):
        d.mkdir(parents=True, exist_ok=True)
    return E2EDirs(
        root=tmp_path,
        data_dir=data_dir,
        packs_dir=packs_dir,
        cache_dir=cache_dir,
        samples_dir=samples_dir,
        web_dist_dir=web_dist_dir,
        db_path=data_dir / "gridforge_jobs.sqlite3",
    )


@pytest.fixture(scope="session")
def synthetic_fixtures() -> SyntheticFixtures:
    """Return verified offline fixture paths."""
    return SyntheticFixtures(
        dem_tif=_FIXTURES_DIR / "synthetic_dem.tif",
        osm=_FIXTURES_DIR / "synthetic_osm.osm",
        overlay_geojson=_FIXTURES_DIR / "synthetic_overlay.geojson",
        overlay_gpx=_FIXTURES_DIR / "synthetic_overlay.gpx",
        overlay_csv=_FIXTURES_DIR / "synthetic_overlay.csv",
        katoomba_route=_EXAMPLES_DIR / "katoomba_route.geojson",
    )


def same_origin_headers(base_url: str = "http://localhost:8080") -> dict[str, str]:
    """Return same-origin browser write headers matching ``SameOriginWritesMiddleware``."""
    return {"Origin": base_url.rstrip("/"), "Sec-Fetch-Site": "same-origin"}


def seed_synthetic_pack(
    packs_dir: Path,
    pack_id: str = "blue-mountains",
    *,
    with_basemap: bool = False,
) -> Path:
    """Seed an offline pack containing ``synthetic_dem.tif`` and ``pack.json``."""
    pack_dir = packs_dir / pack_id
    pack_dir.mkdir(parents=True, exist_ok=True)

    dem_src = _FIXTURES_DIR / "synthetic_dem.tif"
    shutil.copy2(dem_src, pack_dir / "dem.tif")

    osm_src = _FIXTURES_DIR / "synthetic_osm.osm"
    if osm_src.is_file():
        shutil.copy2(osm_src, pack_dir / "osm.osm.pbf")
    else:
        (pack_dir / "osm.osm.pbf").write_bytes(b"fake-osm-pbf")

    basemap_rel: str | None = None
    if with_basemap:
        bm_path = pack_dir / "basemap.mbtiles"
        conn = sqlite3.connect(str(bm_path))
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (name TEXT, value TEXT);
                CREATE TABLE IF NOT EXISTS tiles (
                    zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB
                );
                """
            )
            conn.execute("INSERT INTO metadata VALUES ('name', 'Synthetic Basemap')")
            conn.execute("INSERT INTO metadata VALUES ('format', 'png')")
            conn.execute("INSERT INTO tiles VALUES (8, 234, 102, ?)", (_TINY_PNG,))
            conn.commit()
        finally:
            conn.close()
        basemap_rel = "basemap.mbtiles"

    manifest = PackManifest(
        pack_id=pack_id,
        name="Blue Mountains (NSW)" if pack_id == "blue-mountains" else f"Pack {pack_id}",
        region=pack_id,
        bbox_wgs84=BBox(min_x=150.22, min_y=-33.81, max_x=150.41, max_y=-33.65),
        utm_zones=["56H"],
        dem_path="dem.tif",
        vertical_datum="AHD",
        osm_pbf_path="osm.osm.pbf",
        basemap_path=basemap_rel,
        overlays={},
        attributions=["OpenStreetMap", "Geoscience Australia"],
        created_at="2026-11-26T08:00:00Z",
        size_bytes=max(1024, (pack_dir / "dem.tif").stat().st_size),
    )
    (pack_dir / "pack.json").write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    return pack_dir


def seed_fake_pack(
    packs_dir: Path,
    pack_id: str = "blue-mountains",
    *,
    with_basemap: bool = False,
) -> Path:
    """Alias for ``seed_synthetic_pack`` matching unit/E2E test conventions."""
    return seed_synthetic_pack(packs_dir, pack_id=pack_id, with_basemap=with_basemap)


def seed_job_pdf_artifact(
    data_dir: Path,
    db_path: Path,
    job_id: str,
    *,
    filename: str = "map.pdf",
    elapsed_seconds: float = 1.25,
    peak_rss_mb: float = 256.0,
) -> Path:
    """Write a valid 1-page PDF artifact and transition ``job_id`` to ``succeeded``."""
    from gridforge.web import db
    from gridforge.web.schemas import JobArtifact, JobStatus

    art_dir = data_dir / job_id / "artifacts"
    art_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = art_dir / filename

    writer = PdfWriter()
    writer.add_blank_page(width=595.28, height=841.89)
    with pdf_path.open("wb") as fh:
        writer.write(fh)
    writer.close()

    # Transition only the target job cleanly from queued -> running before complete_job
    conn = sqlite3.connect(str(db_path), timeout=15.0)
    try:
        conn.execute(
            "UPDATE jobs SET status = ?, stage = 'rendering', progress_pct = 90, started_at = COALESCE(started_at, ?) "
            "WHERE job_id = ? AND status = ?",
            (
                JobStatus.RUNNING.value,
                datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                job_id,
                JobStatus.QUEUED.value,
            ),
        )
        conn.commit()
    finally:
        conn.close()

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
        elapsed_seconds=elapsed_seconds,
        peak_rss_mb=peak_rss_mb,
    )
    if completed is None:
        raise RuntimeError(f"Failed to complete seeded job '{job_id}' in SQLite.")
    return pdf_path


def seed_completed_job_pdf(
    data_dir: Path,
    db_path: Path,
    job_id: str,
    *,
    filename: str = "map.pdf",
    elapsed_seconds: float = 1.25,
    peak_rss_mb: float = 256.0,
) -> Path:
    """Alias for ``seed_job_pdf_artifact``."""
    return seed_job_pdf_artifact(
        data_dir,
        db_path,
        job_id,
        filename=filename,
        elapsed_seconds=elapsed_seconds,
        peak_rss_mb=peak_rss_mb,
    )


def age_sqlite_timestamps(
    db_path: Path,
    *,
    table: str,
    id_col: str | None = None,
    row_id: str | None = None,
    age_hours: float | None = None,
    hours_ago: float | None = None,
    columns: tuple[str, ...] = ("created_at", "finished_at", "updated_at", "expires_at"),
) -> None:
    """Deterministically backdate ISO-8601 UTC timestamp columns in SQLite WAL mode."""
    if not _SAFE_SQL_IDENT_RE.match(table):
        raise ValueError(f"Invalid SQLite table name: {table!r}")
    if id_col is not None and not _SAFE_SQL_IDENT_RE.match(id_col):
        raise ValueError(f"Invalid SQLite column name: {id_col!r}")

    delta_hours = age_hours if age_hours is not None else hours_ago
    if delta_hours is None:
        raise ValueError("Specify either age_hours or hours_ago.")

    target_iso = (datetime.now(timezone.utc) - timedelta(hours=float(delta_hours))).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    conn = sqlite3.connect(str(db_path), timeout=15.0)
    try:
        table_cols = {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        valid_cols = [c for c in columns if c in table_cols and _SAFE_SQL_IDENT_RE.match(c)]
        for col in valid_cols:
            if id_col is not None and row_id is not None:
                conn.execute(
                    f"UPDATE {table} SET {col} = ? WHERE {id_col} = ? AND {col} IS NOT NULL",
                    (target_iso, row_id),
                )
            else:
                conn.execute(
                    f"UPDATE {table} SET {col} = ? WHERE {col} IS NOT NULL",
                    (target_iso,),
                )
        conn.commit()
    finally:
        conn.close()


def set_file_mtime_atime(path: Path, age_hours: float) -> None:
    """Set both access and modification times of ``path`` to ``age_hours`` in the past."""
    target_ts = time.time() - (float(age_hours) * 3600.0)
    os.utime(path, (target_ts, target_ts))


def _default_offline_pack_builder(region_or_bbox: str, out_dir: Path, **kwargs: Any) -> PackManifest:
    """Fast deterministic pack builder stub used when E2E tests do not supply one."""
    pdir = Path(out_dir)
    pdir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_FIXTURES_DIR / "synthetic_dem.tif", pdir / "dem.tif")
    shutil.copy2(_FIXTURES_DIR / "synthetic_osm.osm", pdir / "osm.osm.pbf")
    manifest = PackManifest(
        pack_id=pdir.name,
        name=kwargs.get("name") or f"Built {region_or_bbox}",
        region=region_or_bbox,
        bbox_wgs84=BBox(min_x=148.74, min_y=-35.94, max_x=149.42, max_y=-35.10),
        utm_zones=["55H"],
        dem_path="dem.tif",
        vertical_datum="AHD",
        osm_pbf_path="osm.osm.pbf",
        overlays={},
        attributions=["OpenStreetMap"],
        created_at="2026-11-26T12:00:00Z",
        size_bytes=4096,
    )
    (pdir / "pack.json").write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    return manifest


@dataclass
class OutboundHTTPMock:
    """Configurable URL-routing interceptor for outbound ``httpx`` calls in E2E tests."""

    turnstile_valid_tokens: set[str] = field(default_factory=lambda: {"valid-turnstile-token"})
    turnstile_force_success: bool | None = None
    turnstile_error_codes: list[str] = field(default_factory=lambda: ["invalid-input-response"])
    turnstile_calls: list[dict[str, Any]] = field(default_factory=list)

    google_email: str = "oauth-user@example.com"
    google_name: str = "OAuth User"
    google_sub: str = "google-sub-123"
    google_verified_email: bool = True
    google_token_error: bool = False
    google_token_calls: list[dict[str, Any]] = field(default_factory=list)
    google_userinfo_calls: list[dict[str, Any]] = field(default_factory=list)

    nominatim_offline: bool = False
    nominatim_results: list[dict[str, Any]] = field(
        default_factory=lambda: [
            {
                "display_name": "Katoomba, New South Wales, Australia",
                "name": "Katoomba",
                "lat": "-33.715",
                "lon": "150.312",
                "type": "town",
                "class": "place",
            }
        ]
    )
    nominatim_calls: list[dict[str, Any]] = field(default_factory=list)

    requests: list[tuple[str, str]] = field(default_factory=list)

    def configure_turnstile(
        self,
        *,
        success: bool | None = None,
        valid_tokens: set[str] | None = None,
        error_codes: list[str] | None = None,
    ) -> None:
        self.turnstile_force_success = success
        if valid_tokens is not None:
            self.turnstile_valid_tokens = set(valid_tokens)
        if error_codes is not None:
            self.turnstile_error_codes = list(error_codes)

    def configure_google_oauth(
        self,
        *,
        email: str = "oauth-user@example.com",
        name: str = "OAuth User",
        sub: str = "google-sub-123",
        verified_email: bool = True,
        token_error: bool = False,
    ) -> None:
        self.google_email = email
        self.google_name = name
        self.google_sub = sub
        self.google_verified_email = verified_email
        self.google_token_error = token_error

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        host = (request.url.host or "").lower()
        path = request.url.path
        self.requests.append((request.method, str(request.url)))

        # 1. Cloudflare Turnstile siteverify
        if host == "challenges.cloudflare.com" and "/turnstile/v0/siteverify" in path:
            raw_body = request.content.decode("utf-8", errors="replace")
            content_type = (request.headers.get("content-type") or "").lower()
            token = ""
            remoteip = ""
            secret = ""
            if "application/json" in content_type:
                import json

                try:
                    data = json.loads(raw_body)
                    token = str(data.get("response") or "")
                    remoteip = str(data.get("remoteip") or "")
                    secret = str(data.get("secret") or "")
                except Exception:
                    pass
            else:
                parsed = parse_qs(raw_body)
                token = (parsed.get("response") or [""])[0]
                remoteip = (parsed.get("remoteip") or [""])[0]
                secret = (parsed.get("secret") or [""])[0]

            self.turnstile_calls.append(
                {"token": token, "remoteip": remoteip, "secret": secret}
            )
            if self.turnstile_force_success is not None:
                ok = self.turnstile_force_success
            else:
                ok = token in self.turnstile_valid_tokens

            if ok:
                return httpx.Response(200, json={"success": True, "hostname": "localhost"})
            return httpx.Response(
                200,
                json={"success": False, "error-codes": list(self.turnstile_error_codes)},
            )

        # 2. Google OAuth token endpoint
        if host == "oauth2.googleapis.com" and path == "/token":
            raw_body = request.content.decode("utf-8", errors="replace")
            self.google_token_calls.append({"body": raw_body})
            if self.google_token_error:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(
                200,
                json={
                    "access_token": "mock-google-access-token",
                    "id_token": "mock-google-id-token",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
            )

        # 3. Google OAuth userinfo endpoint
        if (host == "openidconnect.googleapis.com" and path == "/v1/userinfo") or (
            host == "www.googleapis.com"
            and path in ("/oauth2/v3/userinfo", "/oauth2/v2/userinfo")
        ):
            self.google_userinfo_calls.append({"url": str(request.url)})
            return httpx.Response(
                200,
                json={
                    "sub": self.google_sub,
                    "id": self.google_sub,
                    "email": self.google_email,
                    "email_verified": self.google_verified_email,
                    "verified_email": self.google_verified_email,
                    "name": self.google_name,
                },
            )

        # 4. OSM Nominatim geocoding
        if host == "nominatim.openstreetmap.org" and "/search" in path:
            self.nominatim_calls.append({"query": dict(request.url.params)})
            if self.nominatim_offline:
                raise httpx.ConnectError("Mocked offline Nominatim error", request=request)
            return httpx.Response(200, json=self.nominatim_results)

        raise AssertionError(
            f"Unexpected live outbound HTTP request in E2E suite: {request.method} {request.url}"
        )


@pytest.fixture()
def mock_outbound_http(monkeypatch: pytest.MonkeyPatch) -> OutboundHTTPMock:
    """Intercept all outbound sync/async ``httpx`` calls and block unmocked internet access."""
    mock = OutboundHTTPMock()

    def _sync_handle(self: Any, request: httpx.Request) -> httpx.Response:
        return mock.handle_request(request)

    async def _async_handle(self: Any, request: httpx.Request) -> httpx.Response:
        return mock.handle_request(request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _sync_handle)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _async_handle)
    return mock


@pytest.fixture()
def mock_turnstile(mock_outbound_http: OutboundHTTPMock) -> OutboundHTTPMock:
    """Expose Turnstile verification mock controls on ``mock_outbound_http``."""
    return mock_outbound_http


@pytest.fixture()
def mock_google_oauth(mock_outbound_http: OutboundHTTPMock) -> OutboundHTTPMock:
    """Expose Google OAuth mock controls on ``mock_outbound_http``."""
    return mock_outbound_http


@pytest.fixture()
def mock_nominatim(mock_outbound_http: OutboundHTTPMock) -> Iterator[OutboundHTTPMock]:
    """Expose Nominatim mock controls and clear ``_geocode_cache`` around the test."""
    _reset_optional_web_singletons()
    yield mock_outbound_http
    _reset_optional_web_singletons()


@dataclass
class E2EEnv:
    """Per-test E2E environment providing isolated dirs, app builder, clients, and mocks."""

    dirs: E2EDirs
    monkeypatch: pytest.MonkeyPatch
    outbound_http: OutboundHTTPMock
    admin_email: str = _DEFAULT_ADMIN_EMAIL
    admin_password: str = _DEFAULT_ADMIN_PASSWORD
    session_secret: str = _DEFAULT_SESSION_SECRET
    _clients: list[TestClient] = field(default_factory=list)

    @property
    def root(self) -> Path:
        return self.dirs.root

    @property
    def data_dir(self) -> Path:
        return self.dirs.data_dir

    @property
    def packs_dir(self) -> Path:
        return self.dirs.packs_dir

    @property
    def cache_dir(self) -> Path:
        return self.dirs.cache_dir

    @property
    def samples_dir(self) -> Path:
        return self.dirs.samples_dir

    @property
    def web_dist_dir(self) -> Path:
        return self.dirs.web_dist_dir

    @property
    def db_path(self) -> Path:
        return self.dirs.db_path

    def build_app(
        self,
        *,
        auth_mode: str | None = "none",
        env: dict[str, str] | None = None,
        web_dist_with_index: bool = False,
        pack_builder: Callable[..., Any] | None = None,
        memory_budget_mb: int = 3072,
        **env_overrides: str,
    ) -> FastAPI:
        """Build a fresh FastAPI app against ``self.dirs`` with explicit env overrides."""
        _clear_gridforge_env(self.monkeypatch)
        _reset_optional_web_singletons()

        self.monkeypatch.setenv("GRIDFORGE_DATA_DIR", str(self.data_dir))
        self.monkeypatch.setenv("GRIDFORGE_PACKS_DIR", str(self.packs_dir))
        self.monkeypatch.setenv("GRIDFORGE_CACHE_DIR", str(self.cache_dir))
        self.monkeypatch.setenv("GRIDFORGE_SAMPLES_DIR", str(self.samples_dir))
        self.monkeypatch.setenv("GRIDFORGE_WEB_DIST", str(self.web_dist_dir))

        if auth_mode is None:
            self.monkeypatch.delenv("GRIDFORGE_AUTH_MODE", raising=False)
        else:
            self.monkeypatch.setenv("GRIDFORGE_AUTH_MODE", auth_mode)

        if auth_mode == "hosted":
            self.monkeypatch.setenv("GRIDFORGE_SESSION_SECRET", self.session_secret)
            self.monkeypatch.setenv("GRIDFORGE_ADMIN_EMAIL", self.admin_email)
            self.monkeypatch.setenv("GRIDFORGE_ADMIN_PASSWORD", self.admin_password)

        merged_env: dict[str, str] = {}
        if env:
            merged_env.update(env)
        if env_overrides:
            merged_env.update(env_overrides)

        for key, val in merged_env.items():
            if val is None:
                self.monkeypatch.delenv(key, raising=False)
            else:
                self.monkeypatch.setenv(key, str(val))

        if web_dist_with_index:
            self.web_dist_dir.mkdir(parents=True, exist_ok=True)
            assets_dir = self.web_dist_dir / "assets"
            assets_dir.mkdir(parents=True, exist_ok=True)
            (self.web_dist_dir / "index.html").write_text(
                "<!doctype html><html><body><div id='app'>GridForge</div></body></html>",
                encoding="utf-8",
            )
            (assets_dir / "app.js").write_text("console.log('gridforge');\n", encoding="utf-8")

        resolved_builder = (
            pack_builder if pack_builder is not None else _default_offline_pack_builder
        )
        # Lazy-import gridforge.web.app after env sanitization & isolated dirs are configured
        # so pytest collection never triggers module-level create_app() with dirty shell env.
        from gridforge.web import app as app_mod

        return app_mod.create_app(
            data_dir=self.data_dir,
            packs_dir=self.packs_dir,
            web_dist_dir=self.web_dist_dir,
            memory_budget_mb=memory_budget_mb,
            pack_builder=resolved_builder,
        )

    def new_client(
        self,
        app: FastAPI,
        *,
        base_url: str = "http://localhost:8080",
        headers: dict[str, str] | None = None,
    ) -> TestClient:
        """Create an isolated ``TestClient`` with its own cookie jar and tracked lifecycle."""
        client = TestClient(app, base_url=base_url, headers=headers or {})
        client.__enter__()
        self._clients.append(client)
        return client

    def login_admin(
        self,
        app: FastAPI,
        *,
        base_url: str = "http://localhost:8080",
    ) -> TestClient:
        """Return an authenticated Admin ``TestClient`` via ``POST /api/auth/login``."""
        client = self.new_client(app, base_url=base_url)
        resp = client.post(
            "/api/auth/login",
            json={"email": self.admin_email, "password": self.admin_password},
            headers=same_origin_headers(base_url),
        )
        assert resp.status_code == 200, f"Admin login failed: {resp.status_code} {resp.text}"
        return client

    def invite_and_accept_friend(
        self,
        app: FastAPI,
        admin_client: TestClient,
        *,
        email: str = "friend@example.com",
        password: str = "FriendPass!234",
        display_name: str = "Trail Buddy",
        role: str = "friend",
        base_url: str = "http://localhost:8080",
    ) -> tuple[TestClient, dict[str, Any]]:
        """Onboard an invited user via public HTTP admin invite + accept-invite endpoints."""
        headers = same_origin_headers(base_url)
        inv_resp = admin_client.post(
            "/api/admin/invites",
            json={"email": email, "role": role},
            headers=headers,
        )
        assert inv_resp.status_code == 201, f"Invite create failed: {inv_resp.status_code} {inv_resp.text}"
        token = inv_resp.json()["token"]

        friend_client = self.new_client(app, base_url=base_url)
        accept_resp = friend_client.post(
            "/api/auth/accept-invite",
            json={
                "token": token,
                "email": email,
                "password": password,
                "display_name": display_name,
            },
            headers=headers,
        )
        assert accept_resp.status_code == 200, (
            f"Invite acceptance failed: {accept_resp.status_code} {accept_resp.text}"
        )
        return (friend_client, accept_resp.json())

    def make_guest_client(
        self,
        app: FastAPI,
        *,
        base_url: str = "http://localhost:8080",
        headers: dict[str, str] | None = None,
    ) -> TestClient:
        """Create a fresh guest ``TestClient`` and initialize its signed guest cookie."""
        client = self.new_client(app, base_url=base_url, headers=headers)
        sess = client.get("/api/auth/session")
        assert sess.status_code == 200, f"Guest session bootstrap failed: {sess.status_code} {sess.text}"
        return client

    def seed_synthetic_pack(
        self, pack_id: str = "blue-mountains", *, with_basemap: bool = False
    ) -> Path:
        return seed_synthetic_pack(self.packs_dir, pack_id=pack_id, with_basemap=with_basemap)

    def seed_fake_pack(
        self, pack_id: str = "blue-mountains", *, with_basemap: bool = False
    ) -> Path:
        return seed_fake_pack(self.packs_dir, pack_id=pack_id, with_basemap=with_basemap)

    def seed_completed_job_pdf(self, job_id: str, *, filename: str = "map.pdf") -> Path:
        return seed_completed_job_pdf(self.data_dir, self.db_path, job_id, filename=filename)

    def mock_turnstile(
        self,
        _monkeypatch: pytest.MonkeyPatch | None = None,
        *,
        success: bool | None = None,
        valid_tokens: set[str] | None = None,
        error_codes: list[str] | None = None,
    ) -> OutboundHTTPMock:
        self.outbound_http.configure_turnstile(
            success=success,
            valid_tokens=valid_tokens,
            error_codes=error_codes,
        )
        return self.outbound_http

    def mock_google_oauth(
        self,
        _monkeypatch: pytest.MonkeyPatch | None = None,
        *,
        email: str = "oauth-user@example.com",
        name: str = "OAuth User",
        sub: str = "google-sub-123",
        verified_email: bool = True,
        token_error: bool = False,
    ) -> OutboundHTTPMock:
        self.outbound_http.configure_google_oauth(
            email=email,
            name=name,
            sub=sub,
            verified_email=verified_email,
            token_error=token_error,
        )
        return self.outbound_http

    def mock_nominatim(
        self,
        _monkeypatch: pytest.MonkeyPatch | None = None,
        *,
        offline: bool = False,
        results: list[dict[str, Any]] | None = None,
    ) -> OutboundHTTPMock:
        _reset_optional_web_singletons()
        self.outbound_http.nominatim_offline = offline
        if results is not None:
            self.outbound_http.nominatim_results = results
        return self.outbound_http

    def age_sqlite_rows(
        self,
        *,
        table: str,
        id_col: str | None = None,
        row_id: str | None = None,
        hours_ago: float | None = None,
        age_hours: float | None = None,
        columns: tuple[str, ...] = ("created_at", "finished_at", "updated_at", "expires_at"),
    ) -> None:
        age_sqlite_timestamps(
            self.db_path,
            table=table,
            id_col=id_col,
            row_id=row_id,
            hours_ago=hours_ago,
            age_hours=age_hours,
            columns=columns,
        )

    def age_sqlite_timestamps(
        self,
        *,
        table: str,
        id_col: str | None = None,
        row_id: str | None = None,
        age_hours: float | None = None,
        hours_ago: float | None = None,
        columns: tuple[str, ...] = ("created_at", "finished_at", "updated_at", "expires_at"),
    ) -> None:
        age_sqlite_timestamps(
            self.db_path,
            table=table,
            id_col=id_col,
            row_id=row_id,
            age_hours=age_hours,
            hours_ago=hours_ago,
            columns=columns,
        )

    def close_all(self) -> None:
        while self._clients:
            client = self._clients.pop()
            try:
                client.__exit__(None, None, None)
            except Exception:
                pass


@pytest.fixture()
def e2e_env(
    e2e_dirs: E2EDirs,
    monkeypatch: pytest.MonkeyPatch,
    mock_outbound_http: OutboundHTTPMock,
) -> Iterator[E2EEnv]:
    """Provide an isolated ``E2EEnv`` harness with clean client teardown."""
    env = E2EEnv(dirs=e2e_dirs, monkeypatch=monkeypatch, outbound_http=mock_outbound_http)
    try:
        yield env
    finally:
        env.close_all()


@dataclass
class E2EAppContext:
    """Bundled FastAPI application + E2EEnv returned by ``make_e2e_app``."""

    app: FastAPI
    env: E2EEnv
    data_dir: Path
    packs_dir: Path
    cache_dir: Path
    samples_dir: Path
    web_dist_dir: Path
    db_path: Path

    def new_client(
        self,
        *,
        base_url: str = "http://localhost:8080",
        headers: dict[str, str] | None = None,
    ) -> TestClient:
        return self.env.new_client(self.app, base_url=base_url, headers=headers)


@pytest.fixture()
def make_e2e_app(e2e_env: E2EEnv) -> Callable[..., E2EAppContext]:
    """Factory fixture building an isolated FastAPI app and returning ``E2EAppContext``."""

    def _factory(
        *,
        auth_mode: str | None = "none",
        env: dict[str, str] | None = None,
        web_dist_with_index: bool = False,
        pack_builder: Callable[..., Any] | None = None,
        memory_budget_mb: int = 3072,
        **env_overrides: str,
    ) -> E2EAppContext:
        app = e2e_env.build_app(
            auth_mode=auth_mode,
            env=env,
            web_dist_with_index=web_dist_with_index,
            pack_builder=pack_builder,
            memory_budget_mb=memory_budget_mb,
            **env_overrides,
        )
        return E2EAppContext(
            app=app,
            env=e2e_env,
            data_dir=e2e_env.data_dir,
            packs_dir=e2e_env.packs_dir,
            cache_dir=e2e_env.cache_dir,
            samples_dir=e2e_env.samples_dir,
            web_dist_dir=e2e_env.web_dist_dir,
            db_path=e2e_env.db_path,
        )

    return _factory


@pytest.fixture()
def none_client(make_e2e_app: Callable[..., E2EAppContext]) -> tuple[TestClient, E2EAppContext]:
    """Return ``(client, ctx)`` configured in ``GRIDFORGE_AUTH_MODE=none``."""
    ctx = make_e2e_app(auth_mode="none")
    client = ctx.new_client(base_url="http://localhost:8080")
    return (client, ctx)


@pytest.fixture()
def hosted_app_env(
    make_e2e_app: Callable[..., E2EAppContext],
    mock_turnstile: OutboundHTTPMock,
) -> E2EAppContext:
    """Return an ``E2EAppContext`` configured in ``GRIDFORGE_AUTH_MODE=hosted``."""
    _ = mock_turnstile
    return make_e2e_app(auth_mode="hosted")


@pytest.fixture()
def guest_client(hosted_app_env: E2EAppContext) -> TestClient:
    """Return an unauthenticated guest ``TestClient`` with signed guest cookie."""
    return hosted_app_env.env.make_guest_client(hosted_app_env.app)


@pytest.fixture()
def admin_client(hosted_app_env: E2EAppContext) -> TestClient:
    """Return a logged-in admin ``TestClient``."""
    return hosted_app_env.env.login_admin(hosted_app_env.app)


@pytest.fixture()
def create_invited_friend(
    hosted_app_env: E2EAppContext,
    admin_client: TestClient,
) -> Callable[..., tuple[TestClient, dict[str, Any]]]:
    """Factory fixture onboarding an invited Friend user via public HTTP endpoints."""

    def _create(
        email: str = "friend@example.com",
        password: str = "FriendPass!234",
        display_name: str = "Trail Buddy",
    ) -> tuple[TestClient, dict[str, Any]]:
        return hosted_app_env.env.invite_and_accept_friend(
            hosted_app_env.app,
            admin_client,
            email=email,
            password=password,
            display_name=display_name,
        )

    return _create


@pytest.fixture(name="age_sqlite_rows")
def _age_sqlite_rows_fixture() -> Callable[..., None]:
    """Fixture returning ``age_sqlite_timestamps`` helper for deterministic TTL tests."""
    return age_sqlite_timestamps


@pytest.fixture(name="seed_fake_pack")
def _seed_fake_pack_fixture() -> Callable[..., Path]:
    """Fixture returning ``seed_synthetic_pack`` helper."""
    return seed_synthetic_pack


@pytest.fixture(name="seed_completed_job_pdf")
def _seed_completed_job_pdf_fixture() -> Callable[..., Path]:
    """Fixture returning ``seed_completed_job_pdf`` helper."""
    return seed_completed_job_pdf


@pytest.fixture()
def mock_preview_dem(
    monkeypatch: pytest.MonkeyPatch,
    synthetic_fixtures: SyntheticFixtures,
) -> None:
    """Stub DEM source and terrain computation for fast offline viewshed/RF preview tests."""

    class _FakeElevSource:
        def fetch_dem(self, _layout: Any, _work_dir: Path) -> Path:
            return synthetic_fixtures.dem_tif

    def _fake_compute_viewshed_geojson(
        dem_utm_tif: Path, observer: Any, utm_epsg: int, work_dir: Path
    ) -> dict[str, Any]:
        assert dem_utm_tif.is_file()
        assert utm_epsg > 0
        return {
            "type": "FeatureCollection",
            "properties": {
                "label": observer.label,
                "observer_elev_m": 640.0,
                "visible_area_km2": 2.45,
            },
            "features": [
                {
                    "type": "Feature",
                    "properties": {"label": observer.label, "visible": 1},
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            [
                                [150.300, -33.740],
                                [150.324, -33.740],
                                [150.324, -33.724],
                                [150.300, -33.724],
                                [150.300, -33.740],
                            ]
                        ],
                    },
                }
            ],
        }

    def _fake_compute_rf_link_profile(
        dem_tif: Path, link: Any, utm_epsg: int, **kwargs: Any
    ) -> dict[str, Any]:
        assert dem_tif.is_file()
        assert utm_epsg > 0
        return {
            "status": "CLEAR",
            "distance_km": 4.35,
            "freq_mhz": float(link.freq_mhz),
            "fspl_db": 104.4,
            "min_clearance_m": 28.5,
            "min_fresnel_pct": 145.0,
            "los_clear": True,
            "fresnel_60pct_clear": True,
            "bearing_true_deg": 40.0,
            "bearing_grid_deg": 39.2,
            "bearing_mag_deg": 27.5,
            "bearing_mils": 697.0,
            "from_elev_m": 650.0,
            "to_elev_m": 610.0,
            "from_mgrs": "56H KH 5100 6600",
            "to_mgrs": "56H KH 5380 6930",
            "from_label": link.from_label,
            "to_label": link.to_label,
            "from_height_m": link.from_height_m,
            "to_height_m": link.to_height_m,
            "samples": [
                {
                    "dist_km": 0.0,
                    "terrain_m": 650.0,
                    "los_m": 652.0,
                    "fresnel_lo_m": 652.0,
                    "fresnel_hi_m": 652.0,
                    "fresnel60_lo_m": 652.0,
                    "clearance_m": 2.0,
                },
                {
                    "dist_km": 4.35,
                    "terrain_m": 610.0,
                    "los_m": 612.0,
                    "fresnel_lo_m": 612.0,
                    "fresnel_hi_m": 612.0,
                    "fresnel60_lo_m": 612.0,
                    "clearance_m": 2.0,
                },
            ],
        }

    monkeypatch.setattr(
        "gridforge.web.viewshed._resolve_preview_elevation_source",
        lambda *a, **kw: _FakeElevSource(),
    )
    monkeypatch.setattr(
        "gridforge.pipeline.terrain.compute_viewshed_geojson",
        _fake_compute_viewshed_geojson,
        raising=False,
    )
    monkeypatch.setattr(
        "gridforge.pipeline.terrain.compute_rf_link_profile",
        _fake_compute_rf_link_profile,
        raising=False,
    )


def login_admin(app: FastAPI, env: E2EEnv, *, base_url: str = "http://localhost:8080") -> TestClient:
    """Module-level convenience wrapper for ``env.login_admin(app)``."""
    return env.login_admin(app, base_url=base_url)


def invite_and_accept_friend(
    app: FastAPI,
    env: E2EEnv,
    admin_client: TestClient,
    *,
    email: str = "friend@example.com",
    password: str = "FriendPass!234",
    display_name: str = "Trail Buddy",
    base_url: str = "http://localhost:8080",
) -> tuple[TestClient, dict[str, Any]]:
    """Module-level convenience wrapper for ``env.invite_and_accept_friend(...)``."""
    return env.invite_and_accept_friend(
        app,
        admin_client,
        email=email,
        password=password,
        display_name=display_name,
        base_url=base_url,
    )


def make_guest_client(
    app: FastAPI, env: E2EEnv, *, base_url: str = "http://localhost:8080"
) -> TestClient:
    """Module-level convenience wrapper for ``env.make_guest_client(app)``."""
    return env.make_guest_client(app, base_url=base_url)
