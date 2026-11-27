"""Opaque-box E2E tests (Tiers 1 & 2) for Worker, Entrypoint, WAL, Role TTL & LRU Cache.

Covers `PROJECT.md § Feature Inventory`:
  - Feature #8: Single-Container Entrypoint, Docker Config & WAL Concurrency (6 Tier 1 + 5 Tier 2)
  - Feature #9: Role-Aware TTL & LRU Cache Pruning (5 Tier 1 + 6 Tier 2)

Baseline tests (without `@pytest.mark.m1_contract`) verify landed M1 DB/worker/Docker
invariants and pass immediately. Tests tagged `@pytest.mark.m1_contract` assert the
M2 deliverables (`cache_prune.py`, `worker.py` role TTL/LRU wiring, single-container
entrypoint & `/data/cache` Docker config).
"""

from __future__ import annotations

import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter

from tests.e2e.conftest import same_origin_headers, set_file_mtime_atime

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SAME_ORIGIN = same_origin_headers("http://localhost:8080")


def _write_minimal_pdf(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    writer.add_blank_page(width=595.28, height=841.89)
    with path.open("wb") as fh:
        writer.write(fh)
    writer.close()


def _mock_worker_render(
    monkeypatch: pytest.MonkeyPatch,
    *,
    on_prepare: Any | None = None,
    render_error: Exception | None = None,
) -> dict[str, Any]:
    """Patch worker PyQGIS/workspace calls cleanly without importing PyQGIS."""
    from gridforge.config import RenderResult
    from gridforge.geo.crs import compute_sheet_layout
    from gridforge.web import worker as worker_mod

    state: dict[str, Any] = {"prepare_calls": 0, "render_calls": 0, "work_dirs": []}

    def _fake_prepare(spec, job_dir, **kwargs):
        state["prepare_calls"] += 1
        state["work_dirs"].append(Path(job_dir))
        if on_prepare is not None:
            on_prepare(spec, Path(job_dir), kwargs)
        return compute_sheet_layout(spec)

    def _fake_render_pdf(self, workspace, output_pdf: Path):
        state["render_calls"] += 1
        if render_error is not None:
            raise render_error
        _write_minimal_pdf(Path(output_pdf))
        return RenderResult(
            output_pdf=Path(output_pdf),
            page_width_mm=210.0,
            page_height_mm=297.0,
            scale=25000,
            utm_epsg=32756,
            elapsed_seconds=0.05,
        )

    monkeypatch.setattr(worker_mod.runner, "prepare_job_workspace", _fake_prepare)
    monkeypatch.setattr(worker_mod.PyQGISRenderer, "render_pdf", _fake_render_pdf)
    return state


def _seed_terminal_job(
    db_path: Path,
    data_dir: Path,
    *,
    job_id: str,
    owner_id: str,
    owner_role: str,
    age_hours: float,
    status: str = "succeeded",
) -> Path:
    from gridforge.config import ColorMode, Orientation, PaperSize
    from gridforge.web import db

    db.init_db(db_path)
    db.create_job(
        db_path,
        job_id=job_id,
        title=f"Job {job_id}",
        center="-33.732, 150.312",
        center_mgrs="56H KH 5095 6431",
        scale=25000,
        paper=PaperSize.A4,
        orientation=Orientation.PORTRAIT,
        color_mode=ColorMode.COLOR,
        request_json=json.dumps({"center": "-33.732, 150.312", "outputs": {"pdf": True}}),
        owner_id=owner_id,
        owner_role=owner_role,
    )
    job_dir = data_dir / job_id
    art_dir = job_dir / "artifacts"
    _write_minimal_pdf(art_dir / "map.pdf")

    target_iso = (datetime.now(timezone.utc) - timedelta(hours=age_hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    conn = sqlite3.connect(str(db_path), timeout=15.0)
    try:
        conn.execute(
            "UPDATE jobs SET status = ?, stage = ?, progress_pct = 100, "
            "created_at = ?, finished_at = ? WHERE job_id = ?",
            (status, status, target_iso, target_iso, job_id),
        )
        conn.commit()
    finally:
        conn.close()
    return job_dir


# =============================================================================
# Feature #8: Single-Container Entrypoint, Docker Config & WAL Concurrency
# (6 Tier 1 + 5 Tier 2 = 11 tests)
# =============================================================================


class TestEntrypointDockerWalTier1:
    """Tier 1 tests for Docker/Compose configuration, worker execution & WAL concurrency."""

    def test_t1_compose_local_first_defaults_and_service_layout(self) -> None:
        for fname in ("docker-compose.yml", "docker-compose.ghcr.yml"):
            text = (_REPO_ROOT / fname).read_text(encoding="utf-8")
            assert "127.0.0.1:" in text, f"{fname} must bind ports to 127.0.0.1"
            assert re.search(r"FORWARDED_ALLOW_IPS=\$\{FORWARDED_ALLOW_IPS:-127\.0\.0\.1\}", text)
            assert "GRIDFORGE_AUTH_MODE=hosted" not in text
            assert "QT_QPA_PLATFORM=offscreen" in text
            assert "GRIDFORGE_WORKER_CONCURRENCY=1" in text
            assert "/data/jobs" in text and "/data/packs" in text
            assert "mem_limit: 512m" in text
            assert "mem_limit: 3g" in text
            assert "uvicorn gridforge.web.app:app" in text or '"uvicorn", "gridforge.web.app:app"' in text
            assert "gridforge.web.worker" in text

    def test_t1_dockerfile_headless_runtime_and_web_dist_build(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dockerfile = (_REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
        assert "FROM node:22-slim AS frontend" in dockerfile
        assert "COPY --from=frontend /web/dist /app/web/dist" in dockerfile
        assert "python3-qgis" in dockerfile
        assert re.search(r"^\s*qgis\s*\\?$", dockerfile, flags=re.MULTILINE) is None
        assert "QT_QPA_PLATFORM=offscreen" in dockerfile
        assert "GDAL_CACHEMAX=192" in dockerfile
        assert "OMP_THREAD_LIMIT=1" in dockerfile
        assert "EXPOSE 8080" in dockerfile
        for data_sub in ("/data/jobs", "/data/packs", "/data/cache", "/data/samples"):
            assert data_sub in dockerfile
        assert "ENTRYPOINT" not in dockerfile
        assert "HEALTHCHECK" in dockerfile
        assert "/api/health" in dockerfile
        assert 'CMD ["python3", "-m", "gridforge.web.entrypoint"]' in dockerfile
        assert "GRIDFORGE_DATA_DIR=/data/jobs" not in dockerfile
        assert "GRIDFORGE_PACKS_DIR=/data/packs" not in dockerfile
        assert "GRIDFORGE_CACHE_DIR=/tmp/gridforge-cache" not in dockerfile

        from gridforge.web import entrypoint

        monkeypatch.setattr(Path, "mkdir", lambda self, *args, **kwargs: None)
        default_env, default_host, default_port = entrypoint.configure_environment(
            {},
            seed_samples_dir=tmp_path / "no_samples",
        )
        assert (default_host, default_port) == ("0.0.0.0", 8080)
        resolved_data_root = Path("/data").resolve()
        assert default_env["GRIDFORGE_DATA_DIR"] == str(resolved_data_root / "jobs")
        assert default_env["GRIDFORGE_PACKS_DIR"] == str(resolved_data_root / "packs")
        assert default_env["GRIDFORGE_CACHE_DIR"] == str(resolved_data_root / "cache")
        assert default_env["GRIDFORGE_SAMPLES_DIR"] == str(resolved_data_root / "samples")

    def test_t1_worker_once_claims_renders_and_updates_health_heartbeat(
        self, e2e_env, monkeypatch
    ) -> None:
        from gridforge.web import worker as worker_mod

        _mock_worker_render(monkeypatch)
        app = e2e_env.build_app(auth_mode="none")
        client = e2e_env.new_client(app)

        created = client.post(
            "/api/jobs",
            json={"center": "-33.732, 150.312", "title": "Worker Render", "outputs": {"pdf": True}},
            headers=_SAME_ORIGIN,
        )
        assert created.status_code == 202
        job_id = created.json()["job_id"]

        rc = worker_mod.main(
            [
                "--data-dir",
                str(e2e_env.data_dir),
                "--packs-dir",
                str(e2e_env.packs_dir),
                "--worker-id",
                "e2e-worker-1",
                "--once",
            ]
        )
        assert rc == 0

        health = client.get("/api/health").json()
        assert health["worker_alive"] is True
        assert health["queue_depth"] == 0

        job = client.get(f"/api/jobs/{job_id}").json()
        assert job["status"] == "succeeded"
        assert len(job["artifacts"]) == 1
        pdf_fname = job["artifacts"][0]["filename"]

        pdf_resp = client.get(f"/api/jobs/{job_id}/artifacts/{pdf_fname}")
        assert pdf_resp.status_code == 200
        assert pdf_resp.content.startswith(b"%PDF-")

        thumb_resp = client.get(f"/api/jobs/{job_id}/thumbnail.png")
        assert thumb_resp.status_code == 200
        assert thumb_resp.headers["content-type"] == "image/png"

        assert "qgis" not in sys.modules
        assert "PyQt5" not in sys.modules

    def test_t1_shared_wal_concurrent_api_and_worker_reads_writes(
        self, e2e_env, monkeypatch
    ) -> None:
        from gridforge.web import worker as worker_mod

        _mock_worker_render(monkeypatch)
        app = e2e_env.build_app(auth_mode="hosted")
        admin = e2e_env.login_admin(app)
        admin_cookies = dict(admin.cookies)

        job_ids: list[str] = []
        for idx in range(3):
            r = admin.post(
                "/api/jobs",
                json={"center": "-33.732, 150.312", "title": f"WAL Job {idx}"},
                headers=_SAME_ORIGIN,
            )
            assert r.status_code == 202
            job_ids.append(r.json()["job_id"])

        errors: list[BaseException] = []

        def _api_writer(thread_idx: int) -> None:
            try:
                with TestClient(app, base_url="http://localhost:8080") as cli:
                    cli.cookies.update(admin_cookies)
                    for step in range(4):
                        p = cli.post(
                            "/api/projects",
                            json={"name": f"WAL-T{thread_idx}-P{step}", "state": {"step": step}},
                            headers=_SAME_ORIGIN,
                        )
                        assert p.status_code == 201, p.text
                        assert cli.get("/api/jobs").status_code == 200
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=_api_writer, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()

        # Worker runs on main thread (signal handlers require main thread) concurrently with API threads
        for _ in range(3):
            assert (
                worker_mod.main(
                    [
                        "--data-dir",
                        str(e2e_env.data_dir),
                        "--packs-dir",
                        str(e2e_env.packs_dir),
                        "--once",
                    ]
                )
                == 0
            )

        for t in threads:
            t.join(timeout=10.0)
            assert not t.is_alive()

        assert not errors, f"Concurrent WAL thread errors: {errors!r}"

        conn = sqlite3.connect(str(e2e_env.db_path))
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            assert str(mode).lower() == "wal"
        finally:
            conn.close()

        for jid in job_ids:
            assert admin.get(f"/api/jobs/{jid}").json()["status"] == "succeeded"
        assert len(admin.get("/api/projects").json()["projects"]) == 8

    @pytest.mark.m1_contract
    def test_t1_single_container_entrypoint_and_data_cache_contract_m1_contract(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for var in (
            "PORT",
            "RAILWAY_ENVIRONMENT_ID",
            "RAILWAY_SERVICE_ID",
            "RAILWAY_PROJECT_ID",
            "RAILWAY_PUBLIC_DOMAIN",
        ):
            monkeypatch.delenv(var, raising=False)

        dockerfile = (_REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
        for data_sub in ("/data/jobs", "/data/packs", "/data/cache", "/data/samples"):
            assert data_sub in dockerfile
        assert "GRIDFORGE_CACHE_DIR=/tmp/gridforge-cache" not in dockerfile
        assert "GRIDFORGE_DATA_DIR=/data/jobs" not in dockerfile
        assert "GRIDFORGE_PACKS_DIR=/data/packs" not in dockerfile
        assert "ENTRYPOINT" not in dockerfile
        assert "HEALTHCHECK" in dockerfile
        assert "/api/health" in dockerfile
        assert 'CMD ["python3", "-m", "gridforge.web.entrypoint"]' in dockerfile

        py_entry = _REPO_ROOT / "src" / "gridforge" / "web" / "entrypoint.py"
        assert py_entry.is_file(), (
            "Expected M2 single-container entrypoint at src/gridforge/web/entrypoint.py"
        )

        from gridforge.web import entrypoint

        # Default derivation (GRIDFORGE_VOLUME_ROOT unset, env=None) without touching host /data
        with monkeypatch.context() as mp:
            mp.setattr(Path, "mkdir", lambda self, *args, **kwargs: None)
            default_env, default_host, default_port = entrypoint.configure_environment(
                seed_samples_dir=tmp_path / "no_samples",
            )
        assert (default_host, default_port) == ("0.0.0.0", 8080)
        resolved_data_root = Path("/data").resolve()
        assert default_env["GRIDFORGE_DATA_DIR"] == str(resolved_data_root / "jobs")
        assert default_env["GRIDFORGE_PACKS_DIR"] == str(resolved_data_root / "packs")
        assert default_env["GRIDFORGE_CACHE_DIR"] == str(resolved_data_root / "cache")
        assert default_env["GRIDFORGE_SAMPLES_DIR"] == str(resolved_data_root / "samples")
        assert default_env["FORWARDED_ALLOW_IPS"] == "127.0.0.1"

        # GRIDFORGE_VOLUME_ROOT env var derivation + real directory creation
        vol_root = tmp_path / "data"
        monkeypatch.setenv("GRIDFORGE_VOLUME_ROOT", str(vol_root))
        configured, host, port = entrypoint.configure_environment(
            seed_samples_dir=tmp_path / "no_samples",
        )
        assert (host, port) == ("0.0.0.0", 8080)
        assert configured["GRIDFORGE_DATA_DIR"] == str((vol_root / "jobs").resolve())
        assert configured["GRIDFORGE_PACKS_DIR"] == str((vol_root / "packs").resolve())
        assert configured["GRIDFORGE_CACHE_DIR"] == str((vol_root / "cache").resolve())
        assert configured["GRIDFORGE_SAMPLES_DIR"] == str((vol_root / "samples").resolve())
        assert configured["FORWARDED_ALLOW_IPS"] == "127.0.0.1"
        for sub in ("jobs", "packs", "cache", "samples"):
            assert (vol_root / sub).is_dir()

        # Explicit dir overrides take precedence over GRIDFORGE_VOLUME_ROOT while siblings still derive
        custom_jobs = tmp_path / "custom_jobs"
        custom_cache = tmp_path / "custom_cache"
        monkeypatch.setenv("GRIDFORGE_DATA_DIR", str(custom_jobs))
        monkeypatch.setenv("GRIDFORGE_CACHE_DIR", str(custom_cache))
        monkeypatch.setenv("GRIDFORGE_AUTH_MODE", "hosted")
        overridden, ov_host, ov_port = entrypoint.configure_environment(
            seed_samples_dir=tmp_path / "no_samples",
        )
        assert (ov_host, ov_port) == ("0.0.0.0", 8080)
        assert overridden["GRIDFORGE_DATA_DIR"] == str(custom_jobs)
        assert overridden["GRIDFORGE_CACHE_DIR"] == str(custom_cache)
        assert custom_jobs.is_dir()
        assert custom_cache.is_dir()
        assert overridden["GRIDFORGE_PACKS_DIR"] == str((vol_root / "packs").resolve())
        assert overridden["GRIDFORGE_SAMPLES_DIR"] == str((vol_root / "samples").resolve())
        assert overridden["FORWARDED_ALLOW_IPS"] == entrypoint.DEFAULT_HOSTED_FORWARDED_ALLOW_IPS

        monkeypatch.setenv("FORWARDED_ALLOW_IPS", "10.0.0.1")
        explicit_fwd, _, _ = entrypoint.configure_environment(
            seed_samples_dir=tmp_path / "no_samples",
        )
        assert explicit_fwd["FORWARDED_ALLOW_IPS"] == "10.0.0.1"

        web_cmd, worker_cmd = entrypoint.build_commands(default_host, default_port)
        assert "uvicorn" in web_cmd and "gridforge.web.app:app" in web_cmd
        assert "--host" in web_cmd and "0.0.0.0" in web_cmd
        assert "--port" in web_cmd and "8080" in web_cmd
        assert "--proxy-headers" in web_cmd
        assert "gridforge.web.worker" in worker_cmd

    def test_t1_worker_startup_recovers_orphaned_running_jobs(
        self, e2e_env, monkeypatch
    ) -> None:
        from gridforge.config import ColorMode, Orientation, PaperSize
        from gridforge.web import db, worker as worker_mod

        _mock_worker_render(monkeypatch)
        db.init_db(e2e_env.db_path)

        # Simulate a job left stuck in `running` from an earlier crash
        db.create_job(
            e2e_env.db_path,
            job_id="orphanjob001",
            title="Orphaned",
            center="-33.732, 150.312",
            center_mgrs="56H KH 5095 6431",
            scale=25000,
            paper=PaperSize.A4,
            orientation=Orientation.PORTRAIT,
            color_mode=ColorMode.COLOR,
            request_json=json.dumps({"center": "-33.732, 150.312"}),
        )
        claimed = db.claim_next_job(e2e_env.db_path)
        assert claimed is not None and claimed.record.job_id == "orphanjob001"

        # Queue a fresh job
        db.create_job(
            e2e_env.db_path,
            job_id="freshjob0002",
            title="Fresh",
            center="-33.732, 150.312",
            center_mgrs="56H KH 5095 6431",
            scale=25000,
            paper=PaperSize.A4,
            orientation=Orientation.PORTRAIT,
            color_mode=ColorMode.COLOR,
            request_json=json.dumps({"center": "-33.732, 150.312", "outputs": {"pdf": True}}),
        )

        assert (
            worker_mod.main(
                [
                    "--data-dir",
                    str(e2e_env.data_dir),
                    "--packs-dir",
                    str(e2e_env.packs_dir),
                    "--cache-dir",
                    str(e2e_env.cache_dir),
                    "--once",
                ]
            )
            == 0
        )

        orphan = db.get_job(e2e_env.db_path, "orphanjob001")
        fresh = db.get_job(e2e_env.db_path, "freshjob0002")
        assert orphan is not None and orphan.status.value == "failed"
        assert orphan.error == "Worker restarted before job completed"
        assert fresh is not None and fresh.status.value == "succeeded"


class TestEntrypointDockerWalTier2:
    """Tier 2 edge/error tests for worker failure/cancellation, WAL busy_timeout & entrypoint."""

    def test_t2_worker_cancelled_job_aborts_before_completion(
        self, e2e_env, monkeypatch
    ) -> None:
        from gridforge.config import ColorMode, Orientation, PaperSize
        from gridforge.web import db, worker as worker_mod

        db.init_db(e2e_env.db_path)
        db.create_job(
            e2e_env.db_path,
            job_id="cancelmid001",
            title="Cancel Mid",
            center="-33.732, 150.312",
            center_mgrs="56H KH 5095 6431",
            scale=25000,
            paper=PaperSize.A4,
            orientation=Orientation.PORTRAIT,
            color_mode=ColorMode.COLOR,
            request_json=json.dumps({"center": "-33.732, 150.312", "outputs": {"pdf": True}}),
        )
        claimed = db.claim_next_job(e2e_env.db_path)
        assert claimed is not None

        def _cancel_during_prepare(_spec, _work_dir, _kw) -> None:
            db.cancel_job(e2e_env.db_path, "cancelmid001")

        state = _mock_worker_render(monkeypatch, on_prepare=_cancel_during_prepare)
        res = worker_mod.process_single_job(
            claimed,
            db_path=e2e_env.db_path,
            data_dir=e2e_env.data_dir,
            packs_dir=e2e_env.packs_dir,
        )
        assert res is not None
        assert res.status.value == "cancelled"
        assert state["render_calls"] == 0

    def test_t2_worker_failing_render_records_bounded_error_and_cleans_tmpdir(
        self, e2e_env, monkeypatch
    ) -> None:
        from gridforge.config import ColorMode, Orientation, PaperSize
        from gridforge.web import db, worker as worker_mod

        db.init_db(e2e_env.db_path)
        db.create_job(
            e2e_env.db_path,
            job_id="failjob0001",
            title="Fail Render",
            center="-33.732, 150.312",
            center_mgrs="56H KH 5095 6431",
            scale=25000,
            paper=PaperSize.A4,
            orientation=Orientation.PORTRAIT,
            color_mode=ColorMode.COLOR,
            request_json=json.dumps({"center": "-33.732, 150.312", "outputs": {"pdf": True}}),
        )
        claimed = db.claim_next_job(e2e_env.db_path)
        assert claimed is not None

        state = _mock_worker_render(monkeypatch, render_error=RuntimeError("X" * 5000))
        res = worker_mod.process_single_job(
            claimed,
            db_path=e2e_env.db_path,
            data_dir=e2e_env.data_dir,
            packs_dir=e2e_env.packs_dir,
        )
        assert res is not None
        assert res.status.value == "failed"
        assert res.error is not None and len(res.error) <= 2000
        assert res.elapsed_seconds is not None
        assert res.peak_rss_mb is not None and res.peak_rss_mb > 0
        # Temp gf_worker_* dir is always removed in finally
        assert len(state["work_dirs"]) == 1
        assert not state["work_dirs"][0].exists()
        assert list(Path(tempfile.gettempdir()).glob("gf_worker_failjob0001_*")) == []

    def test_t2_worker_once_exits_cleanly_when_queue_empty(self, e2e_env) -> None:
        from gridforge.web import db, worker as worker_mod

        rc = worker_mod.main(
            [
                "--data-dir",
                str(e2e_env.data_dir),
                "--packs-dir",
                str(e2e_env.packs_dir),
                "--cache-dir",
                str(e2e_env.cache_dir),
                "--worker-id",
                "empty-worker",
                "--once",
            ]
        )
        assert rc == 0
        alive, last_seen = db.get_heartbeat(e2e_env.db_path)
        assert alive is True
        assert last_seen is not None

    def test_t2_wal_busy_timeout_survives_writer_lock_contention(self, e2e_env) -> None:
        from gridforge.config import ColorMode, Orientation, PaperSize
        from gridforge.web import db

        db.init_db(e2e_env.db_path)
        lock_acquired = threading.Event()

        def _hold_short_write_lock() -> None:
            conn = sqlite3.connect(str(e2e_env.db_path), timeout=5.0, isolation_level=None)
            try:
                conn.execute("BEGIN IMMEDIATE")
                lock_acquired.set()
                time.sleep(0.15)
                conn.execute("COMMIT")
            finally:
                conn.close()

        holder = threading.Thread(target=_hold_short_write_lock)
        holder.start()
        assert lock_acquired.wait(timeout=2.0)

        rec = db.create_job(
            e2e_env.db_path,
            job_id="walcontention1",
            title="WAL Busy Test",
            center="-33.732, 150.312",
            center_mgrs="56H KH 5095 6431",
            scale=25000,
            paper=PaperSize.A4,
            orientation=Orientation.PORTRAIT,
            color_mode=ColorMode.COLOR,
            request_json=json.dumps({"center": "-33.732, 150.312"}),
        )
        holder.join(timeout=5.0)
        assert rec.job_id == "walcontention1"

    @pytest.mark.m1_contract
    def test_t2_entrypoint_signal_propagation_and_compose_local_first_m1_contract(
        self, tmp_path: Path
    ) -> None:
        sh_entry = _REPO_ROOT / "scripts" / "entrypoint.sh"
        py_entry = _REPO_ROOT / "src" / "gridforge" / "web" / "entrypoint.py"
        assert sh_entry.is_file() or py_entry.is_file(), (
            "Expected M2 single-container entrypoint script or module"
        )

        if sh_entry.is_file():
            syntax = subprocess.run(
                ["bash", "-n", str(sh_entry)],
                capture_output=True,
                text=True,
                check=False,
            )
            assert syntax.returncode == 0, syntax.stderr
            text = sh_entry.read_text(encoding="utf-8")
            assert "trap" in text and ("TERM" in text or "SIGTERM" in text)
        else:
            from gridforge.web import entrypoint

            vol_root = tmp_path / "vol"
            spawned: list[subprocess.Popen[bytes]] = []
            ready_files: list[Path] = []
            recorded: list[tuple[list[str], dict[str, str] | None, Path]] = []

            child_script = (
                "import pathlib, signal, sys, threading\n"
                "sig_p, rdy_p = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])\n"
                "got = []\n"
                "done = threading.Event()\n"
                "def _h(signum, _frame):\n"
                "    got.append(signum)\n"
                "    done.set()\n"
                "signal.signal(signal.SIGTERM, _h)\n"
                "rdy_p.write_text('ready', encoding='utf-8')\n"
                "done.wait(timeout=10.0)\n"
                "if got:\n"
                "    sig_p.write_text(str(got[0]), encoding='utf-8')\n"
            )

            def _spawn_dummy(
                cmd: list[str], env: dict[str, str] | None = None
            ) -> subprocess.Popen[bytes]:
                idx = len(spawned)
                sig_p = tmp_path / f"sig_{idx}.txt"
                rdy_p = tmp_path / f"rdy_{idx}.txt"
                recorded.append((list(cmd), dict(env) if env is not None else None, sig_p))
                ready_files.append(rdy_p)
                proc = subprocess.Popen(
                    [sys.executable, "-c", child_script, str(sig_p), str(rdy_p)]
                )
                spawned.append(proc)
                return proc

            def _trigger_sigterm() -> None:
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    if len(ready_files) == 2 and all(
                        p.is_file() and p.stat().st_size > 0 for p in ready_files
                    ):
                        os.kill(os.getpid(), signal.SIGTERM)
                        return
                    time.sleep(0.01)
                os.kill(os.getpid(), signal.SIGTERM)

            prev_term = signal.getsignal(signal.SIGTERM)
            trigger = threading.Thread(target=_trigger_sigterm, daemon=True)
            trigger.start()
            try:
                rc = entrypoint.supervise(
                    env={},
                    volume_root=vol_root,
                    seed_samples_dir=tmp_path / "no_samples",
                    poll_interval=0.02,
                    shutdown_grace_seconds=2.0,
                    popen_cls=_spawn_dummy,
                )
            finally:
                trigger.join(timeout=2.0)
                for proc in spawned:
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait(timeout=2.0)

            assert rc == 0
            assert signal.getsignal(signal.SIGTERM) is prev_term
            assert len(recorded) == 2
            assert "uvicorn" in recorded[0][0] and "--proxy-headers" in recorded[0][0]
            assert "gridforge.web.worker" in recorded[1][0]
            assert (
                recorded[0][1] is not None
                and recorded[0][1]["GRIDFORGE_CACHE_DIR"] == str((vol_root / "cache").resolve())
            )
            assert recorded[0][2].read_text(encoding="utf-8") == str(signal.SIGTERM)
            assert recorded[1][2].read_text(encoding="utf-8") == str(signal.SIGTERM)
            assert (vol_root / "jobs" / "gridforge_jobs.sqlite3").is_file()

        # Compose files must continue preserving local-first 127.0.0.1 and zero-login defaults
        for fname in ("docker-compose.yml", "docker-compose.ghcr.yml"):
            ctext = (_REPO_ROOT / fname).read_text(encoding="utf-8")
            assert "127.0.0.1:" in ctext
            assert "GRIDFORGE_AUTH_MODE=hosted" not in ctext


# =============================================================================
# Feature #9: Role-Aware TTL & LRU Cache Pruning (5 Tier 1 + 6 Tier 2 = 11 tests)
# =============================================================================


class TestRoleTtlAndLruCacheTier1:
    """Tier 1 tests for role-aware job/project/auth TTL pruning and LRU cache trimming."""

    def test_t1_db_role_aware_ttl_prunes_guest_24h_and_friend_14d_preserves_admin_none(
        self, e2e_env
    ) -> None:
        from gridforge.web import db

        g_old = _seed_terminal_job(
            e2e_env.db_path, e2e_env.data_dir, job_id="g_old", owner_id="guest:1", owner_role="guest", age_hours=25.0
        )
        g_new = _seed_terminal_job(
            e2e_env.db_path, e2e_env.data_dir, job_id="g_new", owner_id="guest:1", owner_role="guest", age_hours=12.0
        )
        f_old = _seed_terminal_job(
            e2e_env.db_path, e2e_env.data_dir, job_id="f_old", owner_id="usr_1", owner_role="friend", age_hours=360.0
        )
        f_new = _seed_terminal_job(
            e2e_env.db_path, e2e_env.data_dir, job_id="f_new", owner_id="usr_1", owner_role="friend", age_hours=120.0
        )
        a_old = _seed_terminal_job(
            e2e_env.db_path, e2e_env.data_dir, job_id="a_old", owner_id="usr_admin", owner_role="admin", age_hours=1440.0
        )
        n_old = _seed_terminal_job(
            e2e_env.db_path, e2e_env.data_dir, job_id="n_old", owner_id="local", owner_role="none", age_hours=1440.0
        )

        pruned = db.prune_expired_jobs(
            e2e_env.db_path,
            data_dir=e2e_env.data_dir,
            guest_ttl_hours=24.0,
            friend_ttl_hours=336.0,
        )
        assert pruned == 2
        assert not g_old.exists() and db.get_job(e2e_env.db_path, "g_old") is None
        assert not f_old.exists() and db.get_job(e2e_env.db_path, "f_old") is None
        assert g_new.exists() and db.get_job(e2e_env.db_path, "g_new") is not None
        assert f_new.exists() and db.get_job(e2e_env.db_path, "f_new") is not None
        assert a_old.exists() and db.get_job(e2e_env.db_path, "a_old") is not None
        assert n_old.exists() and db.get_job(e2e_env.db_path, "n_old") is not None

    def test_t1_db_prune_projects_sessions_and_invites_by_role(self, e2e_env) -> None:
        from gridforge.web import db, projects

        db.init_db(e2e_env.db_path)
        p_g_old = projects.create_project(
            e2e_env.db_path,
            projects.ProjectCreateRequest(name="Guest Old", state={}),
            owner_id="guest:1",
            owner_role="guest",
        ).project_id
        p_g_new = projects.create_project(
            e2e_env.db_path,
            projects.ProjectCreateRequest(name="Guest New", state={}),
            owner_id="guest:1",
            owner_role="guest",
        ).project_id
        p_f_old = projects.create_project(
            e2e_env.db_path,
            projects.ProjectCreateRequest(name="Friend Old", state={}),
            owner_id="usr_1",
            owner_role="friend",
        ).project_id
        p_f_new = projects.create_project(
            e2e_env.db_path,
            projects.ProjectCreateRequest(name="Friend New", state={}),
            owner_id="usr_1",
            owner_role="friend",
        ).project_id
        p_a_old = projects.create_project(
            e2e_env.db_path,
            projects.ProjectCreateRequest(name="Admin Old", state={}),
            owner_id="usr_admin",
            owner_role="admin",
        ).project_id
        p_n_old = projects.create_project(
            e2e_env.db_path,
            projects.ProjectCreateRequest(name="None Old", state={}),
            owner_id="local",
            owner_role="none",
        ).project_id

        e2e_env.age_sqlite_timestamps(table="projects", id_col="project_id", row_id=p_g_old, age_hours=25.0)
        e2e_env.age_sqlite_timestamps(table="projects", id_col="project_id", row_id=p_g_new, age_hours=12.0)
        e2e_env.age_sqlite_timestamps(table="projects", id_col="project_id", row_id=p_f_old, age_hours=360.0)
        e2e_env.age_sqlite_timestamps(table="projects", id_col="project_id", row_id=p_f_new, age_hours=120.0)
        e2e_env.age_sqlite_timestamps(table="projects", id_col="project_id", row_id=p_a_old, age_hours=2000.0)
        e2e_env.age_sqlite_timestamps(table="projects", id_col="project_id", row_id=p_n_old, age_hours=2000.0)

        now = datetime.now(timezone.utc)
        past_iso = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        future_iso = (now + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
        db.create_user(
            e2e_env.db_path,
            user_id="usr_1",
            email="friend@example.com",
            display_name="Friend",
            role="friend",
        )
        db.create_session(
            e2e_env.db_path,
            session_id="sess_exp",
            user_id="usr_1",
            token_hash="hash_exp",
            expires_at=past_iso,
        )
        db.create_session(
            e2e_env.db_path,
            session_id="sess_ok",
            user_id="usr_1",
            token_hash="hash_ok",
            expires_at=future_iso,
        )
        db.create_invite(
            e2e_env.db_path,
            invite_id="inv_exp",
            token_hash="ihash_exp",
            email="exp@example.com",
            role="friend",
            created_by="usr_admin",
            expires_at=past_iso,
        )
        db.create_invite(
            e2e_env.db_path,
            invite_id="inv_ok",
            token_hash="ihash_ok",
            email="ok@example.com",
            role="friend",
            created_by="usr_admin",
            expires_at=future_iso,
        )

        counts = db.prune_expired_projects_and_auth(
            e2e_env.db_path, guest_ttl_hours=24.0, friend_ttl_hours=336.0
        )
        assert counts == {"projects": 2, "sessions": 1, "invites": 1}

        remaining_pids = {p.project_id for p in projects.list_projects(e2e_env.db_path)}
        assert remaining_pids == {p_g_new, p_f_new, p_a_old, p_n_old}

    @pytest.mark.m1_contract
    def test_t1_worker_once_runs_role_aware_ttl_pruning_via_env_m1_contract(
        self, e2e_env, monkeypatch
    ) -> None:
        from gridforge.web import db, projects, worker as worker_mod

        app = e2e_env.build_app(
            auth_mode="hosted",
            GRIDFORGE_GUEST_TTL_HOURS="24",
            GRIDFORGE_FRIEND_TTL_HOURS="336",
        )
        admin = e2e_env.login_admin(app)
        friend, _ = e2e_env.invite_and_accept_friend(app, admin, email="friend@example.com")
        guest = e2e_env.make_guest_client(app)

        j_g_old = guest.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=_SAME_ORIGIN).json()["job_id"]
        e2e_env.seed_completed_job_pdf(j_g_old)
        j_f_old = friend.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=_SAME_ORIGIN).json()["job_id"]
        e2e_env.seed_completed_job_pdf(j_f_old)
        j_f_new = friend.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=_SAME_ORIGIN).json()["job_id"]
        e2e_env.seed_completed_job_pdf(j_f_new)
        j_a_old = admin.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=_SAME_ORIGIN).json()["job_id"]
        e2e_env.seed_completed_job_pdf(j_a_old)
        j_a_fresh = admin.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=_SAME_ORIGIN).json()["job_id"]
        e2e_env.seed_completed_job_pdf(j_a_fresh)

        p_g_old = guest.post("/api/projects", json={"name": "G Old", "state": {}}, headers=_SAME_ORIGIN).json()["project_id"]
        p_f_new = friend.post("/api/projects", json={"name": "F New", "state": {}}, headers=_SAME_ORIGIN).json()["project_id"]

        e2e_env.age_sqlite_timestamps(table="jobs", id_col="job_id", row_id=j_g_old, age_hours=25.0)
        e2e_env.age_sqlite_timestamps(table="jobs", id_col="job_id", row_id=j_f_old, age_hours=360.0)
        e2e_env.age_sqlite_timestamps(table="jobs", id_col="job_id", row_id=j_f_new, age_hours=48.0)
        e2e_env.age_sqlite_timestamps(table="jobs", id_col="job_id", row_id=j_a_old, age_hours=1000.0)
        e2e_env.age_sqlite_timestamps(table="jobs", id_col="job_id", row_id=j_a_fresh, age_hours=200.0)
        e2e_env.age_sqlite_timestamps(table="projects", id_col="project_id", row_id=p_g_old, age_hours=25.0)
        e2e_env.age_sqlite_timestamps(table="projects", id_col="project_id", row_id=p_f_new, age_hours=48.0)

        monkeypatch.setenv("GRIDFORGE_GUEST_TTL_HOURS", "24")
        monkeypatch.setenv("GRIDFORGE_FRIEND_TTL_HOURS", "336")

        assert (
            worker_mod.main(
                [
                    "--data-dir",
                    str(e2e_env.data_dir),
                    "--packs-dir",
                    str(e2e_env.packs_dir),
                    "--once",
                ]
            )
            == 0
        )

        assert db.get_job(e2e_env.db_path, j_g_old) is None and not (e2e_env.data_dir / j_g_old).exists()
        assert db.get_job(e2e_env.db_path, j_f_old) is None and not (e2e_env.data_dir / j_f_old).exists()
        assert db.get_job(e2e_env.db_path, j_f_new) is not None
        # Default hosted GRIDFORGE_ADMIN_TTL_HOURS=720 prunes 1000h admin jobs while keeping 200h ones
        assert db.get_job(e2e_env.db_path, j_a_old) is None and not (e2e_env.data_dir / j_a_old).exists()
        assert db.get_job(e2e_env.db_path, j_a_fresh) is not None
        remaining_pids = {p.project_id for p in projects.list_projects(e2e_env.db_path)}
        assert p_g_old not in remaining_pids and p_f_new in remaining_pids

        # Setting GRIDFORGE_ADMIN_TTL_HOURS=0 explicitly disables admin job pruning
        j_a_keep = admin.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=_SAME_ORIGIN).json()["job_id"]
        e2e_env.seed_completed_job_pdf(j_a_keep)
        e2e_env.age_sqlite_timestamps(table="jobs", id_col="job_id", row_id=j_a_keep, age_hours=2000.0)
        monkeypatch.setenv("GRIDFORGE_ADMIN_TTL_HOURS", "0")
        assert (
            worker_mod.main(
                [
                    "--data-dir",
                    str(e2e_env.data_dir),
                    "--packs-dir",
                    str(e2e_env.packs_dir),
                    "--once",
                ]
            )
            == 0
        )
        assert db.get_job(e2e_env.db_path, j_a_keep) is not None

    @pytest.mark.m1_contract
    def test_t1_cache_prune_trim_cache_dir_evicts_lru_until_under_cap_m1_contract(
        self, e2e_env
    ) -> None:
        from gridforge.web import cache_prune

        f1 = e2e_env.cache_dir / "dem" / "oldest.tif"
        f2 = e2e_env.cache_dir / "osm" / "mid.osm"
        f3 = e2e_env.cache_dir / "dem" / "newest.tif"
        for p, size in ((f1, 400), (f2, 400), (f3, 400)):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"A" * size)

        set_file_mtime_atime(f1, 48.0)
        set_file_mtime_atime(f2, 12.0)
        set_file_mtime_atime(f3, 1.0)

        cache_prune.trim_cache_dir(e2e_env.cache_dir, max_bytes=800)
        assert not f1.exists()
        assert f2.exists()
        assert f3.exists()
        remaining_bytes = sum(
            p.stat().st_size for p in e2e_env.cache_dir.rglob("*") if p.is_file()
        )
        assert remaining_bytes <= 800

    @pytest.mark.m1_contract
    def test_t1_worker_once_trims_cache_dir_under_max_bytes_env_m1_contract(
        self, e2e_env, monkeypatch
    ) -> None:
        from gridforge.web import worker as worker_mod

        old_tile = e2e_env.cache_dir / "elvis" / "old.tif"
        new_tile = e2e_env.cache_dir / "elvis" / "new.tif"
        old_tile.parent.mkdir(parents=True, exist_ok=True)
        old_tile.write_bytes(b"O" * 1024)
        new_tile.write_bytes(b"N" * 512)
        set_file_mtime_atime(old_tile, 24.0)
        set_file_mtime_atime(new_tile, 0.5)

        monkeypatch.setenv("GRIDFORGE_CACHE_DIR", str(e2e_env.cache_dir))
        monkeypatch.setenv("GRIDFORGE_CACHE_MAX_BYTES", "600")

        assert (
            worker_mod.main(
                [
                    "--data-dir",
                    str(e2e_env.data_dir),
                    "--packs-dir",
                    str(e2e_env.packs_dir),
                    "--once",
                ]
            )
            == 0
        )

        assert not old_tile.exists()
        assert new_tile.exists()


class TestRoleTtlAndLruCacheTier2:
    """Tier 2 BVA & edge tests for role TTL cutoffs, active jobs, lock files & cache boundaries."""

    def test_t2_ttl_boundary_values_23_9h_vs_24_1h_and_335_9h_vs_336_1h(
        self, e2e_env
    ) -> None:
        from gridforge.config import ColorMode, Orientation, PaperSize
        from gridforge.web import db

        g_keep = _seed_terminal_job(
            e2e_env.db_path, e2e_env.data_dir, job_id="g_keep", owner_id="guest:1", owner_role="guest", age_hours=23.9
        )
        g_drop = _seed_terminal_job(
            e2e_env.db_path, e2e_env.data_dir, job_id="g_drop", owner_id="guest:1", owner_role="guest", age_hours=24.1
        )
        f_keep = _seed_terminal_job(
            e2e_env.db_path, e2e_env.data_dir, job_id="f_keep", owner_id="usr_1", owner_role="friend", age_hours=335.9
        )
        f_drop = _seed_terminal_job(
            e2e_env.db_path, e2e_env.data_dir, job_id="f_drop", owner_id="usr_1", owner_role="friend", age_hours=336.1
        )

        # Active queued guest job aged 72h must NEVER be pruned
        db.create_job(
            e2e_env.db_path,
            job_id="g_active",
            title="Active Guest",
            center="-33.732, 150.312",
            center_mgrs="56H KH 5095 6431",
            scale=25000,
            paper=PaperSize.A4,
            orientation=Orientation.PORTRAIT,
            color_mode=ColorMode.COLOR,
            request_json=json.dumps({"center": "-33.732, 150.312"}),
            owner_id="guest:1",
            owner_role="guest",
        )
        e2e_env.age_sqlite_timestamps(
            table="jobs", id_col="job_id", row_id="g_active", age_hours=72.0, columns=("created_at",)
        )

        pruned = db.prune_expired_jobs(
            e2e_env.db_path,
            data_dir=e2e_env.data_dir,
            guest_ttl_hours=24.0,
            friend_ttl_hours=336.0,
        )
        assert pruned == 2
        assert g_keep.exists() and db.get_job(e2e_env.db_path, "g_keep") is not None
        assert not g_drop.exists() and db.get_job(e2e_env.db_path, "g_drop") is None
        assert f_keep.exists() and db.get_job(e2e_env.db_path, "f_keep") is not None
        assert not f_drop.exists() and db.get_job(e2e_env.db_path, "f_drop") is None
        assert db.get_job(e2e_env.db_path, "g_active") is not None

    def test_t2_legacy_prune_without_role_ttls_remains_backward_compatible(
        self, e2e_env
    ) -> None:
        from gridforge.web import db

        n_old = _seed_terminal_job(
            e2e_env.db_path, e2e_env.data_dir, job_id="n_old", owner_id="local", owner_role="none", age_hours=30.0
        )
        n_new = _seed_terminal_job(
            e2e_env.db_path, e2e_env.data_dir, job_id="n_new", owner_id="local", owner_role="none", age_hours=5.0
        )

        pruned = db.prune_expired_jobs(e2e_env.db_path, data_dir=e2e_env.data_dir, max_age_hours=24.0)
        assert pruned == 1
        assert not n_old.exists() and db.get_job(e2e_env.db_path, "n_old") is None
        assert n_new.exists() and db.get_job(e2e_env.db_path, "n_new") is not None

    @pytest.mark.m1_contract
    def test_t2_cache_prune_preserves_tmp_and_part_lock_files_m1_contract(
        self, e2e_env
    ) -> None:
        from gridforge.web import cache_prune

        tmp_file = e2e_env.cache_dir / "dem" / "clip.tif.tmp"
        part_file = e2e_env.cache_dir / "osm" / "region.osm.pbf.part"
        old_regular = e2e_env.cache_dir / "dem" / "stale.tif"
        for p, size in ((tmp_file, 1000), (part_file, 1000), (old_regular, 500)):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"L" * size)

        # Make .tmp and .part oldest on disk; they must STILL be spared
        set_file_mtime_atime(tmp_file, 200.0)
        set_file_mtime_atime(part_file, 150.0)
        set_file_mtime_atime(old_regular, 10.0)

        cache_prune.trim_cache_dir(e2e_env.cache_dir, max_bytes=100)
        assert tmp_file.exists()
        assert part_file.exists()
        assert not old_regular.exists()

    @pytest.mark.m1_contract
    def test_t2_cache_prune_removes_empty_subdirs_and_prefers_recent_atime_m1_contract(
        self, e2e_env
    ) -> None:
        from gridforge.web import cache_prune

        evict_dir = e2e_env.cache_dir / "evict_me"
        keep_dir = e2e_env.cache_dir / "keep_me"
        lock_dir = e2e_env.cache_dir / "lock_dir"
        evict_dir.mkdir(parents=True, exist_ok=True)
        keep_dir.mkdir(parents=True, exist_ok=True)
        lock_dir.mkdir(parents=True, exist_ok=True)

        f_stale_atime = evict_dir / "unaccessed.tif"
        f_fresh_atime = keep_dir / "recently_read.tif"
        f_lock = lock_dir / "active.osm.tmp"

        f_stale_atime.write_bytes(b"1" * 500)
        f_fresh_atime.write_bytes(b"2" * 500)
        f_lock.write_bytes(b"3" * 100)

        # f_fresh_atime has an old mtime (48h) but a very recent atime (0.05h)
        now = time.time()
        os.utime(f_stale_atime, (now - 10 * 3600, now - 10 * 3600))
        os.utime(f_fresh_atime, (now - 0.05 * 3600, now - 48 * 3600))
        os.utime(f_lock, (now - 100 * 3600, now - 100 * 3600))

        cache_prune.trim_cache_dir(e2e_env.cache_dir, max_bytes=550)

        assert not f_stale_atime.exists()
        assert not evict_dir.exists()
        assert f_fresh_atime.exists()
        assert f_lock.exists()
        assert e2e_env.cache_dir.exists()

    @pytest.mark.m1_contract
    def test_t2_cache_prune_boundary_exact_cap_missing_dir_and_zero_cap_m1_contract(
        self, e2e_env
    ) -> None:
        from gridforge.web import cache_prune

        # Missing cache_dir is a clean no-op
        missing = e2e_env.root / "missing_cache"
        cache_prune.trim_cache_dir(missing, max_bytes=100)

        f1 = e2e_env.cache_dir / "a.tif"
        f2 = e2e_env.cache_dir / "b.tif"
        lock = e2e_env.cache_dir / "c.part"
        f1.write_bytes(b"A" * 300)
        f2.write_bytes(b"B" * 300)
        set_file_mtime_atime(f1, 20.0)
        set_file_mtime_atime(f2, 5.0)

        # Exact cap (600 == 600): deletes 0 files
        cache_prune.trim_cache_dir(e2e_env.cache_dir, max_bytes=600)
        assert f1.exists() and f2.exists()

        # Off-by-one cap (599 < 600): evicts oldest (f1), keeps f2
        cache_prune.trim_cache_dir(e2e_env.cache_dir, max_bytes=599)
        assert not f1.exists() and f2.exists()

        # Zero cap: deletes remaining regular files while sparing .part lock file
        lock.write_bytes(b"P" * 50)
        cache_prune.trim_cache_dir(e2e_env.cache_dir, max_bytes=0)
        assert not f2.exists()
        assert lock.exists()

    def test_t2_ttl_pruned_job_returns_404_and_frees_disk_via_api(self, e2e_env) -> None:
        from gridforge.web import db

        app = e2e_env.build_app(auth_mode="hosted")
        guest = e2e_env.make_guest_client(app)

        created = guest.post("/api/jobs", json={"center": "-33.732, 150.312"}, headers=_SAME_ORIGIN)
        assert created.status_code == 202
        job_id = created.json()["job_id"]
        e2e_env.seed_completed_job_pdf(job_id)

        assert guest.get(f"/api/jobs/{job_id}").status_code == 200
        assert guest.get(f"/api/jobs/{job_id}/artifacts/map.pdf").status_code == 200

        e2e_env.age_sqlite_timestamps(table="jobs", id_col="job_id", row_id=job_id, age_hours=25.0)
        pruned = db.prune_expired_jobs(
            e2e_env.db_path,
            data_dir=e2e_env.data_dir,
            guest_ttl_hours=24.0,
            friend_ttl_hours=336.0,
        )
        assert pruned == 1
        assert not (e2e_env.data_dir / job_id).exists()
        assert guest.get(f"/api/jobs/{job_id}").status_code == 404
        assert guest.get(f"/api/jobs/{job_id}/artifacts/map.pdf").status_code == 404
