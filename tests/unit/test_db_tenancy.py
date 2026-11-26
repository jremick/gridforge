"""Unit tests for SQLite additive multi-tenant migrations, invite atomicity, and role-aware TTL pruning."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gridforge.config import ColorMode, Orientation, PaperSize
from gridforge.web import db, projects as projects_store
from gridforge.web.projects import ProjectCreateRequest


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class TestAdditiveMigrationsAndTenancy:
    def test_legacy_pre_tenancy_sqlite_upgrades_cleanly(self, tmp_path: Path) -> None:
        db_path = tmp_path / "legacy.sqlite3"
        conn = sqlite3.connect(str(db_path))
        conn.executescript(
            """
            CREATE TABLE jobs (
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
            CREATE TABLE projects (
                project_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                state_json TEXT NOT NULL,
                last_job_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO jobs (
                job_id, status, stage, progress_pct, title, center, center_mgrs,
                scale, paper, orientation, color_mode, request_json, created_at
            ) VALUES (
                'legacyjob01', 'succeeded', 'done', 100, 'Legacy', '-33.7,150.3',
                '56H KH 500 640', 25000, 'a4', 'portrait', 'color', '{}', '2026-01-01T00:00:00Z'
            );
            INSERT INTO projects (
                project_id, name, description, state_json, last_job_id, created_at, updated_at
            ) VALUES (
                'legacyproj01', 'Legacy Project', '', '{}', NULL, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'
            );
            """
        )
        conn.commit()
        conn.close()

        # Must run ALTER TABLE before creating owner_id/owner_role indexes without crashing.
        db.init_db(db_path)

        jobs = db.list_jobs(db_path)
        assert len(jobs) == 1
        assert jobs[0].job_id == "legacyjob01"
        assert jobs[0].owner_id == "local"
        assert jobs[0].owner_role == "none"

        projs = projects_store.list_projects(db_path)
        assert len(projs) == 1
        assert projs[0].project_id == "legacyproj01"
        assert projs[0].owner_id == "local"
        assert projs[0].owner_role == "none"

        # Tenant-filtered listing isolates local vs guest/friend records.
        db.create_job(
            db_path,
            job_id="guestjob001",
            title="Guest Job",
            center="-33.7,150.3",
            center_mgrs=None,
            scale=25000,
            paper=PaperSize.A4,
            orientation=Orientation.PORTRAIT,
            color_mode=ColorMode.COLOR,
            request_json="{}",
            owner_id="guest:abc123",
            owner_role="guest",
        )
        assert [j.job_id for j in db.list_jobs(db_path, owner_id="guest:abc123")] == ["guestjob001"]
        assert [j.job_id for j in db.list_jobs(db_path, owner_id="local")] == ["legacyjob01"]

        projects_store.create_project(
            db_path,
            ProjectCreateRequest(name="Friend Proj", state={"scale": 25000}),
            owner_id="usr_friend1",
            owner_role="friend",
        )
        assert [p.name for p in projects_store.list_projects(db_path, owner_id="usr_friend1")] == [
            "Friend Proj"
        ]
        assert [p.name for p in projects_store.list_projects(db_path, owner_id="local")] == [
            "Legacy Project"
        ]


class TestRoleAwarePruning:
    def _seed_job_with_timestamp(
        self,
        db_path: Path,
        data_dir: Path,
        *,
        job_id: str,
        owner_id: str,
        owner_role: str,
        status: str,
        finished_at: datetime | None,
        created_at: datetime,
    ) -> None:
        db.create_job(
            db_path,
            job_id=job_id,
            title=job_id,
            center="-33.7,150.3",
            center_mgrs=None,
            scale=25000,
            paper=PaperSize.A4,
            orientation=Orientation.PORTRAIT,
            color_mode=ColorMode.COLOR,
            request_json="{}",
            owner_id=owner_id,
            owner_role=owner_role,
        )
        conn = db._connect(db_path)
        try:
            conn.execute(
                "UPDATE jobs SET status = ?, created_at = ?, finished_at = ? WHERE job_id = ?",
                (
                    status,
                    _iso(created_at),
                    _iso(finished_at) if finished_at is not None else None,
                    job_id,
                ),
            )
        finally:
            conn.close()
        art_dir = data_dir / job_id / "artifacts"
        art_dir.mkdir(parents=True, exist_ok=True)
        (art_dir / "map.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")

    def test_role_aware_prune_expired_jobs_and_legacy_fallback(self, tmp_path: Path) -> None:
        db_path = tmp_path / "jobs.sqlite3"
        data_dir = tmp_path / "jobs"
        db.init_db(db_path)

        now = datetime(2026, 11, 26, 12, 0, 0, tzinfo=timezone.utc)
        # 1. Guest job 30h old (expired under 24h)
        self._seed_job_with_timestamp(
            db_path,
            data_dir,
            job_id="guest_old_01",
            owner_id="guest:1",
            owner_role="guest",
            status="succeeded",
            finished_at=now - timedelta(hours=30),
            created_at=now - timedelta(hours=31),
        )
        # 2. Guest job 12h old (retained)
        self._seed_job_with_timestamp(
            db_path,
            data_dir,
            job_id="guest_new_01",
            owner_id="guest:2",
            owner_role="guest",
            status="succeeded",
            finished_at=now - timedelta(hours=12),
            created_at=now - timedelta(hours=12),
        )
        # 3. Guest job 48h old but still queued (never pruned)
        self._seed_job_with_timestamp(
            db_path,
            data_dir,
            job_id="guest_queued",
            owner_id="guest:3",
            owner_role="guest",
            status="queued",
            finished_at=None,
            created_at=now - timedelta(hours=48),
        )
        # 4. Friend job 5 days old (retained under 14d = 336h)
        self._seed_job_with_timestamp(
            db_path,
            data_dir,
            job_id="friend_fresh",
            owner_id="usr_f1",
            owner_role="friend",
            status="failed",
            finished_at=now - timedelta(days=5),
            created_at=now - timedelta(days=5),
        )
        # 5. Friend job 15 days old (expired under 336h)
        self._seed_job_with_timestamp(
            db_path,
            data_dir,
            job_id="friend_old01",
            owner_id="usr_f2",
            owner_role="friend",
            status="cancelled",
            finished_at=now - timedelta(days=15),
            created_at=now - timedelta(days=15),
        )
        # 6. Admin & none jobs 60 days old (preserved in role-aware mode!)
        self._seed_job_with_timestamp(
            db_path,
            data_dir,
            job_id="admin_old_01",
            owner_id="usr_admin",
            owner_role="admin",
            status="succeeded",
            finished_at=now - timedelta(days=60),
            created_at=now - timedelta(days=60),
        )
        self._seed_job_with_timestamp(
            db_path,
            data_dir,
            job_id="local_old_01",
            owner_id="local",
            owner_role="none",
            status="succeeded",
            finished_at=now - timedelta(days=60),
            created_at=now - timedelta(days=60),
        )

        pruned = db.prune_expired_jobs(
            db_path,
            data_dir=data_dir,
            guest_ttl_hours=24.0,
            friend_ttl_hours=336.0,
            now=now,
        )
        assert pruned == 2
        remaining_ids = {j.job_id for j in db.list_jobs(db_path)}
        assert remaining_ids == {
            "guest_new_01",
            "guest_queued",
            "friend_fresh",
            "admin_old_01",
            "local_old_01",
        }
        assert not (data_dir / "guest_old_01").exists()
        assert not (data_dir / "friend_old01").exists()
        assert (data_dir / "admin_old_01").exists()

        # Legacy signature (both role TTLs None) preserves max_age_hours cleanup of terminal jobs.
        legacy_pruned = db.prune_expired_jobs(
            db_path,
            data_dir=data_dir,
            max_age_hours=24.0,
            now=now,
        )
        assert legacy_pruned == 3
        assert {j.job_id for j in db.list_jobs(db_path)} == {"guest_new_01", "guest_queued"}

    def test_prune_expired_projects_and_auth(self, tmp_path: Path) -> None:
        db_path = tmp_path / "auth.sqlite3"
        db.init_db(db_path)
        now = datetime(2026, 11, 26, 12, 0, 0, tzinfo=timezone.utc)

        p_guest_old = projects_store.create_project(
            db_path, ProjectCreateRequest(name="G Old"), owner_id="guest:1", owner_role="guest"
        )
        p_guest_new = projects_store.create_project(
            db_path, ProjectCreateRequest(name="G New"), owner_id="guest:2", owner_role="guest"
        )
        p_friend_old = projects_store.create_project(
            db_path, ProjectCreateRequest(name="F Old"), owner_id="usr_f1", owner_role="friend"
        )
        p_friend_new = projects_store.create_project(
            db_path, ProjectCreateRequest(name="F New"), owner_id="usr_f2", owner_role="friend"
        )
        p_admin_old = projects_store.create_project(
            db_path, ProjectCreateRequest(name="A Old"), owner_id="usr_a", owner_role="admin"
        )
        p_none_old = projects_store.create_project(
            db_path, ProjectCreateRequest(name="L Old"), owner_id="local", owner_role="none"
        )

        conn = db._connect(db_path)
        try:
            for pid, dt in (
                (p_guest_old.project_id, now - timedelta(hours=30)),
                (p_guest_new.project_id, now - timedelta(hours=6)),
                (p_friend_old.project_id, now - timedelta(days=15)),
                (p_friend_new.project_id, now - timedelta(days=4)),
                (p_admin_old.project_id, now - timedelta(days=90)),
                (p_none_old.project_id, now - timedelta(days=90)),
            ):
                conn.execute(
                    "UPDATE projects SET created_at = ?, updated_at = ? WHERE project_id = ?",
                    (_iso(dt), _iso(dt), pid),
                )
        finally:
            conn.close()

        db.create_user(db_path, user_id="usr_f1", email="f1@example.com", role="friend")
        db.create_session(
            db_path,
            session_id="sess_exp",
            token_hash="th_exp",
            user_id="usr_f1",
            expires_at=_iso(now - timedelta(minutes=1)),
        )
        db.create_session(
            db_path,
            session_id="sess_ok",
            token_hash="th_ok",
            user_id="usr_f1",
            expires_at=_iso(now + timedelta(hours=5)),
        )
        db.create_invite(
            db_path,
            invite_id="inv_exp",
            token_hash="ih_exp",
            email=None,
            role="friend",
            created_by="usr_a",
            expires_at=_iso(now - timedelta(minutes=5)),
        )
        db.create_invite(
            db_path,
            invite_id="inv_ok",
            token_hash="ih_ok",
            email=None,
            role="friend",
            created_by="usr_a",
            expires_at=_iso(now + timedelta(hours=5)),
        )

        counts = db.prune_expired_projects_and_auth(
            db_path, guest_ttl_hours=24.0, friend_ttl_hours=336.0, now=now
        )
        assert counts == {"projects": 2, "sessions": 1, "invites": 1}

        remaining_proj_names = {p.name for p in projects_store.list_projects(db_path)}
        assert remaining_proj_names == {"G New", "F New", "A Old", "L Old"}
        assert db.get_session_and_user_by_token_hash(db_path, "th_exp", now=now) is None
        assert db.get_session_and_user_by_token_hash(db_path, "th_ok", now=now) is not None
        assert db.get_invite_by_token_hash(db_path, "ih_exp") is None
        assert db.get_invite_by_token_hash(db_path, "ih_ok") is not None

    def test_accept_invite_atomic_error_paths(self, tmp_path: Path) -> None:
        db_path = tmp_path / "invites.sqlite3"
        db.init_db(db_path)

        now = datetime.now(timezone.utc)
        db.create_invite(
            db_path,
            invite_id="inv_pinned",
            token_hash="hash_pinned",
            email="friend@example.com",
            role="friend",
            created_by="usr_admin",
            expires_at=_iso(now + timedelta(hours=24)),
        )
        with pytest.raises(db.InviteActionError) as mismatch:
            db.accept_invite_and_create_user(
                db_path,
                token_hash="hash_pinned",
                new_user_id="usr_1",
                supplied_email="other@example.com",
                display_name="Other",
                password_hash="pbkdf2_sha256$10000$00$00",
                session_id="s1",
                session_token_hash="st1",
                session_expires_at=_iso(now + timedelta(hours=24)),
            )
        assert mismatch.value.status_code == 403

        user = db.accept_invite_and_create_user(
            db_path,
            token_hash="hash_pinned",
            new_user_id="usr_1",
            supplied_email=None,
            display_name="Friend One",
            password_hash="pbkdf2_sha256$10000$00$00",
            session_id="s1",
            session_token_hash="st1",
            session_expires_at=_iso(now + timedelta(hours=24)),
        )
        assert user["email"] == "friend@example.com"
        assert user["role"] == "friend"

        # Second claim on same invite must fail with 403.
        with pytest.raises(db.InviteActionError) as reused:
            db.accept_invite_and_create_user(
                db_path,
                token_hash="hash_pinned",
                new_user_id="usr_2",
                supplied_email="friend@example.com",
                display_name="Dup",
                password_hash="pbkdf2_sha256$10000$00$00",
                session_id="s2",
                session_token_hash="st2",
                session_expires_at=_iso(now + timedelta(hours=24)),
            )
        assert reused.value.status_code == 403
