"""Lightweight SQLite WAL job store and worker heartbeat registry (zero Redis)."""

from __future__ import annotations

import json
import logging
import re
import shutil
import sqlite3
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from gridforge.config import ColorMode, Orientation, PaperSize
from gridforge.web.schemas import JobArtifact, JobRecord, JobStatus

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _dt_to_iso(dt: datetime) -> str:
    utc_dt = dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_now(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    return now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


class QuotaLimitError(Exception):
    """Raised when an atomic SQLite quota, rate limit, queue, or kill-switch check rejects."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class InviteActionError(Exception):
    """Raised when atomic invite acceptance fails."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


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
    owner_id = str(row["owner_id"]) if "owner_id" in keys and row["owner_id"] else "local"
    owner_role = str(row["owner_role"]) if "owner_role" in keys and row["owner_role"] else "none"
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
        owner_id=owner_id,
        owner_role=owner_role,
        created_at=str(row["created_at"]),
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        elapsed_seconds=float(row["elapsed_seconds"]) if row["elapsed_seconds"] is not None else None,
        peak_rss_mb=float(row["peak_rss_mb"]) if row["peak_rss_mb"] is not None else None,
        error=row["error"],
        artifacts=artifacts,
    )


def init_db(db_path: Path) -> None:
    """Create SQLite tables, run additive migrations, and create indexes in WAL mode."""
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
                owner_id TEXT NOT NULL DEFAULT 'local',
                owner_role TEXT NOT NULL DEFAULT 'none',
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
                owner_id TEXT NOT NULL DEFAULT 'local',
                owner_role TEXT NOT NULL DEFAULT 'none',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_projects_updated
                ON projects (updated_at DESC);

            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                email TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL CHECK (role IN ('friend', 'admin')),
                password_hash TEXT,
                google_sub TEXT,
                disabled INTEGER NOT NULL DEFAULT 0,
                daily_render_limit INTEGER,
                hourly_preview_limit INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_login_at TEXT
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_users_google_sub
                ON users (google_sub) WHERE google_sub IS NOT NULL;

            CREATE TABLE IF NOT EXISTS invites (
                invite_id TEXT PRIMARY KEY,
                token_hash TEXT NOT NULL UNIQUE,
                email TEXT,
                role TEXT NOT NULL DEFAULT 'friend' CHECK (role IN ('friend', 'admin')),
                created_by TEXT,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used_at TEXT,
                used_by_user_id TEXT,
                revoked_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_invites_expires
                ON invites (expires_at);

            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                token_hash TEXT NOT NULL UNIQUE,
                user_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_sessions_user
                ON sessions (user_id);
            CREATE INDEX IF NOT EXISTS idx_sessions_expires
                ON sessions (expires_at);

            CREATE TABLE IF NOT EXISTS usage_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                actor_role TEXT NOT NULL,
                client_ip TEXT NOT NULL DEFAULT '',
                job_id TEXT,
                detail_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_usage_type_created
                ON usage_events (event_type, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_usage_actor_type_created
                ON usage_events (actor_id, event_type, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_usage_ip_type_created
                ON usage_events (client_ip, event_type, created_at DESC);

            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS shared_snapshots (
                share_id TEXT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                state_json TEXT NOT NULL,
                source_project_id TEXT,
                owner_id TEXT NOT NULL DEFAULT 'local',
                owner_role TEXT NOT NULL DEFAULT 'none',
                view_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                expires_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_shared_snapshots_owner_created
                ON shared_snapshots (owner_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_shared_snapshots_expires
                ON shared_snapshots (expires_at);

            CREATE TABLE IF NOT EXISTS api_keys (
                key_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                name TEXT NOT NULL,
                key_prefix TEXT NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,
                scopes TEXT NOT NULL DEFAULT 'all',
                created_at TEXT NOT NULL,
                expires_at TEXT,
                revoked_at TEXT,
                last_used_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_api_keys_user
                ON api_keys (user_id, created_at DESC);
            """
        )
        # Lightweight additive migrations for existing SQLite databases.
        existing_job_cols = {
            str(r["name"]) for r in conn.execute("PRAGMA table_info(jobs)").fetchall()
        }
        if "grid_cols" not in existing_job_cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN grid_cols INTEGER NOT NULL DEFAULT 1")
        if "grid_rows" not in existing_job_cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN grid_rows INTEGER NOT NULL DEFAULT 1")
        if "owner_id" not in existing_job_cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'local'")
        if "owner_role" not in existing_job_cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN owner_role TEXT NOT NULL DEFAULT 'none'")

        existing_proj_cols = {
            str(r["name"]) for r in conn.execute("PRAGMA table_info(projects)").fetchall()
        }
        if "owner_id" not in existing_proj_cols:
            conn.execute("ALTER TABLE projects ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'local'")
        if "owner_role" not in existing_proj_cols:
            conn.execute("ALTER TABLE projects ADD COLUMN owner_role TEXT NOT NULL DEFAULT 'none'")

        # Create tenant/role indexes strictly AFTER additive ALTER TABLE checks.
        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_jobs_owner_created
                ON jobs (owner_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_jobs_role_status_created
                ON jobs (owner_role, status, created_at);
            CREATE INDEX IF NOT EXISTS idx_projects_owner_updated
                ON projects (owner_id, updated_at DESC);
            """
        )
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
    owner_id: str = "local",
    owner_role: str = "none",
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
                owner_id, owner_role, request_json, overlay_path, created_at, artifacts_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]')
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
                owner_id,
                owner_role,
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


def list_jobs(
    db_path: Path,
    *,
    limit: int = 50,
    owner_id: str | None = None,
) -> list[JobRecord]:
    """List jobs ordered newest first, optionally filtered by ``owner_id``."""
    conn = _connect(db_path)
    try:
        clamped = max(1, min(500, limit))
        if owner_id is not None:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE owner_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (owner_id, clamped),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (clamped,),
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
    guest_ttl_hours: float | None = None,
    friend_ttl_hours: float | None = None,
    now: datetime | None = None,
    orphan_grace_seconds: float = 60.0,
) -> int:
    """Remove expired terminal jobs from SQLite and disk.

    When ``guest_ttl_hours`` and ``friend_ttl_hours`` are both ``None``, preserves
    legacy ``max_age_hours`` pruning across all terminal jobs. When either role TTL
    is provided, only matching ``guest`` / ``friend`` terminal jobs are pruned and
    ``admin`` / ``none`` jobs are preserved.
    """
    ref_now = _resolve_now(now)
    conn = _connect(db_path)
    expired_ids: list[str] = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        terminal_statuses = (
            JobStatus.SUCCEEDED.value,
            JobStatus.FAILED.value,
            JobStatus.CANCELLED.value,
        )
        if guest_ttl_hours is None and friend_ttl_hours is None:
            cutoff = _dt_to_iso(ref_now - timedelta(hours=max_age_hours))
            rows = conn.execute(
                """
                SELECT job_id FROM jobs
                WHERE status IN (?, ?, ?)
                  AND COALESCE(finished_at, created_at) < ?
                """,
                (*terminal_statuses, cutoff),
            ).fetchall()
            expired_ids = [str(r["job_id"]) for r in rows]
        else:
            role_clauses: list[str] = []
            params: list[Any] = list(terminal_statuses)
            if guest_ttl_hours is not None and guest_ttl_hours >= 0:
                guest_cutoff = _dt_to_iso(ref_now - timedelta(hours=guest_ttl_hours))
                role_clauses.append("(owner_role = 'guest' AND COALESCE(finished_at, created_at) < ?)")
                params.append(guest_cutoff)
            if friend_ttl_hours is not None and friend_ttl_hours >= 0:
                friend_cutoff = _dt_to_iso(ref_now - timedelta(hours=friend_ttl_hours))
                role_clauses.append("(owner_role = 'friend' AND COALESCE(finished_at, created_at) < ?)")
                params.append(friend_cutoff)
            if role_clauses:
                where_roles = " OR ".join(role_clauses)
                rows = conn.execute(
                    f"""
                    SELECT job_id FROM jobs
                    WHERE status IN (?, ?, ?)
                      AND ({where_roles})
                    """,
                    params,
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

    if data_dir is not None and data_dir.is_dir():
        root = data_dir.resolve()
        for jid in expired_ids:
            jdir = (root / jid).resolve()
            if jdir.is_relative_to(root) and jdir.exists():
                shutil.rmtree(jdir, ignore_errors=True)

        # Also reclaim orphaned job directories left behind if a running job was deleted
        # or a worker exited mid-render. Skip non-job system dirs and recent uploads.
        _skip_names = {"packs", "cache", "samples", "lost+found"}
        _job_dir_re = re.compile(r"^[a-zA-Z0-9_-]{4,64}$")
        try:
            conn_ids = _connect(db_path)
            try:
                live_ids = {
                    str(r["job_id"])
                    for r in conn_ids.execute("SELECT job_id FROM jobs").fetchall()
                }
            finally:
                conn_ids.close()
            now_epoch = time.time()
            for entry in root.iterdir():
                if entry.is_symlink() or not entry.is_dir():
                    continue
                name = entry.name
                if (
                    name.startswith(".")
                    or name.startswith(".tmp_upload_")
                    or name in _skip_names
                    or not _job_dir_re.match(name)
                ):
                    continue
                if name in live_ids:
                    continue
                try:
                    mtime = entry.stat().st_mtime
                except OSError:
                    continue
                if orphan_grace_seconds <= 0 or (now_epoch - mtime) >= orphan_grace_seconds:
                    resolved = entry.resolve()
                    if resolved.is_relative_to(root):
                        shutil.rmtree(resolved, ignore_errors=True)
        except (sqlite3.Error, OSError) as exc:
            logger.warning("Orphan job directory sweep skipped: %s", exc)

    return len(expired_ids)


def prune_expired_projects_and_auth(
    db_path: Path,
    *,
    guest_ttl_hours: float = 24.0,
    friend_ttl_hours: float = 336.0,
    now: datetime | None = None,
) -> dict[str, int]:
    """Delete expired guest/friend projects and expired sessions/invites, preserving admin/none."""
    ref_now = _resolve_now(now)
    now_iso = _dt_to_iso(ref_now)
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        proj_clauses: list[str] = []
        proj_params: list[Any] = []
        if guest_ttl_hours >= 0:
            proj_clauses.append("(owner_role = 'guest' AND COALESCE(updated_at, created_at) < ?)")
            proj_params.append(_dt_to_iso(ref_now - timedelta(hours=guest_ttl_hours)))
        if friend_ttl_hours >= 0:
            proj_clauses.append("(owner_role = 'friend' AND COALESCE(updated_at, created_at) < ?)")
            proj_params.append(_dt_to_iso(ref_now - timedelta(hours=friend_ttl_hours)))

        n_proj = 0
        if proj_clauses:
            cur_proj = conn.execute(
                f"DELETE FROM projects WHERE {' OR '.join(proj_clauses)}",
                proj_params,
            )
            n_proj = int(cur_proj.rowcount)

        cur_sess = conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now_iso,))
        cur_inv = conn.execute("DELETE FROM invites WHERE expires_at <= ?", (now_iso,))
        conn.execute("COMMIT")
        return {
            "projects": n_proj,
            "sessions": int(cur_sess.rowcount),
            "invites": int(cur_inv.rowcount),
        }
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def prune_expired_shares(
    db_path: Path,
    *,
    now: datetime | None = None,
) -> int:
    """Delete expired rows from ``shared_snapshots``, preserving permanent shares."""
    now_iso = _dt_to_iso(_resolve_now(now))
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "DELETE FROM shared_snapshots WHERE expires_at IS NOT NULL AND expires_at <= ?",
            (now_iso,),
        )
        conn.execute("COMMIT")
        return int(cur.rowcount)
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Auth, Users, Invites, Sessions, Usage Events & App Settings Helpers
# ---------------------------------------------------------------------------


def get_setting(db_path: Path, key: str, default: Any = None) -> Any:
    """Read a JSON-encoded setting from ``app_settings``."""
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT value_json FROM app_settings WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value_json"])
        except Exception:
            return default
    finally:
        conn.close()


def set_setting(db_path: Path, key: str, value: Any) -> None:
    """Persist a JSON-encoded setting into ``app_settings``."""
    now = _utc_now_iso()
    encoded = json.dumps(value)
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            INSERT INTO app_settings (key, value_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value_json = excluded.value_json,
                updated_at = excluded.updated_at
            """,
            (key, encoded, now),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def get_bool_setting(db_path: Path, key: str, default: bool = False) -> bool:
    """Read a boolean setting from ``app_settings``."""
    val = get_setting(db_path, key, default)
    return bool(val)


def _row_to_user_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "user_id": str(row["user_id"]),
        "email": str(row["email"]),
        "display_name": str(row["display_name"] or ""),
        "role": str(row["role"]),
        "password_hash": row["password_hash"],
        "google_sub": row["google_sub"],
        "disabled": bool(row["disabled"]),
        "daily_render_limit": (
            int(row["daily_render_limit"]) if row["daily_render_limit"] is not None else None
        ),
        "hourly_preview_limit": (
            int(row["hourly_preview_limit"]) if row["hourly_preview_limit"] is not None else None
        ),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "last_login_at": row["last_login_at"],
    }


def create_user(
    db_path: Path,
    *,
    user_id: str,
    email: str,
    display_name: str = "",
    role: str = "friend",
    password_hash: str | None = None,
    google_sub: str | None = None,
    disabled: bool = False,
    daily_render_limit: int | None = None,
    hourly_preview_limit: int | None = None,
) -> dict[str, Any]:
    """Insert a new user record."""
    now = _utc_now_iso()
    norm_email = email.strip().lower()
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            INSERT INTO users (
                user_id, email, display_name, role, password_hash, google_sub,
                disabled, daily_render_limit, hourly_preview_limit,
                created_at, updated_at, last_login_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                user_id,
                norm_email,
                display_name.strip(),
                role,
                password_hash,
                google_sub,
                1 if disabled else 0,
                daily_render_limit,
                hourly_preview_limit,
                now,
                now,
            ),
        )
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        conn.execute("COMMIT")
        assert row is not None
        return _row_to_user_dict(row)
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def get_user_by_id(db_path: Path, user_id: str) -> dict[str, Any] | None:
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return _row_to_user_dict(row) if row is not None else None
    finally:
        conn.close()


def get_user_by_email(db_path: Path, email: str) -> dict[str, Any] | None:
    norm = email.strip().lower()
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (norm,)).fetchone()
        return _row_to_user_dict(row) if row is not None else None
    finally:
        conn.close()


def get_user_by_google_sub(db_path: Path, google_sub: str) -> dict[str, Any] | None:
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT * FROM users WHERE google_sub = ?", (google_sub,)).fetchone()
        return _row_to_user_dict(row) if row is not None else None
    finally:
        conn.close()


def update_user(
    db_path: Path,
    user_id: str,
    *,
    disabled: bool | None = None,
    set_disabled: bool = False,
    daily_render_limit: int | None = None,
    set_daily_render_limit: bool = False,
    hourly_preview_limit: int | None = None,
    set_hourly_preview_limit: bool = False,
    role: str | None = None,
    set_role: bool = False,
    google_sub: str | None = None,
    set_google_sub: bool = False,
    password_hash: str | None = None,
    set_password_hash: bool = False,
    last_login_at: str | None = None,
    set_last_login_at: bool = False,
) -> dict[str, Any] | None:
    """Update mutable user fields atomically."""
    now = _utc_now_iso()
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        updates: list[str] = ["updated_at = ?"]
        params: list[Any] = [now]
        if set_disabled:
            updates.append("disabled = ?")
            params.append(1 if disabled else 0)
        if set_daily_render_limit:
            updates.append("daily_render_limit = ?")
            params.append(daily_render_limit)
        if set_hourly_preview_limit:
            updates.append("hourly_preview_limit = ?")
            params.append(hourly_preview_limit)
        if set_role and role is not None:
            updates.append("role = ?")
            params.append(role)
        if set_google_sub:
            updates.append("google_sub = ?")
            params.append(google_sub)
        if set_password_hash:
            updates.append("password_hash = ?")
            params.append(password_hash)
        if set_last_login_at:
            updates.append("last_login_at = ?")
            params.append(last_login_at)
        params.append(user_id)
        conn.execute(f"UPDATE users SET {', '.join(updates)} WHERE user_id = ?", params)
        updated = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        conn.execute("COMMIT")
        return _row_to_user_dict(updated) if updated is not None else None
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def list_users(db_path: Path) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        rows = conn.execute("SELECT * FROM users ORDER BY created_at DESC, rowid DESC").fetchall()
        return [_row_to_user_dict(r) for r in rows]
    finally:
        conn.close()


def ensure_seed_admin(
    db_path: Path,
    *,
    user_id: str,
    email: str,
    display_name: str,
    password_hash: str | None,
) -> dict[str, Any]:
    """Create or ensure the bootstrap admin account exists and is enabled."""
    now = _utc_now_iso()
    norm_email = email.strip().lower()
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute("SELECT * FROM users WHERE email = ?", (norm_email,)).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO users (
                    user_id, email, display_name, role, password_hash, google_sub,
                    disabled, daily_render_limit, hourly_preview_limit,
                    created_at, updated_at, last_login_at
                ) VALUES (?, ?, ?, 'admin', ?, NULL, 0, NULL, NULL, ?, ?, NULL)
                """,
                (user_id, norm_email, display_name.strip() or "Admin", password_hash, now, now),
            )
            row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        else:
            updates = ["role = 'admin'", "disabled = 0", "updated_at = ?"]
            params: list[Any] = [now]
            if existing["password_hash"] is None and password_hash is not None:
                updates.append("password_hash = ?")
                params.append(password_hash)
            params.append(str(existing["user_id"]))
            conn.execute(f"UPDATE users SET {', '.join(updates)} WHERE user_id = ?", params)
            row = conn.execute(
                "SELECT * FROM users WHERE user_id = ?", (str(existing["user_id"]),)
            ).fetchone()
        conn.execute("COMMIT")
        assert row is not None
        return _row_to_user_dict(row)
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def _row_to_invite_dict(row: sqlite3.Row, *, now_iso: str | None = None) -> dict[str, Any]:
    ref_iso = now_iso or _utc_now_iso()
    if row["revoked_at"] is not None:
        inv_status = "revoked"
    elif row["used_at"] is not None:
        inv_status = "used"
    elif str(row["expires_at"]) <= ref_iso:
        inv_status = "expired"
    else:
        inv_status = "pending"
    return {
        "invite_id": str(row["invite_id"]),
        "token_hash": str(row["token_hash"]),
        "email": str(row["email"]) if row["email"] else None,
        "role": str(row["role"]),
        "created_by": str(row["created_by"]) if row["created_by"] else None,
        "created_at": str(row["created_at"]),
        "expires_at": str(row["expires_at"]),
        "used_at": row["used_at"],
        "used_by_user_id": row["used_by_user_id"],
        "revoked_at": row["revoked_at"],
        "status": inv_status,
    }


def create_invite(
    db_path: Path,
    *,
    invite_id: str,
    token_hash: str,
    email: str | None,
    role: str,
    created_by: str | None,
    expires_at: str,
) -> dict[str, Any]:
    now = _utc_now_iso()
    norm_email = email.strip().lower() if email and email.strip() else None
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            INSERT INTO invites (
                invite_id, token_hash, email, role, created_by,
                created_at, expires_at, used_at, used_by_user_id, revoked_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)
            """,
            (invite_id, token_hash, norm_email, role, created_by, now, expires_at),
        )
        row = conn.execute("SELECT * FROM invites WHERE invite_id = ?", (invite_id,)).fetchone()
        conn.execute("COMMIT")
        assert row is not None
        return _row_to_invite_dict(row, now_iso=now)
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def get_invite_by_token_hash(db_path: Path, token_hash: str) -> dict[str, Any] | None:
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM invites WHERE token_hash = ?", (token_hash,)
        ).fetchone()
        return _row_to_invite_dict(row) if row is not None else None
    finally:
        conn.close()


def revoke_invite(db_path: Path, invite_id: str) -> bool:
    now = _utc_now_iso()
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "UPDATE invites SET revoked_at = COALESCE(revoked_at, ?) WHERE invite_id = ?",
            (now, invite_id),
        )
        conn.execute("COMMIT")
        return cur.rowcount > 0
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def list_invites(db_path: Path) -> list[dict[str, Any]]:
    now = _utc_now_iso()
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM invites ORDER BY created_at DESC, rowid DESC"
        ).fetchall()
        return [_row_to_invite_dict(r, now_iso=now) for r in rows]
    finally:
        conn.close()


def accept_invite_and_create_user(
    db_path: Path,
    *,
    token_hash: str,
    new_user_id: str,
    supplied_email: str | None,
    display_name: str | None,
    password_hash: str | None,
    google_sub: str | None = None,
    session_id: str,
    session_token_hash: str,
    session_expires_at: str,
) -> dict[str, Any]:
    """Atomically validate invite, create user, mark invite used, and open a session."""
    now = _utc_now_iso()
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        inv = conn.execute(
            "SELECT * FROM invites WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
        if inv is None:
            conn.execute("COMMIT")
            raise InviteActionError(403, "Invalid invite token.")
        if inv["revoked_at"] is not None:
            conn.execute("COMMIT")
            raise InviteActionError(403, "Invite has been revoked.")
        if inv["used_at"] is not None:
            conn.execute("COMMIT")
            raise InviteActionError(403, "Invite has already been used.")
        if str(inv["expires_at"]) <= now:
            conn.execute("COMMIT")
            raise InviteActionError(403, "Invite has expired.")

        pinned_email = str(inv["email"]).strip().lower() if inv["email"] else None
        cleaned_supplied = supplied_email.strip().lower() if supplied_email and supplied_email.strip() else None

        if pinned_email:
            if cleaned_supplied is not None and cleaned_supplied != pinned_email:
                conn.execute("COMMIT")
                raise InviteActionError(403, "Invite email does not match the provided email.")
            target_email = pinned_email
        else:
            if not cleaned_supplied or "@" not in cleaned_supplied:
                conn.execute("COMMIT")
                raise InviteActionError(422, "A valid email address is required for this invite.")
            target_email = cleaned_supplied

        existing_user = conn.execute(
            "SELECT user_id FROM users WHERE email = ?", (target_email,)
        ).fetchone()
        if existing_user is not None:
            conn.execute("COMMIT")
            raise InviteActionError(409, "An account with this email already exists.")

        resolved_name = (
            display_name.strip()
            if display_name and display_name.strip()
            else target_email.split("@", 1)[0]
        )
        conn.execute(
            """
            INSERT INTO users (
                user_id, email, display_name, role, password_hash, google_sub,
                disabled, daily_render_limit, hourly_preview_limit,
                created_at, updated_at, last_login_at
            ) VALUES (?, ?, ?, ?, ?, ?, 0, NULL, NULL, ?, ?, ?)
            """,
            (
                new_user_id,
                target_email,
                resolved_name,
                str(inv["role"]),
                password_hash,
                google_sub,
                now,
                now,
                now,
            ),
        )
        cur = conn.execute(
            """
            UPDATE invites
            SET used_at = ?, used_by_user_id = ?
            WHERE invite_id = ? AND used_at IS NULL AND revoked_at IS NULL
            """,
            (now, new_user_id, str(inv["invite_id"])),
        )
        if cur.rowcount != 1:
            conn.execute("ROLLBACK")
            raise InviteActionError(403, "Invite could not be claimed.")

        conn.execute(
            """
            INSERT INTO sessions (session_id, token_hash, user_id, created_at, expires_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (session_id, session_token_hash, new_user_id, now, session_expires_at, now),
        )
        user_row = conn.execute("SELECT * FROM users WHERE user_id = ?", (new_user_id,)).fetchone()
        conn.execute("COMMIT")
        assert user_row is not None
        return _row_to_user_dict(user_row)
    except InviteActionError:
        raise
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def create_session(
    db_path: Path,
    *,
    session_id: str,
    token_hash: str,
    user_id: str,
    expires_at: str,
) -> None:
    now = _utc_now_iso()
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            INSERT INTO sessions (session_id, token_hash, user_id, created_at, expires_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (session_id, token_hash, user_id, now, expires_at, now),
        )
        conn.execute(
            "UPDATE users SET last_login_at = ?, updated_at = ? WHERE user_id = ?",
            (now, now, user_id),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def get_session_and_user_by_token_hash(
    db_path: Path,
    token_hash: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    now_iso = _dt_to_iso(_resolve_now(now))
    conn = _connect(db_path)
    try:
        row = conn.execute(
            """
            SELECT u.*, s.session_id, s.expires_at AS session_expires_at
            FROM sessions s
            JOIN users u ON u.user_id = s.user_id
            WHERE s.token_hash = ? AND s.expires_at > ?
            """,
            (token_hash, now_iso),
        ).fetchone()
        if row is None:
            return None
        user = _row_to_user_dict(row)
        user["session_id"] = str(row["session_id"])
        user["session_expires_at"] = str(row["session_expires_at"])
        return user
    finally:
        conn.close()


def delete_session_by_token_hash(db_path: Path, token_hash: str) -> bool:
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
        conn.execute("COMMIT")
        return cur.rowcount > 0
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def delete_sessions_for_user(db_path: Path, user_id: str) -> int:
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        conn.execute("COMMIT")
        return int(cur.rowcount)
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def _parse_scopes(raw: str | None) -> list[str]:
    if not raw:
        return ["all"]
    parts = [part.strip() for part in str(raw).split(",") if part.strip()]
    return parts or ["all"]


def _row_to_api_key_dict(row: sqlite3.Row, *, now_iso: str | None = None) -> dict[str, Any]:
    ref_iso = now_iso or _utc_now_iso()
    if row["revoked_at"] is not None:
        key_status = "revoked"
    elif row["expires_at"] is not None and str(row["expires_at"]) <= ref_iso:
        key_status = "expired"
    else:
        key_status = "active"
    return {
        "key_id": str(row["key_id"]),
        "user_id": str(row["user_id"]),
        "name": str(row["name"]),
        "key_prefix": str(row["key_prefix"]),
        "scopes": _parse_scopes(row["scopes"]),
        "created_at": str(row["created_at"]),
        "expires_at": str(row["expires_at"]) if row["expires_at"] is not None else None,
        "revoked_at": str(row["revoked_at"]) if row["revoked_at"] is not None else None,
        "last_used_at": str(row["last_used_at"]) if row["last_used_at"] is not None else None,
        "status": key_status,
    }


def create_api_key(
    db_path: Path,
    *,
    key_id: str,
    user_id: str,
    name: str,
    key_prefix: str,
    token_hash: str,
    scopes: str | Sequence[str] = "all",
    expires_at: str | None = None,
) -> dict[str, Any]:
    """Insert a new personal access API key record for a user."""
    now = _utc_now_iso()
    if isinstance(scopes, str):
        scopes_str = ",".join(_parse_scopes(scopes))
    else:
        cleaned = [str(s).strip() for s in scopes if str(s).strip()]
        scopes_str = ",".join(cleaned) if cleaned else "all"
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            INSERT INTO api_keys (
                key_id, user_id, name, key_prefix, token_hash, scopes,
                created_at, expires_at, revoked_at, last_used_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
            """,
            (key_id, user_id, name.strip(), key_prefix, token_hash, scopes_str, now, expires_at),
        )
        row = conn.execute("SELECT * FROM api_keys WHERE key_id = ?", (key_id,)).fetchone()
        conn.execute("COMMIT")
        assert row is not None
        return _row_to_api_key_dict(row, now_iso=now)
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def get_api_key(db_path: Path, key_id: str) -> dict[str, Any] | None:
    """Fetch a single API key record by ``key_id``, or None if not found."""
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT * FROM api_keys WHERE key_id = ?", (key_id,)).fetchone()
        return _row_to_api_key_dict(row) if row is not None else None
    finally:
        conn.close()


def list_api_keys(
    db_path: Path,
    *,
    user_id: str | None = None,
) -> list[dict[str, Any]]:
    """List API keys newest first, optionally filtered by owning ``user_id``."""
    now = _utc_now_iso()
    conn = _connect(db_path)
    try:
        if user_id is not None:
            rows = conn.execute(
                "SELECT * FROM api_keys WHERE user_id = ? ORDER BY created_at DESC, rowid DESC",
                (user_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM api_keys ORDER BY created_at DESC, rowid DESC"
            ).fetchall()
        return [_row_to_api_key_dict(r, now_iso=now) for r in rows]
    finally:
        conn.close()


def get_api_key_and_user_by_token_hash(
    db_path: Path,
    token_hash: str,
    *,
    now: datetime | None = None,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Resolve an active, non-revoked, non-expired API key and its owning user by token hash."""
    now_iso = _dt_to_iso(_resolve_now(now))
    conn = _connect(db_path)
    try:
        row = conn.execute(
            """
            SELECT u.*,
                   k.key_id AS k_key_id,
                   k.user_id AS k_user_id,
                   k.name AS k_name,
                   k.key_prefix AS k_key_prefix,
                   k.scopes AS k_scopes,
                   k.created_at AS k_created_at,
                   k.expires_at AS k_expires_at,
                   k.revoked_at AS k_revoked_at,
                   k.last_used_at AS k_last_used_at
            FROM api_keys k
            JOIN users u ON u.user_id = k.user_id
            WHERE k.token_hash = ?
              AND k.revoked_at IS NULL
              AND (k.expires_at IS NULL OR k.expires_at > ?)
            """,
            (token_hash, now_iso),
        ).fetchone()
        if row is None:
            return None
        user = _row_to_user_dict(row)
        api_key = {
            "key_id": str(row["k_key_id"]),
            "user_id": str(row["k_user_id"]),
            "name": str(row["k_name"]),
            "key_prefix": str(row["k_key_prefix"]),
            "scopes": _parse_scopes(row["k_scopes"]),
            "created_at": str(row["k_created_at"]),
            "expires_at": str(row["k_expires_at"]) if row["k_expires_at"] is not None else None,
            "revoked_at": str(row["k_revoked_at"]) if row["k_revoked_at"] is not None else None,
            "last_used_at": str(row["k_last_used_at"]) if row["k_last_used_at"] is not None else None,
            "status": "active",
        }
        return (api_key, user)
    finally:
        conn.close()


def revoke_api_key(
    db_path: Path,
    key_id: str,
    *,
    user_id: str | None = None,
) -> bool:
    """Revoke an API key by setting ``revoked_at``. Returns True if a matching row existed."""
    now = _utc_now_iso()
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        if user_id is not None:
            cur = conn.execute(
                "UPDATE api_keys SET revoked_at = COALESCE(revoked_at, ?) WHERE key_id = ? AND user_id = ?",
                (now, key_id, user_id),
            )
        else:
            cur = conn.execute(
                "UPDATE api_keys SET revoked_at = COALESCE(revoked_at, ?) WHERE key_id = ?",
                (now, key_id),
            )
        conn.execute("COMMIT")
        return cur.rowcount > 0
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def touch_api_key_last_used(
    db_path: Path,
    key_id: str,
    *,
    now: datetime | None = None,
) -> None:
    """Update ``last_used_at`` timestamp for an API key."""
    now_iso = _dt_to_iso(_resolve_now(now))
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE api_keys SET last_used_at = ? WHERE key_id = ?",
            (now_iso, key_id),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def record_usage_event(
    db_path: Path,
    *,
    event_type: str,
    actor_id: str,
    actor_role: str,
    client_ip: str = "",
    job_id: str | None = None,
    detail: dict[str, Any] | None = None,
    created_at: str | None = None,
) -> None:
    ts = created_at or _utc_now_iso()
    detail_json = json.dumps(detail or {}, separators=(",", ":"))
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            INSERT INTO usage_events (event_type, actor_id, actor_role, client_ip, job_id, detail_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (event_type, actor_id, actor_role, client_ip, job_id, detail_json, ts),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def count_usage_events(
    db_path: Path,
    *,
    event_type: str | Sequence[str],
    since_iso: str,
    actor_id: str | None = None,
    actor_role: str | None = None,
    client_ip: str | None = None,
) -> int:
    if isinstance(event_type, str):
        event_types: tuple[str, ...] = (event_type,)
    else:
        event_types = tuple(event_type)
    if not event_types:
        return 0
    if len(event_types) == 1:
        clauses = ["event_type = ?", "created_at >= ?"]
        params: list[Any] = [event_types[0], since_iso]
    else:
        placeholders = ",".join("?" for _ in event_types)
        clauses = [f"event_type IN ({placeholders})", "created_at >= ?"]
        params = [*event_types, since_iso]
    if actor_id is not None:
        clauses.append("actor_id = ?")
        params.append(actor_id)
    if actor_role is not None:
        clauses.append("actor_role = ?")
        params.append(actor_role)
    if client_ip is not None:
        clauses.append("client_ip = ?")
        params.append(client_ip)
    conn = _connect(db_path)
    try:
        row = conn.execute(
            f"SELECT COUNT(*) FROM usage_events WHERE {' AND '.join(clauses)}",
            params,
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def atomic_check_and_create_job(
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
    owner_id: str = "local",
    owner_role: str = "none",
    client_ip: str = "",
    max_queue_depth: int | None = None,
    global_daily_public_limit: int | None = None,
    daily_render_limit: int | None = None,
    enforce_kill_switch: bool = False,
    record_usage: bool = False,
    now: datetime | None = None,
) -> JobRecord:
    """Atomically enforce queue, kill-switch, global budget, and per-role daily render limits, then insert job."""
    ref_now = _resolve_now(now)
    now_iso = _dt_to_iso(ref_now)
    today_start_iso = _dt_to_iso(ref_now.replace(hour=0, minute=0, second=0, microsecond=0))

    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")

        if enforce_kill_switch and owner_role == "guest":
            row_ks = conn.execute(
                "SELECT value_json FROM app_settings WHERE key = 'public_renders_paused'"
            ).fetchone()
            paused = False
            if row_ks is not None:
                try:
                    paused = bool(json.loads(row_ks["value_json"]))
                except Exception:
                    paused = False
            if paused:
                conn.execute("COMMIT")
                raise QuotaLimitError(
                    503,
                    "Public map rendering is temporarily paused by the administrator.",
                )

        if max_queue_depth is not None:
            q_row = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE status IN (?, ?)",
                (JobStatus.QUEUED.value, JobStatus.RUNNING.value),
            ).fetchone()
            depth = int(q_row[0]) if q_row else 0
            if depth >= max_queue_depth:
                conn.execute("COMMIT")
                raise QuotaLimitError(
                    503,
                    "Render queue is currently full; please try again shortly.",
                )

        if owner_role == "guest" and global_daily_public_limit is not None:
            g_row = conn.execute(
                """
                SELECT COUNT(*) FROM usage_events
                WHERE event_type = 'job_render'
                  AND actor_role = 'guest'
                  AND created_at >= ?
                """,
                (today_start_iso,),
            ).fetchone()
            global_used = int(g_row[0]) if g_row else 0
            if global_used >= global_daily_public_limit:
                conn.execute("COMMIT")
                raise QuotaLimitError(
                    503,
                    "Global daily public render budget reached; sign in or try again tomorrow UTC.",
                )

        if daily_render_limit is not None:
            a_row = conn.execute(
                """
                SELECT COUNT(*) FROM usage_events
                WHERE event_type = 'job_render'
                  AND actor_id = ?
                  AND created_at >= ?
                """,
                (owner_id, today_start_iso),
            ).fetchone()
            used = int(a_row[0]) if a_row else 0
            if owner_role == "guest" and client_ip:
                ip_row = conn.execute(
                    """
                    SELECT COUNT(*) FROM usage_events
                    WHERE event_type = 'job_render'
                      AND actor_role = 'guest'
                      AND client_ip = ?
                      AND created_at >= ?
                    """,
                    (client_ip, today_start_iso),
                ).fetchone()
                used = max(used, int(ip_row[0]) if ip_row else 0)
            if used >= daily_render_limit:
                conn.execute("COMMIT")
                raise QuotaLimitError(
                    429,
                    f"Daily render quota ({daily_render_limit}/day) reached.",
                )

        conn.execute(
            """
            INSERT INTO jobs (
                job_id, status, stage, progress_pct, title, center, center_mgrs,
                scale, paper, orientation, color_mode, grid_cols, grid_rows,
                owner_id, owner_role, request_json, overlay_path, created_at, artifacts_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]')
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
                owner_id,
                owner_role,
                request_json,
                str(overlay_path) if overlay_path is not None else None,
                now_iso,
            ),
        )
        if record_usage:
            detail_json = json.dumps(
                {"grid_cols": int(grid_cols), "grid_rows": int(grid_rows), "scale": int(scale)},
                separators=(",", ":"),
            )
            conn.execute(
                """
                INSERT INTO usage_events (
                    event_type, actor_id, actor_role, client_ip, job_id, detail_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                ("job_render", owner_id, owner_role, client_ip, job_id, detail_json, now_iso),
            )
        row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        conn.execute("COMMIT")
        assert row is not None
        return _row_to_record(row)
    except QuotaLimitError:
        raise
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def atomic_check_and_record_rate_limit(
    db_path: Path,
    *,
    event_type: str,
    actor_id: str,
    actor_role: str,
    client_ip: str,
    limit: int | None,
    window_seconds: int = 3600,
    by_ip_only: bool = False,
    count_event_types: Sequence[str] | None = None,
    detail: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> int:
    """Atomically enforce a rolling-window rate limit and record the ``usage_events`` row."""
    ref_now = _resolve_now(now)
    now_iso = _dt_to_iso(ref_now)
    since_iso = _dt_to_iso(ref_now - timedelta(seconds=window_seconds))
    detail_json = json.dumps(detail or {}, separators=(",", ":"))
    event_types = tuple(count_event_types) if count_event_types else (event_type,)
    if len(event_types) == 1:
        ev_clause = "event_type = ?"
        ev_params: tuple[str, ...] = (event_types[0],)
    else:
        ev_placeholders = ",".join("?" for _ in event_types)
        ev_clause = f"event_type IN ({ev_placeholders})"
        ev_params = event_types

    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        if limit is not None:
            if by_ip_only:
                row = conn.execute(
                    f"""
                    SELECT COUNT(*) FROM usage_events
                    WHERE {ev_clause} AND client_ip = ? AND created_at >= ?
                    """,
                    (*ev_params, client_ip, since_iso),
                ).fetchone()
                used = int(row[0]) if row else 0
            else:
                a_row = conn.execute(
                    f"""
                    SELECT COUNT(*) FROM usage_events
                    WHERE {ev_clause} AND actor_id = ? AND created_at >= ?
                    """,
                    (*ev_params, actor_id, since_iso),
                ).fetchone()
                used = int(a_row[0]) if a_row else 0
                if actor_role == "guest" and client_ip:
                    ip_row = conn.execute(
                        f"""
                        SELECT COUNT(*) FROM usage_events
                        WHERE {ev_clause} AND actor_role = 'guest' AND client_ip = ? AND created_at >= ?
                        """,
                        (*ev_params, client_ip, since_iso),
                    ).fetchone()
                    used = max(used, int(ip_row[0]) if ip_row else 0)
            if used >= limit:
                conn.execute("COMMIT")
                raise QuotaLimitError(
                    429,
                    f"Rate limit ({limit} requests per {window_seconds // 60} min) exceeded.",
                )

        conn.execute(
            """
            INSERT INTO usage_events (
                event_type, actor_id, actor_role, client_ip, job_id, detail_json, created_at
            ) VALUES (?, ?, ?, ?, NULL, ?, ?)
            """,
            (event_type, actor_id, actor_role, client_ip, detail_json, now_iso),
        )
        conn.execute("COMMIT")
        return 1
    except QuotaLimitError:
        raise
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def get_admin_usage_report(
    db_path: Path,
    *,
    global_daily_public_limit: int | None = None,
    recent_limit: int = 50,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build aggregate usage, per-role breakdown, job performance stats, and recent events."""
    ref_now = _resolve_now(now)
    today_start_iso = _dt_to_iso(ref_now.replace(hour=0, minute=0, second=0, microsecond=0))
    week_start_iso = _dt_to_iso(ref_now - timedelta(days=7))

    conn = _connect(db_path)
    try:
        renders_today = int(
            conn.execute(
                "SELECT COUNT(*) FROM usage_events WHERE event_type = 'job_render' AND created_at >= ?",
                (today_start_iso,),
            ).fetchone()[0]
        )
        renders_7d = int(
            conn.execute(
                "SELECT COUNT(*) FROM usage_events WHERE event_type = 'job_render' AND created_at >= ?",
                (week_start_iso,),
            ).fetchone()[0]
        )
        previews_today = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM usage_events
                WHERE event_type IN ('viewshed_preview', 'rf_link_profile', 'ruler_metrics')
                  AND created_at >= ?
                """,
                (today_start_iso,),
            ).fetchone()[0]
        )
        active_queue_depth = int(
            conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE status IN (?, ?)",
                (JobStatus.QUEUED.value, JobStatus.RUNNING.value),
            ).fetchone()[0]
        )
        global_daily_public_used = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM usage_events
                WHERE event_type = 'job_render' AND actor_role = 'guest' AND created_at >= ?
                """,
                (today_start_iso,),
            ).fetchone()[0]
        )

        ks_row = conn.execute(
            "SELECT value_json FROM app_settings WHERE key = 'public_renders_paused'"
        ).fetchone()
        public_renders_paused = False
        if ks_row is not None:
            try:
                public_renders_paused = bool(json.loads(ks_row["value_json"]))
            except Exception:
                public_renders_paused = False

        perf_row = conn.execute(
            """
            SELECT AVG(elapsed_seconds) AS avg_elapsed, MAX(peak_rss_mb) AS max_rss
            FROM jobs
            WHERE status = ?
            """,
            (JobStatus.SUCCEEDED.value,),
        ).fetchone()
        avg_elapsed = (
            round(float(perf_row["avg_elapsed"]), 3)
            if perf_row is not None and perf_row["avg_elapsed"] is not None
            else None
        )
        max_rss = (
            round(float(perf_row["max_rss"]), 1)
            if perf_row is not None and perf_row["max_rss"] is not None
            else None
        )

        role_rows = conn.execute(
            """
            SELECT actor_role,
                   SUM(CASE WHEN event_type = 'job_render' AND created_at >= ? THEN 1 ELSE 0 END) AS renders_today,
                   SUM(CASE WHEN event_type = 'job_render' AND created_at >= ? THEN 1 ELSE 0 END) AS renders_7d,
                   SUM(CASE WHEN event_type IN ('viewshed_preview', 'rf_link_profile', 'ruler_metrics') AND created_at >= ? THEN 1 ELSE 0 END) AS previews_today,
                   SUM(CASE WHEN event_type = 'geocode_miss' AND created_at >= ? THEN 1 ELSE 0 END) AS geocode_misses_today
            FROM usage_events
            WHERE created_at >= ?
            GROUP BY actor_role
            """,
            (today_start_iso, week_start_iso, today_start_iso, today_start_iso, week_start_iso),
        ).fetchall()
        by_role: dict[str, dict[str, int]] = {}
        for r in role_rows:
            by_role[str(r["actor_role"])] = {
                "renders_today": int(r["renders_today"] or 0),
                "renders_7d": int(r["renders_7d"] or 0),
                "previews_today": int(r["previews_today"] or 0),
                "geocode_misses_today": int(r["geocode_misses_today"] or 0),
            }

        ev_rows = conn.execute(
            """
            SELECT event_id, event_type, actor_id, actor_role, client_ip, job_id, detail_json, created_at
            FROM usage_events
            ORDER BY event_id DESC
            LIMIT ?
            """,
            (max(1, min(200, recent_limit)),),
        ).fetchall()
        recent_events: list[dict[str, Any]] = []
        for ev in ev_rows:
            try:
                detail = json.loads(ev["detail_json"]) if ev["detail_json"] else {}
                if not isinstance(detail, dict):
                    detail = {}
            except Exception:
                detail = {}
            recent_events.append(
                {
                    "event_id": int(ev["event_id"]),
                    "event_type": str(ev["event_type"]),
                    "actor_id": str(ev["actor_id"]),
                    "actor_role": str(ev["actor_role"]),
                    "client_ip": str(ev["client_ip"] or ""),
                    "job_id": ev["job_id"],
                    "detail": detail,
                    "created_at": str(ev["created_at"]),
                }
            )

        return {
            "summary": {
                "renders_today": renders_today,
                "renders_7d": renders_7d,
                "previews_today": previews_today,
                "active_queue_depth": active_queue_depth,
                "global_daily_public_used": global_daily_public_used,
                "global_daily_public_limit": global_daily_public_limit,
                "public_renders_paused": public_renders_paused,
                "avg_elapsed_seconds": avg_elapsed,
                "max_peak_rss_mb": max_rss,
            },
            "by_role": by_role,
            "recent_events": recent_events,
        }
    finally:
        conn.close()
