"""Lightweight SQLite WAL job store and worker heartbeat registry (zero Redis)."""

from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from gridforge.config import ColorMode, Orientation, PaperSize
from gridforge.web.schemas import JobArtifact, JobRecord, JobStatus


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=15.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


@dataclass
class ClaimedJob:
    """Internal worker representation of an atomically claimed job."""

    record: JobRecord
    request_json: str
    overlay_path: Path | None


def _row_to_record(row: sqlite3.Row) -> JobRecord:
    raw_artifacts = json.loads(row["artifacts_json"]) if row["artifacts_json"] else []
    artifacts = [JobArtifact.model_validate(item) for item in raw_artifacts]
    keys = set(row.keys())
    return JobRecord(
        job_id=str(row["job_id"]),
        status=JobStatus(row["status"]),
        stage=str(row["stage"]),
        progress_pct=int(row["progress_pct"]),
        title=row["title"],
        center=str(row["center"]),
        center_mgrs=row["center_mgrs"],
        scale=int(row["scale"]),
        paper=PaperSize(row["paper"]),
        orientation=Orientation(row["orientation"]),
        color_mode=ColorMode(row["color_mode"]),
        grid_cols=int(row["grid_cols"]) if "grid_cols" in keys and row["grid_cols"] is not None else 1,
        grid_rows=int(row["grid_rows"]) if "grid_rows" in keys and row["grid_rows"] is not None else 1,
        created_at=str(row["created_at"]),
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        elapsed_seconds=float(row["elapsed_seconds"]) if row["elapsed_seconds"] is not None else None,
        peak_rss_mb=float(row["peak_rss_mb"]) if row["peak_rss_mb"] is not None else None,
        error=row["error"],
        artifacts=artifacts,
    )


def init_db(db_path: Path) -> None:
    """Create SQLite tables and indexes in WAL mode if they do not exist."""
    conn = _connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                stage TEXT NOT NULL,
                progress_pct INTEGER NOT NULL DEFAULT 0,
                title TEXT,
                center TEXT NOT NULL,
                center_mgrs TEXT,
                scale INTEGER NOT NULL,
                paper TEXT NOT NULL,
                orientation TEXT NOT NULL,
                color_mode TEXT NOT NULL,
                grid_cols INTEGER NOT NULL DEFAULT 1,
                grid_rows INTEGER NOT NULL DEFAULT 1,
                request_json TEXT NOT NULL,
                overlay_path TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                elapsed_seconds REAL,
                peak_rss_mb REAL,
                error TEXT,
                artifacts_json TEXT NOT NULL DEFAULT '[]'
            );

            CREATE INDEX IF NOT EXISTS idx_jobs_status_created
                ON jobs (status, created_at);

            CREATE TABLE IF NOT EXISTS worker_heartbeat (
                worker_id TEXT PRIMARY KEY,
                last_seen TEXT NOT NULL,
                pid INTEGER,
                active_job_id TEXT
            );

            CREATE TABLE IF NOT EXISTS projects (
                project_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                state_json TEXT NOT NULL,
                last_job_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_projects_updated
                ON projects (updated_at DESC);
            """
        )
        # Lightweight additive migration for existing SQLite databases created pre-atlas.
        existing_cols = {
            str(r["name"]) for r in conn.execute("PRAGMA table_info(jobs)").fetchall()
        }
        if "grid_cols" not in existing_cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN grid_cols INTEGER NOT NULL DEFAULT 1")
        if "grid_rows" not in existing_cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN grid_rows INTEGER NOT NULL DEFAULT 1")
    finally:
        conn.close()


def create_job(
    db_path: Path,
    *,
    job_id: str,
    title: str | None,
    center: str,
    center_mgrs: str | None,
    scale: int,
    paper: PaperSize,
    orientation: Orientation,
    color_mode: ColorMode,
    request_json: str,
    overlay_path: Path | None = None,
    grid_cols: int = 1,
    grid_rows: int = 1,
) -> JobRecord:
    """Insert a new queued job and return its JobRecord."""
    now = _utc_now_iso()
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            INSERT INTO jobs (
                job_id, status, stage, progress_pct, title, center, center_mgrs,
                scale, paper, orientation, color_mode, grid_cols, grid_rows,
                request_json, overlay_path, created_at, artifacts_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]')
            """,
            (
                job_id,
                JobStatus.QUEUED.value,
                "queued",
                0,
                title,
                center,
                center_mgrs,
                scale,
                paper.value,
                orientation.value,
                color_mode.value,
                int(grid_cols),
                int(grid_rows),
                request_json,
                str(overlay_path) if overlay_path is not None else None,
                now,
            ),
        )
        row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        conn.execute("COMMIT")
        assert row is not None
        return _row_to_record(row)
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def get_job(db_path: Path, job_id: str) -> JobRecord | None:
    """Fetch a single job by ID, or None if not found."""
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return _row_to_record(row) if row is not None else None
    finally:
        conn.close()


def list_jobs(db_path: Path, *, limit: int = 50) -> list[JobRecord]:
    """List jobs ordered newest first."""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (max(1, min(500, limit)),),
        ).fetchall()
        return [_row_to_record(r) for r in rows]
    finally:
        conn.close()


def get_queue_depth(db_path: Path) -> int:
    """Count active jobs in queued or running state."""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status IN (?, ?)",
            (JobStatus.QUEUED.value, JobStatus.RUNNING.value),
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def claim_next_job(db_path: Path) -> ClaimedJob | None:
    """Atomically claim the oldest queued job using SQLite ``BEGIN IMMEDIATE``."""
    now = _utc_now_iso()
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT * FROM jobs
            WHERE status = ?
            ORDER BY created_at ASC, rowid ASC
            LIMIT 1
            """,
            (JobStatus.QUEUED.value,),
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None

        job_id = str(row["job_id"])
        conn.execute(
            """
            UPDATE jobs
            SET status = ?, stage = ?, progress_pct = ?, started_at = ?
            WHERE job_id = ? AND status = ?
            """,
            (
                JobStatus.RUNNING.value,
                "preparing",
                5,
                now,
                job_id,
                JobStatus.QUEUED.value,
            ),
        )
        updated = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        conn.execute("COMMIT")
        assert updated is not None
        ov_raw = updated["overlay_path"]
        return ClaimedJob(
            record=_row_to_record(updated),
            request_json=str(updated["request_json"]),
            overlay_path=Path(ov_raw) if ov_raw else None,
        )
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def update_job_progress(
    db_path: Path,
    job_id: str,
    *,
    stage: str,
    progress_pct: int,
) -> bool:
    """Update stage/progress for a running job. Returns False if job was cancelled/missing."""
    clamped = max(0, min(100, int(progress_pct)))
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            """
            UPDATE jobs
            SET stage = ?, progress_pct = ?
            WHERE job_id = ? AND status = ?
            """,
            (stage, clamped, job_id, JobStatus.RUNNING.value),
        )
        conn.execute("COMMIT")
        return cur.rowcount > 0
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def complete_job(
    db_path: Path,
    job_id: str,
    *,
    artifacts: list[JobArtifact],
    elapsed_seconds: float,
    peak_rss_mb: float | None = None,
) -> JobRecord | None:
    """Mark a running job as succeeded with its downloadable artifact inventory."""
    now = _utc_now_iso()
    artifacts_json = json.dumps([a.model_dump() for a in artifacts])
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            UPDATE jobs
            SET status = ?, stage = 'done', progress_pct = 100,
                finished_at = ?, elapsed_seconds = ?, peak_rss_mb = ?,
                artifacts_json = ?, error = NULL
            WHERE job_id = ? AND status = ?
            """,
            (
                JobStatus.SUCCEEDED.value,
                now,
                round(elapsed_seconds, 3),
                round(peak_rss_mb, 1) if peak_rss_mb is not None else None,
                artifacts_json,
                job_id,
                JobStatus.RUNNING.value,
            ),
        )
        row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        conn.execute("COMMIT")
        return _row_to_record(row) if row is not None else None
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def fail_job(
    db_path: Path,
    job_id: str,
    *,
    error: str,
    elapsed_seconds: float | None = None,
    peak_rss_mb: float | None = None,
) -> JobRecord | None:
    """Mark a job as failed with a bounded error diagnostic."""
    now = _utc_now_iso()
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            UPDATE jobs
            SET status = ?, stage = 'failed', finished_at = ?,
                elapsed_seconds = ?, peak_rss_mb = ?, error = ?
            WHERE job_id = ? AND status IN (?, ?)
            """,
            (
                JobStatus.FAILED.value,
                now,
                round(elapsed_seconds, 3) if elapsed_seconds is not None else None,
                round(peak_rss_mb, 1) if peak_rss_mb is not None else None,
                error[:2000],
                job_id,
                JobStatus.QUEUED.value,
                JobStatus.RUNNING.value,
            ),
        )
        row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        conn.execute("COMMIT")
        return _row_to_record(row) if row is not None else None
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def cancel_job(db_path: Path, job_id: str) -> JobRecord | None:
    """Cancel a queued or running job. Returns updated record or None if missing."""
    now = _utc_now_iso()
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None

        if row["status"] in (JobStatus.QUEUED.value, JobStatus.RUNNING.value):
            conn.execute(
                """
                UPDATE jobs
                SET status = ?, stage = 'cancelled', finished_at = ?
                WHERE job_id = ?
                """,
                (JobStatus.CANCELLED.value, now, job_id),
            )
            row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        conn.execute("COMMIT")
        assert row is not None
        return _row_to_record(row)
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def delete_job(db_path: Path, job_id: str, *, data_dir: Path | None = None) -> bool:
    """Delete a job from SQLite and remove its artifact/upload directory on disk."""
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
        conn.execute("COMMIT")
        deleted = cur.rowcount > 0
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    if deleted and data_dir is not None:
        job_dir = (data_dir / job_id).resolve()
        if job_dir.is_relative_to(data_dir.resolve()) and job_dir.exists():
            shutil.rmtree(job_dir, ignore_errors=True)

    return deleted


def reset_orphaned_running_jobs(db_path: Path) -> int:
    """Mark any stale running jobs as failed on worker startup after a restart/crash."""
    now = _utc_now_iso()
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            """
            UPDATE jobs
            SET status = ?, stage = 'failed', finished_at = ?,
                error = 'Worker restarted before job completed'
            WHERE status = ?
            """,
            (JobStatus.FAILED.value, now, JobStatus.RUNNING.value),
        )
        conn.execute("COMMIT")
        return int(cur.rowcount)
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def touch_heartbeat(
    db_path: Path,
    *,
    worker_id: str = "worker-1",
    pid: int | None = None,
    active_job_id: str | None = None,
) -> str:
    """Update the worker heartbeat timestamp."""
    now = _utc_now_iso()
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            INSERT INTO worker_heartbeat (worker_id, last_seen, pid, active_job_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(worker_id) DO UPDATE SET
                last_seen = excluded.last_seen,
                pid = excluded.pid,
                active_job_id = excluded.active_job_id
            """,
            (worker_id, now, pid, active_job_id),
        )
        conn.execute("COMMIT")
        return now
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def get_heartbeat(db_path: Path, *, stale_after_seconds: int = 20) -> tuple[bool, str | None]:
    """Return ``(worker_alive, most_recent_last_seen_iso)`` across registered workers."""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT last_seen FROM worker_heartbeat ORDER BY last_seen DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    if row is None or not row["last_seen"]:
        return (False, None)

    last_seen_str = str(row["last_seen"])
    last_dt = _parse_iso(last_seen_str)
    if last_dt is None:
        return (False, last_seen_str)

    age = (datetime.now(timezone.utc) - last_dt).total_seconds()
    return (age <= stale_after_seconds, last_seen_str)


def prune_expired_jobs(
    db_path: Path,
    *,
    data_dir: Path | None = None,
    max_age_hours: float = 24.0,
) -> int:
    """Remove terminal jobs older than ``max_age_hours`` from SQLite and disk."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """
            SELECT job_id FROM jobs
            WHERE status IN (?, ?, ?)
              AND COALESCE(finished_at, created_at) < ?
            """,
            (
                JobStatus.SUCCEEDED.value,
                JobStatus.FAILED.value,
                JobStatus.CANCELLED.value,
                cutoff,
            ),
        ).fetchall()
        expired_ids = [str(r["job_id"]) for r in rows]
        if expired_ids:
            placeholders = ",".join("?" for _ in expired_ids)
            conn.execute(f"DELETE FROM jobs WHERE job_id IN ({placeholders})", expired_ids)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    if data_dir is not None:
        root = data_dir.resolve()
        for jid in expired_ids:
            jdir = (root / jid).resolve()
            if jdir.is_relative_to(root) and jdir.exists():
                shutil.rmtree(jdir, ignore_errors=True)

    return len(expired_ids)
